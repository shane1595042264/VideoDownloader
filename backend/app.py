"""
VideoDownloader - FastAPI Backend
A modern, scalable video downloader supporting YouTube, Bilibili, and 1000+ sites.
"""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import re
import shutil

import yt_dlp

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
    return {"status": "ok", "has_ffmpeg": HAS_FFMPEG}

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

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _extract_info, url, ydl_opts)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

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

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    # For specific video-only format IDs (e.g. "137", "401"), always add
    # best audio so the user doesn't get a silent file.
    # Only do this if ffmpeg is available (merging requires ffmpeg).
    if HAS_FFMPEG:
        if (fmt_type == "video"
                and "+" not in format_id
                and format_id not in ("best", "bestvideo+bestaudio/best")):
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
    asyncio.create_task(_run_download(task_id, url, format_id, title))
    return {"task_id": task_id}


async def _run_download(task_id: str, url: str, format_id: str, title: str):
    """Run yt-dlp download in a thread and broadcast progress via WebSocket."""
    safe_title = "".join(c if c.isalnum() or c in " -_." else "_" for c in title)[:80]
    output_template = str(DOWNLOAD_DIR / f"{task_id}_{safe_title}.%(ext)s")

    is_audio = format_id == "bestaudio"

    # Track the final filename from yt-dlp's postprocessor hooks
    final_filename_holder = {"path": None}

    def _pp_hook(d):
        if d.get("status") == "finished":
            final_filename_holder["path"] = d.get("info_dict", {}).get("filepath")

    ydl_opts = {
        "format": format_id,
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [lambda d: _progress_hook(task_id, d)],
        "postprocessor_hooks": [_pp_hook],
        "keepvideo": False,
    }

    if HAS_FFMPEG:
        if not is_audio:
            ydl_opts["merge_output_format"] = "mp4"
            # Re-encode audio to AAC for QuickTime compatibility
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
            "error": str(e),
        })

    # Notify all connected WebSocket clients
    await _broadcast(task_id)


def _do_download(url: str, opts: dict):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def _progress_hook(task_id: str, d: dict):
    if d["status"] == "downloading":
        download_progress[task_id].update({
            "status": "downloading",
            "percent": _parse_percent(d.get("_percent_str", "0%")),
            "speed": d.get("_speed_str", ""),
            "eta": d.get("_eta_str", ""),
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
        return float(s.strip().replace("%", ""))
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
# Serve frontend
# ---------------------------------------------------------------------------
FRONTEND_DIR = BASE_DIR / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
