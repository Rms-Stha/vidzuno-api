import ipaddress
import logging
import os
import re
import socket
from urllib.parse import quote, urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vidzuno")

# Render > Environment ma DEBUG=1 rakhda asli error dekhincha (testing ko lagi)
DEBUG = os.getenv("DEBUG") == "1"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://vidzuno.blogspot.com",
        "http://vidzuno.blogspot.com",
    ],
    allow_origin_regex=r"https://vidzuno\.blogspot\.[a-z.]+",
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


def safe_url(u: str) -> bool:
    """Blocks localhost / private IP links (server security)"""
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        for i in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(i[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except Exception:
        return False
    return True


def clean_filename(title: str, ext: str) -> str:
    """Removes invalid filename characters safely"""
    base = re.sub(r'[\\/*?:"<>|\r\n]+', "_", (title or "video")).strip()
    base = base[:120] or "video"
    return f"{base}.{ext}"


def friendly(err: str) -> str:
    e = err.lower()
    if "not a bot" in e:
        return "The source site blocked our server (bot check). Please try again later or use another link."
    if "drm" in e:
        return "This video is DRM-protected and cannot be downloaded."
    if "unsupported url" in e:
        return "This website is not supported yet."
    if any(k in e for k in ("private", "login", "log in", "sign in", "members", "age-restricted", "cookies")):
        return "This video is private or requires login."
    if any(k in e for k in ("country", "geo", "region")):
        return "This video is not available in our server's region."
    if any(k in e for k in ("404", "not found", "removed", "unavailable", "does not exist")):
        return "Video not found or it has been removed."
    if any(k in e for k in ("429", "403", "forbidden", "too many requests")):
        return "The source site blocked the request. Please try again later."
    return "Could not fetch this video. Please check the link and try again."


def fail(e: Exception):
    raw = str(e)
    log.warning("extract failed: %s", raw)
    detail = {"message": friendly(raw)}
    if DEBUG:
        detail["debug"] = raw[:300]
    raise HTTPException(400, detail)


def extract(url: str):
    opts = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        d = ydl.extract_info(url, download=False)
    if d and d.get("entries"):
        entries = [x for x in d["entries"] if x]
        if entries:
            d = entries[0]
    return d


def direct(f):
    return f.get("url") and f.get("protocol") in ("http", "https")


def has_v(f):
    return f.get("vcodec") not in (None, "none")


def has_a(f):
    return f.get("acodec") not in (None, "none")


def pick_format(d, quality: str):
    fmts = [f for f in d.get("formats", []) if direct(f)]

    # Audio-only option
    if quality in ("mp3", "audio"):
        aud = [f for f in fmts if has_a(f) and not has_v(f)]
        aud.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0)
        if aud:
            return aud[-1]
        # Audio-only stream nabhaye video+audio stream fallback
        both = [f for f in fmts if has_a(f)]
        both.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0)
        return both[-1] if both else None

    # Video + Audio (progressive/muxed)
    vids = [f for f in fmts if has_v(f) and has_a(f)]
    vids.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    if not vids:
        return None

    want = int(quality) if quality.isdigit() else 0
    exact = [f for f in vids if f.get("height") == want]
    if exact:
        return exact[-1]

    # Exact quality nabhaye nजिकको (equal or lower) quality dincha
    lower = [f for f in vids if (f.get("height") or 0) <= want] if want else []
    return lower[-1] if lower else vids[-1]


@app.get("/")
def health():
    return {"ok": True}


@app.get("/api/info")
def info(url: str):
    if not safe_url(url):
        raise HTTPException(400, {"message": "Invalid link."})
    try:
        d = extract(url)
    except Exception as e:
        fail(e)

    heights = sorted({
        f["height"]
        for f in d.get("formats", [])
        if direct(f) and has_v(f) and has_a(f) and f.get("height")
    })
    return {
        "title": d.get("title"),
        "thumbnail": d.get("thumbnail"),
        "heights": heights,
    }


@app.get("/api/download")
async def download(url: str, quality: str = "best"):
    if not safe_url(url):
        raise HTTPException(400, {"message": "Invalid link."})
    try:
        d = await run_in_threadpool(extract, url)
    except Exception as e:
        fail(e)

    f = pick_format(d, quality)
    if not f:
        raise HTTPException(404, {"message": "No downloadable format found for this video."})
    if not safe_url(f["url"]):
        raise HTTPException(400, {"message": "Invalid video source."})

    ext = f.get("ext") or "mp4"
    name = clean_filename(d.get("title"), ext)

    client = httpx.AsyncClient(follow_redirects=True, timeout=None)
    r = await client.send(
        client.build_request("GET", f["url"], headers=f.get("http_headers") or {}),
        stream=True,
    )
    if r.status_code >= 400:
        await r.aclose()
        await client.aclose()
        raise HTTPException(502, {"message": "The source site blocked the download."})

    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}
    if r.headers.get("content-length"):
        headers["Content-Length"] = r.headers["content-length"]

    async def gen():
        try:
            async for chunk in r.aiter_bytes(65536):
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(
        gen(),
        media_type=r.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )
