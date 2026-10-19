"""
Boxing Media Reel API (Vercel).

POST /api/titles  {"url": "<post link>"}
    -> {"titles": [...3], "video": "<direct video url or null>", "caption": "..."}
POST /api/render  {"url": "<post link>", "title": "...", "video": "<optional direct url>",
                   "focus": "top|center|bottom", "fit": false}
    -> {"video_url": "<mp4 url>"}

Every request needs header  x-key: <APP_KEY>
Env: APP_KEY, ANTHROPIC_API_KEY, APIFY_TOKEN (Instagram), BLOB_READ_WRITE_TOKEN
"""
import base64, json, os, re, subprocess, tempfile, time, uuid
from pathlib import Path

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
TMP = Path(tempfile.gettempdir())


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


FFMPEG = ffmpeg_exe()
MODEL = os.environ.get("REEL_MODEL", "claude-sonnet-5-5")

# ---- Template geometry (1080x1920), measured from the Boxing Media post ----
W, H = 1080, 1920
MARGIN_X, HEADER_Y, HEADER_W = 104, 352, 605
TITLE_Y, TITLE_MAX_W, TITLE_SIZE, TITLE_LINE_GAP = 478, 700, 44, 1.2
BOX_X, BOX_Y, BOX_W, BOX_H = 44, 619, 992, 972
GLOVE = True

app = FastAPI()


def check_key(key):
    want = os.environ.get("APP_KEY")
    if not want or key != want:
        raise HTTPException(401, "Wrong or missing key")


# ---------------------------------------------------------------- source
def is_instagram(url):
    return "instagram.com" in url


def apify_instagram(url):
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        return None
    r = requests.post(
        "https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items",
        params={"token": token},
        json={"directUrls": [url], "resultsType": "posts", "resultsLimit": 1, "addParentData": False},
        timeout=120,
    )
    r.raise_for_status()
    items = r.json()
    if not items:
        return None
    it = items[0]
    video = it.get("videoUrl") or it.get("video_url")
    if not video:
        raise HTTPException(422, "That post has no video")
    return {"video": video, "caption": it.get("caption") or "",
            "uploader": it.get("ownerUsername") or ""}


def ytdlp_download(url, dest_dir):
    import yt_dlp
    opts = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "ffmpeg_location": FFMPEG,
        "cachedir": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = [p for p in dest_dir.glob("source.*") if p.suffix in (".mp4", ".mov", ".webm", ".mkv")]
    if not files:
        raise HTTPException(502, "Download failed")
    return files[0], {"caption": info.get("description") or info.get("title") or "",
                      "uploader": info.get("uploader") or info.get("channel") or ""}


def fetch_file(video_url, dest):
    with requests.get(video_url, stream=True, timeout=120,
                      headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest


def get_source(url, video_url=None):
    """Returns (path, meta, direct_video_url_or_None)."""
    work = Path(tempfile.mkdtemp(dir=TMP))
    if video_url:
        try:
            return fetch_file(video_url, work / "source.mp4"), {}, video_url
        except Exception:
            pass  # expired link -> resolve again
    if is_instagram(url):
        meta = apify_instagram(url)
        if meta:
            return fetch_file(meta["video"], work / "source.mp4"), meta, meta["video"]
    path, meta = ytdlp_download(url, work)
    return path, meta, None


def probe(path):
    """Width, height, duration, has_audio — parsed from ffmpeg (no ffprobe needed)."""
    err = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True).stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if not m:
        raise HTTPException(422, "Could not read the video")
    w, h = int(m.group(1)), int(m.group(2))
    rot = re.search(r"rotation of (-?\d+)|rotate\s*:\s*(-?\d+)", err)
    if rot and abs(int(rot.group(1) or rot.group(2))) in (90, 270):
        w, h = h, w
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0
    return w, h, dur, "Audio:" in err


# ---------------------------------------------------------------- titles
TITLE_PROMPT = """You write the title card for Boxing Media (@boxing.media), a big UK boxing page on Instagram/Facebook.
The title sits above the clip in bold white capitals, like:
"POV: YOU HAVE JUST FOUND THE BOXING SIDE OF INSTAGRAM"

Write 3 different title options for this clip. Rules:
- 4 to 12 words, punchy, scroll-stopping, written for 18-35 boxing fans
- Mix the angles: one POV / relatable, one hype or fact-led, one curiosity or debate-starter
- Only name a fighter if the caption or uploader makes it clear who it is. Never guess a name.
- No hashtags, no emojis, no quote marks
Return JSON only: {"titles": ["...", "...", "..."]}

Post caption: {caption}
Uploader: {uploader}
"""


def grab_frames(video, duration, n=4):
    frames = []
    for i in range(n):
        t = max(duration, 1) * (i + 1) / (n + 1)
        f = video.parent / f"frame{i}.jpg"
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=640:-2", str(f)])
        if f.exists():
            frames.append(f)
    return frames


