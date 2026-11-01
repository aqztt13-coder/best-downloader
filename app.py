import ipaddress
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
import yt_dlp


BASE = os.path.dirname(os.path.abspath(__file__))
MAX_DOWNLOAD_SIZE = 300 * 1024 * 1024  # 300 MB
MAX_REQUEST_SIZE = 16 * 1024  # API requests only need a small JSON body
YTDLP_SOCKET_TIMEOUT = 30
PROGRESS_TTL_SECONDS = 60 * 60
MAX_PROGRESS_JOBS = 100
JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_SIZE

# Progress data is kept in memory because this project is designed to run as
# one local Flask process. Multi-worker/public deployments should use Redis or
# another shared store instead.
_progress_jobs = {}
_progress_lock = threading.Lock()


class YtDlpLogger:
    """Send yt-dlp warnings/errors to the Flask application log."""

    def debug(self, message):
        if app.debug:
            app.logger.debug("yt-dlp: %s", message)

    def warning(self, message):
        app.logger.warning("yt-dlp: %s", message)

    def error(self, message):
        app.logger.error("yt-dlp: %s", message)


def _clean_old_progress_jobs_locked(now=None):
    """Remove expired progress records. Caller must hold _progress_lock."""
    now = now or time.time()
    expired = [
        job_id
        for job_id, item in _progress_jobs.items()
        if now - item.get("updated_at", now) > PROGRESS_TTL_SECONDS
    ]
    for job_id in expired:
        _progress_jobs.pop(job_id, None)

    if len(_progress_jobs) > MAX_PROGRESS_JOBS:
        oldest = sorted(
            _progress_jobs.items(),
            key=lambda pair: pair[1].get("updated_at", 0),
        )
        for job_id, _ in oldest[: len(_progress_jobs) - MAX_PROGRESS_JOBS]:
            _progress_jobs.pop(job_id, None)


def _register_progress_job(job_id):
    with _progress_lock:
        _clean_old_progress_jobs_locked()
        _progress_jobs[job_id] = {
            "status": "starting",
            "percentage": 0,
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed": None,
            "eta": None,
            "message": "جارٍ الاتصال بمصدر الفيديو...",
            "updated_at": time.time(),
        }
        _clean_old_progress_jobs_locked()


def _update_progress_job(job_id, **changes):
    if not job_id:
        return
    with _progress_lock:
        item = _progress_jobs.get(job_id)
        if item is None:
            return
        item.update(changes)
        item["updated_at"] = time.time()


def _get_progress_job(job_id):
    with _progress_lock:
        _clean_old_progress_jobs_locked()
        item = _progress_jobs.get(job_id)
        return dict(item) if item else None


def _valid_job_id(job_id):
    return bool(job_id and JOB_ID_RE.fullmatch(job_id))


def _safe_int(value):
    try:
        if value is None:
            return None
        number = int(value)
        return max(number, 0)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_float(value):
    try:
        if value is None:
            return None
        number = float(value)
        return number if number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _progress_percent(data):
    """Get a file/fragment percentage from a yt-dlp progress event."""
    downloaded = _safe_int(data.get("downloaded_bytes"))
    total = _safe_int(data.get("total_bytes"))
    if not total:
        total = _safe_int(data.get("total_bytes_estimate"))

    if total and downloaded is not None:
        return min(100.0, (downloaded / total) * 100.0), downloaded, total

    fragment_index = _safe_int(data.get("fragment_index"))
    fragment_count = _safe_int(data.get("fragment_count"))
    if fragment_count and fragment_index is not None:
        pct = min(100.0, (fragment_index / fragment_count) * 100.0)
        return pct, downloaded or 0, None

    return None, downloaded or 0, total


