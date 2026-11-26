
import os
import re
import ipaddress
import socket
import logging
import tempfile
import shutil
import asyncio
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

# =====================================================
# VIDZUNO VIDEO DOWNLOADER API
# Blogger Frontend + Render Backend
# =====================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vidzuno")

DEBUG = os.getenv("DEBUG", "0") == "1"

MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "1500"))
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))

semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# =====================================================
# FASTAPI APP
# =====================================================

app = FastAPI(
    title="Vidzuno API",
    description="Vidzuno Video Downloader",
    version="2.0.0"
)

origins = [
    "https://vidzuno.blogspot.com",
    "http://vidzuno.blogspot.com"
]

custom_domain = os.getenv("CUSTOM_ORIGIN")
if custom_domain:
    origins.append(custom_domain.rstrip("/"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"]
)

# =====================================================
# SECURITY
# =====================================================

def safe_url(url):
    try:
        p = urlparse(url)

        if p.scheme not in ("http", "https"):
            return False

        if not p.hostname:
            return False

        if p.username or p.password:
            return False

        if p.port not in (None, 80, 443):
            return False

        host = p.hostname.rstrip(".").lower()

        if host == "localhost" or host.endswith(".local"):
            return False

        try:
            ip = ipaddress.ip_address(host)
            return ip.is_global
        except ValueError:
            pass

        results = socket.getaddrinfo(
            host,
            p.port or (443 if p.scheme == "https" else 80),
            type=socket.SOCK_STREAM
        )

        if not results:
            return False

        for result in results:
            ip = ipaddress.ip_address(result[4][0])
            if not ip.is_global:
                return False

        return True

    except Exception:
        return False


def validate_url(url):
    if not safe_url(url):
        raise HTTPException(
            status_code=400,
            detail={"message": "Invalid or unsafe video URL."}
        )


# =====================================================
# FILENAME CLEANER
# =====================================================

def clean_filename(title, ext):
    title = re.sub(
        r'[\\/*?:"<>|\r\n]+',
        "_",
        title or "video"
    )

    title = title.strip(" ._")[:120] or "video"
    ext = re.sub(r"[^a-zA-Z0-9]", "", ext or "mp4")

    return f"{title}.{ext}"


# =====================================================
# ERROR HANDLER
# =====================================================

def friendly_error(error):
    e = str(error).lower()

    if "unsupported url" in e:
        return "This website is not supported."

    if "drm" in e:
        return "This video is DRM protected."

    if "not a bot" in e:
        return "The source website blocked our server."

    if any(x in e for x in (
        "private video", "login required",
        "sign in", "members only", "age-restricted"
    )):
        return "This video requires login or permission."

    if any(x in e for x in (
        "geo restricted", "country", "region"
    )):
        return "This video is unavailable in our region."

    if any(x in e for x in (
        "404", "not found", "removed", "unavailable"
    )):
        return "Video not found or removed."

    if any(x in e for x in (
        "429", "403", "forbidden", "too many requests"
    )):
        return "The source website blocked the request."

    return "Unable to process this video. Please try another link."


def fail(error):
    log.exception("Vidzuno error: %s", error)

    detail = {
        "message": friendly_error(error)
    }

    if DEBUG:
        detail["debug"] = str(error)[:300]

    raise HTTPException(400, detail)


# =====================================================
# YT-DLP OPTIONS
# =====================================================

def base_options():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "ignoreerrors": False
    }


# =====================================================
# VIDEO EXTRACTION
# =====================================================

def extract_video(url):
    options = base_options()
    options["skip_download"] = True

    with yt_dlp.YoutubeDL(options) as ydl:
        data = ydl.extract_info(url, download=False)

    if not data:
        raise ValueError("Video information not found.")

    if data.get("entries"):
        entries = [x for x in data["entries"] if x]

        if not entries:
            raise ValueError("No video found.")

        data = entries[0]

    return data


# =====================================================
# AVAILABLE VIDEO QUALITIES
# =====================================================