def ai_titles(meta, frames):
    import anthropic
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                "data": base64.b64encode(f.read_bytes()).decode()}} for f in frames]
    content.append({"type": "text", "text": TITLE_PROMPT
                    .replace("{caption}", (meta.get("caption") or "none")[:1500])
                    .replace("{uploader}", meta.get("uploader") or "unknown")})
    msg = anthropic.Anthropic().messages.create(
        model=MODEL, max_tokens=400, messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    titles = json.loads(m.group(0))["titles"] if m else []
    return [t.strip().upper() for t in titles if t.strip()][:3]


# ---------------------------------------------------------------- template
def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def render_card(title, out_png):
    img = Image.new("RGB", (W, H), "black")
    header = Image.open(ASSETS / "header.png").convert("RGBA")
    header = header.resize((HEADER_W, round(header.height * HEADER_W / header.width)), Image.LANCZOS)
    img.paste(header, (MARGIN_X, HEADER_Y), header)

    title = " ".join(title.upper().split())
    draw = ImageDraw.Draw(img)
    glove = Image.open(ASSETS / "glove.png").convert("RGBA") if GLOVE else None
    size = TITLE_SIZE
    while True:
        font = ImageFont.truetype(str(ASSETS / "title-font.otf"), size)
        lines = wrap(draw, title, font, TITLE_MAX_W)
        if len(lines) <= 2 or size <= 36:
            break
        size -= 2
    line_h = int(size * TITLE_LINE_GAP)
    y = TITLE_Y - max(0, len(lines) - 2) * line_h // 2
    for i, line in enumerate(lines):
        draw.text((MARGIN_X, y), line, font=font, fill="white")
        if glove and i == len(lines) - 1:
            x = MARGIN_X + draw.textlength(line, font=font) + size * 0.2
            gs = size * 1.45
            g = glove.resize((int(gs), int(gs * glove.height / glove.width)), Image.LANCZOS)
            img.paste(g, (int(x), y - int(size * 0.02)), g)
        y += line_h
    img.save(out_png)


def crop_filter(sw, sh, focus="center", fit=False):
    if fit:
        return (f"scale={BOX_W}:{BOX_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={BOX_W}:{BOX_H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
    target = BOX_W / BOX_H
    if sw / sh > target:
        cw, ch = int(sh * target) // 2 * 2, sh
        x, y = (sw - cw) // 2, 0
    else:
        cw, ch = sw, int(sw / target) // 2 * 2
        x = 0
        y = {"top": 0, "bottom": sh - ch}.get(focus, (sh - ch) // 2)
    return f"crop={cw}:{ch}:{x}:{y},scale={BOX_W}:{BOX_H}:flags=lanczos,setsar=1"


def render_video(video, card, out_mp4, vf, audio):
    fc = f"[1:v]{vf}[clip];[0:v][clip]overlay={BOX_X}:{BOX_Y}:shortest=1,format=yuv420p[v]"
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-loop", "1", "-i", str(card), "-i", str(video),
           "-filter_complex", fc, "-map", "[v]"]
    if audio:
        cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", os.environ.get("X264_PRESET", "veryfast"),
            "-crf", "18", "-r", "30", "-movflags", "+faststart", str(out_mp4)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise HTTPException(500, "Render failed: " + r.stderr[-400:])


def upload(path, title):
    from vercel.blob.sync import put_blob
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "reel"
    res = put_blob(f"reels/{time.strftime('%Y%m%d')}-{slug}-{uuid.uuid4().hex[:8]}.mp4",
                   path.read_bytes(), access="public", content_type="video/mp4")
    return res.url


# ---------------------------------------------------------------- routes
class TitlesIn(BaseModel):
    url: str


class RenderIn(BaseModel):
    url: str = ""
    title: str
    video: str | None = None
    focus: str = "center"
    fit: bool = False


@app.get("/")
@app.get("/api")
def home():
    return HTMLResponse("<body style='background:#000;color:#fff;font:16px system-ui;padding:24px'>"
                        "<h3>Boxing Media Reel API</h3><p>Running. Use the iPhone Shortcut.</p></body>")


@app.post("/api/titles")
def titles(body: TitlesIn, x_key: str = Header(default="")):
    check_key(x_key)
    path, meta, direct = get_source(body.url.strip())
    _, _, dur, _ = probe(path)
    try:
        ts = ai_titles(meta, grab_frames(path, dur))
    except Exception as e:
        raise HTTPException(502, f"Title writing failed: {e}")
    if not ts:
        raise HTTPException(502, "No titles came back")
    return {"titles": ts, "video": direct, "caption": meta.get("caption", "")[:300]}


@app.post("/api/render")
def render(body: RenderIn, x_key: str = Header(default="")):
    check_key(x_key)
    if not (body.url or body.video):
        raise HTTPException(400, "Need a url")
    path, _, _ = get_source(body.url.strip(), body.video)
    sw, sh, _, audio = probe(path)
    card = path.parent / "card.png"
    render_card(body.title, card)
    out = path.parent / "reel.mp4"
    render_video(path, card, out, crop_filter(sw, sh, body.focus, body.fit), audio)
    return {"video_url": upload(out, body.title)}
