"""Song folder building, UltraStar .txt parsing, local scanning, and matching.

Also provides validate_output_path() which the worker uses before writing.
"""

import uuid
from pathlib import Path

from config import DATA_DIR
from utils import sanitize_filename, normalize
from smb_utils import safe_move, safe_rename_dir, safe_rmtree


# === Output path validation (Task 4) ===

def validate_output_path(output_base):
    """Normalize, ensure the directory exists, and verify it is writable.

    Returns (normalized_path_str, error). error is None on success.
    """
    if not output_base:
        output_base = str(DATA_DIR / "output")
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


# === .txt patching ===

def _patch_txt_content(txt_content, base_name, video_filename=None,
                       audio_filename=None):
    """Patch #MP3/#VIDEO lines to reference local files.

    - #MP3 references the actual audio file. If ``audio_filename`` is given
      it is used as-is; otherwise it falls back to <base_name>.mp3.
      This is critical because yt-dlp may produce .m4a/.ogg/.opus and the
      #MP3 header MUST match the real filename or UltraStar can't find it.
    - If a local video file exists, #VIDEO is set (or inserted) to reference it,
      replacing any stale USDB youtube-id reference. #VIDEOGAP:0 is added when
      a video is present and none exists yet.
    - If no local video is expected, an existing #VIDEO line is left untouched.
    """
    mp3_ref = audio_filename or f"{base_name}.mp3"
    lines = txt_content.split("\n")
    patched = []
    saw_video = False
    for line in lines:
        if line.startswith("#MP3:"):
            patched.append(f"#MP3:{mp3_ref}")
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


# === Song folder build ===

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
        # Determine actual filenames BEFORE moving (the move deletes the source).
        # This is critical: if yt-dlp saved audio.m4a or audio.ogg, the
        # #MP3 header MUST reference that exact name or UltraStar won't find it.
        audio_filename = Path(audio_path).name if audio_path else None
        video_filename = None
        if video_path and Path(video_path).exists():
            video_filename = Path(video_path).name

        # Move already-downloaded audio/video into the partial folder.
        if video_path:
            src = Path(video_path)
            if src.exists():
                dst = partial_folder / src.name
                safe_move(str(src), str(dst))
        if audio_path:
            src = Path(audio_path)
            if src.exists():
                dst = partial_folder / src.name
                safe_move(str(src), str(dst))

        # Patch the txt to reference local media.
        txt_content = _patch_txt_content(txt_content, base_name, video_filename,
                                         audio_filename)

        # Write .txt
        txt_path = partial_folder / f"{base_name}.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(txt_content)

        # Write cover
        if cover_bytes:
            cover_path = partial_folder / f"{base_name} [CO].jpg"
            with open(cover_path, "wb") as f:
                f.write(cover_bytes)

        # Write _links file with source URLs for reference.
        # Named without .txt extension so UltraStar scanners don't pick it up as a song.
        usdb_url = f"https://usdb.animux.de/index.php?link=detail&id={song_id}"
        yt_url = data.get("youtube_url", "")
        links_path = partial_folder / "_links"
        with open(links_path, "w", encoding="utf-8") as f:
            f.write(f"USDB: {usdb_url}\n")
            if yt_url:
                f.write(f"YouTube: {yt_url}\n")
    except Exception:
        # Clean up partial folder on any failure.
        safe_rmtree(partial_folder)
        raise

    # Safe collision policy: an existing song is never overwritten or mixed
    # with new assets. The default is deliberately "skip" until a versioning
    # policy is exposed explicitly in the product UI.
    if final_folder.exists():
        safe_rmtree(partial_folder)
        raise FileExistsError(f"Song existiert bereits und wurde uebersprungen: {final_folder}")
    try:
        safe_rename_dir(partial_folder, final_folder)
    except OSError:
        safe_rmtree(partial_folder)
        raise
    return str(final_folder)


# === UltraStar .txt parser ===

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


# === Local scanner ===

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
                "has_mp3": any(folder.glob(p) for p in
                               ("*.mp3", "*.m4a", "*.aac", "*.ogg", "*.opus", "*.wma", "*.flac")),
                "has_video": any(folder.glob(p) for p in
                                 ("*.mp4", "*.avi", "*.webm", "*.mkv", "*.mov", "*.mpg", "*.mpeg", "*.ts")),
                "has_cover": bool(any(folder.glob("*cover*"))
                                  or any("[CO]" in f.name for f in folder.iterdir() if f.is_file())),
                "has_background": bool(any(folder.glob("*bg*"))
                                       or any(folder.glob("*background*"))),
            }
            parsed["folder"] = str(folder)
            results.append(parsed)
    return results

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


