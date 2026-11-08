"""Regista AI — worker RunPod per il montaggio (ffmpeg).

Azioni:
  montage   -> unisce le clip con titoli, transizioni e musica, restituisce l'MP4
  lastframe -> estrae l'ultimo fotogramma di una clip come JPEG
"""
import base64, json, os, shutil, subprocess, tempfile, uuid
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


def fetch(src, dest):
    """src: {'url': ...} oppure {'b64': ...}"""
    if src.get("b64"):
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


def deliver(path, name):
    """Con un bucket S3 configurato sull'endpoint restituisce un link, altrimenti il file in base64."""
    if os.environ.get("BUCKET_ENDPOINT_URL"):
        from runpod.serverless.utils import rp_upload
        url = rp_upload.upload_file_to_bucket(name, path)
        return {"url": url}
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
        raw = fetch(c, os.path.join(tmp, f"raw{i}.mp4"))
        aud = fetch(c["audio"], os.path.join(tmp, f"aud{i}.mp4")) if c.get("audio") else None
        out = os.path.join(tmp, f"n{i}.mp4")
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
    if inp.get("music_b64"):
        mus = fetch({"b64": inp["music_b64"]}, os.path.join(tmp, "music." + inp.get("music_ext", "mp3")))
        final = os.path.join(tmp, "final.mp4")
        add_music(joined, mus, final, float(inp.get("music_vol", 0.6)), keep)

    fast = os.path.join(tmp, "film.mp4")
    run(["ffmpeg", "-y", "-i", final, "-c", "copy", "-movflags", "+faststart", fast])
    res = deliver(fast, f"film-{uuid.uuid4().hex[:8]}.mp4")
    res["duration"] = round(duration(fast), 2)
    return res


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
        if action == "ping":
            return {"ok": True}
        return montage(inp, tmp)
    except Exception as e:
        return {"error": str(e)[:1000]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


runpod.serverless.start({"handler": handler})
