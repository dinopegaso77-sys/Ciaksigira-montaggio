"""Ciak si gira — worker RunPod per il montaggio (ffmpeg).

Azioni:
  montage     -> unisce clip/immagini con titoli, transizioni e musica, restituisce l'MP4
  lastframe   -> estrae l'ultimo fotogramma di una clip come JPEG
  mux         -> mette l'audio di una clip sul video di un'altra
  render_timeline -> editor a tracce: video con transizioni per giunzione, testi, voci, musica
  voiceover   -> aggiunge una voce/audio a un video (audio originale abbassato, tenuto o tolto)
  presign_put -> link firmato per caricare un file nell'archivio R2 (dal telefono)
  urls        -> link firmati per leggere file dell'archivio
  list/delete -> elenco e cancellazione dei file
  save_url    -> copia un file da un link (es. ciak RunPod) nell'archivio

Archivio Cloudflare R2: variabili d'ambiente R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (R2_BUCKET facoltativa).
"""
import base64, json, os, re, shutil, subprocess, tempfile, time, uuid
from urllib.parse import urlparse
import requests
import runpod
from PIL import Image, ImageDraw, ImageFont

FONT_TITLE = "/fonts/Kalam-Bold.ttf"
FONT_LABEL = "/fonts/BebasNeue-Regular.ttf"
FPS = 30
TITLE_SEC, END_SEC, DISSOLVE, FADE = 3.0, 2.5, 0.6, 0.5


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + p.stderr[-800:])
    return p.stdout


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path])
    return float(json.loads(out)["format"]["duration"])


def probe_size(path):
    try:
        out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", path])
        s = json.loads(out)["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:
        return 0, 0


def has_audio(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", path])
    return bool(json.loads(out).get("streams"))


# ---------------- archivio R2 ----------------
_S3 = None


def r2():
    """Client S3 per Cloudflare R2, o None se le variabili non sono impostate."""
    global _S3
    ep = os.environ.get("R2_ENDPOINT", "").strip()
    if not ep or not os.environ.get("R2_ACCESS_KEY_ID") or not os.environ.get("R2_SECRET_ACCESS_KEY"):
        return None, None
    u = urlparse(ep if "://" in ep else "https://" + ep)
    bucket = os.environ.get("R2_BUCKET", "").strip() or u.path.strip("/").split("/")[0] or "ciaksigira"
    if _S3 is None:
        import boto3
        from botocore.config import Config
        _S3 = boto3.client("s3", endpoint_url=f"{u.scheme}://{u.netloc}",
                           aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
                           aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
                           region_name="auto", config=Config(signature_version="s3v4"))
    return _S3, bucket


def need_r2():
    s3, b = r2()
    if not s3:
        raise RuntimeError("archivio R2 non configurato sull'endpoint")
    return s3, b


def get_url(key, expires=86400):
    s3, b = need_r2()
    return s3.generate_presigned_url("get_object", Params={"Bucket": b, "Key": key}, ExpiresIn=int(min(expires, 604800)))


def safe_name(name):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "file").strip("-")[:80] or "file"
    return base


def fetch(src, dest):
    """src: {'url': ...} | {'b64': ...} | {'key': ...} (file dell'archivio R2)"""
    if src.get("key"):
        s3, b = need_r2()
        s3.download_file(b, src["key"], dest)
    elif src.get("b64"):
        data = src["b64"].split(",", 1)[-1]
        with open(dest, "wb") as f:
            f.write(base64.b64decode(data))
    else:
        r = requests.get(src["url"], timeout=120, stream=True)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)


