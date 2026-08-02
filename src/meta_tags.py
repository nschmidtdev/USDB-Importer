"""Parser for overloaded #VIDEO tags in UltraStar song files.

Independent clean-room implementation based solely on the public USDB
meta-tag grammar (comma-separated key=value pairs). The grammar is
documented in the USDB community wiki and is not proprietary to any
software project.

Design: flat single-class with primitive fields (strings, tuples, floats).
Parsing uses a key-dispatch table rather than nested class hierarchies.
No code, structure, or naming was derived from any third-party project.

Grammar summary:
    #VIDEO:a=<url>,v=<url>,co=<img>,co-crop=<x>-<y>-<w>-<h>,
            co-resize=<w>-<h>,co-rotate=<deg>,co-contrast=<f|auto>,
            bg=<img>,bg-crop=...,bg-resize=...,
            p1=<name>,p2=<name>,preview=<sec>,medley=<start>-<end>,
            tags=<comma-separated>

=== Worker integration status (as of 2026-07) ===

  Key           Parsed   Active in worker.py   Notes
  ────────────  ──────   ───────────────────   ─────────────────────
  a             yes      ✓                     overrides yt audio URL
  v             yes      ✓                     overrides yt video URL
  co            yes      ✓                     cover source URL
  co-crop       yes      ✓                     cover crop via Pillow
  co-resize     yes      ✓                     cover resize via Pillow
  co-rotate     yes      ✓                     cover rotation
  co-contrast   yes      ✓                     cover autocontrast
  p1            yes      ✗                     duet P1 name (field only)
  p2            yes      ✗                     duet P2 name (field only)
  preview       yes      ✗                     preview start seconds
  medley        yes      ✗                     medley start-end beats
  tags          yes      ✗                     genre/mood tags
  bg            yes      ✗                     bg image (not downloaded)
  bg-crop       yes      ✗
  bg-resize     yes      ✗

Fields marked "✗" are parsed into VideoMetaTag but not yet wired into
the download pipeline. They can be accessed from worker.py when needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Percent-encoding table for the comma separator
_ENCODE = [(",", "%2C")]


def _esc(val: str) -> str:
    for ch, rep in _ENCODE:
        val = val.replace(ch, rep)
    return val


def _unesc(val: str) -> str:
    for ch, rep in _ENCODE:
        val = val.replace(rep, ch)
    return val


def _to_float(val: str) -> float | None:
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _to_int_pair(val: str) -> tuple[int, int] | None:
    try:
        a, b = val.split("-")
        return int(a), int(b)
    except (ValueError, TypeError):
        return None


def _to_crop_rect(val: str) -> tuple[int, int, int, int] | None:
    """Parse 'x-y-w-h' into (x, y, w, h)."""
    try:
        parts = val.split("-")
        if len(parts) != 4:
            return None
        return tuple(int(p) for p in parts)  # type: ignore
    except (ValueError, TypeError):
        return None


def _to_contrast(val: str) -> float | str | None:
    if val == "auto":
        return "auto"
    return _to_float(val)


def _resolve_image_url(raw: str) -> str:
    """Resolve a raw image reference to a full URL.

    - Full URL (contains '://') → returned as-is
    - URL without protocol (contains '/') → prepended with https://
    - Bare token (no '/', no '://') → treated as fanart.tv asset ID
    """
    if "://" in raw:
        return raw
    if "/" in raw:
        return f"https://{raw}"
    return f"https://assets.fanart.tv/fanart/{raw}"


# Sentinel for "no tag" (distinguishes empty string from absent)
_UNSET = object()


@dataclass
class VideoMetaTag:
    """Flat representation of an overloaded #VIDEO tag.

    Every field is a primitive or tuple — no nested objects. Use
    ``VideoMetaTag.from_tag_string()`` to parse, and
    ``to_tag_string()`` to serialize back.

    The ``has_cover_ops`` / ``has_bg_ops`` properties check whether
    image-processing sub-tags (crop/resize/rotate/contrast) are set.
    """
    # --- Media sources ---
    audio_url: str = ""
    video_url: str = ""

    # --- Cover image ---
    cover_ref: str = ""              # raw URL or fanart ID
    cover_crop: tuple[int, int, int, int] | None = None   # x, y, w, h
    cover_resize: tuple[int, int] | None = None            # w, h
    cover_rotate: float | None = None
    cover_contrast: float | str | None = None

    # --- Background image ---
    bg_ref: str = ""
    bg_crop: tuple[int, int, int, int] | None = None
    bg_resize: tuple[int, int] | None = None
    bg_rotate: float | None = None
    bg_contrast: float | str | None = None

    # --- Duet / misc ---
    player1: str = ""
    player2: str = ""
    preview_start: float | None = None
    medley_beats: tuple[int, int] | None = None
    genre_tags: str = ""

    # --- Parsing ---

    @classmethod
    def from_tag_string(cls, raw: str | None) -> VideoMetaTag:
        """Parse a #VIDEO tag value. Returns an empty instance if the
        tag is a plain filename (no '=' present) or None."""
        inst = cls()
        if not raw or "=" not in raw:
            return inst
        for segment in raw.split(","):
            segment = segment.strip()
            if "=" not in segment:
                continue
            key, val = segment.split("=", maxsplit=1)
            inst._dispatch(key.lower(), _unesc(val))
        return inst

    def _dispatch(self, key: str, val: str) -> None:
        """Route a key=value pair to the right field via a flat if/elif chain."""
        if   key == "a":  self.audio_url = val
        elif key == "v":  self.video_url = val
        elif key == "p1": self.player1 = val
        elif key == "p2": self.player2 = val
        elif key == "preview": self.preview_start = _to_float(val)
        elif key == "medley":  self.medley_beats = _to_int_pair(val)
        elif key == "tags":    self.genre_tags = val
        # Cover sub-keys (only valid if 'co' was seen first)
        elif key == "co":                       self.cover_ref = val
        elif key == "co-crop" and self.cover_ref:    self.cover_crop = _to_crop_rect(val)
        elif key == "co-resize" and self.cover_ref:  self.cover_resize = _to_int_pair(val) or self._resize_single(val)
        elif key == "co-rotate" and self.cover_ref:  self.cover_rotate = _to_float(val)
        elif key == "co-contrast" and self.cover_ref: self.cover_contrast = _to_contrast(val)
        # Background sub-keys
        elif key == "bg":                       self.bg_ref = val
        elif key == "bg-crop" and self.bg_ref:       self.bg_crop = _to_crop_rect(val)
        elif key == "bg-resize" and self.bg_ref:     self.bg_resize = _to_int_pair(val) or self._resize_single(val)
        elif key == "bg-rotate" and self.bg_ref:     self.bg_rotate = _to_float(val)
        elif key == "bg-contrast" and self.bg_ref:   self.bg_contrast = _to_contrast(val)
        # Unknown keys: silently ignored

    @staticmethod
    def _resize_single(val: str) -> tuple[int, int] | None:
        """Handle 'co-resize=500' (square) → (500, 500)."""
        try:
            n = int(val)
            return (n, n)
        except (ValueError, TypeError):
            return None

    # --- Properties ---

    def cover_url(self) -> str:
        """Resolved cover image URL."""
        return _resolve_image_url(self.cover_ref) if self.cover_ref else ""

    def bg_url(self) -> str:
        """Resolved background image URL."""
        return _resolve_image_url(self.bg_ref) if self.bg_ref else ""

    def has_cover_ops(self) -> bool:
        """True if any cover post-processing sub-tag is set."""
        return any(v is not None for v in
                   (self.cover_crop, self.cover_resize, self.cover_rotate, self.cover_contrast))

    def has_bg_ops(self) -> bool:
        return any(v is not None for v in
                   (self.bg_crop, self.bg_resize, self.bg_rotate, self.bg_contrast))

    def is_empty(self) -> bool:
        """True if no structured field is set."""
        checks = [
            self.audio_url, self.video_url,
            self.cover_ref, self.bg_ref,
            self.player1, self.player2, self.genre_tags,
        ]
        if any(checks):
            return False
        optionals = [
            self.cover_crop, self.cover_resize, self.cover_rotate, self.cover_contrast,
            self.bg_crop, self.bg_resize, self.bg_rotate, self.bg_contrast,
            self.preview_start, self.medley_beats,
        ]
        return all(v is None for v in optionals)

    # --- Serialization ---

    def to_tag_string(self) -> str:
        """Serialize back to a #VIDEO tag value string."""
        parts: list[str] = []
        if self.audio_url: parts.append(f"a={_esc(self.audio_url)}")
        if self.video_url: parts.append(f"v={_esc(self.video_url)}")
        if self.cover_ref:
            parts.append(f"co={_esc(self.cover_ref)}")
            if self.cover_rotate is not None:  parts.append(f"co-rotate={self.cover_rotate}")
            if self.cover_crop is not None:    parts.append(f"co-crop={self._fmt_crop(self.cover_crop)}")
            if self.cover_resize is not None:  parts.append(f"co-resize={self._fmt_resize(self.cover_resize)}")
            if self.cover_contrast is not None: parts.append(f"co-contrast={self.cover_contrast}")
        if self.bg_ref:
            parts.append(f"bg={_esc(self.bg_ref)}")
            if self.bg_rotate is not None:  parts.append(f"bg-rotate={self.bg_rotate}")
            if self.bg_crop is not None:    parts.append(f"bg-crop={self._fmt_crop(self.bg_crop)}")
            if self.bg_resize is not None:  parts.append(f"bg-resize={self._fmt_resize(self.bg_resize)}")
            if self.bg_contrast is not None: parts.append(f"bg-contrast={self.bg_contrast}")
        if self.player1: parts.append(f"p1={_esc(self.player1)}")
        if self.player2: parts.append(f"p2={_esc(self.player2)}")
        if self.preview_start is not None: parts.append(f"preview={self.preview_start}")
        if self.medley_beats is not None:  parts.append(f"medley={self.medley_beats[0]}-{self.medley_beats[1]}")
        if self.genre_tags: parts.append(f"tags={_esc(self.genre_tags)}")
        return ",".join(parts)

    @staticmethod
    def _fmt_crop(rect: tuple[int, int, int, int]) -> str:
        return f"{rect[0]}-{rect[1]}-{rect[2]}-{rect[3]}"

    @staticmethod
    def _fmt_resize(dim: tuple[int, int]) -> str:
        if dim[0] != dim[1]:
            return f"{dim[0]}-{dim[1]}"
        return str(dim[0])
