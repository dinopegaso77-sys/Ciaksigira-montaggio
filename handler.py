"""Regista AI — worker RunPod per il montaggio (ffmpeg).

Azioni:
  montage     -> unisce clip/immagini con titoli, transizioni e musica, restituisce l'MP4
  lastframe   -> estrae l'ultimo fotogramma di una clip come JPEG
  mux         -> mette l'audio di una clip sul video di un'altra
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
        d.text((w / 2, y + (len(lines) - 1) * lh + fs * 1.1), "UN FILM REALIZZATO CON REGISTA AI", font=f2, fill=(161, 159, 152), anchor="mm")
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


def lastframe(inp, tmp):
    src = fetch(inp, os.path.join(tmp, "clip.mp4"))
    out = os.path.join(tmp, "last.jpg")
    run(["ffmpeg", "-y", "-sseof", "-0.1", "-i", src, "-frames:v", "1", "-q:v", "2",
         "-vf", "scale='min(1280,iw)':-2", out])
    if not os.path.exists(out) or os.path.getsize(out) == 0:  # clip molto corta: ultimo fotogramma dal fondo
        run(["ffmpeg", "-y", "-i", src, "-vf", "reverse,scale='min(1280,iw)':-2", "-frames:v", "1", "-q:v", "2", out])
    with open(out, "rb") as f:
        return {"image": "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()}


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