def get_qualities(data):
    qualities = set()

    for f in data.get("formats", []):
        height = f.get("height")

        if (
            height
            and f.get("vcodec") not in (None, "none")
        ):
            qualities.add(int(height))

    return sorted(qualities)


# =====================================================
# FORMAT SELECTOR
# =====================================================

ALLOWED_QUALITIES = {
    "360", "480", "720", "1080",
    "1440", "2160", "best", "mp3"
}


def select_format(quality):
    if quality == "mp3":
        return "bestaudio/best"

    if quality == "best":
        return "bestvideo+bestaudio/best"

    height = int(quality)

    return (
        f"bestvideo[height<={height}]"
        f"+bestaudio/"
        f"best[height<={height}]/"
        f"bestvideo+bestaudio/best"
    )


# =====================================================
# DOWNLOAD PROCESSOR
# =====================================================

def process_download(url, quality, folder):
    template = os.path.join(
        folder,
        "%(title).150B-%(id)s.%(ext)s"
    )

    options = base_options()

    options.update({
        "format": select_format(quality),
        "outtmpl": template,
        "overwrites": True,
        "continuedl": True,
        "merge_output_format": "mp4"
    })

    if quality == "mp3":
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192"
        }]

    with yt_dlp.YoutubeDL(options) as ydl:
        data = ydl.extract_info(url, download=True)

    if not data:
        raise ValueError("Download failed.")

    if data.get("entries"):
        entries = [x for x in data["entries"] if x]
        if not entries:
            raise ValueError("No video downloaded.")
        data = entries[0]

    allowed = {
        ".mp4", ".mkv", ".webm", ".mov",
        ".m4v", ".mp3", ".m4a", ".opus",
        ".ogg", ".wav", ".flac"
    }

    files = [
        p for p in Path(folder).iterdir()
        if p.is_file()
        and p.suffix.lower() in allowed
        and p.stat().st_size > 0
    ]

    if not files:
        raise ValueError("Output file not found.")

    output = max(
        files,
        key=lambda p: p.stat().st_size
    )

    if output.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise ValueError("Download exceeds maximum size.")

    return output, data


# =====================================================
# HEALTH CHECK
# =====================================================

@app.get("/")
def home():
    return {
        "ok": True,
        "name": "Vidzuno API",
        "version": "2.0.0",
        "status": "online"
    }


@app.get("/health")
def health():
    return {"status": "online"}


# =====================================================
# VIDEO INFORMATION API
# =====================================================

@app.get("/api/info")
async def video_info(
    url: str = Query(..., max_length=2048)
):
    validate_url(url)

    try:
        data = await run_in_threadpool(
            extract_video,
            url
        )

        qualities = get_qualities(data)

        return {
            "success": True,
            "title": data.get("title") or "Unknown Video",
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration"),
            "uploader": data.get("uploader"),
            "qualities": qualities,
            "heights": qualities,
            "formats": ["mp4", "mp3"]
        }

    except HTTPException:
        raise

    except Exception as e:
        fail(e)


# =====================================================
# VIDEO / MP3 DOWNLOAD API
# =====================================================

@app.get("/api/download")
async def download(
    url: str = Query(..., max_length=2048),
    quality: str = Query("best")
):
    validate_url(url)

    quality = quality.lower().strip()

    if quality not in ALLOWED_QUALITIES:
        raise HTTPException(
            status_code=400,
            detail={"message": "Unsupported quality."}
        )

    folder = tempfile.mkdtemp(prefix="vidzuno_")

    try:
        async with semaphore:
            output, data = await run_in_threadpool(
                process_download,
                url,
                quality,
                folder
            )

        filename = clean_filename(
            data.get("title") or "video",
            output.suffix.lstrip(".")
        )

        return FileResponse(
            path=str(output),
            filename=filename,
            media_type="application/octet-stream",
            background=BackgroundTask(
                shutil.rmtree,
                folder,
                ignore_errors=True
            )
        )

    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        fail(e)


# =====================================================
# END OF VIDZUNO API
# =====================================================