def card_png(text, w, h, path, small=False):
    """Cartello stile ciak: lavagna nera, strisce bianche e nere sopra e sotto, scritta a gesso."""
    img = Image.new("RGB", (w, h), (10, 10, 10))
    d = ImageDraw.Draw(img)
    sw = max(12, int(h * 0.035))
    for x in range(-sw * 2, w + sw * 2, sw * 2):
        for y0, y1 in ((0, sw), (h - sw, h)):
            d.polygon([(x, y1), (x + sw, y0), (x + sw * 2, y0), (x + sw, y1)], fill=(243, 241, 234))
    fs = int(min(w, h) * (0.12 if small else 0.085))
    f = font(FONT_TITLE, fs)
    words, lines, line = text.split(), [], ""
    for wd in words:
        t = (line + " " + wd).strip()
        if d.textlength(t, font=f) > w * 0.84 and line:
            lines.append(line); line = wd
        else:
            line = t
    lines.append(line)
    lh = int(fs * 1.2)
    y = h / 2 - (len(lines) - 1) * lh / 2
    for i, l in enumerate(lines):
        d.text((w / 2, y + i * lh), l, font=f, fill=(251, 250, 245), anchor="mm")
    if not small:
        f2 = font(FONT_LABEL, int(fs * 0.32))
        d.text((w / 2, y + (len(lines) - 1) * lh + fs * 1.1), "UN FILM REALIZZATO CON CIAK SI GIRA", font=f2, fill=(161, 159, 152), anchor="mm")
    img.save(path)


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
       "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def normalize(src, dst, w, h, keep_audio, audio_src=None):
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={FPS}"
    if keep_audio and audio_src and has_audio(audio_src):  # audio preso da un'altra clip (es. Kling muto)
        cmd = ["ffmpeg", "-y", "-i", src, "-i", audio_src, "-vf", vf, "-map", "0:v:0", "-map", "1:a:0", *ENC, "-shortest", dst]
    elif keep_audio and has_audio(src):
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf, "-map", "0:v:0", "-map", "0:a:0", *ENC, "-shortest", dst]
    else:
        cmd = ["ffmpeg", "-y", "-i", src, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               "-vf", vf, "-map", "0:v:0", "-map", "1:a:0", *ENC, "-shortest", dst]
    run(cmd)


def card_clip(png, dst, secs):
    run(["ffmpeg", "-y", "-loop", "1", "-t", str(secs), "-i", png, "-f", "lavfi", "-t", str(secs), "-i", "anullsrc=r=48000:cl=stereo",
         "-vf", f"fps={FPS},fade=t=in:st=0:d={FADE},fade=t=out:st={secs - FADE}:d={FADE},format=yuv420p",
         "-map", "0:v", "-map", "1:a", *ENC, "-shortest", dst])


def with_fades(src, dst, fade_in, fade_out):
    d = duration(src)
    vf, af = [], []
    if fade_in:
        vf.append(f"fade=t=in:st=0:d={FADE}"); af.append(f"afade=t=in:st=0:d={FADE}")
    if fade_out:
        vf.append(f"fade=t=out:st={max(0, d - FADE)}:d={FADE}"); af.append(f"afade=t=out:st={max(0, d - FADE)}:d={FADE}")
    if not vf:
        shutil.copy(src, dst); return
    run(["ffmpeg", "-y", "-i", src, "-vf", ",".join(vf), "-af", ",".join(af), *ENC, dst])


def concat(parts, dst, tmp):
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", dst])


def xfade_chain(parts, dst):
    """Dissolvenza incrociata tra tutte le parti (video e audio)."""
    if len(parts) == 1:
        shutil.copy(parts[0], dst); return
    durs = [duration(p) for p in parts]
    inputs = []
    for p in parts:
        inputs += ["-i", p]
    fc, vlast, alast, offset = [], "[0:v]", "[0:a]", 0.0
    for i in range(1, len(parts)):
        offset += durs[i - 1] - DISSOLVE
        vo, ao = f"[v{i}]", f"[a{i}]"
        fc.append(f"{vlast}[{i}:v]xfade=transition=fade:duration={DISSOLVE}:offset={offset:.3f}{vo}")
        fc.append(f"{alast}[{i}:a]acrossfade=d={DISSOLVE}{ao}")
        vlast, alast = vo, ao
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fc), "-map", vlast, "-map", alast, *ENC, dst])


