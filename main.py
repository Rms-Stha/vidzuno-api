import ipaddress, socket
from urllib.parse import quote, urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://vidzuno.blogspot.com"],  # aafno Blogger URL
    allow_methods=["GET"],
    allow_headers=["*"],
)

def safe_url(u: str) -> bool:
    """localhost / private IP ko link block garcha (server ko security)"""
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

def extract(url: str):
    opts = {"quiet": True, "noplaylist": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def direct(f):
    return f.get("url") and f.get("protocol") in ("http", "https")

def has_v(f): return f.get("vcodec") not in (None, "none")
def has_a(f): return f.get("acodec") not in (None, "none")

def pick_format(d, quality: str):
    fmts = [f for f in d.get("formats", []) if direct(f)]
    if quality == "mp3":  # audio only
        aud = [f for f in fmts if has_a(f) and not has_v(f)]
        aud.sort(key=lambda f: f.get("abr") or 0)
        return aud[-1] if aud else None
    vids = [f for f in fmts if has_v(f) and has_a(f)]
    vids.sort(key=lambda f: f.get("height") or 0)
    if not vids:
        return None
    want = int(quality) if quality.isdigit() else 0
    exact = [f for f in vids if f.get("height") == want]
    return exact[-1] if exact else vids[-1]   # nabhaye original (sabai bhanda thulo)

@app.get("/")
def health():
    return {"ok": True}

@app.get("/api/info")
def info(url: str):
    if not safe_url(url):
        raise HTTPException(400, "Link thik chaina")
    try:
        d = extract(url)
    except Exception:
        raise HTTPException(400, "Yo link bata video paiyena")
    heights = sorted({f["height"] for f in d.get("formats", [])
                      if direct(f) and has_v(f) and has_a(f) and f.get("height")})
    return {"title": d.get("title"), "thumbnail": d.get("thumbnail"), "heights": heights}

@app.get("/api/download")
async def download(url: str, quality: str = "best"):
    if not safe_url(url):
        raise HTTPException(400, "Link thik chaina")
    try:
        d = await run_in_threadpool(extract, url)
    except Exception:
        raise HTTPException(400, "Yo link bata video paiyena")
    f = pick_format(d, quality)
    if not f:
        raise HTTPException(404, "Download garne format paiyena")

    ext = f.get("ext") or "mp4"
    name = f"{d.get('title') or 'video'}.{ext}"
    client = httpx.AsyncClient(follow_redirects=True, timeout=None)
    r = await client.send(
        client.build_request("GET", f["url"], headers=f.get("http_headers") or {}),
        stream=True,
    )
    if r.status_code >= 400:
        await r.aclose(); await client.aclose()
        raise HTTPException(502, "Source site le download roki")

    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}
    if r.headers.get("content-length"):
        headers["Content-Length"] = r.headers["content-length"]

    async def gen():
        try:
            async for chunk in r.aiter_bytes(65536):
                yield chunk
        finally:
            await r.aclose(); await client.aclose()

    return StreamingResponse(gen(), media_type=r.headers.get("content-type", "application/octet-stream"), headers=headers)
