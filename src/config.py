"""Configuration, constants, and credential management.

Handles:
- Path discovery (CODE_DIR, DATA_DIR)
- USDB URL constants
- Keyring-backed cookie and login credential storage
- load_config / save_config
"""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from utils import _atomic_write_json, _load_json_safe


# === Path discovery ===

if getattr(sys, "frozen", False):
    # PyInstaller onefile: bundled resources (static/) live in _MEIPASS,
    # but user files (config.json) live next to the .exe.
    CODE_DIR = Path(sys._MEIPASS)
    EXE_DIR = Path(sys.executable).parent
else:
    # src/ layout: project root is one level above this module
    CODE_DIR = Path(__file__).resolve().parent.parent
    EXE_DIR = CODE_DIR


def _default_data_dir():
    """Return OS-appropriate persistent data directory."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "UltraStarImporter"
    elif sys.platform == "darwin":
        home = Path.home()
        return home / "Library" / "Application Support" / "UltraStarImporter"
    else:
        home = os.environ.get("XDG_DATA_HOME")
        if home:
            return Path(home) / "UltraStarImporter"
        return Path.home() / ".local" / "share" / "UltraStarImporter"


# Resolve an explicit data-directory pointer from a stable, OS-specific location.
# This survives moving away from the default directory and is consulted before
# loading the real configuration on the next process start.
_DEFAULT_DATA_DIR = _default_data_dir()
_DATA_POINTER_FILE = _DEFAULT_DATA_DIR / "data-location.json"
_data_dir_override = None
if _DATA_POINTER_FILE.exists():
    try:
        _pointer = json.loads(_DATA_POINTER_FILE.read_text(encoding="utf-8"))
        _data_dir_override = _pointer.get("data_path") or None
    except Exception:
        pass

# Legacy migration: also honor config.json next to the executable/script.
_config_probe = EXE_DIR / "config.json"
if not _data_dir_override and _config_probe.exists():
    try:
        _tmp = json.loads(_config_probe.read_text(encoding="utf-8"))
        _data_dir_override = _tmp.get("data_path") or None
    except Exception:
        pass

# Docker / server mode: use /app/data if it exists
_docker_data = Path("/app/data")
if _docker_data.exists() and _docker_data.is_dir() and not _data_dir_override:
    _data_dir_override = str(_docker_data)

DATA_DIR = Path(_data_dir_override) if _data_dir_override else _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"

# Migrate config from old location (CODE_DIR/config.json) if new location is empty
if not CONFIG_FILE.exists() and _config_probe.exists():
    try:
        import shutil
        shutil.copy2(_config_probe, CONFIG_FILE)
    except Exception:
        pass


# === USDB constants ===

USDB_BASE = "https://usdb.animux.de/"
USDB_DETAIL = "index.php?link=detail&id={}"


# === Config defaults ===

CONFIG_DEFAULTS = {
    "local_path": "", "delay": 0.5,
    "output_path": os.environ.get("USDB_OUTPUT_PATH", ""),
    "download_video": True, "collision_policy": "skip",
    # Audio quality
    "audio_format": "mp3",          # mp3 | m4a | opus | vorbis
    "audio_bitrate": 192,           # kbps (128/192/256/320 for mp3/m4a)
    "audio_normalize": False,       # ffmpeg-normalize EBU R128
    "audio_normalize_strength": "loudnorm",  # loudnorm | replaygain
    # Video quality
    "video_resolution": 1080,       # max height (480/720/1080)
    "video_format": "mp4",          # mp4 | webm | mkv
    # Image processing
    "cover_resize": 0,              # max px (0 = off)
    "cover_autofix": False,         # auto-contrast + rotate fix
    # Meta-tag behavior
    "use_meta_tags": True,          # parse #VIDEO tag for resources
    # Concurrency
    "max_workers": 1,               # parallel downloads (1=sequential, 3-5 recommended)
}


# === Credential manager constants ===

COOKIE_SERVICE = "ultrastar-importer"
COOKIE_ACCOUNT = "usdb-session"
LOGIN_USER_ACCOUNT = "usdb-user"
LOGIN_PASS_ACCOUNT = "usdb-pass"


# === Cookie management ===

_SERVER_SECRET_LOCK = threading.Lock()


def _server_mode_enabled():
    return os.environ.get("USDB_SERVER_MODE", "").strip().lower() in {"1", "true", "yes"}


def _server_secrets_path():
    return DATA_DIR / "server-secrets.json"


def _read_server_secrets():
    return _load_json_safe(_server_secrets_path(), lambda: {})


def _write_server_secrets(values):
    """Atomically persist server-only secrets with mode 0600."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".server-secrets-", suffix=".tmp", dir=DATA_DIR)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(values, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, _server_secrets_path())
        try:
            os.chmod(_server_secrets_path(), 0o600)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _set_server_secrets(**updates):
    with _SERVER_SECRET_LOCK:
        values = _read_server_secrets()
        for key, value in updates.items():
            if value:
                values[key] = value
            else:
                values.pop(key, None)
        _write_server_secrets(values)


