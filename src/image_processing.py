"""Image postprocessing for covers and backgrounds.

Independent implementation using Pillow. Supports:
- Resize (with aspect ratio preservation)
- Crop (to a target rectangle)
- Auto-contrast enhancement
- Rotation fix (from EXIF orientation tag)
- Meta-tag-driven processing (from VideoMetaTag fields)

All operations are optional and degrade gracefully if Pillow is missing.
No code derived from any third-party project.
"""

from __future__ import annotations

import io
from typing import Optional

from meta_tags import VideoMetaTag


def process_cover_bytes(
    image_bytes: bytes,
    meta_tag: Optional[VideoMetaTag] = None,
    max_size: int = 0,
    autofix: bool = False,
) -> tuple[bytes, Optional[str]]:
    """Process cover/background image bytes.

    Applies meta-tag operations (crop, resize, rotate, contrast) first,
    then global settings (max_size, autofix).

    Returns (processed_bytes, error). On error, returns original bytes
    so the pipeline doesn't fail just because image processing failed.
    """
    try:
        from PIL import Image, ImageOps, ImageEnhance
    except ImportError:
        return image_bytes, "Pillow nicht installiert"

    if not image_bytes:
        return image_bytes, "Keine Bilddaten"

    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        return image_bytes, f"Bild konnte nicht geladen werden: {e}"

    try:
        # 1. EXIF orientation fix (always applied — cheap and correct)
        img = ImageOps.exif_transpose(img)

        # 2. Meta-tag-driven processing
        if meta_tag is not None:
            img = _apply_meta_ops(img, meta_tag, is_cover=True)

        # 3. Global resize (max_size = longest side in pixels)
        if max_size and max_size > 0:
            img = _resize_to_max(img, max_size)

        # 4. Auto-fix: contrast enhancement
        if autofix:
            img = ImageOps.autocontrast(img, cutoff=2)

        # Convert to RGB if necessary (for JPEG output)
        if img.mode in ("RGBA", "P", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Save as JPEG
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue(), None
    except Exception as e:
        return image_bytes, f"Bildverarbeitung fehlgeschlagen: {e}"


def _apply_meta_ops(img, meta: VideoMetaTag, is_cover: bool = True):
    """Apply crop/resize/rotate/contrast from a VideoMetaTag's cover or bg fields."""
    from PIL import ImageEnhance

    # Pick the right field set
    if is_cover:
        crop = meta.cover_crop
        resize = meta.cover_resize
        rotate = meta.cover_rotate
        contrast = meta.cover_contrast
    else:
        crop = meta.bg_crop
        resize = meta.bg_resize
        rotate = meta.bg_rotate
        contrast = meta.bg_contrast

    # Crop first (before resize, so crop coordinates match the original)
    if crop:
        x, y, w, h = crop
        img = img.crop((x, y, x + w, y + h))

    # Resize
    if resize:
        img = img.resize(resize, _resample_filter())

    # Rotate
    if rotate is not None and rotate != 0:
        img = img.rotate(-rotate, expand=True)

    # Contrast
    if contrast is not None:
        if contrast == "auto":
            from PIL import ImageOps
            img = ImageOps.autocontrast(img, cutoff=2)
        else:
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(contrast)

    return img


def _resize_to_max(img, max_size: int):
    """Resize so the longest side is at most max_size pixels."""
    w, h = img.size
    if w <= max_size and h <= max_size:
        return img
    if w >= h:
        new_w = max_size
        new_h = int(h * (max_size / w))
    else:
        new_h = max_size
        new_w = int(w * (max_size / h))
    return img.resize((new_w, new_h), _resample_filter())


def _resample_filter():
    """Return the best available resampling filter."""
    try:
        from PIL import Image
        return Image.Resampling.LANCZOS
    except AttributeError:
        from PIL import Image
        return Image.LANCZOS


def download_image_from_tag(
    meta: VideoMetaTag,
    is_cover: bool = True,
    cookie: str = "",
) -> tuple[Optional[bytes], Optional[str]]:
    """Download an image referenced in a VideoMetaTag.

    Returns (image_bytes, error).
    """
    try:
        import requests
    except ImportError:
        return None, "requests nicht installiert"

    url = meta.cover_url() if is_cover else meta.bg_url()
    if not url:
        return None, "Keine Bild-URL im Meta-Tag"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if cookie:
        headers["Cookie"] = cookie

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 100:
            return resp.content, None
        return None, f"HTTP {resp.status_code} oder leer"
    except Exception as e:
        return None, str(e)
