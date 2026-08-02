"""Queue processing worker and input validators.

process_single() handles one queue item end-to-end (metadata, .txt, cover,
audio, video, folder build). process_queue() runs pending items for one
generation until stopped or empty.
"""

import time
import uuid
from pathlib import Path

from config import get_cookie, has_login_credentials, auto_login, DATA_DIR
from usdb import extract_usdb_id, fetch_detail, fetch_txt, fetch_cover, USDB_URL_RE, USDB_BASE, USDB_DETAIL
from youtube import is_valid_youtube_url, download_youtube_audio, download_youtube_video
from songs import validate_output_path, build_song_folder
from utils import sanitize_filename, _atomic_write_json, _load_json_safe
from state import state, sse_broadcast
from smb_utils import safe_rmtree
from meta_tags import VideoMetaTag
from postprocessing import normalize_audio, write_audio_tags
from image_processing import process_cover_bytes
from status import SongStatus, status_payload


# === USDB cache persistence ===

def save_usdb_cache():
    with state.cache_lock:
        _atomic_write_json(DATA_DIR / "usdb_cache.json", dict(state.usdb_cache))


def cache_song(song_id, data):
    """Update and persist one cache entry without breaking a completed import."""
    with state.cache_lock:
        state.usdb_cache[str(song_id)] = data
        try:
            _atomic_write_json(DATA_DIR / "usdb_cache.json", dict(state.usdb_cache))
        except Exception as exc:
            return str(exc)
    return None


def load_usdb_cache():
    state.usdb_cache = _load_json_safe(
        DATA_DIR / "usdb_cache.json", lambda: {}
    )


# === Input validation helpers (Task 3) ===

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


# === Core worker ===

def video_meta_tag_from_txt(txt_content):
    """Extract and parse the structured #VIDEO value from an UltraStar file."""
    for line in (txt_content or "").splitlines():
        if line.upper().startswith("#VIDEO:"):
            return VideoMetaTag.from_tag_string(line.split(":", 1)[1].strip())
    return VideoMetaTag()