def get_cookie():
    """Read the USDB session from the server store or OS credential store."""
    if _server_mode_enabled():
        with _SERVER_SECRET_LOCK:
            return str(_read_server_secrets().get("cookie", ""))
    try:
        import keyring
        return keyring.get_password(COOKIE_SERVICE, COOKIE_ACCOUNT) or ""
    except Exception as exc:
        print(f"WARNING: Cookie konnte nicht aus Credential Manager gelesen werden: {exc}")
        return ""


def set_cookie(cookie):
    """Store/remove the USDB session outside the project tree."""
    if _server_mode_enabled():
        _set_server_secrets(cookie=cookie)
        return
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
        raise RuntimeError(f"Credential Manager nicht verfügbar: {exc}") from exc


# === Login credentials ===

def get_login_credentials():
    """Read stored USDB login credentials."""
    if _server_mode_enabled():
        with _SERVER_SECRET_LOCK:
            values = _read_server_secrets()
        return str(values.get("login_user", "")), str(values.get("login_password", ""))
    try:
        import keyring
        user = keyring.get_password(COOKIE_SERVICE, LOGIN_USER_ACCOUNT) or ""
        password = keyring.get_password(COOKIE_SERVICE, LOGIN_PASS_ACCOUNT) or ""
        return user, password
    except Exception:
        return "", ""


def set_login_credentials(user, password):
    """Store USDB login credentials."""
    if _server_mode_enabled():
        _set_server_secrets(login_user=user, login_password=password)
        return
    try:
        import keyring
        if user:
            keyring.set_password(COOKIE_SERVICE, LOGIN_USER_ACCOUNT, user)
        else:
            try:
                keyring.delete_password(COOKIE_SERVICE, LOGIN_USER_ACCOUNT)
            except keyring.errors.PasswordDeleteError:
                pass
        if password:
            keyring.set_password(COOKIE_SERVICE, LOGIN_PASS_ACCOUNT, password)
        else:
            try:
                keyring.delete_password(COOKIE_SERVICE, LOGIN_PASS_ACCOUNT)
            except keyring.errors.PasswordDeleteError:
                pass
    except Exception as exc:
        raise RuntimeError(f"Credential Manager nicht verfügbar: {exc}") from exc


def has_login_credentials():
    """Check if login credentials are stored."""
    user, password = get_login_credentials()
    return bool(user and password)


def auto_login():
    """Attempt to log into USDB with stored credentials.
    Returns (cookie_string, error).
    """
    try:
        import requests as req_lib
    except ImportError:
        return "", "requests nicht installiert"

    user, password = get_login_credentials()
    if not user or not password:
        return "", "Keine Login-Daten hinterlegt"
    try:
        session = req_lib.Session()
        resp = session.post(
            USDB_BASE + "?link=login",
            data={"user": user, "pass": password, "remember": "1", "login": "Login"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=15,
            allow_redirects=True,
        )
        # Check if login succeeded: look for "Logout" (only shown when logged in)
        if "logout" not in resp.text.lower():
            return "", "Login fehlgeschlagen - Benutzer/Passwort falsch?"
        # Extract cookies
        cookies = session.cookies.get_dict()
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if "PHPSESSID" not in cookie_str:
            return "", "Login fehlgeschlagen - keine Session erhalten"
        # Store the cookie
        set_cookie(cookie_str)
        return cookie_str, None
    except Exception as e:
        return "", f"Netzwerkfehler: {e}"


# === Config load / save ===

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


def save_config(config):
    """Persist config and any requested data-directory move for next restart."""
    _atomic_write_json(CONFIG_FILE, config)
    if "data_path" not in config:
        return

    desired_raw = str(config.get("data_path", "")).strip()
    if not desired_raw:
        try:
            _DATA_POINTER_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return

    desired = Path(desired_raw).expanduser().resolve()
    desired.mkdir(parents=True, exist_ok=True)
    target_config = desired / "config.json"
    if target_config.resolve() != CONFIG_FILE.resolve():
        _atomic_write_json(target_config, config)
    _DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(_DATA_POINTER_FILE, {"data_path": str(desired)})


# === Network port ===

_ACTIVE_PORT = int(os.environ.get("USDB_PORT", "5776"))


def get_active_port():
    return _ACTIVE_PORT
