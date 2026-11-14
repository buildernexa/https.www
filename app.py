# language: Python, file: app.py, target: TikTok Studio backend
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp, httpx, re, asyncio, io

app = FastAPI(title="TikTok Studio")
app.mount("/static", StaticFiles(directory="static"), name="static")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
TIKWM = "https://www.tikwm.com/api/"

class Req(BaseModel):
    url: str

def _is_tt(u: str) -> bool:
    return bool(re.search(r"tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com", u or ""))

def _ytdlp(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "format": "best"}
    with yt_dlp.YoutubeDL(opts) as y:
        i = y.extract_info(url, download=False)
    return {
        "source": "yt-dlp",
        "title": (i.get("title") or "TikTok video")[:200],
        "author": (i.get("uploader") or i.get("creator") or "unknown")[:80],
        "thumbnail": i.get("thumbnail"),
        "duration": i.get("duration"),
        "video_url": i.get("url"),
        "ext": i.get("ext", "mp4"),
    }

async def _tikwm(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as c:
        r = await c.get(TIKWM, params={"url": url, "hd": 1})
        j = r.json()
    if j.get("code") != 0 or not j.get("data"):
        raise RuntimeError(j.get("msg", "tikwm no data"))
    d = j["data"]
    return {
        "source": "tikwm",
        "title": (d.get("title") or "TikTok video")[:200],
        "author": (d.get("author") or {}).get("unique_id", "unknown")[:80],
        "thumbnail": d.get("cover"),
        "duration": d.get("duration"),
        "video_url": d.get("hdplay") or d.get("play"),
        "music_url": (d.get("music_info") or {}).get("play"),
        "music_title": (d.get("music_info") or {}).get("title"),
        "ext": "mp4",
    }

async def _douyin(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as c:
        r = await c.get(TIKWM, params={"url": url, "hd": 1, "region": "cn"})
        j = r.json()
    if j.get("code") != 0 or not j.get("data"):
        raise RuntimeError("douyin no data")
    d = j["data"]
    return {
        "source": "douyin-mirror",
        "title": (d.get("title") or "TikTok video")[:200],
        "author": (d.get("author") or {}).get("unique_id", "unknown")[:80],
        "thumbnail": d.get("cover"),
        "duration": d.get("duration"),
        "video_url": d.get("hdplay") or d.get("play"),
        "music_url": (d.get("music_info") or {}).get("play"),
        "ext": "mp4",
    }

async def _snaptik(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA, "Referer": "https://snaptik.app/"}, follow_redirects=True) as c:
        r = await c.post("https://snaptik.app/abc2.php", data={"url": url})
    m = re.search(r'href="(https://[^"]+\.mp4[^"]*)"', r.text)
    if not m:
        raise RuntimeError("snaptik no mp4")
    return {"source": "snaptik", "title": "TikTok via snaptik", "author": "unknown",
            "thumbnail": None, "duration": None, "video_url": m.group(1), "ext": "mp4"}

SOURCES = [("yt-dlp", _ytdlp), ("tikwm", _tikwm), ("douyin-mirror", _douyin), ("snaptik", _snaptik)]

async def resolve(url: str) -> dict:
    errs = []
    for name, fn in SOURCES:
        try:
            data = await asyncio.to_thread(fn, url) if name == "yt-dlp" else await fn(url)
            if data.get("video_url"):
                data["fallback"] = [e[0] for e in errs]
                return {"ok": True, "data": data}
            errs.append((name, "no url"))
        except Exception as e:
            errs.append((name, str(e)[:120]))
    return {"ok": False, "errors": errs}

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/resolve")
async def api_resolve(req: Req):
    if not _is_tt(req.url):
        raise HTTPException(400, "URL TikTok tidak valid")
    return await resolve(req.url)

async def _fetch(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=90, follow_redirects=True, headers={"User-Agent": UA}) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content

@app.post("/api/download/video")
async def dl_video(req: Req):
    if not _is_tt(req.url): raise HTTPException(400, "URL tidak valid")
    res = await resolve(req.url)
    if not res["ok"]: raise HTTPException(502, f"Semua sumber gagal: {res['errors']}")
    d = res["data"]
    buf = await _fetch(d["video_url"])
    name = re.sub(r"[^\w\-_. ]", "_", d["title"])[:80] or "tiktok"
    return Response(buf, media_type="video/mp4",
                    headers={"Content-Disposition": f'attachment; filename="{name}.mp4"'})

@app.post("/api/download/audio")
async def dl_audio(req: Req):
    if not _is_tt(req.url): raise HTTPException(400, "URL tidak valid")
    res = await resolve(req.url)
    if not res["ok"]: raise HTTPException(502, f"Semua sumber gagal: {res['errors']}")
    d = res["data"]
    if not d.get("music_url"):
        raise HTTPException(404, "Audio tidak tersedia dari sumber ini")
    buf = await _fetch(d["music_url"])
    name = re.sub(r"[^\w\-_. ]", "_", d.get("music_title") or d["title"])[:80] or "tiktok-audio"
    return Response(buf, media_type="audio/mpeg",
                    headers={"Content-Disposition": f'attachment; filename="{name}.mp3"'})

@app.post("/api/download/thumb")
async def dl_thumb(req: Req):
    if not _is_tt(req.url): raise HTTPException(400, "URL tidak valid")
    res = await resolve(req.url)
    if not res["ok"] or not res["data"].get("thumbnail"):
        raise HTTPException(404, "Thumbnail tidak tersedia")
    d = res["data"]
    buf = await _fetch(d["thumbnail"])
    name = re.sub(r"[^\w\-_. ]", "_", d["title"])[:80] or "tiktok-thumb"
    return Response(buf, media_type="image/jpeg",
                    headers={"Content-Disposition": f'attachment; filename="{name}.jpg"'})

@app.post("/api/proxy")
async def proxy(req: Req):
    """Proxy thumbnail/image to bypass referrer restrictions."""
    try:
        buf = await _fetch(req.url)
        return Response(buf, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(502, str(e)[:120])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000)