# === Typed Song-TXT Parser ===
#
# A strict, validating parser for UltraStar .txt files. Unlike the legacy
# parse_ultrastar_txt() (which returns a flat dict of strings), this produces
# a SongTxt dataclass with typed fields, numeric validation, and note-line
# syntax checking. The legacy function remains for backward compatibility.

import re as _re
from dataclasses import dataclass as _dataclass, field as _field
from typing import Optional as _Optional


# Note-line regex: TYPE BEAT LENGTH PITCH TEXT
# Type: : F R G * (regular, freestyle, golden, phrase)
# Example: ": 0 1 60 Hello"
_NOTE_RE = _re.compile(
    r"^([:FGR\*])\s+(\d+)\s+(\d+)\s+(-?\d+)(?:\s+(.*))?$"
)

# Headers that carry numeric values
_NUMERIC_HEADERS = {"BPM", "GAP", "VIDEOGAP", "PREVIEWSTART",
                     "MEDLEYSTARTBEAT", "MEDLEYENDBEAT",
                     "START", "END"}

# Valid #RELATIVE values
_VALID_RELATIVE = {"yes", "no", "0", "1"}


@_dataclass
class SongTxt:
    """Typed representation of an UltraStar .txt file.

    String fields default to "" (empty). Numeric fields default to None
    (absent in the file). ``warnings`` collects non-fatal validation issues.
    ``note_count`` is the total number of note lines parsed.
    """
    # --- String headers ---
    artist: str = ""
    title: str = ""
    language: str = ""
    edition: str = ""
    genre: str = ""
    album: str = ""
    year: str = ""
    mp3: str = ""
    video: str = ""
    cover: str = ""
    background: str = ""
    vocals: str = ""        # #VOCALS: path to vocals track
    instrument: str = ""    # #INSTRUMENT: path to instrumental track
    encoding: str = ""      # #ENCODING: UTF-8, CP1252, etc.
    comment: str = ""

    # --- Numeric headers ---
    bpm: _Optional[float] = None
    gap: _Optional[float] = None
    videogap: _Optional[float] = None
    previewstart: _Optional[float] = None
    medleystartbeat: _Optional[int] = None
    medleyendbeat: _Optional[int] = None
    start: _Optional[float] = None
    end: _Optional[float] = None

    # --- Flags ---
    relative: bool = False
    source: str = ""        # #SOURCE: USDB id or URL

    # --- Parsed note info ---
    note_count: int = 0
    note_types: dict = _field(default_factory=lambda: {"R": 0, "G": 0, "F": 0, "P": 0})

    # --- Validation ---
    warnings: list = _field(default_factory=list)
    encoding_detected: str = ""

    def to_dict(self) -> dict:
        """Convert to a flat dict compatible with parse_ultrastar_txt format."""
        d = {}
        for attr in ("artist", "title", "language", "edition", "genre", "album",
                      "year", "mp3", "video", "cover", "background", "vocals",
                      "instrument", "encoding", "comment", "source"):
            val = getattr(self, attr)
            if val:
                d[attr] = val
        for attr in ("bpm", "gap", "videogap", "previewstart",
                      "medleystartbeat", "medleyendbeat", "start", "end"):
            val = getattr(self, attr)
            if val is not None:
                d[attr] = val
        d["relative"] = self.relative
        d["note_count"] = self.note_count
        d["warnings"] = list(self.warnings)
        return d


def _detect_encoding(raw_bytes: bytes) -> str:
    """Detect encoding from raw bytes using BOM or #ENCODING header."""
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw_bytes.startswith(b"\xff\xfe") or raw_bytes.startswith(b"\xfe\xff"):
        return "utf-16"
    # Look for an explicit #ENCODING header in the first 2KB (latin-1 safe read)
    head = raw_bytes[:2048].decode("ascii", errors="ignore")
    for line in head.split("\n"):
        line = line.strip()
        if line.upper().startswith("#ENCODING:"):
            enc = line[len("#ENCODING:"):].strip()
            return enc if _is_valid_codec(enc) else "utf-8"
        if line and not line.startswith("#"):
            break
    return "utf-8"


def _is_valid_codec(name: str) -> bool:
    try:
        f"test".encode(name)
        return True
    except (LookupError, TypeError):
        return False


