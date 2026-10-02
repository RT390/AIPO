from flask import Flask, render_template, request, jsonify, Response, send_file
import yt_dlp
import os
import re
import tempfile
import shutil
import uuid
import json

app = Flask(__name__)

DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "ytdlp_downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ========== أدوات ==========

def human_size(bytes_size):
    if not bytes_size:
        return "غير معروف"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} PB"


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


# ========== استخراج المعلومات ==========

def extract_info(url):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = []
    seen = set()

    for f in info.get("formats", []):
        if not f.get("url"):
            continue

        height = f.get("height")
        ext = f.get("ext", "?")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        filesize = f.get("filesize") or f.get("filesize_approx")

        has_video = vcodec != "none"
        has_audio = acodec != "none"

        if has_video and has_audio:
            kind, icon = "فيديو + صوت", "🎬"
        elif has_video:
            kind, icon = "فيديو فقط (يُدمج)", "🎥"
        else:
            kind, icon = "صوت فقط", "🎵"

        key = (height, ext, kind, filesize)
        if key in seen:
            continue
        seen.add(key)

        label = f"{height}p" if height else (f.get("format_note") or "غير محدد")

        formats.append({
            "format_id": f.get("format_id"),
            "label": label,
            "ext": ext,
            "kind": kind,
            "kind_icon": icon,
            "filesize": human_size(filesize),
            "filesize_bytes": filesize or 0,
            "fps": f.get("fps"),
            "has_video": has_video,
            "has_audio": has_audio,
            "quality_score": (height or 0) * 100 + (1 if has_audio else 0),
        })

    formats.sort(key=lambda x: x["quality_score"], reverse=True)

    return {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "formats": formats,
        "ffmpeg_available": has_ffmpeg(),
    }


# ========== المسارات ==========

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json() or {}
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "الرجاء إدخال رابط"}), 400

    try:
        return jsonify(extract_info(url))
    except Exception as e:
        return jsonify({"error": f"فشل استخراج المعلومات: {str(e)}"}), 500


@app.route("/api/download")
def api_download():
    """تحميل مع بث حالة التقدم عبر Server-Sent Events"""
    url = request.args.get("url", "").strip()
    format_id = request.args.get("format_id", "").strip()
    kind = request.args.get("kind", "").strip()
    title = request.args.get("title", "video")

    if not url or not format_id:
        return "معطيات ناقصة", 400

    safe_name = re.sub(r'[^\w\s\-\u0600-\u06FF]', '_', title)[:80].strip()
    if not safe_name:
        safe_name = f"video_{uuid.uuid4().hex[:8]}"

    output_template = os.path.join(DOWNLOAD_DIR, f"{safe_name}.%(ext)s")

    if "فيديو فقط" in kind and has_ffmpeg():
        fmt = f"{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio/{format_id}"
        merge = "mp4"
    else:
        fmt = format_id
        merge = None

    # قائمة لتخزين حالة التقدم
    progress_data = {"percent": 0, "speed": "", "eta": "", "size": "", "status": "starting"}

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                progress_data["percent"] = round(downloaded * 100 / total, 1)
            progress_data["size"] = human_size(total) if total else ""
            speed = d.get("speed")
            if speed:
                progress_data["speed"] = human_size(speed) + "/s"
            eta = d.get("eta")
            if eta:
                progress_data["eta"] = f"{int(eta)} ثانية"
            progress_data["status"] = "downloading"
        elif d["status"] == "finished":
            progress_data["percent"] = 100
            progress_data["status"] = "processing"
        elif d["status"] == "error":
            progress_data["status"] = "error"

    def generate():
        """SSE stream يرسل التقدم ثم رابط التحميل النهائي"""
        ydl_opts = {
            "format": fmt,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "progress_hooks": [progress_hook],
        }
        if merge:
            ydl_opts["merge_output_format"] = merge

        try:
            # إرسال حالة البدء
            yield f"data: {json.dumps({'status': 'starting'})}\n\n"

            import threading
            result_holder = {"filepath": None, "error": None, "done": False}

            def run_download():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        filepath = ydl.prepare_filename(info)

                        if not os.path.exists(filepath):
                            base, _ = os.path.splitext(filepath)
                            for ext in [".mp4", ".mkv", ".webm", ".m4a", ".mp3"]:
                                if os.path.exists(base + ext):
                                    filepath = base + ext
                                    break

                        result_holder["filepath"] = filepath
                except Exception as e:
                    result_holder["error"] = str(e)
                finally:
                    result_holder["done"] = True

            t = threading.Thread(target=run_download, daemon=True)
            t.start()

            import time
            while not result_holder["done"]:
                time.sleep(0.5)
                yield f"data: {json.dumps(progress_data)}\n\n"

            if result_holder["error"]:
                yield f"data: {json.dumps({'status': 'error', 'error': result_holder['error']})}\n\n"
                return

            filepath = result_holder["filepath"]
            if not filepath or not os.path.exists(filepath):
                yield f"data: {json.dumps({'status': 'error', 'error': 'لم يتم العثور على الملف'})}\n\n"
                return

            # إرسال الحالة النهائية مع رابط التحميل
            download_token = uuid.uuid4().hex
            # نخزّن الملف في مجلد قابل للوصول
            final_name = os.path.basename(filepath)
            yield f"data: {json.dumps({'status': 'done', 'filename': final_name, 'filesize': human_size(os.path.getsize(filepath))})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'error': str(e)})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.route("/api/file")
def api_file():
    """إرسال الملف النهائي"""
    filename = request.args.get("name", "")
    if not filename:
        return "اسم الملف مفقود", 400

    # حماية من path traversal
    filename = os.path.basename(filename)
    filepath = os.path.join(DOWNLOAD_DIR, filename)

    if not os.path.exists(filepath):
        return "الملف غير موجود", 404

    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename,
    )


if __name__ == "__main__":
    if not has_ffmpeg():
        print("⚠️  ffmpeg غير مثبّت — الجودات العالية ستُحمّل بدون صوت")
        print("   للتثبيت: pkg install ffmpeg")
    print("🚀 السيرفر يعمل على: http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)