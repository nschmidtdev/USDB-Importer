"""YouTube audio/video downloading via yt-dlp.

Provides URL validation, a progress hook factory that broadcasts SSE updates,
and the two public download functions used by the worker.
"""

from pathlib import Path
from urllib.parse import urlparse

from state import sse_broadcast


# Allowed YouTube hosts for URL validation (Task 6).
_YOUTUBE_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
})


def is_valid_youtube_url(url):
    """Return True only for an https URL whose host is an allowed YouTube host.

    Used to reject non-YouTube or plaintext-HTTP URLs before invoking yt-dlp
    (Task 6).
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in _YOUTUBE_HOSTS


def _make_progress_hook(item, media_label):
    """Build a yt-dlp progress hook that broadcasts SSE progress updates."""
    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            pct = None
            if total:
                try:
                    pct = round(downloaded / total * 100, 1)
                except Exception:
                    pct = None
            speed = d.get("speed")
            progress_msg = f"{media_label}: "
            if pct is not None:
                progress_msg += f"{pct}%"
            else:
                progress_msg += "Lade ..."
            if speed:
                try:
                    progress_msg += f" ({round(speed / 1024)} KB/s)"
                except Exception:
                    pass
            item["progress"] = progress_msg
            sse_broadcast("status", {
                "id": item["id"], "status": "processing",
                "progress": progress_msg,
            })
        elif d.get("status") == "finished":
            item["progress"] = f"{media_label}: Nachbearbeitung ..."
            sse_broadcast("status", {
                "id": item["id"], "status": "processing",
                "progress": item["progress"],
            })
        elif d.get("status") == "error":
            item["progress"] = f"{media_label}: Fehler"
            sse_broadcast("status", {
                "id": item["id"], "status": "processing",
                "progress": item["progress"],
            })
    return hook


# === Format maps ===

# Audio codec → (yt-dlp preferredcodec, accepted file extensions)
_AUDIO_FORMATS = {
    "mp3":   ("mp3",  (".mp3",)),
    "m4a":   ("m4a",  (".m4a",)),
    "opus":  ("opus", (".opus",)),
    "vorbis":("vorbis", (".ogg",)),
}

# Video codec → (format selector fragment, accepted extensions)
_VIDEO_FORMATS = {
    "mp4":  ("best[ext=mp4]", (".mp4",)),
    "webm": ("best[ext=webm]", (".webm",)),
    "mkv":  ("best", (".mkv", ".mp4", ".webm")),
}


def download_youtube_audio(youtube_url, target_path, item=None,
                           audio_format="mp3", bitrate=192):
    """Download audio from YouTube using yt-dlp.

    ``audio_format``: mp3 | m4a | opus | vorbis
    ``bitrate``: kbps (128/192/256/320)
    ``target_path``: full path WITHOUT extension.
    Returns (filepath, error).
    """
    try:
        import yt_dlp
    except ImportError:
        return None, "yt-dlp nicht installiert (pip install yt-dlp)"

    codec, extensions = _AUDIO_FORMATS.get(audio_format, _AUDIO_FORMATS["mp3"])

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": target_path + ".%(ext)s",
        "noplaylist": True,
        "allowed_extractors": ["youtube.*"],
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
            "preferredquality": str(bitrate),
        }],
    }
    if item is not None:
        ydl_opts["progress_hooks"] = [_make_progress_hook(item, "Audio")]
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])
        # Find the output file by expected extension first
        for ext in extensions:
            p = Path(target_path + ext)
            if p.exists():
                return str(p), None
        # Fallback: search for any audio file with the base name
        parent = Path(target_path).parent
        base = Path(target_path).name
        for f in parent.glob(base + ".*"):
            if f.suffix in (".mp3", ".m4a", ".webm", ".opus", ".ogg"):
                return str(f), None
        return None, "Datei nach Download nicht gefunden"
    except Exception as e:
        return None, str(e)


def download_youtube_video(youtube_url, target_path, item=None,
                           video_format="mp4", max_height=1080):
    """Download video from YouTube using yt-dlp.

    ``video_format``: mp4 | webm | mkv
    ``max_height``: maximum vertical resolution (480/720/1080)
    ``target_path``: full path WITHOUT extension.
    Returns (filepath, error).
    """
    try:
        import yt_dlp
    except ImportError:
        return None, "yt-dlp nicht installiert"

    fmt_selector, extensions = _VIDEO_FORMATS.get(video_format, _VIDEO_FORMATS["mp4"])
    # Build format selector with height cap
    fmt = f"{fmt_selector}[height<={max_height}]/best[height<={max_height}]/best"

    ydl_opts = {
        "format": fmt,
        "outtmpl": target_path + ".%(ext)s",
        "noplaylist": True,
        "allowed_extractors": ["youtube.*"],
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
    }
    if item is not None:
        ydl_opts["progress_hooks"] = [_make_progress_hook(item, "Video")]
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])
        for ext in extensions:
            p = Path(target_path + ext)
            if p.exists():
                return str(p), None
        parent = Path(target_path).parent
        base = Path(target_path).name
        for f in parent.glob(base + ".*"):
            if f.suffix in (".mp4", ".webm", ".mkv"):
                return str(f), None
        return None, "Datei nach Download nicht gefunden"
    except Exception as e:
        return None, str(e)