def add_music(video, music, dst, vol, keep_audio):
    total = duration(video)
    mv = max(0.0, min(1.0, vol)) * (0.55 if keep_audio else 0.9)
    fo = max(0, total - 2)
    fc = (f"[1:a]volume={mv:.3f},atrim=0:{total:.3f},afade=t=out:st={fo:.3f}:d=2[m];"
          f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    run(["ffmpeg", "-y", "-i", video, "-stream_loop", "-1", "-i", music, "-filter_complex", fc,
         "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", dst])


def deliver(path, name, folder="film"):
    """Con l'archivio R2 salva il file lì e restituisce chiave + link, altrimenti il file in base64."""
    s3, b = r2()
    if s3:
        key = f"{folder}/{time.strftime('%Y%m%d-%H%M%S')}-{name}"
        s3.upload_file(path, b, key, ExtraArgs={"ContentType": "video/mp4"})
        return {"key": key, "url": get_url(key, 604800)}
    with open(path, "rb") as f:
        return {"video_b64": base64.b64encode(f.read()).decode()}


def montage(inp, tmp):
    w, h = int(inp.get("width", 1280)), int(inp.get("height", 720))
    trans = inp.get("transition", "dissolve")
    keep = bool(inp.get("keep_audio", True))
    clips = inp.get("clips") or []
    if not clips:
        raise ValueError("Nessuna clip")

    norm = []
    for i, c in enumerate(clips):
        out = os.path.join(tmp, f"n{i}.mp4")
        if c.get("image"):  # foto: diventa una clip con un lento zoom
            img = fetch(c, os.path.join(tmp, f"img{i}"))
            image_clip(img, out, w, h, float(c.get("duration") or 4))
        else:
            raw = fetch(c, os.path.join(tmp, f"raw{i}.mp4"))
            aud = fetch(c["audio"], os.path.join(tmp, f"aud{i}.mp4")) if c.get("audio") else None
            normalize(raw, out, w, h, keep, aud)
        norm.append(out)

    parts = []
    if inp.get("title"):
        png = os.path.join(tmp, "title.png"); card_png(inp["title"], w, h, png)
        parts.append(os.path.join(tmp, "title.mp4")); card_clip(png, parts[-1], TITLE_SEC)
    first_clip = len(parts)
    parts += norm
    if inp.get("end_card", True):
        png = os.path.join(tmp, "end.png"); card_png("Fine", w, h, png, small=True)
        parts.append(os.path.join(tmp, "end.mp4")); card_clip(png, parts[-1], END_SEC)

    joined = os.path.join(tmp, "joined.mp4")
    if trans == "dissolve":
        xfade_chain(parts, joined)
    elif trans == "black":
        faded = []
        for i, p in enumerate(parts):
            if i < first_clip or i >= first_clip + len(norm):
                faded.append(p); continue  # i cartelli hanno già le loro dissolvenze
            o = os.path.join(tmp, f"f{i}.mp4"); with_fades(p, o, True, True); faded.append(o)
        concat(faded, joined, tmp)
    else:
        concat(parts, joined, tmp)

    final = joined
    if inp.get("music_b64") or inp.get("music_key"):
        msrc = {"key": inp["music_key"]} if inp.get("music_key") else {"b64": inp["music_b64"]}
        mus = fetch(msrc, os.path.join(tmp, "music." + inp.get("music_ext", "mp3")))
        final = os.path.join(tmp, "final.mp4")
        add_music(joined, mus, final, float(inp.get("music_vol", 0.6)), keep)

    fast = os.path.join(tmp, "film.mp4")
    run(["ffmpeg", "-y", "-i", final, "-c", "copy", "-movflags", "+faststart", fast])
    res = deliver(fast, f"film-{uuid.uuid4().hex[:8]}.mp4")
    res["duration"] = round(duration(fast), 2)
    return res


def image_clip(img, dst, w, h, secs):
    """Foto -> clip con zoom lento (effetto Ken Burns) e audio muto."""
    frames = max(1, int(secs * FPS))
    vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=decrease,pad={w * 2}:{h * 2}:(ow-iw)/2:(oh-ih)/2:black,"
          f"zoompan=z='min(zoom+0.0006,1.08)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={FPS},setsar=1,format=yuv420p")
    run(["ffmpeg", "-y", "-i", img, "-f", "lavfi", "-t", str(secs), "-i", "anullsrc=r=48000:cl=stereo",
         "-vf", vf, "-t", str(secs), "-map", "0:v", "-map", "1:a", *ENC, "-shortest", dst])


