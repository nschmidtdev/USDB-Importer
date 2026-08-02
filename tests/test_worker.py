import threading
from pathlib import Path

import worker


def test_cache_write_failure_keeps_in_memory_song(monkeypatch):
    old_cache = worker.state.usdb_cache
    worker.state.usdb_cache = {}
    monkeypatch.setattr(
        worker,
        "_atomic_write_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    try:
        error = worker.cache_song("42", {"artist": "A"})
        assert worker.state.usdb_cache["42"] == {"artist": "A"}
        assert "disk full" in error
    finally:
        worker.state.usdb_cache = old_cache


def test_video_meta_tag_is_parsed_from_downloaded_txt():
    meta = worker.video_meta_tag_from_txt(
        "#ARTIST:A\n#VIDEO:a=https://youtu.be/abcdefghijk,v=https://youtu.be/lmnopqrstuv\nE"
    )

    assert meta.audio_url == "https://youtu.be/abcdefghijk"
    assert meta.video_url == "https://youtu.be/lmnopqrstuv"


def test_auto_login_cookie_is_used_for_next_queue_item(monkeypatch):
    items = iter([
        {"id": 1, "status": "pending", "progress": ""},
        {"id": 2, "status": "pending", "progress": ""},
        None,
    ])
    cookies = iter(["first-cookie", "renewed-cookie"])
    observed = []
    monkeypatch.setattr(worker, "_claim_next_pending", lambda: next(items))
    monkeypatch.setattr(worker, "get_cookie", lambda: next(cookies))
    monkeypatch.setattr(worker, "sse_broadcast", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker,
        "process_single",
        lambda item, cookie, delay, stop_event: observed.append((item["id"], cookie)),
    )

    worker._process_queue_sequential(1, threading.Event(), "startup-cookie", 0)

    assert observed == [(1, "first-cookie"), (2, "renewed-cookie")]


def test_auto_login_cookie_is_used_for_remaining_requests(monkeypatch, tmp_path):
    item = {
        "id": 1,
        "url": "https://usdb.animux.de/index.php?link=detail&id=123",
        "status": "processing",
        "progress": "",
    }
    observed = {"txt": None, "cover": None}
    detail = {
        "artist": "Artist",
        "title": "Title",
        "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
    }

    monkeypatch.setattr(
        worker,
        "fetch_detail",
        lambda song_id, cookie: (detail, None) if cookie == "fresh-cookie" else (None, "Nicht eingeloggt"),
    )
    monkeypatch.setattr(worker, "has_login_credentials", lambda: True)
    monkeypatch.setattr(worker, "auto_login", lambda: ("fresh-cookie", None))

    def fetch_txt(song_id, cookie):
        observed["txt"] = cookie
        return ("#ARTIST:Artist\n#TITLE:Title\nE", None) if cookie == "fresh-cookie" else (None, "expired")

    def fetch_cover(song_id, cookie):
        observed["cover"] = cookie
        return b"cover", None

    def download_audio(url, target, **kwargs):
        path = Path(target + ".mp3")
        path.write_bytes(b"audio")
        return str(path), None

    monkeypatch.setattr(worker, "fetch_txt", fetch_txt)
    monkeypatch.setattr(worker, "fetch_cover", fetch_cover)
    monkeypatch.setattr(worker, "download_youtube_audio", download_audio)
    monkeypatch.setattr(worker, "is_valid_youtube_url", lambda url: True)
    monkeypatch.setattr(worker, "build_song_folder", lambda *args, **kwargs: str(tmp_path / "final"))
    monkeypatch.setattr(worker, "cache_song", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "safe_rmtree", lambda path: None)
    monkeypatch.setattr(worker.time, "sleep", lambda delay: None)

    old_config = worker.state.config.copy()
    old_cache = worker.state.usdb_cache
    worker.state.config.update({
        "output_path": str(tmp_path),
        "download_video": False,
        "use_meta_tags": False,
        "audio_normalize": False,
        "audio_format": "mp3",
        "audio_bitrate": 192,
    })
    worker.state.usdb_cache = {}
    try:
        result = worker.process_single(item, "expired-cookie", 0, threading.Event())
    finally:
        worker.state.config = old_config
        worker.state.usdb_cache = old_cache

    assert result is True, item
    assert observed == {"txt": "fresh-cookie", "cover": "fresh-cookie"}
