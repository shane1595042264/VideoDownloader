"""
VideoDownloader - FastAPI Backend
A modern, scalable video downloader supporting YouTube, Bilibili, and 1000+ sites.
"""

import asyncio
import base64
import json
import os
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import html as html_mod
import re
import shutil

import yt_dlp

# Check if curl_cffi is available for browser impersonation
try:
    import curl_cffi.requests as cffi_requests
    HAS_IMPERSONATE = True
except Exception:
    # Try to auto-install curl_cffi
    import subprocess as _sp
    import sys as _sys
    try:
        _sp.run(
            [_sys.executable, "-m", "pip", "install", "curl_cffi>=0.7.0",
             "--quiet", "--break-system-packages"],
            capture_output=True, timeout=120, check=True,
        )
        import curl_cffi.requests as cffi_requests
        HAS_IMPERSONATE = True
    except Exception:
        cffi_requests = None
        HAS_IMPERSONATE = False


def _decode_packed_js(packed_code: str) -> str:
    """Decode a Dean Edwards p.a.c.k.e.r packed JS string."""
    match = re.search(
        r"}\('([^'\\]*(?:\\.[^'\\]*)*)',\s*(\d+),\s*(\d+),\s*'([^']+)'\.split",
        packed_code,
    )
    if not match:
        return ""
    payload, radix, count, keywords_str = match.groups()
    radix, count = int(radix), int(count)
    keywords = keywords_str.split("|")

    def _base_n(num, base):
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        if num < base:
            return digits[num]
        return _base_n(num // base, base) + digits[num % base]

    # Replace each base-N token with its keyword
    def replacer(m):
        word = m.group(0)
        idx = int(word, radix) if radix <= 36 else int(word)
        return keywords[idx] if idx < len(keywords) and keywords[idx] else word

    return re.sub(r'\b\w+\b', replacer, payload)


def _try_custom_extract(url: str) -> dict | None:
    """Try to extract video info from sites that need special handling.
    Returns a dict with 'title', 'formats', 'thumbnail', etc. or None.
    """
    if not HAS_IMPERSONATE:
        return None

    try:
        resp = cffi_requests.get(url, impersonate="chrome136", timeout=15)
        if resp.status_code != 200:
            return None
    except Exception:
        return None

    html = resp.text

    # Look for packed JS with m3u8 URLs (missav, similar sites)
    eval_blocks = re.findall(
        r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\(.*?\)\)",
        html, re.DOTALL,
    )
    m3u8_url = None
    for block in eval_blocks:
        decoded = _decode_packed_js(block)
        urls = re.findall(r"https?://[^\s'\"]+\.m3u8", decoded)
        if urls:
            # Prefer the playlist.m3u8 (master playlist)
            for u in urls:
                if "playlist" in u:
                    m3u8_url = u
                    break
            if not m3u8_url:
                m3u8_url = urls[0]
            break

    if not m3u8_url:
        return None

    # Extract page title
    title_match = re.search(r"<title[^>]*>([^<]+)</title>", html)
    title = html_mod.unescape(title_match.group(1).strip()) if title_match else "Video"
    # Clean up title (remove " - Site Name" suffixes)
    title = re.split(r'\s*[-|–]\s*(?:MissAV|missav)', title, flags=re.IGNORECASE)[0].strip()

    # Extract thumbnail
    thumb_match = re.search(r'property="og:image"\s+content="([^"]+)"', html)
    thumbnail = thumb_match.group(1) if thumb_match else ""

    # Fetch the master m3u8 to get available qualities
    referer = re.match(r'(https?://[^/]+)', url)
    referer_url = referer.group(1) + "/" if referer else ""
    base_url = m3u8_url.rsplit("/", 1)[0]

    try:
        m3u8_resp = cffi_requests.get(
            m3u8_url,
            headers={"Referer": referer_url, "Origin": referer_url.rstrip("/")},
            impersonate="chrome136",
            timeout=10,
        )
        m3u8_text = m3u8_resp.text
    except Exception:
        m3u8_text = ""

    formats = []
    # Parse HLS master playlist
    lines = m3u8_text.strip().split("\n")
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = line.split(":", 1)[1]
            res_match = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
            bw_match = re.search(r"BANDWIDTH=(\d+)", attrs)
            height = int(res_match.group(2)) if res_match else 0
            bandwidth = int(bw_match.group(1)) if bw_match else 0
            if i + 1 < len(lines):
                stream_path = lines[i + 1].strip()
                stream_url = (
                    stream_path if stream_path.startswith("http")
                    else f"{base_url}/{stream_path}"
                )
                formats.append({
                    "format_id": f"hls-{height}p",
                    "label": f"{height}p",
                    "ext": "mp4",
                    "height": height,
                    "has_audio": True,
                    "filesize": None,
                    "type": "video",
                    "url": stream_url,
                    "bandwidth": bandwidth,
                })

    formats.sort(key=lambda x: x.get("height", 0), reverse=True)

    if not formats:
        # Fallback: just offer the master playlist
        formats.append({
            "format_id": "hls-best",
            "label": "Best Quality",
            "ext": "mp4",
            "height": 9999,
            "has_audio": True,
            "filesize": None,
            "type": "video",
            "url": m3u8_url,
        })

    return {
        "title": title,
        "thumbnail": thumbnail,
        "duration": 0,
        "uploader": "",
        "view_count": None,
        "upload_date": None,
        "description": "",
        "webpage_url": url,
        "extractor": "custom",
        "formats": formats,
        "_referer": referer_url,
        "_m3u8_base": base_url,
    }

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
HAS_FFMPEG = shutil.which("ffmpeg") is not None
DOWNLOAD_DIR.mkdir(exist_ok=True)
HISTORY_FILE = BASE_DIR / "history.json"

app = FastAPI(title="VideoDownloader", version="1.0.0")

if not HAS_FFMPEG:
    print("\n" + "=" * 60)
    print("  WARNING: ffmpeg not found!")
    print("  Install it for best results: brew install ffmpeg")
    print("  Without ffmpeg, only pre-combined formats will work.")
    print("=" * 60 + "\n")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "ok", "has_ffmpeg": HAS_FFMPEG, "has_impersonate": HAS_IMPERSONATE}


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
download_progress: dict[str, dict] = {}   # task_id -> progress info
active_websockets: dict[str, list[WebSocket]] = {}  # task_id -> [ws, …]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            return []
    return []


def _save_history(history: list[dict]):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def _add_to_history(entry: dict):
    history = _load_history()
    history.insert(0, entry)
    # Keep last 100 entries
    _save_history(history[:100])


# ---------------------------------------------------------------------------
# API: Fetch video info
# ---------------------------------------------------------------------------
@app.post("/api/info")
async def get_video_info(payload: dict):
    url = payload.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    # First, try yt-dlp's built-in extractors
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    loop = asyncio.get_event_loop()
    info = None
    use_custom = False

    try:
        info = await loop.run_in_executor(None, _extract_info, url, ydl_opts)
    except Exception as ydl_err:
        print(f"[INFO] yt-dlp failed for {url}: {ydl_err}")
        # yt-dlp failed — try our custom extractor (handles Cloudflare sites)
        try:
            custom = await loop.run_in_executor(None, _try_custom_extract, url)
            if custom:
                return custom
            print(f"[INFO] Custom extractor returned None for {url}")
        except Exception as custom_err:
            print(f"[ERROR] Custom extractor failed for {url}: {custom_err}")
            import traceback
            traceback.print_exc()

    if info is None:
        raise HTTPException(
            status_code=400,
            detail="Could not extract video info. The site may not be supported.",
        )

    # Build format list
    formats = []

    if HAS_FFMPEG:
        # With ffmpeg: offer all video qualities — we merge video+audio
        raw_formats = []
        for f in info.get("formats", []):
            height = f.get("height")
            ext = f.get("ext", "mp4")
            vcodec = f.get("vcodec", "none")
            format_id = f.get("format_id", "")
            filesize = f.get("filesize") or f.get("filesize_approx")

            if vcodec != "none" and height:
                raw_formats.append({
                    "format_id": format_id,
                    "ext": ext,
                    "height": height,
                    "filesize": filesize,
                })

        # Keep one entry per height — prefer mp4 over webm
        by_height: dict[int, dict] = {}
        for f in raw_formats:
            h = f["height"]
            if h not in by_height or (f["ext"] == "mp4" and by_height[h]["ext"] != "mp4"):
                by_height[h] = f

        for h in sorted(by_height.keys(), reverse=True):
            f = by_height[h]
            formats.append({
                "format_id": f["format_id"],
                "label": f"{h}p",
                "ext": "mp4",
                "height": h,
                "has_audio": True,
                "filesize": f["filesize"],
                "type": "video",
            })

        # Add best quality combined at top
        formats.insert(0, {
            "format_id": "bestvideo+bestaudio/best",
            "label": "Best Quality",
            "ext": "mp4",
            "height": 9999,
            "has_audio": True,
            "filesize": None,
            "type": "video",
        })

        # Add audio-only
        formats.append({
            "format_id": "bestaudio",
            "label": "Audio Only (MP3)",
            "ext": "mp3",
            "height": 0,
            "has_audio": True,
            "filesize": None,
            "type": "audio",
        })
    else:
        # Without ffmpeg: only offer pre-combined formats (have both video+audio)
        seen_heights = set()
        for f in info.get("formats", []):
            height = f.get("height")
            ext = f.get("ext", "mp4")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            format_id = f.get("format_id", "")
            filesize = f.get("filesize") or f.get("filesize_approx")

            # Only include formats that already have both video AND audio
            if vcodec != "none" and acodec != "none" and height:
                if height not in seen_heights:
                    seen_heights.add(height)
                    formats.append({
                        "format_id": format_id,
                        "label": f"{height}p",
                        "ext": ext,
                        "height": height,
                        "has_audio": True,
                        "filesize": filesize,
                        "type": "video",
                    })

        formats.sort(key=lambda x: x.get("height", 0), reverse=True)

        # Add "best" as a pre-combined option
        formats.insert(0, {
            "format_id": "best",
            "label": "Best Quality",
            "ext": "mp4",
            "height": 9999,
            "has_audio": True,
            "filesize": None,
            "type": "video",
        })

        # Add audio-only (bestaudio without postprocessing, just raw)
        formats.append({
            "format_id": "bestaudio",
            "label": "Audio Only",
            "ext": "webm",
            "height": 0,
            "has_audio": True,
            "filesize": None,
            "type": "audio",
        })

    return {
        "title": info.get("title", "Unknown"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration", 0),
        "uploader": info.get("uploader", "Unknown"),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
        "description": (info.get("description") or "")[:300],
        "webpage_url": info.get("webpage_url", url),
        "extractor": info.get("extractor", ""),
        "formats": formats,
    }


def _clean_error(msg: str) -> str:
    """Strip ANSI escape codes from yt-dlp error messages."""
    return re.sub(r'\x1b\[[0-9;]*m', '', msg)


def _extract_info(url: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# ---------------------------------------------------------------------------
# API: Start download
# ---------------------------------------------------------------------------
@app.post("/api/download")
async def start_download(payload: dict):
    url = payload.get("url", "").strip()
    format_id = payload.get("format_id", "bestvideo+bestaudio/best")
    fmt_type = payload.get("type", "video")  # "video" or "audio"
    title = payload.get("title", "video")
    # Custom-extracted fields (for Cloudflare-protected sites)
    hls_url = payload.get("hls_url", "")
    referer = payload.get("referer", "")

    if not url and not hls_url:
        raise HTTPException(status_code=400, detail="URL is required")

    # If we have a direct HLS URL from custom extraction, use that
    download_url = hls_url if hls_url else url

    # For specific video-only format IDs (e.g. "137", "401"), always add
    # best audio so the user doesn't get a silent file.
    # Only do this if ffmpeg is available (merging requires ffmpeg).
    if HAS_FFMPEG and not hls_url:
        if (fmt_type == "video"
                and "+" not in format_id
                and format_id not in ("best", "bestvideo+bestaudio/best")
                and not format_id.startswith("hls-")):
            format_id = f"{format_id}+bestaudio"

    task_id = str(uuid.uuid4())[:8]
    download_progress[task_id] = {
        "status": "starting",
        "percent": 0,
        "speed": "",
        "eta": "",
        "filename": "",
        "title": title,
    }

    # Fire-and-forget download in background
    asyncio.create_task(_run_download(task_id, download_url, format_id, title, referer))
    return {"task_id": task_id}


async def _run_download(task_id: str, url: str, format_id: str, title: str, referer: str = ""):
    """Run yt-dlp download in a thread and broadcast progress via WebSocket."""
    safe_title = "".join(c if c.isalnum() or c in " -_." else "_" for c in title)[:80]
    output_template = str(DOWNLOAD_DIR / f"{task_id}_{safe_title}.%(ext)s")

    is_audio = format_id == "bestaudio"
    is_hls = format_id.startswith("hls-")

    # Track the final filename from yt-dlp's postprocessor hooks
    final_filename_holder = {"path": None}

    def _pp_hook(d):
        if d.get("status") == "finished":
            final_filename_holder["path"] = d.get("info_dict", {}).get("filepath")

    ydl_opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [lambda d: _progress_hook(task_id, d)],
        "postprocessor_hooks": [_pp_hook],
        "keepvideo": False,
    }

    # For HLS streams (custom-extracted), use 'best' format and set Referer
    if is_hls:
        ydl_opts["format"] = "best"
        if referer:
            ydl_opts["http_headers"] = {
                "Referer": referer,
                "Origin": referer.rstrip("/"),
            }
    else:
        ydl_opts["format"] = format_id

    if HAS_FFMPEG:
        if not is_audio:
            ydl_opts["merge_output_format"] = "mp4"
            # Re-encode audio to AAC for QuickTime compatibility (merger case)
            ydl_opts["postprocessor_args"] = {
                "merger": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"],
            }

        if is_audio:
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
    # Without ffmpeg: no merging, no post-processing — download as-is

    # Remove None values
    ydl_opts = {k: v for k, v in ydl_opts.items() if v is not None}

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_download, url, ydl_opts)

        # Determine final filename — prefer what yt-dlp told us, otherwise
        # pick the largest non-fragment file that matches our task_id.
        downloaded_file = None
        if final_filename_holder["path"]:
            downloaded_file = Path(final_filename_holder["path"]).name

        if not downloaded_file:
            best_size = -1
            for f in DOWNLOAD_DIR.iterdir():
                if f.name.startswith(task_id):
                    if re.search(r'\.f\d+\.', f.name):
                        continue
                    size = f.stat().st_size
                    if size > best_size:
                        best_size = size
                        downloaded_file = f.name

        # Clean up intermediate fragment files (e.g. .f251.webm, .f398.mp4)
        for f in DOWNLOAD_DIR.iterdir():
            if f.name.startswith(task_id) and re.search(r'\.f\d+\.', f.name):
                try:
                    f.unlink()
                except Exception:
                    pass

        # Ensure QuickTime compatibility (re-encode VP9/opus etc.)
        if downloaded_file and not is_audio:
            full_path = str(DOWNLOAD_DIR / downloaded_file)
            download_progress[task_id].update({
                "status": "processing",
                "percent": 99,
            })
            await _broadcast(task_id)
            await loop.run_in_executor(None, _ensure_qt_compatible, full_path)

        download_progress[task_id].update({
            "status": "completed",
            "percent": 100,
            "filename": downloaded_file or "",
        })

        # Add to history
        _add_to_history({
            "task_id": task_id,
            "title": title,
            "url": url,
            "filename": downloaded_file,
            "format": format_id,
            "timestamp": time.time(),
        })

    except Exception as e:
        download_progress[task_id].update({
            "status": "error",
            "error": _clean_error(str(e)),
        })

    # Notify all connected WebSocket clients
    await _broadcast(task_id)


