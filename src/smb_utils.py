r"""
SMB-safe filesystem operations.

These wrappers handle common SMB/SMB2/SMB3 issues:
- Path.replace() is NOT atomic on SMB -> use copy+unlink with retry
- Directory rename fails on locked files -> use copytree+rmtree
- Long paths (>260 chars) -> use \\?\ prefix on Windows
- Transient network errors -> retry with exponential backoff
"""

import os
import sys
import shutil
import time
import uuid
import tempfile
from pathlib import Path

MAX_RETRIES = 3
RETRY_DELAYS = [0.5, 1.0, 2.0]  # seconds, exponential


def _long_path(path):
    r"""Add \\?\ prefix on Windows for paths >260 chars. No-op on POSIX."""
    if sys.platform == "win32":
        s = str(path)
        if len(s) > 255 and not s.startswith("\\\\?\\"):
            # Normalize separators
            s = os.path.normpath(s)
            if s.startswith("\\\\"):
                # UNC path: \\?\UNC\server\share...
                return "\\\\?\\UNC\\" + s[2:]
            return "\\\\?\\" + s
    return str(path)


def _retry(fn, *args, **kwargs):
    """Run fn with retries on transient OS/IO errors."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except (OSError, IOError) as exc:
            last_exc = exc
            # Error codes that indicate "try again"
            retry_errors = {
                13,   # EACCES - file in use (SMB mandatory locking)
                32,   # EPIPE / sharing violation
                53,   # ENETDOWN - network path not found
                64,   # EHOSTDOWN
                121,  # SEM_TIMEOUT
                123,  # ERROR_INVALID_NAME (sometimes transient on SMB)
                145,  # ERROR_DIR_NOT_EMPTY (SMB delayed delete)
                1921, # ERROR_FILE_LOCKED
            }
            err_no = getattr(exc, "errno", None) or getattr(exc, "winerror", None)
            if err_no not in retry_errors and attempt == MAX_RETRIES - 1:
                raise
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                time.sleep(delay)
    raise last_exc


def safe_replace(src, dst):
    """Atomically replace dst with src. Falls back to copy+unlink on SMB.

    On NTFS: os.replace is atomic.
    On SMB: os.replace may fail if the target is locked.
            We retry, then fall back to copy+unlink.
    """
    src = _long_path(src)
    dst = _long_path(dst)
    try:
        return _retry(os.replace, src, dst)
    except OSError:
        pass
    # Fallback: copy over target, then remove source
    _retry(shutil.copy2, src, dst)
    try:
        os.unlink(src)
    except OSError:
        pass


def safe_move(src, dst):
    """Move a file safely. Uses copy+unlink (never os.rename)

    shutil.move falls back to copy+delete on cross-device,
    but on same-device it uses os.rename which can fail on SMB.
    We always copy+unlink for reliability.
    """
    src_path = Path(_long_path(src))
    dst_path = Path(_long_path(dst))
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    _retry(shutil.copy2, str(src_path), str(dst_path))
    try:
        _retry(os.unlink, str(src_path))
    except OSError:
        pass


def safe_rename_dir(src, dst):
    """Rename a directory safely. Falls back to copytree+rmtree.

    On SMB, os.rename on directories fails if any file inside is locked.
    """
    src_p = Path(_long_path(src))
    dst_p = Path(_long_path(dst))

    # Try rename first (fast path, works on local NTFS)
    try:
        return _retry(os.rename, str(src_p), str(dst_p))
    except OSError:
        pass

    # Fallback: copy entire tree, then remove source
    if dst_p.exists():
        raise FileExistsError(f"Ziel existiert bereits: {dst_p}")
    _retry(shutil.copytree, str(src_p), str(dst_p))

    # Remove source with retries (SMB delayed delete)
    for attempt in range(MAX_RETRIES):
        try:
            shutil.rmtree(src_p, ignore_errors=False)
            return
        except OSError:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAYS[attempt])
            # Last resort: mark for deletion on next access
            try:
                for f in src_p.rglob("*"):
                    if f.is_file():
                        _retry(os.unlink, str(f))
                shutil.rmtree(src_p, ignore_errors=True)
            except Exception:
                pass


def safe_rmtree(path):
    """Remove a directory tree with retries for SMB delayed deletes."""
    path = Path(_long_path(path))
    if not path.exists():
        return
    for attempt in range(MAX_RETRIES):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except OSError:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAYS[attempt])
    # Last resort: best-effort
    shutil.rmtree(path, ignore_errors=True)


def safe_atomic_write_json(path, data):
    """Write JSON to a temp file in the same directory, then safely replace.

    Uses copy+unlink fallback on SMB.
    """
    import json
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with open(_long_path(tmp), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        safe_replace(tmp, path)
    except Exception:
        try:
            os.unlink(_long_path(tmp))
        except OSError:
            pass
        raise
