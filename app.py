#!/usr/bin/env python3
"""
USDB Song Importer - Web UI Backend
Flask backend with SSE for live status, cookie management, link queue.
"""

import json
import math
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

from flask import Flask, request, jsonify, Response, send_file

try:
    import requests as req_lib
    from bs4 import BeautifulSoup
except ImportError:
    print("pip install requests beautifulsoup4 flask")
    raise

app = Flask(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

USDB_BASE = "https://usdb.animux.de/"
USDB_DETAIL = "index.php?link=detail&id={}"

CONFIG_DEFAULTS = {
    "local_path": "", "delay": 0.5, "output_path": "",
    "download_video": True, "collision_policy": "skip",
}
COOKIE_SERVICE = "ultrastar-importer"
COOKIE_ACCOUNT = "usdb-session"


def get_cookie():
    """Read the USDB session from Windows Credential Manager via keyring."""
    try:
        import keyring
        return keyring.get_password(COOKIE_SERVICE, COOKIE_ACCOUNT) or ""
    except Exception as exc:
        print(f"WARNING: Cookie konnte nicht aus Credential Manager gelesen werden: {exc}")
        return ""


def set_cookie(cookie):
    """Store/remove the USDB session outside the project tree."""
    try:
        import keyring
        if cookie:
            keyring.set_password(COOKIE_SERVICE, COOKIE_ACCOUNT, cookie)
        else:
            try:
                keyring.delete_password(COOKIE_SERVICE, COOKIE_ACCOUNT)
            except keyring.errors.PasswordDeleteError:
                pass
    except Exception as exc:
        raise RuntimeError(f"Credential Manager nicht verfuegbar: {exc}") from exc


def _origin_is_local(origin):
    """Accept browser requests only from this loopback UI (or no Origin in tests)."""
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 5776


@app.before_request
def reject_cross_origin_mutations():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _origin_is_local(request.headers.get("Origin")):
        return jsonify({"ok": False, "error": "Cross-Origin-Anfrage abgelehnt"}), 403


# === Atomic JSON helpers (Task 5) ===

def _atomic_write_json(path, data):
    """Write JSON to a temp file in the same directory, then atomically replace.

    Each call uses a UUID-suffixed temp file so concurrent writers cannot
    collide on the same `.tmp` name (Task 5).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        Path(tmp).replace(path)
    finally:
        # Clean up the temp file if replace failed or never happened.
        try:
            if Path(tmp).exists():
                Path(tmp).unlink()
        except Exception:
            pass


def _load_json_safe(path, fallback, on_corrupt=None):
    """Load JSON, falling back gracefully on corrupt files.

    `fallback` is a callable returning the default value.
    `on_corrupt(path, exc)` is called when a JSONDecodeError is caught, so the
    caller can back up / log. The file is backed up to <path>.corrupt-<ts>.
    """
    path = Path(path)
    if not path.exists():
        return fallback()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        # Back up the corrupt file and fall back to defaults with a warning.
        backup = path.with_name(path.name + f".corrupt-{int(time.time())}")
        try:
            shutil.copy2(path, backup)
            print(f"WARNING: {path.name} was corrupt; backed up to {backup.name}")
        except Exception:
            print(f"WARNING: {path.name} was corrupt and could not be backed up")
        if on_corrupt:
            try:
                on_corrupt(path, exc)
            except Exception:
                pass
        return fallback()
    except Exception:
        return fallback()


def load_config():
    cfg = _load_json_safe(CONFIG_FILE, lambda: dict(CONFIG_DEFAULTS))
    # One-time migration from the legacy gitignored config.json storage.
    legacy_cookie = cfg.pop("cookie", "")
    if isinstance(legacy_cookie, str) and legacy_cookie:
        set_cookie(legacy_cookie)
        _atomic_write_json(CONFIG_FILE, cfg)
    # Ensure all keys exist (for configs saved before new keys were added)
    for k, v in CONFIG_DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v
    return cfg


def save_config():
    _atomic_write_json(CONFIG_FILE, state.config)


class State:
    def __init__(self):
        self.config = load_config()
        self.queue = []
        self.queue_lock = threading.Lock()
        # Worker lifecycle lock: makes start/stop atomic (Task 3)
        self.worker_lock = threading.Lock()
        # Monotonic counter so queue IDs are never reused after deletion
        self.queue_next_id = 1
        self.sse_clients = []
        self.sse_lock = threading.Lock()
        self.worker_running = False
        self.worker_thread = None
        self.worker_generation = 0
        self.worker_stop_event = threading.Event()
        self.login_transfer_token = None
        self.local_songs = []
        self.usdb_cache = {}


state = State()


# === SSE ===

def sse_broadcast(msg_type, data):
    msg = json.dumps({"type": msg_type, "data": data}, ensure_ascii=False)
    dead = []
    with state.sse_lock:
        for i, q in enumerate(state.sse_clients):
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(i)
        for i in reversed(dead):
            state.sse_clients.pop(i)


@app.route("/stream")
def stream():
    def event_stream():
        q = queue.Queue(maxsize=256)
        with state.sse_lock:
            state.sse_clients.append(q)
        q.put_nowait(json.dumps({"type": "queue", "data": get_queue_snapshot()},
                                ensure_ascii=False))
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with state.sse_lock:
                if q in state.sse_clients:
                    state.sse_clients.remove(q)
    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def get_queue_snapshot():
    with state.queue_lock:
        return [dict(item) for item in state.queue]


# === USDB Parsing ===

def extract_usdb_id(url):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "id" in qs:
        return qs["id"][0]
    m = re.search(r"id=(\d+)", url)
    return m.group(1) if m else None


def fetch_detail(song_id, cookie_string, session=None):
    if session is None:
        session = req_lib.Session()
    url = USDB_BASE + USDB_DETAIL.format(song_id)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_string,
    }
    try:
        resp = session.get(url, headers=headers, timeout=15)
    except Exception as e:
        return None, str(e)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    soup = BeautifulSoup(resp.text, "html.parser")
    if "Datensatz nicht gefunden" in soup.get_text():
        return None, "Nicht eingeloggt oder ID existiert nicht"
    return parse_detail(soup, song_id), None


def fetch_txt(song_id, cookie_string, session=None):
    """Download the UltraStar .txt content from USDB via POST with wd=1."""
    if session is None:
        session = req_lib.Session()
    url = USDB_BASE + f"index.php?link=gettxt&id={song_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_string,
    }
    try:
        resp = session.post(url, headers=headers, data={"wd": "1"}, timeout=15)
    except Exception as e:
        return None, str(e)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text()
    if "not logged in" in page_text.lower() or "nicht eingeloggt" in page_text.lower():
        return None, "Nicht eingeloggt! Bitte neu einloggen."
    for ta in soup.find_all("textarea"):
        content = ta.get_text()
        if "#ARTIST" in content or "#TITLE" in content:
            import html as html_mod
            content = html_mod.unescape(content)
            content = content.replace("\r\n", "\n").replace("\r", "\n")
            return content, None
    return None, "Kein .txt verfuegbar (evtl. noch nicht hochgeladen oder nicht eingeloggt)"


def fetch_cover(song_id, cookie_string, session=None):
    """Download cover image bytes from USDB."""
    if session is None:
        session = req_lib.Session()
    url = USDB_BASE + f"data/cover/{song_id}.jpg"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_string,
    }
    try:
        resp = session.get(url, headers=headers, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 100:
            return resp.content, None
        return None, f"HTTP {resp.status_code} oder leer"
    except Exception as e:
        return None, str(e)


# === yt-dlp downloads (Task 4) ===

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


def download_youtube_audio(youtube_url, target_path, item=None):
    """Download audio from YouTube as MP3 using yt-dlp.
    target_path should be the full path WITHOUT extension.
    Returns (filepath, error)."""
    try:
        import yt_dlp
    except ImportError:
        return None, "yt-dlp nicht installiert (pip install yt-dlp)"

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": target_path + ".%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    if item is not None:
        ydl_opts["progress_hooks"] = [_make_progress_hook(item, "Audio")]
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])
        # Find the actual mp3 file
        p = Path(target_path + ".mp3")
        if p.exists():
            return str(p), None
        # Search for any audio file with the base name
        parent = Path(target_path).parent
        base = Path(target_path).name
        for f in parent.glob(base + ".*"):
            if f.suffix in (".mp3", ".m4a", ".webm", ".opus"):
                return str(f), None
        return None, "Datei nach Download nicht gefunden"
    except Exception as e:
        return None, str(e)


def download_youtube_video(youtube_url, target_path, item=None):
    """Download video from YouTube as MP4 using yt-dlp.
    target_path should be the full path WITHOUT extension.
    Returns (filepath, error)."""
    try:
        import yt_dlp
    except ImportError:
        return None, "yt-dlp nicht installiert"

    ydl_opts = {
        "format": "best[ext=mp4][height<=?1080]/best[height<=?1080]/best",
        "outtmpl": target_path + ".%(ext)s",
        "noplaylist": True,
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
        p = Path(target_path + ".mp4")
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


def sanitize_filename(name):
    """Remove characters that are invalid in Windows filenames."""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "")
    return name.strip().rstrip(".")


# === Output path validation (Task 4) ===

def validate_output_path(output_base):
    """Normalize, ensure the directory exists, and verify it is writable.

    Returns (normalized_path_str, error). error is None on success.
    """
    if not output_base:
        output_base = str(BASE_DIR / "output")
    try:
        base = Path(output_base).resolve()
    except (OSError, ValueError) as e:
        return output_base, f"Ungueltiger Pfad: {e}"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return str(base), f"Kann Pfad nicht erstellen: {e}"
    # Probe writability with a temp file.
    probe = base / f".writeprobe-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as e:
        return str(base), f"Pfad nicht schreibbar: {e}"
    return str(base), None


# === Song folder build (Task 4: atomic transaction) ===

def _patch_txt_content(txt_content, base_name, video_filename=None):
    """Patch #MP3/#VIDEO lines to reference local files.

    - #MP3 always references <base_name>.mp3.
    - If a local video file exists, #VIDEO is set (or inserted) to reference it,
      replacing any stale USDB youtube-id reference. #VIDEOGAP:0 is added when
      a video is present and none exists yet.
    - If no local video is expected, an existing #VIDEO line is left untouched.
    """
    lines = txt_content.split("\n")
    patched = []
    saw_video = False
    for line in lines:
        if line.startswith("#MP3:"):
            patched.append(f"#MP3:{base_name}.mp3")
        elif line.startswith("#VIDEO:"):
            saw_video = True
            if video_filename:
                patched.append(f"#VIDEO:{video_filename}")
            else:
                patched.append(line)
        elif line.startswith("#VIDEOGAP:"):
            # Drop existing VIDEOGAP; re-added below if a video is present.
            continue
        else:
            patched.append(line)
    # If a video was downloaded but no #VIDEO line existed, insert one.
    if video_filename and not saw_video:
        patched.append(f"#VIDEO:{video_filename}")
    # Ensure a #VIDEOGAP line exists when a local video is referenced.
    if video_filename and not any(l.startswith("#VIDEOGAP:") for l in patched):
        patched.append("#VIDEOGAP:0")
    return "\n".join(patched)


def build_song_folder(data, txt_content, cover_bytes, output_base,
                      audio_path=None, video_path=None):
    """Create a complete UltraStar song folder structure.

    The song is built inside a `.partial-<uuid>` directory and atomically
    renamed to its final folder on success. On any failure the partial
    directory is removed before re-raising.

    Returns the final folder path (str).
    """
    artist = sanitize_filename(data.get("artist", "Unknown"))
    title = sanitize_filename(data.get("title", "Unknown"))
    song_id = data.get("usdb_id", "")
    folder_name = f"{artist} - {title}"
    base_name = f"{artist} - {title}"

    final_folder = Path(output_base) / folder_name
    # Stage in a sibling partial directory.
    partial_folder = Path(output_base) / f"{folder_name}.partial-{uuid.uuid4().hex[:8]}"
    partial_folder.mkdir(parents=True, exist_ok=True)

    try:
        # Move already-downloaded audio/video into the partial folder.
        video_filename = None
        if video_path:
            src = Path(video_path)
            if src.exists():
                dst = partial_folder / src.name
                shutil.move(str(src), str(dst))
                video_filename = dst.name
        if audio_path:
            src = Path(audio_path)
            if src.exists():
                dst = partial_folder / src.name
                # Avoid collision with video (different extensions normally)
                shutil.move(str(src), str(dst))

        # Patch the txt to reference local media.
        txt_content = _patch_txt_content(txt_content, base_name, video_filename)

        # Write .txt
        txt_path = partial_folder / f"{base_name}.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(txt_content)

        # Write cover
        if cover_bytes:
            cover_path = partial_folder / f"{base_name} [CO].jpg"
            with open(cover_path, "wb") as f:
                f.write(cover_bytes)
    except Exception:
        # Clean up partial folder on any failure.
        shutil.rmtree(partial_folder, ignore_errors=True)
        raise

    # Safe collision policy: an existing song is never overwritten or mixed
    # with new assets. The default is deliberately "skip" until a versioning
    # policy is exposed explicitly in the product UI.
    if final_folder.exists():
        shutil.rmtree(partial_folder, ignore_errors=True)
        raise FileExistsError(f"Song existiert bereits und wurde uebersprungen: {final_folder}")
    try:
        partial_folder.rename(final_folder)
    except OSError:
        shutil.rmtree(partial_folder, ignore_errors=True)
        raise
    return str(final_folder)


def count_cover_files(folder):
    """Count files ending with [CO].jpg in folder."""
    return len([f for f in Path(folder).iterdir() if f.name.endswith("[CO].jpg")])


def parse_detail(soup, song_id):
    """Extract metadata from a USDB detail page.
    USDB layout: first unnamed table row = [Artist] [Title],
    subsequent rows have German labels (Sprache, Jahr, etc.).
    YouTube is an iframe embed. Cover is data/cover/<id>.jpg."""
    data = {"usdb_id": song_id}

    # --- Artist + Title from page <title> tag ---
    # Format: "USDB - <Artist> - <Title>"
    page_title = ""
    if soup.title:
        page_title = soup.title.get_text(strip=True)
    if page_title.startswith("USDB - "):
        rest = page_title[7:]  # strip "USDB - "
        if " - " in rest:
            parts = rest.rsplit(" - ", 1)
            data["artist"] = parts[0].strip()
            data["title"] = parts[1].strip()
        else:
            data["title"] = rest.strip()

    # German label -> internal field
    label_map = {
        "sprache": "language",
        "jahr": "year", "year": "year",
        "genre": "genre",
        "edition": "edition",
        "goldene noten": "golden_notes",
        "songcheck": "songcheck",
        "bpm": "bpm",
        "aufrufe": "views",
        "hochgeladen von": "creator",
        "song editiert von:": "editor",
    }

    # Find the detail table: it has rows like [Artist]=[Title], [Sprache]=[English]...
    # Skip navigation tables (first table is usually the Options menu)
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        # Skip tables that are just navigation
        first_row_text = ""
        if rows:
            first_cells = rows[0].find_all("td")
            if first_cells:
                first_row_text = first_cells[0].get_text(strip=True).lower()

        if "options" in first_row_text and "start" in first_row_text:
            continue

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            label_raw = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)
            if not label_raw:
                continue

            label_lower = label_raw.rstrip(":").lower()

            if label_lower in label_map:
                data[label_map[label_lower]] = value
            elif label_lower in ("artist", "interpret") and "artist" not in data:
                data["artist"] = value
            elif label_lower in ("title", "titel") and "title" not in data:
                data["title"] = value
            # No more guessing from unlabeled rows - title tag is authoritative

    # YouTube: check iframe embed URLs
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "")
        if "youtube.com/embed/" in src:
            # Convert embed URL to watch URL
            video_id = src.split("/embed/")[-1].split("?")[0].split("&")[0]
            data["youtube_url"] = f"https://www.youtube.com/watch?v={video_id}"
            break
        elif "youtube.com/watch" in src or "youtu.be/" in src:
            data["youtube_url"] = src
            break

    # Fallback: check links
    if "youtube_url" not in data:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "youtube.com/watch" in href or "youtu.be/" in href:
                data["youtube_url"] = href
                break

    # Cover: data/cover/<id>.jpg
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "data/cover/" in src or ("cover" in src.lower() and src.endswith((".jpg", ".png", ".jpeg"))):
            data["cover_url"] = urljoin(USDB_BASE, src)
            break

    if "artist" not in data and "title" not in data:
        return None
    return data


# === Local Scanner (Task 5: utf-8-sig + unicodedata normalization) ===

def parse_ultrastar_txt(filepath):
    data = {"file": str(filepath)}
    try:
        # utf-8-sig handles a leading BOM transparently.
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                for header in ("#ARTIST:", "#TITLE:", "#LANGUAGE:", "#EDITION:",
                               "#GENRE:", "#YEAR:", "#MP3:", "#VIDEO:",
                               "#COVER:", "#BACKGROUND:"):
                    if line.upper().startswith(header):
                        key = header[1:-1].lower()
                        data[key] = line[len(header):].strip()
                        break
                if line and not line.startswith("#"):
                    break
    except Exception as e:
        data["error"] = str(e)
    return data


def scan_local(path):
    results = []
    base = Path(path)
    if not base.exists():
        return []
    for txt_file in sorted(base.rglob("*.txt")):
        parsed = parse_ultrastar_txt(txt_file)
        if "artist" in parsed or "title" in parsed:
            folder = txt_file.parent
            parsed["assets"] = {
                "has_mp3": any(folder.glob("*.mp3")),
                "has_video": (any(folder.glob("*.mp4"))
                              or any(folder.glob("*.avi"))
                              or any(folder.glob("*.webm"))),
                "has_cover": bool(any(folder.glob("*cover*"))
                                  or any(folder.glob("*[Cc][Oo]*"))),
                "has_background": bool(any(folder.glob("*bg*"))
                                       or any(folder.glob("*background*"))),
            }
            parsed["folder"] = str(folder)
            results.append(parsed)
    return results


def normalize(s):
    """Accent-insensitive normalization for matching.

    Uses NFKD decomposition and strips combining marks so that e.g.
    'café' matches 'cafe'. Non-ASCII Latin letters decompose to base+accent;
    the accent is stripped while the base letter survives. Non-Latin
    characters are preserved (not deleted).
    """
    s = s or ""
    # NFKD splits accented characters into base + combining mark.
    decomposed = unicodedata.normalize("NFKD", s)
    # Drop combining marks (category Mn), then lowercase.
    out = []
    for ch in decomposed:
        cat = unicodedata.category(ch)
        if cat == "Mn":
            continue
        out.append(ch)
    text = "".join(out).lower()
    # Strip non-alphanumeric for a stable comparison key, keeping unicode alnum.
    return re.sub(r"[^\w]", "", text, flags=re.UNICODE)


# === Match (Task 5: dict[key, list[song]]) ===

def build_match_report(usdb_songs, local_songs):
    report = {
        "matched": [], "usdb_only": [], "local_only": [],
        "missing_youtube": [], "missing_video": [],
        "missing_cover": [], "missing_mp3": [],
    }
    # Index local songs as key -> list of songs to handle duplicates.
    local_index = {}
    for ls in local_songs:
        key = normalize(ls.get("artist", "")) + "|" + normalize(ls.get("title", ""))
        local_index.setdefault(key, []).append(ls)

    usdb_keys = set()
    for us in usdb_songs:
        key = normalize(us.get("artist", "")) + "|" + normalize(us.get("title", ""))
        usdb_keys.add(key)
        matches = local_index.get(key, [])
        if matches:
            for local_match in matches:
                assets = local_match.get("assets", {})
                entry = {
                    "usdb_id": us.get("usdb_id"),
                    "artist": us.get("artist", ""),
                    "title": us.get("title", ""),
                    "youtube_url": us.get("youtube_url", ""),
                    "local_folder": local_match.get("folder", ""),
                    "local_has_mp3": assets.get("has_mp3", False),
                    "local_has_video": assets.get("has_video", False),
                    "local_has_cover": assets.get("has_cover", False),
                }
                report["matched"].append(entry)
                if not us.get("youtube_url"):
                    report["missing_youtube"].append(entry)
                if not assets.get("has_video", False):
                    report["missing_video"].append(entry)
                if not assets.get("has_cover", False):
                    report["missing_cover"].append(entry)
                if not assets.get("has_mp3", False):
                    report["missing_mp3"].append(entry)
        else:
            report["usdb_only"].append(us)

    for ls in local_songs:
        key = normalize(ls.get("artist", "")) + "|" + normalize(ls.get("title", ""))
        if key not in usdb_keys:
            report["local_only"].append(ls)
    return report


# === Worker ===

def save_usdb_cache():
    _atomic_write_json(DATA_DIR / "usdb_cache.json", state.usdb_cache)


def load_usdb_cache():
    state.usdb_cache = _load_json_safe(
        DATA_DIR / "usdb_cache.json", lambda: {}
    )


def process_single(item, cookie, delay, stop_event=None):
    """Process a single queue item. Returns True on success."""
    if stop_event is not None and stop_event.is_set():
        item["status"] = "pending"
        item["progress"] = "Angehalten"
        return False
    song_id = extract_usdb_id(item["url"])
    if not song_id:
        item["status"] = "error"
        item["progress"] = "Ungueltige URL (keine USDB-ID erkennbar)"
        return False

    # Step 1: Metadaten laden
    item["progress"] = f"Lade Metadaten (ID:{song_id}) ..."
    sse_broadcast("status", {"id": item["id"], "status": "processing",
                             "progress": item["progress"]})

    data, err = fetch_detail(song_id, cookie)
    time.sleep(delay)

    if not data:
        err_lower = (err or "").lower()
        if "nicht eingeloggt" in err_lower or "datensatz nicht gefunden" in err_lower:
            item["status"] = "error"
            item["progress"] = "Nicht eingeloggt! Bitte neu einloggen (Settings)."
        elif "existiert nicht" in err_lower:
            item["status"] = "error"
            item["progress"] = f"Song-ID {song_id} existiert nicht bei USDB"
        elif "http" in err_lower or "timeout" in err_lower or "connection" in err_lower:
            item["status"] = "error"
            item["progress"] = f"Netzwerkfehler: {err}"
        else:
            item["status"] = "error"
            item["progress"] = err or "Unbekannter Fehler"
        sse_broadcast("status", {"id": item["id"], "status": "error",
                                 "progress": item["progress"]})
        return False

    item["artist"] = data.get("artist", "?")
    item["title"] = data.get("title", "?")
    item["youtube_url"] = data.get("youtube_url", "")
    item["cover_url"] = data.get("cover_url", "")

    # Step 2: .txt herunterladen
    item["progress"] = "Lade .txt (Lyrics+Timing) ..."
    sse_broadcast("status", {"id": item["id"], "status": "processing",
                             "progress": item["progress"]})
    txt_content, txt_err = fetch_txt(song_id, cookie)
    time.sleep(delay)

    if not txt_content:
        item["status"] = "error"
        item["progress"] = f"Fehler beim .txt-Download: {txt_err}"
        sse_broadcast("status", {"id": item["id"], "status": "error",
                                 "progress": item["progress"]})
        # The queue lifecycle owns worker state; returning here prevents a
        # stale worker from clearing a newer worker's state.
        return False

    # Step 3: Cover herunterladen
    item["progress"] = "Lade Cover ..."
    sse_broadcast("status", {"id": item["id"], "status": "processing",
                             "progress": item["progress"]})
    cover_bytes, cover_err = fetch_cover(song_id, cookie)
    time.sleep(delay)

    # Step 4: Validate output path (Task 4)
    output_base, out_err = validate_output_path(state.config.get("output_path", ""))
    if out_err:
        item["status"] = "error"
        item["progress"] = f"Ungueltiger Output-Pfad: {out_err}"
        sse_broadcast("status", {"id": item["id"], "status": "error",
                                 "progress": item["progress"]})
        return False

    artist_clean = sanitize_filename(data.get("artist", "Unknown"))
    title_clean = sanitize_filename(data.get("title", "Unknown"))
    base_name = f"{artist_clean} - {title_clean}"

    # Use a temporary staging directory for downloads (Task 4 transaction).
    staging_root = Path(output_base) / f".staging-{uuid.uuid4().hex[:8]}"
    staging_root.mkdir(parents=True, exist_ok=True)

    audio_path = None
    video_path = None
    try:
        # Step 5: Audio (MP3) von YouTube herunterladen
        yt_url = data.get("youtube_url", "")
        audio_ok = False
        if yt_url and is_valid_youtube_url(yt_url):
            item["progress"] = "Lade Audio (MP3) von YouTube ..."
            sse_broadcast("status", {"id": item["id"], "status": "processing",
                                     "progress": item["progress"]})
            audio_target = str(staging_root / base_name)
            audio_path, audio_err = download_youtube_audio(yt_url, audio_target, item=item)
            if audio_path:
                audio_ok = True
            else:
                item["progress"] = f"Audio-Download fehlgeschlagen: {audio_err}"
                sse_broadcast("status", {"id": item["id"], "status": "processing",
                                         "progress": item["progress"]})
        else:
            item["progress"] = "Kein YouTube-Link gefunden, ueberspringe Audio"
            sse_broadcast("status", {"id": item["id"], "status": "processing",
                                     "progress": item["progress"]})

        # Step 6: Video (MP4) von YouTube herunterladen
        video_ok = False
        if yt_url and is_valid_youtube_url(yt_url) and state.config.get("download_video", True):
            item["progress"] = "Lade Video (MP4) von YouTube ..."
            sse_broadcast("status", {"id": item["id"], "status": "processing",
                                     "progress": item["progress"]})
            video_target = str(staging_root / base_name)
            video_path, video_err = download_youtube_video(yt_url, video_target, item=item)
            if video_path:
                video_ok = True
            else:
                item["progress"] = f"Video-Download fehlgeschlagen: {video_err}"
                sse_broadcast("status", {"id": item["id"], "status": "processing",
                                         "progress": item["progress"]})

        # Step 6b: Verify mp3/mp4 exist and are non-empty (Task 4)
        if audio_path:
            ap = Path(audio_path)
            if not ap.exists() or ap.stat().st_size == 0:
                item["progress"] = "Audio-Datei fehlt oder ist leer"
                sse_broadcast("status", {"id": item["id"], "status": "processing",
                                         "progress": item["progress"]})
                audio_path = None
        if video_path:
            vp = Path(video_path)
            if not vp.exists() or vp.stat().st_size == 0:
                item["progress"] = "Video-Datei fehlt oder ist leer"
                sse_broadcast("status", {"id": item["id"], "status": "processing",
                                         "progress": item["progress"]})
                video_path = None

        if not audio_path:
            item["status"] = "error"
            item["progress"] = "Audio-Datei fehlt; Song wurde nicht importiert"
            sse_broadcast("status", {"id": item["id"], "status": "error", "progress": item["progress"]})
            return False
        if stop_event is not None and stop_event.is_set():
            item["status"] = "pending"
            item["progress"] = "Angehalten"
            return False

        # Step 7: Song-Ordner fertigstellen (.txt + Cover) -- atomic transaction
        item["progress"] = "Schreibe .txt und Cover ..."
        sse_broadcast("status", {"id": item["id"], "status": "processing",
                                 "progress": item["progress"]})

        try:
            folder_path = build_song_folder(
                data, txt_content, cover_bytes, output_base,
                audio_path=audio_path, video_path=video_path,
            )
        except Exception as e:
            item["status"] = "error"
            item["progress"] = f"Fehler beim Ordner erstellen: {e}"
            sse_broadcast("status", {"id": item["id"], "status": "error",
                                     "progress": item["progress"]})
            return False
    finally:
        # Always clean up the staging directory.
        shutil.rmtree(staging_root, ignore_errors=True)

    # Fertig
    item["usdb_id"] = song_id
    item["genre"] = data.get("genre", "")
    item["year"] = data.get("year", "")
    item["language"] = data.get("language", "")
    item["progress"] = "Fertig"
    item["status"] = "done"
    item["output_folder"] = folder_path

    state.usdb_cache[str(song_id)] = data
    save_usdb_cache()

    sse_broadcast("status", {
        "id": item["id"], "status": "done", "progress": "Fertig",
        "detail": {
            "artist": item["artist"], "title": item["title"],
            "youtube_url": item["youtube_url"],
            "cover_url": item.get("cover_url", ""),
            "usdb_id": song_id,
            "output_folder": folder_path,
        }
    })
    return True


def process_queue(generation, stop_event):
    """Process pending queue items for one lifecycle generation only."""
    try:
        while not stop_event.is_set():
            item = None
            with state.queue_lock:
                for q_item in state.queue:
                    if q_item["status"] == "pending":
                        q_item["status"] = "processing"
                        q_item["progress"] = "Verbinde mit USDB ..."
                        item = q_item
                        break
            if item is None:
                break

            sse_broadcast("status", {"id": item["id"], "status": "processing", "progress": item["progress"]})
            cookie = get_cookie()
            if not cookie or len(cookie) < 10:
                item["status"] = "error"
                item["progress"] = "Kein Cookie! Bitte unter Einstellungen einloggen."
                sse_broadcast("status", {"id": item["id"], "status": "error", "progress": item["progress"]})
                break
            try:
                process_single(item, cookie, state.config.get("delay", 0.5), stop_event)
            except Exception as exc:
                item["status"] = "error"
                item["progress"] = f"Unerwarteter Fehler: {exc}"
                sse_broadcast("status", {"id": item["id"], "status": "error", "progress": item["progress"]})
    finally:
        with state.worker_lock:
            if state.worker_generation == generation:
                state.worker_running = False
                state.worker_thread = None
        sse_broadcast("worker_stopped", {"generation": generation})


# === Input validation helpers (Task 3) ===

USDB_URL_RE = re.compile(
    r"^https?://(?:www\.)?usdb\.animux\.de/", re.IGNORECASE
)


def validate_queue_url(url):
    """Return (clean_url_or_None, error_message)."""
    if url is None:
        return None, "URL fehlt"
    if not isinstance(url, str):
        return None, "URL muss ein String sein"
    url = url.strip()
    if not url:
        return None, "URL ist leer"
    # Accept a bare numeric USDB id.
    if url.isdigit():
        return f"{USDB_BASE}{USDB_DETAIL.format(url)}", None
    if not USDB_URL_RE.match(url):
        return None, "Nur usdb.animux.de-URLs sind erlaubt"
    # Must carry a detail id.
    sid = extract_usdb_id(url)
    if not sid:
        return None, "URL enthaelt keine gueltige USDB-ID"
    return url, None


def validate_delay(value):
    """Return (delay_float, error_message). Accepts numbers; rejects None/NaN/inf."""
    if value is None:
        return None, "Delay fehlt"
    try:
        d = float(value)
    except (TypeError, ValueError):
        return None, "Delay muss eine Zahl sein"
    import math as _math
    if _math.isnan(d) or _math.isinf(d):
        return None, "Delay ist nicht gueltig (NaN/Inf)"
    if d < 0.1 or d > 60.0:
        return None, "Delay muss zwischen 0.1 und 60.0 liegen"
    return d, None


# === API Routes ===

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/queue", methods=["GET"])
def api_queue():
    return jsonify(get_queue_snapshot())


@app.route("/api/queue/add", methods=["POST"])
def api_queue_add():
    data = request.json or {}
    urls = data.get("urls", "")

    # Normalize urls to a list of candidates.
    if isinstance(urls, str):
        url_list = [u.strip() for u in urls.splitlines() if u.strip()]
    elif isinstance(urls, list):
        url_list = urls
    else:
        return jsonify({"ok": False, "error": "'urls' muss ein String oder eine Liste sein"}), 400

    errors = []
    clean_urls = []
    for raw in url_list:
        clean, err = validate_queue_url(raw)
        if err:
            errors.append(f"{raw!r}: {err}")
        else:
            clean_urls.append(clean)

    added = 0
    with state.queue_lock:
        existing_urls = {q["url"] for q in state.queue}
        for url in clean_urls:
            if url in existing_urls:
                continue
            item = {
                "id": state.queue_next_id,
                "url": url,
                "status": "pending",
                "progress": "Warteschlange",
                "artist": "", "title": "",
                "youtube_url": "", "usdb_id": None,
            }
            state.queue_next_id += 1
            state.queue.append(item)
            added += 1
    sse_broadcast("queue", get_queue_snapshot())
    if errors:
        return jsonify({"added": added, "errors": errors}), 400
    return jsonify({"added": added})


@app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear():
    # Task 3: only remove pending items; leave processing/done/error items.
    with state.queue_lock:
        state.queue = [q for q in state.queue if q["status"] != "pending"]
    sse_broadcast("queue", get_queue_snapshot())
    return jsonify({"ok": True})


@app.route("/api/queue/remove/<int:item_id>", methods=["POST"])
def api_queue_remove(item_id):
    with state.queue_lock:
        item = next((q for q in state.queue if q["id"] == item_id), None)
        if item is None:
            return jsonify({"ok": False, "error": "Queue-Item nicht gefunden"}), 404
        if item["status"] != "pending":
            return jsonify({"ok": False, "error": "Nur wartende Items duerfen entfernt werden"}), 409
        state.queue.remove(item)
    sse_broadcast("queue", get_queue_snapshot())
    return jsonify({"ok": True})


@app.route("/api/queue/retry/<int:item_id>", methods=["POST"])
def api_queue_retry(item_id):
    with state.queue_lock:
        item = next((q for q in state.queue if q["id"] == item_id), None)
        if item is None:
            return jsonify({"ok": False, "error": "Queue-Item nicht gefunden"}), 404
        if item["status"] != "error":
            return jsonify({"ok": False, "error": "Nur fehlgeschlagene Items duerfen erneut gestartet werden"}), 409
        item["status"] = "pending"
        item["progress"] = "Warteschlange"
    sse_broadcast("queue", get_queue_snapshot())
    return jsonify({"ok": True})


@app.route("/api/worker/start", methods=["POST"])
def api_worker_start():
    with state.worker_lock:
        if state.worker_thread is not None and state.worker_thread.is_alive():
            return jsonify({"ok": False, "error": "Worker laeuft oder wird noch angehalten"}), 409
        state.worker_generation += 1
        generation = state.worker_generation
        state.worker_stop_event = threading.Event()
        state.worker_running = True
        state.worker_thread = threading.Thread(target=process_queue, args=(generation, state.worker_stop_event), daemon=True)
        state.worker_thread.start()
    return jsonify({"ok": True})


@app.route("/api/worker/stop", methods=["POST"])
def api_worker_stop():
    with state.worker_lock:
        if not state.worker_running:
            return jsonify({"ok": True, "message": "Worker laeuft nicht"})
        state.worker_stop_event.set()
    return jsonify({"ok": True, "message": "Nach dem aktuellen Arbeitsschritt wird angehalten"})


@app.route("/api/worker/status", methods=["GET"])
def api_worker_status():
    with state.worker_lock:
        running = state.worker_running
        stopping = running and state.worker_stop_event.is_set()
    return jsonify({"running": running, "stopping": stopping})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify({
        "has_cookie": bool(get_cookie()),
        "local_path": state.config.get("local_path", ""),
        "delay": state.config.get("delay", 0.5),
        "output_path": state.config.get("output_path", ""),
    })


@app.route("/api/settings", methods=["PUT"])
def api_settings_put():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON-Objekt erwartet"}), 400
    for key in ("local_path", "output_path"):
        if key in data:
            if not isinstance(data[key], str):
                return jsonify({"ok": False, "error": f"{key} muss ein String sein"}), 400
            state.config[key] = data[key].strip()
    if "cookie" in data:
        if not isinstance(data["cookie"], str):
            return jsonify({"ok": False, "error": "cookie muss ein String sein"}), 400
        set_cookie(data["cookie"].strip())
    if "delay" in data:
        d, err = validate_delay(data["delay"])
        if err:
            return jsonify({"ok": False, "error": err}), 400
        state.config["delay"] = d
    save_config()
    return jsonify({"ok": True})


@app.route("/api/cookie/from-browser", methods=["POST"])
def api_cookie_from_browser():
    token = request.headers.get("X-Transfer-Token", "")
    with state.worker_lock:
        expected = state.login_transfer_token
        if not expected or not secrets.compare_digest(token, expected):
            return jsonify({"ok": False, "error": "Ungueltiger Login-Transfer"}), 403
        state.login_transfer_token = None
    cookie = (request.get_json(silent=True) or {}).get("cookie", "")
    if not isinstance(cookie, str) or not cookie.strip():
        return jsonify({"ok": False, "error": "Kein Cookie erhalten"}), 400
    set_cookie(cookie.strip())
    sse_broadcast("cookie_set", {"ok": True})
    return jsonify({"ok": True})


@app.route("/api/cookie/forget", methods=["POST"])
def api_cookie_forget():
    set_cookie("")
    sse_broadcast("cookie_set", {"ok": False})
    return jsonify({"ok": True})


@app.route("/api/login-window", methods=["POST"])
def api_login_window():
    """Launch a login window with a one-time, server-issued transfer token."""
    login_script = str(BASE_DIR / "login_window.py")
    with state.worker_lock:
        state.login_transfer_token = secrets.token_urlsafe(32)
        token = state.login_transfer_token
    try:
        subprocess.Popen([sys.executable, login_script, "--transfer-token", token], cwd=str(BASE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True})
    except Exception as exc:
        with state.worker_lock:
            state.login_transfer_token = None
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/cookie/test", methods=["POST"])
def api_cookie_test():
    supplied = (request.get_json(silent=True) or {}).get("cookie", "")
    cookie = supplied if isinstance(supplied, str) and supplied else get_cookie()

    # Test: can we actually download a .txt? (stricter than detail page)
    txt_result, txt_err = fetch_txt("3045", cookie)
    if txt_result:
        # Also get detail for song name
        detail, _ = fetch_detail("3045", cookie)
        song_name = "?"
        if detail:
            song_name = detail.get("artist", "?") + " - " + detail.get("title", "?")
        return jsonify({"ok": True, "song": song_name,
                        "message": "Cookie gueltig - .txt-Download funktioniert!"})
    else:
        return jsonify({"ok": False,
                        "error": txt_err or "Cookie ungueltig oder abgelaufen"})


@app.route("/api/local/scan", methods=["POST"])
def api_local_scan():
    path = (request.json or {}).get("path") or state.config.get("local_path", "")
    if not path or not Path(path).exists():
        return jsonify({"ok": False, "error": f"Pfad nicht gefunden: {path}"})
    songs = scan_local(path)
    state.local_songs = songs
    # Task 5: atomic write
    _atomic_write_json(DATA_DIR / "local_songs.json", songs)
    return jsonify({"ok": True, "count": len(songs), "songs": songs})


@app.route("/api/local/list", methods=["GET"])
def api_local_list():
    return jsonify({"songs": state.local_songs})


@app.route("/api/match", methods=["GET"])
def api_match():
    report = build_match_report(list(state.usdb_cache.values()), state.local_songs)
    return jsonify(report)


@app.route("/api/match/download", methods=["GET"])
def api_match_download():
    report = build_match_report(list(state.usdb_cache.values()), state.local_songs)
    tmp = DATA_DIR / "match_report.json"
    # Task 5: atomic write
    _atomic_write_json(tmp, report)
    return send_file(tmp, as_attachment=True, download_name="match_report.json")


if __name__ == "__main__":
    load_usdb_cache()
    print("USDB Importer Web UI auf http://127.0.0.1:5776")
    app.run(host="127.0.0.1", port=5776, debug=False, threaded=True)