def _do_download(url: str, opts: dict):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def _ensure_qt_compatible(filepath: str) -> str:
    """Re-encode video to H.264+AAC if codecs aren't QuickTime-compatible.

    Returns the (possibly new) filepath.
    """
    import subprocess
    if not HAS_FFMPEG or not filepath or not os.path.isfile(filepath):
        return filepath

    # Use ffprobe to detect codecs
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", filepath],
            capture_output=True, text=True, timeout=15,
        )
        if probe.returncode != 0:
            return filepath
        import json as _json
        streams = _json.loads(probe.stdout).get("streams", [])
    except Exception:
        return filepath

    vcodec = ""
    acodec = ""
    for s in streams:
        if s.get("codec_type") == "video" and not vcodec:
            vcodec = s.get("codec_name", "")
        elif s.get("codec_type") == "audio" and not acodec:
            acodec = s.get("codec_name", "")

    qt_video = {"h264", "hevc", "mpeg4", "prores"}  # hevc supported on modern macOS
    qt_audio = {"aac", "mp3", "alac", "pcm_s16le", "pcm_s24le"}

    needs_video_reencode = vcodec and vcodec not in qt_video
    needs_audio_reencode = acodec and acodec not in qt_audio

    if not needs_video_reencode and not needs_audio_reencode:
        return filepath  # Already compatible

    print(f"[COMPAT] Re-encoding for QuickTime: video={vcodec}->{'libx264' if needs_video_reencode else 'copy'}, "
          f"audio={acodec}->{'aac' if needs_audio_reencode else 'copy'}")

    out_path = filepath.rsplit(".", 1)[0] + "_qt.mp4"
    cmd = ["ffmpeg", "-y", "-i", filepath]
    cmd += ["-c:v", "libx264", "-crf", "20", "-preset", "fast"] if needs_video_reencode else ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "192k"] if needs_audio_reencode else ["-c:a", "copy"]
    cmd += ["-movflags", "+faststart", out_path]

    try:
        subprocess.run(cmd, capture_output=True, timeout=600, check=True)
        # Replace original with re-encoded version
        os.remove(filepath)
        os.rename(out_path, filepath)
        print(f"[COMPAT] Re-encode complete: {filepath}")
    except Exception as e:
        print(f"[COMPAT] Re-encode failed: {e}")
        # Clean up partial output
        if os.path.exists(out_path):
            os.remove(out_path)

    return filepath


