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


class TestProxySecurity:
    def test_proxy_strips_remote_scripts_and_event_handlers(self):
        client = app.app.test_client()
        class Response:
            text = (
                '<html><head><base href="https://evil.example/">'
                '<meta http-equiv="refresh" content="0;url=/api/settings"></head>'
                '<body onload="alert(1)">'
                '<script>alert("remote")</script>'
                '<iframe src="/api/settings"></iframe>'
                '<object data="/api/settings"></object>'
                '<a href="javascript:alert(2)">bad</a>'
                '<img src="cover.jpg" onerror="alert(3)">'
                '</body></html>'
            )

        with patch.object(app, "get_cookie", return_value="PHPSESSID=valid-session"), \
             patch.object(app.req_lib, "get", return_value=Response()):
            response = client.get("/proxy?link=browse")

        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'alert("remote")' not in body
        assert "onload=" not in body
        assert "onerror=" not in body
        assert "javascript:" not in body
        assert "<base" not in body
        assert "<iframe" not in body
        assert "<object" not in body
        assert "http-equiv" not in body
        assert "object-src 'none'" in response.headers["Content-Security-Policy"]
        # The application-owned enhancement script must remain available.
        assert "usdb-proxy-queue-btn" in body


class TestOutputPathSecurity:
    def test_cover_rejects_parent_directory_traversal(self, tmp_path):
        client = app.app.test_client()
        output = tmp_path / "output"
        outside = tmp_path / "outside"
        output.mkdir()
        outside.mkdir()
        (outside / "Artist - Song [CO].jpg").write_bytes(b"not-a-real-image")

        old_output = app.state.config.get("output_path", "")
        app.state.config["output_path"] = str(output)
        try:
            response = client.get("/api/output/cover", query_string={"folder": "../outside"})
        finally:
            app.state.config["output_path"] = old_output

        assert response.status_code == 400


class TestServerSecurity:
    def test_server_mode_requires_basic_auth(self):
        client = app.app.test_client()
        env = {
            "USDB_SERVER_MODE": "1",
            "USDB_USERNAME": "admin",
            "USDB_PASSWORD": "correct-horse",
        }
        with patch.dict(os.environ, env, clear=False):
            health = client.get("/health")
            denied = client.get("/")
            allowed = client.get("/", headers={"Authorization": "Basic YWRtaW46Y29ycmVjdC1ob3JzZQ=="})

        assert health.status_code == 200
        assert health.get_json() == {"ok": True, "version": "0.1.0"}
        assert denied.status_code == 401
        assert allowed.status_code == 200

    def test_remote_same_origin_is_allowed(self):
        with app.app.test_request_context(
            "/api/queue/clear",
            base_url="http://192.168.1.50:5776",
            headers={"Origin": "http://192.168.1.50:5776"},
        ):
            assert app._origin_is_allowed("http://192.168.1.50:5776")

    def test_server_mode_rejects_desktop_login_window(self):
        client = app.app.test_client()
        env = {
            "USDB_SERVER_MODE": "1",
            "USDB_USERNAME": "admin",
            "USDB_PASSWORD": "correct-horse",
        }
        auth = {"Authorization": "Basic YWRtaW46Y29ycmVjdC1ob3JzZQ=="}
        with patch.dict(os.environ, env, clear=False):
            response = client.post("/api/login-window", headers=auth)
        assert response.status_code == 409


class TestCredentials:
    def test_blank_password_keeps_stored_password(self):
        client = app.app.test_client()
        with patch.object(app, "get_login_credentials", return_value=("old-user", "saved-secret")), \
             patch.object(app, "set_login_credentials") as store:
            response = client.put(
                "/api/credentials",
                json={"username": "new-user", "password": ""},
            )

        assert response.status_code == 200
        store.assert_called_once_with("new-user", "saved-secret")


class TestCookieRoutes:
    def test_transfer_and_browser_extraction_use_distinct_routes(self):
        routes = {(rule.rule, rule.endpoint) for rule in app.app.url_map.iter_rules()}
        assert ("/api/cookie/transfer", "api_cookie_transfer") in routes
        assert ("/api/cookie/from-browser", "api_cookie_from_browser_extract") in routes
        assert sum(rule == "/api/cookie/from-browser" for rule, _ in routes) == 1


class TestOutputDeleteSecurity:
    @pytest.mark.parametrize("folder_name", [".", "./", "nested/song", "nested\\song"])
    def test_delete_rejects_root_and_non_child_paths(self, tmp_path, monkeypatch, folder_name):
        output = tmp_path / "output"
        output.mkdir()
        (output / "keep.txt").write_text("keep", encoding="utf-8")
        nested = output / "nested" / "song"
        nested.mkdir(parents=True)
        monkeypatch.setitem(app.state.config, "output_path", str(output))

        response = app.app.test_client().post(
            "/api/output/delete", json={"folder": folder_name}
        )

        assert response.status_code in {400, 403}
        assert output.is_dir()
        assert (output / "keep.txt").is_file()
        assert nested.is_dir()