def process_single(item, cookie, delay, stop_event=None):
    """Process a single queue item. Returns True on success."""
    if stop_event is not None and stop_event.is_set():
        item["status"] = SongStatus.PENDING.value
        item["progress"] = "Angehalten"
        return False
    song_id = extract_usdb_id(item["url"])
    if not song_id:
        item["status"] = SongStatus.ERROR.value
        item["progress"] = "Ungueltige URL (keine USDB-ID erkennbar)"
        return False

    # Step 1: Metadaten laden
    item["progress"] = f"Lade Metadaten (ID:{song_id}) ..."
    sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                             "progress": item["progress"]})

    data, err = fetch_detail(song_id, cookie)
    time.sleep(delay)

    if not data:
        err_lower = (err or "").lower()
        if "nicht eingeloggt" in err_lower or "datensatz nicht gefunden" in err_lower:
            # Try auto-login if credentials are stored
            if has_login_credentials():
                item["progress"] = "Session abgelaufen - versuche Auto-Login ..."
                sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                         "progress": item["progress"]})
                new_cookie, login_err = auto_login()
                if new_cookie:
                    item["progress"] = "Auto-Login erfolgreich - lade erneut ..."
                    sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                             "progress": item["progress"]})
                    data, err = fetch_detail(song_id, new_cookie)
                    time.sleep(delay)
                    if data:
                        # Use the renewed session for every remaining request.
                        cookie = new_cookie
                if not data:
                    item["status"] = SongStatus.ERROR.value
                    item["progress"] = "Auto-Login fehlgeschlagen - manuell einloggen"
                    sse_broadcast("status", {"id": item["id"], "status": SongStatus.ERROR.value,
                                             "progress": item["progress"]})
                    return False
            else:
                item["status"] = SongStatus.ERROR.value
                item["progress"] = "Nicht eingeloggt! Bitte neu einloggen (Settings)."
                sse_broadcast("status", {"id": item["id"], "status": SongStatus.ERROR.value,
                                         "progress": item["progress"]})
                return False
        elif "existiert nicht" in err_lower:
            item["status"] = SongStatus.ERROR.value
            item["progress"] = f"Song-ID {song_id} existiert nicht bei USDB"
        elif "http" in err_lower or "timeout" in err_lower or "connection" in err_lower:
            item["status"] = SongStatus.ERROR.value
            item["progress"] = f"Netzwerkfehler: {err}"
        else:
            item["status"] = SongStatus.ERROR.value
            item["progress"] = err or "Unbekannter Fehler"
        if not data:
            sse_broadcast("status", {"id": item["id"], "status": SongStatus.ERROR.value,
                                     "progress": item["progress"]})
            return False

    item["artist"] = data.get("artist", "?")
    item["title"] = data.get("title", "?")
    item["youtube_url"] = data.get("youtube_url", "")
    item["cover_url"] = data.get("cover_url", "")

    # Step 1b: Parse meta tags from #VIDEO tag if present
    meta_tags = VideoMetaTag()
    if state.config.get("use_meta_tags", True):
        # The video_tag comes from the USDB detail page's #VIDEO field
        video_tag = data.get("video_tag", "")
        if video_tag:
            meta_tags = VideoMetaTag.from_tag_string(video_tag)
            # Meta-tag audio/video URLs override the detail page's YouTube link
            if meta_tags.audio_url:
                item["meta_audio_url"] = meta_tags.audio_url
            if meta_tags.video_url:
                item["meta_video_url"] = meta_tags.video_url

    # Step 2: .txt herunterladen
    item["progress"] = "Lade .txt (Lyrics+Timing) ..."
    sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                             "progress": item["progress"]})
    txt_content, txt_err = fetch_txt(song_id, cookie)
    time.sleep(delay)

    if not txt_content:
        item["status"] = SongStatus.ERROR.value
        item["progress"] = f"Fehler beim .txt-Download: {txt_err}"
        sse_broadcast("status", {"id": item["id"], "status": SongStatus.ERROR.value,
                                 "progress": item["progress"]})
        # The queue lifecycle owns worker state; returning here prevents a
        # stale worker from clearing a newer worker's state.
        return False

    # Structured #VIDEO metadata lives in the downloaded UltraStar file, not
    # reliably on the detail page. Prefer it over any detail-page fallback.
    if state.config.get("use_meta_tags", True):
        txt_meta_tags = video_meta_tag_from_txt(txt_content)
        if not txt_meta_tags.is_empty():
            meta_tags = txt_meta_tags
        if meta_tags.audio_url:
            item["meta_audio_url"] = meta_tags.audio_url
        if meta_tags.video_url:
            item["meta_video_url"] = meta_tags.video_url

    # Step 3: Cover herunterladen
    item["progress"] = "Lade Cover ..."
    sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                             "progress": item["progress"]})
    cover_bytes, cover_err = fetch_cover(song_id, cookie)
    time.sleep(delay)

    # Step 4: Validate output path (Task 4)
    output_base, out_err = validate_output_path(state.config.get("output_path", ""))
    if out_err:
        item["status"] = SongStatus.ERROR.value
        item["progress"] = f"Ungueltiger Output-Pfad: {out_err}"
        sse_broadcast("status", {"id": item["id"], "status": SongStatus.ERROR.value,
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
        # Step 5: Audio herunterladen
        # Source priority: meta-tag audio URL > detail-page YouTube URL
        audio_url = item.get("meta_audio_url") or data.get("youtube_url", "")
        audio_ok = False
        if audio_url and is_valid_youtube_url(audio_url):
            audio_fmt = state.config.get("audio_format", "mp3")
            audio_br = state.config.get("audio_bitrate", 192)
            item["progress"] = f"Lade Audio ({audio_fmt}@{audio_br}k) von YouTube ..."
            sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                     "progress": item["progress"]})
            audio_target = str(staging_root / base_name)
            audio_path, audio_err = download_youtube_audio(
                audio_url, audio_target, item=item,
                audio_format=audio_fmt, bitrate=audio_br,
            )
            if audio_path:
                audio_ok = True
            else:
                item["progress"] = f"Audio-Download fehlgeschlagen: {audio_err}"
                sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                         "progress": item["progress"]})
        else:
            item["progress"] = "Kein YouTube-Link gefunden, ueberspringe Audio"
            sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                     "progress": item["progress"]})

        # Step 5b: Audio postprocessing (normalize + tags)
        if audio_path:
            # Normalize
            if state.config.get("audio_normalize", False):
                method = state.config.get("audio_normalize_strength", "loudnorm")
                item["progress"] = f"Normalisiere Audio ({method}) ..."
                sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                         "progress": item["progress"]})
                norm_ok, norm_err = normalize_audio(audio_path, method)
                if not norm_ok:
                    item["progress"] = f"Normalisierung übersprungen: {norm_err}"
                    sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                             "progress": item["progress"]})
            # Write metadata tags
            tag_meta = {**data, "artist": item["artist"], "title": item["title"]}
            write_audio_tags(audio_path, tag_meta)

        # Step 6: Video herunterladen
        # Source priority: meta-tag video URL > detail-page YouTube URL
        video_url = item.get("meta_video_url") or data.get("youtube_url", "")
        video_ok = False
        if video_url and is_valid_youtube_url(video_url) and state.config.get("download_video", True):
            vid_fmt = state.config.get("video_format", "mp4")
            vid_res = state.config.get("video_resolution", 1080)
            item["progress"] = f"Lade Video ({vid_fmt}@{vid_res}p) von YouTube ..."
            sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                     "progress": item["progress"]})
            video_target = str(staging_root / base_name)
            video_path, video_err = download_youtube_video(
                video_url, video_target, item=item,
                video_format=vid_fmt, max_height=vid_res,
            )
            if video_path:
                video_ok = True
            else:
                item["progress"] = f"Video-Download fehlgeschlagen: {video_err}"
                sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                         "progress": item["progress"]})

        # Step 6b: Verify mp3/mp4 exist and are non-empty (Task 4)
        if audio_path:
            ap = Path(audio_path)
            if not ap.exists() or ap.stat().st_size == 0:
                item["progress"] = "Audio-Datei fehlt oder ist leer"
                sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                         "progress": item["progress"]})
                audio_path = None
        if video_path:
            vp = Path(video_path)
            if not vp.exists() or vp.stat().st_size == 0:
                item["progress"] = "Video-Datei fehlt oder ist leer"
                sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                         "progress": item["progress"]})
                video_path = None

        if not audio_path:
            item["status"] = SongStatus.ERROR.value
            item["progress"] = "Audio-Datei fehlt; Song wurde nicht importiert"
            sse_broadcast("status", {"id": item["id"], "status": SongStatus.ERROR.value, "progress": item["progress"]})
            return False
        if stop_event is not None and stop_event.is_set():
            item["status"] = SongStatus.PENDING.value
            item["progress"] = "Angehalten"
            return False

        # Step 6c: Cover processing (resize, autofix, meta-tag operations)
        if cover_bytes and (state.config.get("cover_resize", 0) or
                            state.config.get("cover_autofix", False) or
                            (meta_tags and meta_tags.has_cover_ops())):
            item["progress"] = "Verarbeite Cover ..."
            sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                     "progress": item["progress"]})
            cover_bytes, cover_err = process_cover_bytes(
                cover_bytes,
                meta_tag=meta_tags if (meta_tags and meta_tags.has_cover_ops()) else None,
                max_size=state.config.get("cover_resize", 0),
                autofix=state.config.get("cover_autofix", False),
            )

        # Step 7: Song-Ordner fertigstellen (.txt + Cover) -- atomic transaction
        item["progress"] = "Schreibe .txt und Cover ..."
        sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                 "progress": item["progress"]})

        try:
            folder_path = build_song_folder(
                data, txt_content, cover_bytes, output_base,
                audio_path=audio_path, video_path=video_path,
            )
        except Exception as e:
            item["status"] = SongStatus.ERROR.value
            item["progress"] = f"Fehler beim Ordner erstellen: {e}"
            sse_broadcast("status", {"id": item["id"], "status": SongStatus.ERROR.value,
                                     "progress": item["progress"]})
            return False
    finally:
        # Always clean up the staging directory.
        safe_rmtree(staging_root)

    # Fertig
    item["usdb_id"] = song_id
    item["genre"] = data.get("genre", "")
    item["year"] = data.get("year", "")
    item["language"] = data.get("language", "")
    item["progress"] = "Fertig"
    item["status"] = SongStatus.DONE.value
    item["output_folder"] = folder_path

    cache_error = cache_song(song_id, data)
    if cache_error:
        item["cache_warning"] = cache_error

    sse_broadcast("status", {
        "id": item["id"], "status": SongStatus.DONE.value, "progress": "Fertig",
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
    """Process pending queue items for one lifecycle generation only.

    When state.config['max_workers'] > 1, items are processed concurrently
    using a ThreadPoolExecutor. Each worker thread runs process_single()
    independently. The queue_lock protects queue state mutations.
    """
    try:
        max_workers = max(1, int(state.config.get("max_workers", 1)))
        delay = state.config.get("delay", 0.5)
        cookie = get_cookie()
        if not cookie or len(cookie) < 10:
            # Mark all pending items as error
            with state.queue_lock:
                for q_item in state.queue:
                    if q_item["status"] == SongStatus.PENDING.value:
                        q_item["status"] = SongStatus.ERROR.value
                        q_item["progress"] = "Kein Cookie! Bitte unter Einstellungen einloggen."
                        sse_broadcast("status", {"id": q_item["id"], "status": SongStatus.ERROR.value,
                                                  "progress": q_item["progress"]})
            return

        if max_workers == 1:
            _process_queue_sequential(generation, stop_event, cookie, delay)
        else:
            _process_queue_parallel(generation, stop_event, cookie, delay, max_workers)
    finally:
        with state.worker_lock:
            if state.worker_generation == generation:
                state.worker_running = False
                state.worker_thread = None
        sse_broadcast("worker_stopped", {"generation": generation})


def _claim_next_pending():
    """Atomically claim the next pending item. Returns item or None."""
    with state.queue_lock:
        for q_item in state.queue:
            if q_item["status"] == SongStatus.PENDING.value:
                q_item["status"] = SongStatus.PROCESSING.value
                q_item["progress"] = "Verbinde mit USDB ..."
                return q_item
    return None


def _process_queue_sequential(generation, stop_event, cookie, delay):
    """Original single-threaded queue processing."""
    while not stop_event.is_set():
        item = _claim_next_pending()
        if item is None:
            break
        sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                  "progress": item["progress"]})
        try:
            # Auto-login may have renewed and persisted the cookie while another
            # item was running; never reuse only the queue-start snapshot.
            current_cookie = get_cookie() or cookie
            process_single(item, current_cookie, delay, stop_event)
        except Exception as exc:
            item["status"] = SongStatus.ERROR.value
            item["progress"] = f"Unerwarteter Fehler: {exc}"
            sse_broadcast("status", {"id": item["id"], "status": SongStatus.ERROR.value,
                                      "progress": item["progress"]})