def _progress_hook(task_id: str, d: dict):
    if d["status"] == "downloading":
        download_progress[task_id].update({
            "status": "downloading",
            "percent": _parse_percent(d.get("_percent_str", "0%")),
            "speed": _clean_error(d.get("_speed_str", "")),
            "eta": _clean_error(d.get("_eta_str", "")),
            "downloaded": d.get("downloaded_bytes", 0),
            "total": d.get("total_bytes") or d.get("total_bytes_estimate", 0),
        })
        # Schedule broadcast (fire and forget from sync context)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(_broadcast(task_id), loop)
        except Exception:
            pass
    elif d["status"] == "finished":
        download_progress[task_id].update({
            "status": "processing",
            "percent": 99,
        })


def _parse_percent(s: str) -> float:
    try:
        clean = _clean_error(s).strip().replace("%", "")
        return float(clean)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# WebSocket: Real-time progress
# ---------------------------------------------------------------------------
@app.websocket("/ws/progress/{task_id}")
async def websocket_progress(websocket: WebSocket, task_id: str):
    await websocket.accept()
    if task_id not in active_websockets:
        active_websockets[task_id] = []
    active_websockets[task_id].append(websocket)

    try:
        # Send current state immediately
        if task_id in download_progress:
            await websocket.send_json(download_progress[task_id])
        # Keep alive until disconnect
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_websockets[task_id].remove(websocket)