def parse_song_txt_typed(filepath) -> SongTxt:
    """Parse an UltraStar .txt file into a validated SongTxt dataclass.

    Reads the file with encoding auto-detection (BOM, #ENCODING header, or
    UTF-8 fallback). Validates numeric headers, counts note lines, and
    collects non-fatal warnings.
    """
    from pathlib import Path as _P
    p = _P(filepath)
    raw = p.read_bytes()
    enc = _detect_encoding(raw)
    result = SongTxt()
    result.encoding_detected = enc

    try:
        text = raw.decode(enc, errors="replace")
    except (LookupError, TypeError):
        text = raw.decode("utf-8", errors="replace")
        result.warnings.append(f"Encoding '{enc}' nicht verfügbar, UTF-8 verwendet")

    seen_headers = set()

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Note line? (doesn't start with #)
        if not line.startswith("#"):
            m = _NOTE_RE.match(line)
            if m:
                note_type = m.group(1)
                result.note_count += 1
                # Map type char to category: : = Regular, F = Freestyle,
                # G = Golden, R = Rap, * = Phrase
                if note_type == ":":
                    result.note_types["R"] += 1  # Regular
                elif note_type == "F":
                    result.note_types["F"] += 1  # Freestyle
                elif note_type == "G":
                    result.note_types["G"] += 1  # Golden
                elif note_type == "*":
                    result.note_types["P"] += 1  # Phrase
                continue
            # Non-header, non-note line — could be a lyric continuation
            # or a malformed note. Warn but don't fail.
            if line and line[0] not in ("-", "E"):
                result.warnings.append(f"Unbekannte Zeile: {line[:60]}")
            continue

        # Header line
        if ":" not in line:
            continue
        tag, val = line[1:].split(":", 1)
        tag = tag.strip().upper()
        val = val.strip()
        if not val and tag not in ("COMMENT",):
            continue

        seen_headers.add(tag)

        # Route to the right field
        if   tag == "ARTIST":         result.artist = val
        elif tag == "TITLE":          result.title = val
        elif tag == "LANGUAGE":       result.language = val
        elif tag == "EDITION":        result.edition = val
        elif tag == "GENRE":          result.genre = val
        elif tag == "ALBUM":          result.album = val
        elif tag == "YEAR":           result.year = val
        elif tag == "MP3":            result.mp3 = val
        elif tag == "VIDEO":          result.video = val
        elif tag == "COVER":          result.cover = val
        elif tag == "BACKGROUND":     result.background = val
        elif tag == "VOCALS":         result.vocals = val
        elif tag == "INSTRUMENT":     result.instrument = val
        elif tag == "ENCODING":       result.encoding = val
        elif tag == "COMMENT":        result.comment = val
        elif tag == "SOURCE":         result.source = val
        elif tag == "RELATIVE":       result.relative = val.lower() in ("yes", "1")
        elif tag == "BPM":
            result.bpm = _try_float(val, tag, result.warnings)
            if result.bpm is not None and result.bpm <= 0:
                result.warnings.append(f"BPM={result.bpm} ist ≤0 (ungültig)")
        elif tag == "GAP":
            result.gap = _try_float(val, tag, result.warnings)
            if result.gap is not None and result.gap < 0:
                result.warnings.append(f"GAP={result.gap} ist negativ")
        elif tag == "VIDEOGAP":
            result.videogap = _try_float(val, tag, result.warnings)
        elif tag == "PREVIEWSTART":
            result.previewstart = _try_float(val, tag, result.warnings)
        elif tag == "MEDLEYSTARTBEAT":
            result.medleystartbeat = _try_int(val, tag, result.warnings)
        elif tag == "MEDLEYENDBEAT":
            result.medleyendbeat = _try_int(val, tag, result.warnings)
        elif tag == "START":
            result.start = _try_float(val, tag, result.warnings)
        elif tag == "END":
            result.end = _try_float(val, tag, result.warnings)
        # Unknown headers silently ignored (future-proof)

    # Cross-field validation
    if result.bpm is None:
        result.warnings.append("Kein #BPM Header gefunden")
    if result.title and not result.artist:
        result.warnings.append("Titel ohne Artist")
    if result.medleystartbeat is not None and result.medleyendbeat is not None:
        if result.medleystartbeat >= result.medleyendbeat:
            result.warnings.append(
                f"Medley-Start ({result.medleystartbeat}) ≥ End ({result.medleyendbeat})"
            )

    return result


def _try_float(val: str, tag: str, warnings: list) -> _Optional[float]:
    try:
        return float(val)
    except ValueError:
        warnings.append(f"#{tag} ist keine Zahl: '{val}'")
        return None


def _try_int(val: str, tag: str, warnings: list) -> _Optional[int]:
    try:
        return int(val)
    except ValueError:
        warnings.append(f"#{tag} ist keine ganze Zahl: '{val}'")
        return None