def _make_progress_hooks(job_id, kind):
    """Create yt-dlp callbacks that update the browser-polled job record."""
    if not job_id:
        return None, None

    role_percent = {"video": 0.0, "audio": 0.0}
    combined_percent = 0.0
    last_percentage = 0.0

    def calculate_overall(role, file_percent):
        nonlocal combined_percent, last_percentage

        if file_percent is None:
            return last_percentage if last_percentage else None

        if kind == "audio":
            combined_percent = max(combined_percent, file_percent)
            result = combined_percent * 0.90
        elif role == "video":
            role_percent["video"] = max(role_percent["video"], file_percent)
            result = (role_percent["video"] * 0.45) + (role_percent["audio"] * 0.45)
        elif role == "audio":
            role_percent["audio"] = max(role_percent["audio"], file_percent)
            result = (role_percent["video"] * 0.45) + (role_percent["audio"] * 0.45)
        else:
            # A single combined A/V stream (or a site that does not expose
            # codec details) is reported as one continuous download phase.
            combined_percent = max(combined_percent, file_percent)
            result = combined_percent * 0.90

        last_percentage = max(last_percentage, min(90.0, result))
        return last_percentage

    def download_hook(data):
        status = data.get("status")
        info = data.get("info_dict") or {}
        vcodec = info.get("vcodec")
        acodec = info.get("acodec")
        has_video = bool(vcodec and vcodec != "none")
        has_audio = bool(acodec and acodec != "none")

        if kind == "audio":
            role = "audio"
            message = "جارٍ تنزيل الصوت..."
        elif has_video and not has_audio:
            role = "video"
            message = "جارٍ تنزيل الفيديو..."
        elif has_audio and not has_video:
            role = "audio"
            message = "جارٍ تنزيل مسار الصوت..."
        else:
            role = "combined"
            message = "جارٍ تنزيل الفيديو..."

        if status == "downloading":
            file_percent, downloaded, total = _progress_percent(data)
            overall = calculate_overall(role, file_percent)
            _update_progress_job(
                job_id,
                status="downloading",
                percentage=round(overall, 1) if overall is not None else None,
                downloaded_bytes=downloaded,
                total_bytes=total,
                speed=_safe_float(data.get("speed")),
                eta=_safe_float(data.get("eta")),
                message=message,
            )
        elif status == "finished":
            # The file transfer ended; yt-dlp may still merge or post-process it.
            if role == "video":
                role_percent["video"] = 100.0
            elif role == "audio":
                role_percent["audio"] = 100.0
            elif role == "combined":
                combined_percent = 100.0
            overall = calculate_overall(role, 100.0)
            _update_progress_job(
                job_id,
                status="processing",
                percentage=round(overall, 1) if overall is not None else 90,
                speed=None,
                eta=None,
                message="اكتمل نقل المصدر، جارٍ تجهيز الملف...",
            )
        elif status == "error":
            _update_progress_job(
                job_id,
                status="processing",
                message="حدثت مشكلة مؤقتة، جارٍ التعامل معها...",
            )

    def postprocessor_hook(data):
        status = data.get("status")
        if status in ("started", "processing"):
            _update_progress_job(
                job_id,
                status="processing",
                percentage= max(last_percentage, 91.0),
                speed=None,
                eta=None,
                message="جارٍ دمج وتجهيز ملف الفيديو...",
            )
        elif status == "finished":
            _update_progress_job(
                job_id,
                status="processing",
                percentage=95.0,
                speed=None,
                eta=None,
                message="اكتمل تجهيز الملف، جارٍ إرساله إلى جهازك...",
            )

    return download_hook, postprocessor_hook


def valid_url(url: str) -> bool:
    """Accept public HTTP(S) URLs and reject local/private network targets.

    This is a first SSRF defense layer. It does not replace outbound firewall
    rules because extractors may follow redirects or contact other hosts.
    """
    if not isinstance(url, str):
        return False

    url = url.strip()
    if not url or len(url) > 8192:
        return False

    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False

        port = parsed.port  # Access validates malformed/out-of-range ports.
        hostname = parsed.hostname.rstrip(".")
        if not hostname:
            return False

        addresses = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        if not addresses:
            return False

        for result in addresses:
            raw_ip = result[4][0]
            ip = ipaddress.ip_address(raw_ip.split("%", 1)[0])
            if not ip.is_global:
                return False
        return True
    except (ValueError, OSError, socket.gaierror):
        return False
    except Exception:
        app.logger.exception("Unexpected error while validating a URL")
        return False


def _get_json_url():
    """Read the URL field safely without assuming the JSON body is an object."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return ""
    value = payload.get("url", "")
    return value.strip() if isinstance(value, str) else ""


def _base_yt_dlp_options(output_dir=None):
    """Options shared by metadata extraction and actual downloads."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "file_access_retries": 2,
        "max_filesize": MAX_DOWNLOAD_SIZE,
        "logger": YtDlpLogger(),
    }

    if output_dir is not None:
        options.update(
            {
                "outtmpl": os.path.join(output_dir, "%(title).80s.%(ext)s"),
                "windowsfilenames": True,
            }
        )
    return options


def _find_downloaded_file(folder, kind):
    """Return the completed output file, ignoring partial/temp files."""
    candidates = []
    ignored_suffixes = (".part", ".ytdl", ".temp", ".lock")

    for item in Path(folder).iterdir():
        if not item.is_file() or item.name.lower().endswith(ignored_suffixes):
            continue
        candidates.append(item)

    if not candidates:
        raise FileNotFoundError("yt-dlp completed without producing an output file")

    preferred_extension = ".m4a" if kind == "audio" else ".mp4"
    preferred = [item for item in candidates if item.suffix.lower() == preferred_extension]
    return max(preferred or candidates, key=lambda item: item.stat().st_size)


@app.get("/")
def index():
    index_path = os.path.join(BASE, "index.html")
    if not os.path.isfile(index_path):
        return "ضع ملف index.html في نفس مجلد app.py", 404
    return send_from_directory(BASE, "index.html")


ALLOWED = {"css", "js", "png", "jpg", "jpeg", "svg", "webp", "woff2", "ico"}