class TestZipSafety:
    def test_zip_collection_enforces_source_size_limit(self, tmp_path):
        (tmp_path / "large.bin").write_bytes(b"1234")
        with patch.dict(os.environ, {"USDB_ZIP_MAX_BYTES": "3"}, clear=False):
            with pytest.raises(ValueError, match="ZIP-Limit"):
                app._collect_zip_files(tmp_path)

    @pytest.mark.parametrize("folder_name", [".", "./", "nested/song", "nested\\song"])
    def test_zip_folder_rejects_non_child_names(self, tmp_path, monkeypatch, folder_name):
        output = tmp_path / "output"
        (output / "nested" / "song").mkdir(parents=True)
        monkeypatch.setitem(app.state.config, "output_path", str(output))
        response = app.app.test_client().get(
            "/api/output/zip-folder", query_string={"folder": folder_name}
        )
        assert response.status_code in {400, 403}


class TestCredentials:
    def test_put_preserves_password_whitespace(self, monkeypatch):
        captured = {}

        def store(username, password):
            captured["value"] = (username, password)

        monkeypatch.setattr(app, "set_login_credentials", store)

        response = app.app.test_client().put(
            "/api/credentials",
            json={"username": " user ", "password": " secret pass "},
        )

        assert response.status_code == 200
        assert captured["value"] == ("user", " secret pass ")

    def test_frontend_does_not_trim_password(self):
        frontend = (Path(app.CODE_DIR) / "static" / "index.html").read_text(encoding="utf-8")
        assert "const password = document.getElementById('credPass').value;" in frontend


class TestCacheConcurrency:
    def test_clear_cache_uses_cache_lock(self, tmp_path, monkeypatch):
        class TrackingLock:
            entered = False

            def __enter__(self):
                self.entered = True

            def __exit__(self, *args):
                return False

        lock = TrackingLock()
        (tmp_path / "usdb_cache.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(app, "DATA_DIR", tmp_path)
        monkeypatch.setattr(app.state, "cache_lock", lock)
        app.state.usdb_cache = {"42": {"artist": "A"}}

        response = app.app.test_client().post("/api/data/clear-cache")

        assert response.status_code == 200
        assert lock.entered is True
        assert app.state.usdb_cache == {}


class TestAssetRepair:
    def test_mp3_patch_adds_missing_header(self, tmp_path):
        txt = tmp_path / "song.txt"
        txt.write_text("#ARTIST:A\n#TITLE:T\nE", encoding="utf-8")

        app._patch_txt_mp3(txt, "A - T.mp3")

        assert "#MP3:A - T.mp3" in txt.read_text(encoding="utf-8").splitlines()

    def test_frontend_parses_asset_repair_json(self):
        frontend = (Path(app.CODE_DIR) / "static" / "index.html").read_text(encoding="utf-8")
        assert "await apiJSON('/api/output/fix-assets'" in frontend
        assert "await apiJSON('/api/output/fix-covers'" in frontend

    def test_preview_button_avoids_inline_javascript_with_folder_name(self):
        frontend = (Path(app.CODE_DIR) / "static" / "index.html").read_text(encoding="utf-8")
        assert 'onclick="togglePreview' not in frontend
        assert "previewButton.addEventListener('click'" in frontend


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
        # UltraStar scans every *.txt file, so the auxiliary links file must
        # deliberately have no .txt suffix.
        txt_files = list(p.glob("*.txt"))
        assert len(txt_files) == 1
        assert (p / "_links").exists()

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


def test_health_reports_project_version():
    with app.app.test_client() as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "version": "0.1.0"}


@pytest.mark.parametrize(
    ("slug", "marker"),
    [
        ("license", b"GNU GENERAL PUBLIC LICENSE"),
        ("disclaimer", "Eigenverantwortliche Nutzung".encode()),
        ("third-party-notices", b"Third-party notices"),
        ("third-party-licenses", b"Flask"),
    ],
)
def test_legal_documents_are_served(slug, marker):
    with app.app.test_client() as client:
        response = client.get(f"/legal/{slug}")
    assert response.status_code == 200
    assert marker in response.data
    assert response.headers["Content-Type"].startswith("text/plain")


def test_unknown_legal_document_is_not_served():
    with app.app.test_client() as client:
        response = client.get("/legal/../../config.json")
    assert response.status_code == 404
