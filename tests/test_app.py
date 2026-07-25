"""Tests for app.py API endpoints and validation functions."""
import sys
import os
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)

if "app" in sys.modules:
    del sys.modules["app"]
import app


@pytest.fixture(autouse=True)
def isolate_config():
    """Prevent tests from touching real config, credentials, or queue state."""
    cookie = {"value": ""}
    with patch.object(app, 'save_config'), patch.object(app, 'save_usdb_cache'), \
         patch.object(app, 'get_cookie', side_effect=lambda: cookie["value"]), \
         patch.object(app, 'set_cookie', side_effect=lambda value: cookie.__setitem__("value", value)):
        with app.state.queue_lock:
            app.state.queue.clear()
            app.state.queue_next_id = 1
        yield


class TestSettingsAPI:
    """GET /api/settings must never expose cookie value."""

    def test_settings_no_cookie_value(self):
        with app.app.test_client() as c:
            r = c.get("/api/settings")
            data = json.loads(r.data)
            assert "cookie" not in data
            assert "has_cookie" in data

    def test_settings_has_cookie_true_after_put(self):
        with app.app.test_client() as c:
            c.put("/api/settings", json={"cookie": "PHPSESSID=secret"})
            r = c.get("/api/settings")
            data = json.loads(r.data)
            assert data["has_cookie"] is True
            assert "secret" not in r.data.decode()


class TestQueueValidation:
    """Queue items must be validated."""

    def test_add_valid_url(self):
        with app.app.test_client() as c:
            r = c.post("/api/queue/add", json={
                "urls": "https://usdb.animux.de/index.php?link=detail&id=3045"
            })
            assert r.status_code == 200
            assert json.loads(r.data)["added"] == 1

    def test_add_invalid_url_rejected(self):
        with app.app.test_client() as c:
            r = c.post("/api/queue/add", json={
                "urls": "https://evil.com/hack?id=1"
            })
            assert r.status_code == 400

    def test_add_none_rejected(self):
        with app.app.test_client() as c:
            r = c.post("/api/queue/add", json={"urls": None})
            assert r.status_code == 400

    def test_add_non_string_rejected(self):
        with app.app.test_client() as c:
            r = c.post("/api/queue/add", json={"urls": 12345})
            assert r.status_code == 400


class TestDelayValidation:
    """Delay must be validated. Returns (value, error) tuple."""

    def test_valid_delay(self):
        val, err = app.validate_delay(0.5)
        assert val == 0.5
        assert err is None

    def test_negative_delay(self):
        val, err = app.validate_delay(-1)
        assert err is not None

    def test_zero_delay(self):
        val, err = app.validate_delay(0)
        assert err is not None

    def test_huge_delay(self):
        val, err = app.validate_delay(120)
        assert err is not None

    def test_non_numeric_delay(self):
        val, err = app.validate_delay("abc")
        assert err is not None


class TestNormalize:
    """normalize() must be accent-insensitive."""

    def test_ascii(self):
        assert app.normalize("Hello") == "hello"

    def test_accent_insensitive(self):
        assert app.normalize("cafe") == app.normalize("cafe")

    def test_none(self):
        assert app.normalize(None) == ""

    def test_empty(self):
        assert app.normalize("") == ""


class TestSanitizeFilename:
    """sanitize_filename must remove dangerous chars."""

    def test_removes_colon(self):
        assert ":" not in app.sanitize_filename("a:b")

    def test_removes_backslash(self):
        assert "\\" not in app.sanitize_filename("a\\b")

    def test_removes_question_mark(self):
        assert "?" not in app.sanitize_filename("a?b")

    def test_keeps_dash(self):
        assert app.sanitize_filename("a-b") == "a-b"

    def test_keeps_unicode(self):
        assert "\u00fc" in app.sanitize_filename("\u00fcber")


class TestAtomicJSON:
    """_atomic_write_json and _load_json_safe must work correctly."""

    def test_write_and_read(self, tmp_path):
        f = tmp_path / "test.json"
        app._atomic_write_json(f, {"key": "value"})
        assert json.loads(f.read_text())["key"] == "value"

    def test_load_corrupt_falls_back(self, tmp_path):
        f = tmp_path / "corrupt.json"
        f.write_text("{invalid json!!!")
        result = app._load_json_safe(f, lambda: {"default": True})
        assert result == {"default": True}

    def test_load_missing_falls_back(self, tmp_path):
        f = tmp_path / "nonexistent.json"
        result = app._load_json_safe(f, lambda: {})
        assert result == {}


class TestBuildSongFolder:
    """build_song_folder must create correct structure."""

    def test_creates_folder(self, tmp_path):
        data = {"artist": "Test", "title": "Song", "usdb_id": "1"}
        txt = "#ARTIST:Test\n#TITLE:Song\nE 1 1 ~ hi\n"
        cover = b"\xff\xd8fake"
        folder = app.build_song_folder(data, txt, cover, str(tmp_path))
        p = Path(folder)
        assert p.exists()
        assert len(list(p.glob("*.txt"))) == 1

    def test_without_cover(self, tmp_path):
        data = {"artist": "Adele", "title": "Hello", "usdb_id": "1"}
        folder = app.build_song_folder(data, "#ARTIST:A\n", None, str(tmp_path))
        covers = [f for f in Path(folder).iterdir() if f.name.endswith("[CO].jpg")]
        assert len(covers) == 0


class TestURLValidation:
    """validate_queue_url returns (url, error) tuple."""

    def test_valid_usdb_url(self):
        url, err = app.validate_queue_url(
            "https://usdb.animux.de/index.php?link=detail&id=3045"
        )
        assert url is not None
        assert err is None

    def test_non_usdb_url(self):
        url, err = app.validate_queue_url("https://evil.com")
        assert url is None
        assert err is not None

    def test_none(self):
        url, err = app.validate_queue_url(None)
        assert url is None
        assert err is not None

    def test_non_string(self):
        url, err = app.validate_queue_url(12345)
        assert url is None
        assert err is not None

    def test_empty_string(self):
        url, err = app.validate_queue_url("")
        assert url is None
        assert err is not None


class TestQueueStateTransitions:
    def test_processing_item_cannot_be_removed(self):
        app.state.queue.append({"id": 1, "status": "processing", "progress": "", "url": "x"})
        with app.app.test_client() as c:
            assert c.post("/api/queue/remove/1").status_code == 409

    def test_done_item_cannot_be_retried(self):
        app.state.queue.append({"id": 1, "status": "done", "progress": "", "url": "x"})
        with app.app.test_client() as c:
            assert c.post("/api/queue/retry/1").status_code == 409

    def test_origin_protection_rejects_remote_mutation(self):
        with app.app.test_client() as c:
            response = c.post("/api/queue/clear", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403


class TestOutputTransaction:
    def test_existing_song_is_preserved(self, tmp_path):
        existing = tmp_path / "Test - Song"
        existing.mkdir()
        (existing / "keep.txt").write_text("keep")
        with pytest.raises(FileExistsError):
            app.build_song_folder({"artist": "Test", "title": "Song"}, "#TITLE:Song", None, str(tmp_path))
        assert (existing / "keep.txt").read_text() == "keep"
        assert not list(tmp_path.glob("*.partial-*"))


def test_normalize_real_accent():
    assert app.normalize("café") == app.normalize("cafe")