def _process_queue_parallel(generation, stop_event, cookie, delay, max_workers):
    """Multi-threaded queue processing using ThreadPoolExecutor.

    Claims items lazily — each thread asks for the next pending item when
    it finishes, so the queue drains naturally regardless of item count.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    active_futures = set()

    def _worker(item):
        """Thread wrapper: catches exceptions, updates item state."""
        try:
            # Auto-login may have renewed and persisted the cookie while another
            # item was running; never reuse only the queue-start snapshot.
            current_cookie = get_cookie() or cookie
            process_single(item, current_cookie, delay, stop_event)
        except Exception as exc:
            item["status"] = SongStatus.ERROR.value
            item["progress"] = f"Unerwarteter Fehler: {exc}"
            sse_broadcast("status", {"id": item["id"], "status": SongStatus.ERROR.value,
                                      "progress": item["progress"]})

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        # Seed: submit up to max_workers initial items
        for _ in range(max_workers):
            if stop_event.is_set():
                break
            item = _claim_next_pending()
            if item is None:
                break
            sse_broadcast("status", {"id": item["id"], "status": SongStatus.PROCESSING.value,
                                      "progress": item["progress"]})
            active_futures.add(pool.submit(_worker, item))

        # Drain: as each future completes, submit the next pending item
        while active_futures and not stop_event.is_set():
            done, active_futures = _wait_any(active_futures)
            for _ in done:
                if stop_event.is_set():
                    break
                next_item = _claim_next_pending()
                if next_item is None:
                    break
                sse_broadcast("status", {"id": next_item["id"], "status": SongStatus.PROCESSING.value,
                                          "progress": next_item["progress"]})
                active_futures.add(pool.submit(_worker, next_item))

    # If stopped mid-flight, revert processing items to pending
    if stop_event.is_set():
        with state.queue_lock:
            for q_item in state.queue:
                if q_item["status"] == SongStatus.PROCESSING.value:
                    q_item["status"] = SongStatus.PENDING.value
                    q_item["progress"] = "Angehalten"


def _wait_any(futures):
    """Wait for any future to complete. Returns (done_set, remaining_set)."""
    from concurrent.futures import wait, FIRST_COMPLETED
    done, pending = wait(futures, return_when=FIRST_COMPLETED)
    remaining = futures - done
    return done, remaining
