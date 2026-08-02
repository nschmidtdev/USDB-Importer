"""Audio postprocessing: normalization and metadata tag writing.

Clean-room implementation. Uses ffmpeg-normalize (EBU R128 loudnorm) and
mutagen for ID3/M4A/OGG tag writing.

Both features are optional — if the dependency is missing, the function
returns gracefully without failing the download pipeline.
"""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path
from typing import Optional


# === Audio normalization ===

def normalize_audio(filepath: str, method: str = "loudnorm") -> tuple[bool, Optional[str]]:
    """Normalize audio loudness using ffmpeg-normalize.

    ``method``: "loudnorm" (EBU R128) or "replaygain".
    Returns (success, error).
    """
    try:
        from ffmpeg_normalize import FFmpegNormalize, ffmpeg_env
    except ImportError:
        return False, "ffmpeg-normalize nicht installiert"

    # Check ffmpeg binary is available
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg nicht im PATH"

    p = Path(filepath)
    if not p.exists():
        return False, f"Datei nicht gefunden: {filepath}"

    tmp_output = str(p.with_suffix(".normalized" + p.suffix))
    try:
        with ffmpeg_env({}):
            normalizer = FFmpegNormalize(
                normalization=method,
                true_peak=-2.0 if method == "loudnorm" else None,
                keep_loudness_range_target=False,
                dual_mono=False,
            )
            normalizer.add_media_file(filepath, tmp_output)
            normalizer.run_normalization()
    except Exception as e:
        # Clean up partial output
        tmp = Path(tmp_output)
        if tmp.exists():
            tmp.unlink()
        return False, str(e)

    # Replace original with normalized version
    try:
        shutil.move(tmp_output, filepath)
    except Exception as e:
        return False, f"Kann normalisierte Datei nicht übernehmen: {e}"

    return True, None


def normalize_with_ffmpeg_directly(
    filepath: str, method: str = "loudnorm"
) -> tuple[bool, Optional[str]]:
    """Alternative normalization using ffmpeg directly (no ffmpeg-normalize).

    This is a simpler fallback that doesn't need the ffmpeg-normalize package,
    but produces slightly less precise results (single-pass loudnorm).

    Returns (success, error).
    """
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg nicht im PATH"

    p = Path(filepath)
    if not p.exists():
        return False, f"Datei nicht gefunden: {filepath}"

    tmp_output = str(p.with_suffix(".norm" + p.suffix))

    if method == "loudnorm":
        # EBU R128 single-pass (less precise than two-pass ffmpeg-normalize)
        af_filter = "loudnorm=I=-16:TP=-1.5:LRA=11"
    elif method == "replaygain":
        af_filter = "volume=replaygain_track_gain"
    else:
        return False, f"Unbekannte Methode: {method}"

    cmd = [
        "ffmpeg", "-y", "-i", filepath,
        "-af", af_filter,
        "-c:a", "libmp3lame", "-q:a", "2",
        tmp_output,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            tmp = Path(tmp_output)
            if tmp.exists():
                tmp.unlink()
            return False, f"ffmpeg Fehler: {result.stderr[-300:]}"
    except subprocess.TimeoutExpired:
        return False, "ffmpeg Timeout (5min)"
    except Exception as e:
        return False, str(e)

    try:
        shutil.move(tmp_output, filepath)
    except Exception as e:
        return False, f"Kann normalisierte Datei nicht übernehmen: {e}"

    return True, None


# === Audio metadata tags ===

# Tag fields we write (mapped from song metadata dict)
_AUDIO_TAG_KEYS = {
    "artist": "artist",
    "title": "title",
    "album": "album",
    "genre": "genre",
    "year": "date",
    "language": "language",
}


def write_audio_tags(filepath: str, metadata: dict) -> tuple[bool, Optional[str]]:
    """Write ID3/M4A/OGG metadata tags into an audio file.

    Uses mutagen. Detects the format from the file extension.
    Skips unknown formats gracefully.
    Returns (success, error).
    """
    try:
        from mutagen import File as MutagenFile
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, TDRC, TLAN, error as ID3Error
        from mutagen.mp3 import MP3
        from mutagen.mp4 import MP4, MP4Tags
        from mutagen.ogg import OggFileType
    except ImportError:
        return False, "mutagen nicht installiert"

    p = Path(filepath)
    if not p.exists():
        return False, f"Datei nicht gefunden: {filepath}"

    ext = p.suffix.lower()
    try:
        if ext == ".mp3":
            return _write_mp3_tags(filepath, metadata)
        elif ext == ".m4a":
            return _write_m4a_tags(filepath, metadata)
        elif ext in (".ogg", ".opus"):
            return _write_ogg_tags(filepath, metadata)
        else:
            return True, None  # unknown format: skip silently
    except Exception as e:
        return False, str(e)


def _build_tag_dict(metadata: dict) -> dict:
    """Extract relevant fields from song metadata dict."""
    tags = {}
    for meta_key, tag_key in _AUDIO_TAG_KEYS.items():
        val = metadata.get(meta_key)
        if val:
            tags[tag_key] = str(val)
    return tags


def _write_mp3_tags(filepath: str, metadata: dict) -> tuple[bool, Optional[str]]:
    """Write ID3v2 tags into an MP3 file."""
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, TDRC, TLAN

    tags = _build_tag_dict(metadata)
    try:
        audio = ID3(filepath)
    except Exception:
        audio = ID3()

    tag_map = {
        "artist": TPE1, "title": TIT2, "album": TALB,
        "genre": TCON, "date": TDRC, "language": TLAN,
    }
    for key, val in tags.items():
        frame_cls = tag_map.get(key)
        if frame_cls:
            audio.add(frame_cls(encoding=3, text=[val]))
    try:
        audio.save(filepath)
    except Exception as e:
        return False, f"ID3 Save: {e}"
    return True, None


def _write_m4a_tags(filepath: str, metadata: dict) -> tuple[bool, Optional[str]]:
    """Write M4A/MP4 tags."""
    from mutagen.mp4 import MP4

    tags = _build_tag_dict(metadata)
    audio = MP4(filepath)
    # M4A uses different keys: \xa9ART for artist, \xa9nam for title, etc.
    m4a_map = {
        "artist": "\xa9ART",
        "title": "\xa9nam",
        "album": "\xa9alb",
        "genre": "\xa9gen",
        "date": "\xa9day",
    }
    for key, val in tags.items():
        m4a_key = m4a_map.get(key)
        if m4a_key:
            audio[m4a_key] = [val]
    try:
        audio.save()
    except Exception as e:
        return False, f"M4A Save: {e}"
    return True, None


def _write_ogg_tags(filepath: str, metadata: dict) -> tuple[bool, Optional[str]]:
    """Write OGG/Opus tags."""
    from mutagen import File as MutagenFile

    tags = _build_tag_dict(metadata)
    audio = MutagenFile(filepath)
    if audio is None:
        return False, "Mutagen konnte Datei nicht öffnen"
    for key, val in tags.items():
        audio[key] = val
    try:
        audio.save()
    except Exception as e:
        return False, f"OGG Save: {e}"
    return True, None
