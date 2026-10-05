from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
import yt_dlp
import os
import tempfile
from typing import Optional

app = FastAPI(title="YouTube Downloader API")

# إعدادات Proxy (اختياري - للاستضافة على Render)
PROXY_URL = os.getenv("PROXY_URL", None)  # مثال: http://user:pass@host:port
COOKIES_FILE = os.getenv("COOKIES_FILE", "/etc/secrets/cookies.txt")


def get_ydl_opts(format_id: Optional[str] = None, download: bool = False):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "extract_flat": False,
        # تجاوز حظر IP
        "geo_bypass": True,
        "geo_bypass_country": "US",
        # User-Agent عشوائي
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36",
        },
    }

    # إضافة Proxy إذا موجود
    if PROXY_URL:
        opts["proxy"] = PROXY_URL

    # إضافة Cookies إذا موجودة
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    if format_id:
        opts["format"] = format_id

    if download:
        opts["outtmpl"] = "%(title)s.%(ext)s"

    return opts


@app.get("/")
def root():
    return {"status": "ok", "message": "YouTube Downloader API"}


@app.get("/info")
def get_info(url: str = Query(..., description="YouTube URL")):
    """جلب معلومات الفيديو وصيغه المتاحة"""
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        for f in info.get("formats", []):
            formats.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "quality": f.get("format_note") or f.get("resolution"),
                "resolution": f.get("resolution"),
                "fps": f.get("fps"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "type": "video+audio" if f.get("acodec") != "none" and f.get("vcodec") != "none" 
                        else ("video" if f.get("vcodec") != "none" else "audio"),
            })

        return {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "formats": formats,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/download")
def download(
    url: str = Query(..., description="YouTube URL"),
    format_id: str = Query("best", description="Format ID أو best/bestaudio"),
):
    """تحميل الفيديو مباشرة"""
    try:
        opts = get_ydl_opts(format_id=format_id, download=False)
        opts["outtmpl"] = "%(title)s.%(ext)s"

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # الحصول على الرابط المباشر
            if "url" in info:
                direct_url = info["url"]
            elif info.get("requested_formats"):
                # فيديو+صوت منفصلين
                direct_url = info["requested_formats"][0]["url"]
            else:
                direct_url = info.get("url")

            if not direct_url:
                raise HTTPException(status_code=404, detail="لم يتم إيجاد رابط مباشر")

            filename = f"{info.get('title', 'video')}.{info.get('ext', 'mp4')}"

        return JSONResponse({
            "title": info.get("title"),
            "filename": filename,
            "direct_url": direct_url,
            "ext": info.get("ext"),
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/stream")
def stream(
    url: str = Query(..., description="YouTube URL"),
    format_id: str = Query("best[ext=mp4]/best", description="Format ID"),
):
    """بث الفيديو مباشرة (Streaming)"""
    try:
        opts = get_ydl_opts(format_id=format_id)
        opts["outtmpl"] = "%(title)s.%(ext)s"

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            direct_url = info.get("url")
            if not direct_url and info.get("requested_formats"):
                direct_url = info["requested_formats"][0]["url"]

        if not direct_url:
            raise HTTPException(status_code=404, detail="لا يوجد رابط مباشر")

        # إعادة التوجيه للرابط المباشر
        from fastapi.responses import RedirectResponse
        return RedirectResponse(direct_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)