# ---------------- azioni dell'archivio ----------------
def presign_put(inp):
    s3, b = need_r2()
    folder = inp.get("folder", "libreria").strip("/") or "libreria"
    key = f"{folder}/{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}-{safe_name(inp.get('name'))}"
    ctype = inp.get("type") or "application/octet-stream"
    put = s3.generate_presigned_url("put_object", Params={"Bucket": b, "Key": key, "ContentType": ctype}, ExpiresIn=3600)
    return {"key": key, "put": put, "get": get_url(key)}


def urls(inp):
    exp = int(inp.get("expires", 86400))
    return {"urls": {k: get_url(k, exp) for k in (inp.get("keys") or [])[:200]}}


def list_files(inp):
    s3, b = need_r2()
    items, token = [], None
    while True:
        kw = {"Bucket": b, "Prefix": inp.get("prefix", "libreria/"), "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            items.append({"key": o["Key"], "size": o["Size"], "modified": o["LastModified"].isoformat()})
        if not r.get("IsTruncated"):
            break
        token = r.get("NextContinuationToken")
    return {"items": items}


def delete_file(inp):
    s3, b = need_r2()
    s3.delete_object(Bucket=b, Key=inp["key"])
    return {"ok": True}


def save_url(inp, tmp):
    s3, b = need_r2()
    path = fetch({"url": inp["url"]}, os.path.join(tmp, "dl"))
    key = inp.get("key") or f"ciak/{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.mp4"
    s3.upload_file(path, b, key, ExtraArgs={"ContentType": inp.get("type", "video/mp4")})
    return {"key": key}


def mux(inp, tmp):
    """Mette l'audio di una clip (inp['audio']) sul video di un'altra (inp['video']), senza ricodificare il video."""
    v = fetch(inp["video"], os.path.join(tmp, "v.mp4"))
    a = fetch(inp["audio"], os.path.join(tmp, "a.mp4"))
    if not has_audio(a):
        raise ValueError("il ciak di riferimento non ha audio")
    out = os.path.join(tmp, "mux.mp4")
    run(["ffmpeg", "-y", "-i", v, "-i", a, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out])
    with open(out, "rb") as f:
        return {"video_b64": base64.b64encode(f.read()).decode()}


def voiceover(inp, tmp):
    """Mette una voce (o un audio) su un video: inizio regolabile, volume della voce,
    audio originale abbassato ('low'), tenuto ('keep') o tolto ('off'). Il video non viene ricodificato."""
    v = fetch(inp["video"], os.path.join(tmp, "v.mp4"))
    a = fetch(inp["audio"], os.path.join(tmp, "voice"))
    start = max(0.0, float(inp.get("start", 0)))
    vv = max(0.0, min(3.0, float(inp.get("voice_vol", 1.0))))
    orig = inp.get("orig", "low")
    dur = duration(v)
    ms = int(start * 1000)
    voice = f"[1:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},volume={vv:.2f}[vo]"
    if orig != "off" and has_audio(v):
        ov = 0.25 if orig == "low" else 1.0
        fc = (voice + f";[0:a]aresample=48000,aformat=channel_layouts=stereo,volume={ov:.2f}[or];"
              "[or][vo]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,apad[a]")
    else:
        fc = voice + ";[vo]apad[a]"  # audio lungo quanto il video
    out = os.path.join(tmp, "vo.mp4")
    run(["ffmpeg", "-y", "-i", v, "-i", a, "-filter_complex", fc, "-map", "0:v:0", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{dur:.3f}", "-movflags", "+faststart", out])
    name = safe_name(inp.get("name") or "clip-con-voce") + ".mp4"
    res = deliver(out, name, inp.get("folder", "ciak"))
    res["duration"] = round(dur, 2)
    return res


# ---------------- editor a tracce ----------------
XF = {"fade": "fade", "dissolve": "dissolve", "black": "fadeblack", "white": "fadewhite", "wipeleft": "wipeleft",
      "wiperight": "wiperight", "slideleft": "slideleft", "slideright": "slideright", "circle": "circleopen",
      "zoom": "zoomin", "pixel": "pixelize", "cut": "fade"}


def enc_args(preview):
    if preview:
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p", "-r", str(FPS),
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    return ENC


def card_still(text, dst, w, h, secs, small, enc):
    png = dst + ".png"
    card_png(text, w, h, png, small=small)
    run(["ffmpeg", "-y", "-loop", "1", "-t", f"{secs:.3f}", "-i", png, "-f", "lavfi", "-t", f"{secs:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
         "-vf", f"fps={FPS},format=yuv420p", "-map", "0:v", "-map", "1:a", *enc, "-shortest", dst])


def norm_item(src, dst, w, h, start, secs, vol, keep, enc, audio_src=None):
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={FPS}"
    pre = ["-ss", f"{max(0, start):.3f}", "-t", f"{secs:.3f}"]
    asrc = audio_src if (keep and audio_src and has_audio(audio_src)) else (src if keep and has_audio(src) else None)
    if asrc:
        a_in = ["-i", asrc] if asrc != src else []
        a_map = "1:a:0" if asrc != src else "0:a:0"
        af = f"volume={vol:.2f},apad,atrim=0:{secs:.3f}"
        cmd = ["ffmpeg", "-y", *pre, "-i", src, *(["-ss", f"{max(0, start):.3f}", "-t", f"{secs:.3f}"] + a_in if a_in else []),
               "-vf", vf, "-af", af, "-map", "0:v:0", "-map", a_map, *enc, "-t", f"{secs:.3f}", dst]
    else:
        cmd = ["ffmpeg", "-y", *pre, "-i", src, "-f", "lavfi", "-t", f"{secs:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
               "-vf", vf, "-map", "0:v:0", "-map", "1:a:0", *enc, "-t", f"{secs:.3f}", dst]
    run(cmd)


def esc_path(p):
    return p.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def render_timeline(inp, tmp):
    W, H = int(inp.get("width", 1920)), int(inp.get("height", 1080))
    preview = bool(inp.get("preview"))
    if preview:  # anteprima veloce: lato lungo 854 px
        k = 854 / max(W, H)
        W, H = int(W * k) // 2 * 2, int(H * k) // 2 * 2
    enc = enc_args(preview)
    keep = bool(inp.get("clip_audio", True))
    items = inp.get("video") or []
    if not items:
        raise ValueError("La traccia video è vuota")

    parts, durs = [], []
    for i, it in enumerate(items):
        out = os.path.join(tmp, f"p{i}.mp4")
        secs = max(0.2, float(it.get("dur") or 4))
        if it.get("type") == "card":
            card_still(it.get("text") or " ", out, W, H, secs, bool(it.get("small")), enc)
        elif it.get("image"):
            img = fetch(it["src"], os.path.join(tmp, f"img{i}"))
            image_clip(img, out, W, H, secs)
        else:
            raw = fetch(it["src"], os.path.join(tmp, f"raw{i}.mp4"))
            aud = fetch(it["audio"], os.path.join(tmp, f"aud{i}.mp4")) if it.get("audio") else None
            norm_item(raw, out, W, H, float(it.get("in") or 0), secs, float(it.get("vol", 1)), keep, enc, aud)
        parts.append(out); durs.append(duration(out))

    # unione con una transizione per ogni giunzione
    joined = os.path.join(tmp, "joined.mp4")
    if len(parts) == 1:
        shutil.copy(parts[0], joined)
    else:
        ins = []
        for p in parts:
            ins += ["-i", p]
        fc, vl, al, off = [], "[0:v]", "[0:a]", 0.0
        for i in range(1, len(parts)):
            tr = items[i].get("trans") or {}
            ttype = tr.get("type", "cut")
            d = 0.04 if ttype == "cut" else float(tr.get("dur", 0.6))
            d = max(0.04, min(d, durs[i - 1] - 0.05, durs[i] - 0.05))
            off += durs[i - 1] - d
            vo, ao = f"[v{i}]", f"[a{i}]"
            fc.append(f"{vl}[{i}:v]xfade=transition={XF.get(ttype, 'fade')}:duration={d:.3f}:offset={off:.3f}{vo}")
            fc.append(f"{al}[{i}:a]acrossfade=d={d:.3f}{ao}")
            vl, al = vo, ao
        run(["ffmpeg", "-y", *ins, "-filter_complex", ";".join(fc), "-map", vl, "-map", al, *enc, joined])
    total = duration(joined)

    # testi, voci e musica
    texts = inp.get("texts") or []
    auds = inp.get("audio") or []
    final = joined
    if texts or auds:
        cmd = ["ffmpeg", "-y", "-i", joined]
        fc = []
        vchain = "[0:v]"
        for j, t in enumerate(texts):
            tf = os.path.join(tmp, f"t{j}.txt")
            with open(tf, "w", encoding="utf-8") as f:
                f.write(t.get("text") or "")
            title = t.get("style") == "title"
            fs = int(H * (0.085 if title else 0.055))
            font = FONT_TITLE if title else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if not os.path.exists(font):
                font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            y = {"top": f"{int(H * 0.08)}", "center": "(h-text_h)/2"}.get(t.get("pos"), f"h-text_h-{int(H * 0.09)}")
            s0, s1 = float(t.get("start", 0)), float(t.get("start", 0)) + float(t.get("dur", 3))
            out = f"[vt{j}]"
            fc.append(f"{vchain}drawtext=fontfile='{esc_path(font)}':textfile='{esc_path(tf)}':fontsize={fs}:fontcolor=white:"
                      f"box=1:boxcolor=black@0.45:boxborderw={int(fs * 0.35)}:line_spacing={int(fs * 0.2)}:"
                      f"x=(w-text_w)/2:y={y}:enable='between(t,{s0:.2f},{s1:.2f})'{out}")
            vchain = out
        voices, musics = [], []
        for j, a in enumerate(auds):
            path = fetch(a["src"], os.path.join(tmp, f"x{j}"))
            cmd += (["-stream_loop", "-1"] if a.get("loop") else []) + ["-i", path]
            idx = j + 1
            st, ins_, du = float(a.get("start", 0)), float(a.get("in", 0)), float(a.get("dur", 5))
            fi, fo = float(a.get("fin", 0)), float(a.get("fout", 0))
            ch = f"[{idx}:a]atrim=start={ins_:.3f}:duration={du:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo"
            if fi > 0:
                ch += f",afade=t=in:st=0:d={fi:.2f}"
            if fo > 0:
                ch += f",afade=t=out:st={max(0, du - fo):.2f}:d={fo:.2f}"
            ms = int(st * 1000)
            ch += f",volume={float(a.get('vol', 1)):.2f},adelay={ms}|{ms}[x{j}]"
            fc.append(ch)
            (voices if a.get("lane") == "voice" else musics).append(f"[x{j}]")
        mix = ["[0:a]"]
        if voices:
            if len(voices) > 1:
                fc.append(f"{''.join(voices)}amix=inputs={len(voices)}:duration=longest:normalize=0[vox]")
            else:
                fc.append(f"{voices[0]}anull[vox]")
        if musics:
            if len(musics) > 1:
                fc.append(f"{''.join(musics)}amix=inputs={len(musics)}:duration=longest:normalize=0[mus]")
            else:
                fc.append(f"{musics[0]}anull[mus]")
        if voices and musics and inp.get("duck", True):
            fc.append("[vox]asplit=2[vox1][vox2]")
            fc.append("[mus][vox2]sidechaincompress=threshold=0.03:ratio=10:attack=15:release=450[musd]")
            mix += ["[vox1]", "[musd]"]
        else:
            if voices:
                mix.append("[vox]")
            if musics:
                mix.append("[mus]")
        fc.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0[aout]")
        final = os.path.join(tmp, "final.mp4")
        vmap = vchain if texts else "0:v"
        venc = (enc[:enc.index("-c:a")] if texts else ["-c:v", "copy"])  # ricodifica il video solo se ci sono testi
        cmd += ["-filter_complex", ";".join(fc), "-map", vmap, "-map", "[aout]", *venc,
                "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", final]
        run(cmd)

    fast = os.path.join(tmp, "film.mp4")
    run(["ffmpeg", "-y", "-i", final, "-c", "copy", "-movflags", "+faststart", fast])
    res = deliver(fast, ("anteprima" if preview else "film") + f"-{uuid.uuid4().hex[:8]}.mp4", "anteprima" if preview else "film")
    res["duration"] = round(duration(fast), 2)
    res["preview"] = preview
    return res


def lastframe(inp, tmp):
    src = fetch(inp, os.path.join(tmp, "clip.mp4"))
    out = os.path.join(tmp, "last.jpg")
    run(["ffmpeg", "-y", "-sseof", "-0.1", "-i", src, "-frames:v", "1", "-q:v", "2",
         "-vf", "scale='min(1280,iw)':-2", out])
    if not os.path.exists(out) or os.path.getsize(out) == 0:  # clip molto corta: ultimo fotogramma dal fondo
        run(["ffmpeg", "-y", "-i", src, "-vf", "reverse,scale='min(1280,iw)':-2", "-frames:v", "1", "-q:v", "2", out])
    with open(out, "rb") as f:
        return {"image": "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()}


# ---------------- sottotitoli ----------------
WHISPER = None


def whisper_model():
    global WHISPER
    if WHISPER is None:
        from faster_whisper import WhisperModel
        WHISPER = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8",
                               download_root="/models", cpu_threads=os.cpu_count() or 4)
    return WHISPER


def transcribe(inp, tmp):
    """Trascrive l'audio del film con Whisper. translate=True -> traduce direttamente in inglese."""
    src = fetch(inp["src"], os.path.join(tmp, "in.mp4"))
    lang = (inp.get("lang") or "it")[:2]
    if not has_audio(src):
        return {"segments": [], "lang": lang, "note": "il film non ha audio"}
    wav = os.path.join(tmp, "a.wav")
    run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])
    task = "translate" if inp.get("translate") else "transcribe"
    segs, info = whisper_model().transcribe(wav, language=lang, task=task, vad_filter=True, beam_size=5)
    out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs if s.text.strip()]
    return {"segments": out, "lang": lang, "task": task, "duration": round(info.duration, 2)}


