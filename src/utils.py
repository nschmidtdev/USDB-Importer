"""Pure helper functions with no internal dependencies.

These are the leaf-level utilities used across all other modules.
"""

import json
import re
import shutil
import time
import unicodedata
from pathlib import Path


# === File size ===

def _file_size_mb(path):
    """Return file size in MB, 0 if not exists."""
    try:
        return Path(path).stat().st_size / 1048576
    except (OSError, ValueError):
        return 0


# === Atomic JSON I/O ===

def _atomic_write_json(path, data):
    """Write JSON to a temp file in the same directory, then atomically replace.

    Uses SMB-safe operations (copy+unlink fallback, retries).
    """
    from smb_utils import safe_atomic_write_json
    safe_atomic_write_json(path, data)


def _load_json_safe(path, fallback, on_corrupt=None):
    """Load JSON, falling back gracefully on corrupt files.

    ``fallback`` is a callable returning the default value.
    ``on_corrupt(path, exc)`` is called when a JSONDecodeError is caught, so the
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


# === Filename / string helpers ===

def sanitize_filename(name):
    """Remove characters that are invalid in Windows filenames."""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "")
    return name.strip().rstrip(".")


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