async def _broadcast(task_id: str):
    if task_id in active_websockets and task_id in download_progress:
        data = download_progress[task_id]
        dead = []
        for ws in active_websockets[task_id]:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            active_websockets[task_id].remove(ws)


# ---------------------------------------------------------------------------
# API: Serve downloaded file
# ---------------------------------------------------------------------------
@app.get("/api/file/{filename}")
async def serve_file(filename: str):
    file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream",
    )


# ---------------------------------------------------------------------------
# API: Download history
# ---------------------------------------------------------------------------
@app.get("/api/history")
async def get_history():
    return _load_history()


@app.delete("/api/history")
async def clear_history():
    _save_history([])
    return {"status": "cleared"}


@app.delete("/api/history/{task_id}")
async def delete_history_entry(task_id: str):
    history = _load_history()
    history = [h for h in history if h.get("task_id") != task_id]
    _save_history(history)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# API: Check download status (polling fallback)
# ---------------------------------------------------------------------------
@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in download_progress:
        raise HTTPException(status_code=404, detail="Task not found")
    return download_progress[task_id]


# ---------------------------------------------------------------------------
# API: User feedback → Jira ticket
# ---------------------------------------------------------------------------
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "https://a1595042264.atlassian.net")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "SHAN")


@app.post("/api/feedback")
async def submit_feedback(body: dict):
    subject = (body.get("subject") or "").strip()
    message = (body.get("message") or "").strip()
    email = (body.get("email") or "").strip()

    if not subject or not message:
        raise HTTPException(status_code=400, detail="Subject and message are required")

    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        raise HTTPException(status_code=503, detail="Feedback service is not configured")

    # Build Jira issue payload
    description_text = message
    if email:
        description_text += f"\n\nSubmitted by: {email}"

    jira_payload = json.dumps({
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": f"[VideoDownloader] {subject}",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": description_text}],
                    }
                ],
            },
            "issuetype": {"name": "Task"},
        }
    }).encode("utf-8")

    credentials = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    req = urllib.request.Request(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        data=jira_payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {credentials}",
        },
        method="POST",
    )

    try:
        resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=15)
        result = json.loads(resp.read().decode())
        return {"status": "ok", "ticket": result.get("key")}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        raise HTTPException(status_code=502, detail=f"Failed to create ticket: {error_body}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to create ticket: {str(e)}")


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------
FRONTEND_DIR = BASE_DIR / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