def srt_time(t):
    t = max(0.0, float(t))
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(segments, path):
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for s in segments:
            txt = (s.get("text") or "").strip()
            if not txt:
                continue
            n += 1
            f.write(f"{n}\n{srt_time(s['start'])} --> {srt_time(max(s['end'], s['start'] + 0.3))}\n{txt}\n\n")
    return n


ISO3 = {"it": "ita", "en": "eng", "fr": "fra", "es": "spa", "de": "deu"}


def subtitle(inp, tmp):
    """Aggiunge le tracce dei sottotitoli (selezionabili nel lettore) e, a scelta, ne scrive una nel video."""
    src = fetch(inp["src"], os.path.join(tmp, "in.mp4"))
    tracks = [t for t in (inp.get("tracks") or []) if t.get("segments")]
    if not tracks:
        raise ValueError("nessun sottotitolo da aggiungere")
    files = []
    for i, t in enumerate(tracks):
        p = os.path.join(tmp, f"s{i}.srt")
        if write_srt(t["segments"], p):
            files.append((t.get("lang", "it")[:2], p))
    if not files:
        raise ValueError("i sottotitoli sono vuoti")
    burn = inp.get("burn")
    cmd = ["ffmpeg", "-y", "-i", src]
    for _, p in files:
        cmd += ["-i", p]
    cmd += ["-map", "0:v:0", "-map", "0:a?"]
    for i in range(len(files)):
        cmd += ["-map", f"{i + 1}:0"]
    bp = next((p for l, p in files if l == burn), None)
    if bp:
        h = probe_size(src)[1] or 720
        fs = 18 if h >= 1000 else 16
        style = (f"FontName=DejaVu Sans,FontSize={fs},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                 f"BackColour=&H80000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=22")
        cmd += ["-vf", f"subtitles={bp}:charenc=UTF-8:force_style='{style}'",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "copy", "-c:s", "mov_text"]
    for i, (l, _) in enumerate(files):
        cmd += [f"-metadata:s:s:{i}", f"language={ISO3.get(l, l)}",
                f"-metadata:s:s:{i}", f"title={'Italiano' if l == 'it' else 'English' if l == 'en' else l}"]
    if files:
        cmd += ["-disposition:s:0", "default"]
    out = os.path.join(tmp, "sub.mp4")
    run(cmd + ["-movflags", "+faststart", out])
    res = deliver(out, safe_name(inp.get("name") or "film-sottotitolato") + ".mp4", "film")
    res["duration"] = round(duration(out), 2)
    return res


def pdf_text(inp, tmp):
    """Estrae il testo da un PDF (copione / master del cortometraggio)."""
    from pypdf import PdfReader
    src = fetch(inp["src"], os.path.join(tmp, "doc.pdf"))
    rd = PdfReader(src)
    pages = []
    for pg in rd.pages[:200]:
        try:
            pages.append((pg.extract_text() or "").strip())
        except Exception:
            pages.append("")
    text = "\n\n".join(p for p in pages if p)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return {"text": text[:120000], "pages": len(rd.pages), "chars": len(text)}


def handler(job):
    inp = job.get("input") or {}
    tmp = tempfile.mkdtemp()
    try:
        action = inp.get("action", "montage")
        if action == "lastframe":
            return lastframe(inp, tmp)
        if action == "mux":
            return mux(inp, tmp)
        if action == "voiceover":
            return voiceover(inp, tmp)
        if action == "render_timeline":
            return render_timeline(inp, tmp)
        if action == "pdf_text":
            return pdf_text(inp, tmp)
        if action == "transcribe":
            return transcribe(inp, tmp)
        if action == "subtitle":
            return subtitle(inp, tmp)
        if action == "ping":
            return {"ok": True, "r2": bool(r2()[0])}
        if action == "presign_put":
            return presign_put(inp)
        if action == "urls":
            return urls(inp)
        if action == "list":
            return list_files(inp)
        if action == "delete":
            return delete_file(inp)
        if action == "save_url":
            return save_url(inp, tmp)
        return montage(inp, tmp)
    except Exception as e:
        return {"error": str(e)[:1000]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


runpod.serverless.start({"handler": handler})