@app.get("/<name>")
def files(name):
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension in ALLOWED:
        return send_from_directory(BASE, name)
    abort(404)


@app.post("/api/info")
def info():
    url = _get_json_url()
    if not valid_url(url):
        return jsonify(error="الرابط غير صالح. تأكد من نسخه كاملاً."), 400

    try:
        with yt_dlp.YoutubeDL(_base_yt_dlp_options()) as ydl:
            data = ydl.extract_info(url, download=False)
        if not isinstance(data, dict):
            raise ValueError("The extractor returned no video information")

        heights = set()
        for video_format in data.get("formats") or []:
            raw_height = video_format.get("height")
            try:
                height_value = int(raw_height)
            except (TypeError, ValueError, OverflowError):
                continue
            if height_value > 0:
                heights.add(height_value)

        # Keep every metadata response key unchanged for the existing frontend.
        return jsonify(
            title=data.get("title", ""),
            thumbnail=data.get("thumbnail"),
            duration=data.get("duration"),
            uploader=data.get("uploader"),
            heights=sorted(heights, reverse=True),
        )
    except Exception:
        app.logger.exception("Video information extraction failed")
        return jsonify(
            error="تعذّر قراءة الفيديو. قد يكون خاصاً أو المنصة غير مدعومة."
        ), 422


@app.get("/api/progress/<job_id>")
def download_progress(job_id):
    """Read-only progress endpoint polled by script.js during a download."""
    if not _valid_job_id(job_id):
        abort(404)
    data = _get_progress_job(job_id)
    if data is None:
        return jsonify(error="جلسة التنزيل غير موجودة أو انتهت صلاحيتها."), 404
    data.pop("updated_at", None)
    response = jsonify(data)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/download")
def download():
    # Existing query parameters are unchanged. job_id is an optional addition
    # used only by the new live progress indicator.
    url = request.args.get("url", "", type=str).strip()
    kind = request.args.get("kind", "video", type=str).strip().lower()
    raw_height = request.args.get("height", type=str)
    job_id = request.args.get("job_id", "", type=str).strip()

    if not valid_url(url):
        return jsonify(error="رابط غير صالح"), 400
    if job_id and not _valid_job_id(job_id):
        return jsonify(error="معرّف جلسة التنزيل غير صالح."), 400

    if kind != "audio":
        kind = "video"

    height = None
    if raw_height:
        try:
            parsed_height = int(raw_height)
            if parsed_height > 0:
                height = parsed_height
        except (TypeError, ValueError, OverflowError):
            height = None

    if job_id:
        _register_progress_job(job_id)

    temp_dir = tempfile.mkdtemp(prefix="video-download-")
    options = _base_yt_dlp_options(temp_dir)

    if kind == "audio":
        options["format"] = "bestaudio[ext=m4a]/bestaudio"
    else:
        quality_filter = f"[height<={height}]" if height else ""
        options["format"] = (
            f"bestvideo{quality_filter}[ext=mp4]+bestaudio[ext=m4a]/"
            f"best{quality_filter}[ext=mp4]/best{quality_filter}"
        )
        options["merge_output_format"] = "mp4"

    progress_hook, postprocessor_hook = _make_progress_hooks(job_id, kind)
    if progress_hook:
        options["progress_hooks"] = [progress_hook]
        options["postprocessor_hooks"] = [postprocessor_hook]

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        output_path = _find_downloaded_file(temp_dir, kind)
        output_size = output_path.stat().st_size
        if output_size > MAX_DOWNLOAD_SIZE:
            raise ValueError("MAX_FILE_SIZE")

        if job_id:
            _update_progress_job(
                job_id,
                status="ready",
                percentage=95.0,
                downloaded_bytes=output_size,
                total_bytes=output_size,
                speed=None,
                eta=None,
                message="اكتمل تجهيز الملف، جارٍ نقله إلى جهازك...",
            )

        response = send_file(
            output_path,
            as_attachment=True,
            download_name=output_path.name,
        )
        response.call_on_close(
            lambda folder=temp_dir: shutil.rmtree(folder, ignore_errors=True)
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    except ValueError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if str(exc) == "MAX_FILE_SIZE":
            message = "حجم الملف أكبر من الحد المسموح (300 MB)"
            if job_id:
                _update_progress_job(job_id, status="failed", message=message)
            return jsonify(error=message), 413
        app.logger.exception("Invalid download output")
    except Exception:
        app.logger.exception("Video download failed")

    shutil.rmtree(temp_dir, ignore_errors=True)
    if job_id:
        _update_progress_job(
            job_id,
            status="failed",
            message="فشل التنزيل. تحقق من الرابط وحاول مرة أخرى.",
            speed=None,
            eta=None,
        )
    return jsonify(error="فشل التنزيل"), 500


if __name__ == "__main__":
    # Local-only binding is intentional for personal use on this computer.
    # For public/multi-worker deployment, use WSGI + a shared progress store.
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
