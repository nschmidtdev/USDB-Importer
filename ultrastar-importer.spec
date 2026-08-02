# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for UltraStar Importer standalone exe."""

from importlib.util import find_spec
from pathlib import Path
from pkgutil import iter_modules


block_cipher = None

# yt-dlp ships extractors for hundreds of platforms. UltraStar Importer accepts
# only validated YouTube URLs, so retain yt-dlp's lazy registry plus the modules
# that are imported directly and exclude every unrelated platform extractor.
_yt_extractor_spec = find_spec("yt_dlp.extractor")
_yt_extractor_dir = Path(next(iter(_yt_extractor_spec.submodule_search_locations)))
_yt_extractor_keep = {
    "adobepass", "common", "extractors", "generic", "lazy_extractors",
    "openload", "youtube",
}
_yt_extractor_excludes = [
    f"yt_dlp.extractor.{module.name}"
    for module in iter_modules([str(_yt_extractor_dir)])
    if module.name not in _yt_extractor_keep
]

a = Analysis(
    ["main.py"],
    pathex=["src"],
    binaries=[],
    datas=[
        ("static", "static"),
        ("config.example.json", "."),
        ("LICENSE", "."),
        ("DISCLAIMER.md", "."),
        ("THIRD_PARTY_NOTICES.md", "."),
        ("THIRD_PARTY_LICENSES.txt", "."),
    ],
    hiddenimports=[
        "webview",
        "webview.platforms.edgechromium",
        "yt_dlp",
        "yt_dlp.extractor.youtube",
        "yt_dlp.extractor.generic",
        "keyring.backends",
        "keyring.backends.Windows",
        "login_window",
        "smb_utils",
        "meta_tags",
        "postprocessing",
        "image_processing",
        "browser_cookies",
        "status",
        "mutagen",
        "mutagen.id3",
        "mutagen.mp3",
        "mutagen.mp4",
        "mutagen.oggvorbis",
        "browser_cookie3",
        "ffmpeg_normalize",
        "PIL",
        "PIL.Image",
        "PIL.ImageOps",
        "PIL.ImageEnhance",
        "concurrent.futures",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Build-only tools — never needed at runtime
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        # Added: build/dev tools that PyInstaller pulls in transitively
        "pip",
        "setuptools",
        "PyInstaller",
        "test",
        "unittest",
        "pydoc_data",
        # PIL formats we never use (only need JPEG/PNG read for covers)
        "PIL.ImageQt",
        "PIL.ImageTk",
        "PIL.ImageCms",
        "PIL.ImageDraw2",
        "PIL.ImageFilter",
        "PIL.ImageFont",
        "PIL.ImageGrab",
        "PIL.ImageMath",
        "PIL.ImageMorph",
        "PIL.ImageWin",
        "PIL.BmpImagePlugin",
        "PIL.MicImagePlugin",
        "PIL.FliImagePlugin",
        "PIL.TiffImagePlugin",
        "PIL.WebPImagePlugin",
        "PIL.GifImagePlugin",
        "PIL.ImImagePlugin",
        "PIL.PcxImagePlugin",
        "PIL.PsdImagePlugin",
        "PIL.IcoImagePlugin",
        "PIL.Jpeg2KImagePlugin",
        "PIL.SunImagePlugin",
        "PIL.TgaImagePlugin",
        "PIL.XpmImagePlugin",
        "PIL.XbmImagePlugin",
        # Cryptodome modules yt-dlp doesn't use
        "Cryptodome.Signature",
        "Cryptodome.Protocol",
        # Test/debug modules
        "doctest",
        "pdb",
        "profile",
        "pstats",
    ] + _yt_extractor_excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --- Post-analysis binary pruning ---
# Remove heavy native .pyd/.dll files we don't need at runtime.
_strip_binaries = {
    "PIL/_avif",         # 1.8 MB — AVIF codec, never used
    "PIL/_imagingcms",   # ICC color management, not needed
    "PIL/_imagingtk",    # Tkinter binding, excluded
    "PIL/_imagingqt",    # Qt binding, excluded
}
def _should_strip(name):
    # Normalise to forward slashes, strip leading ./ etc.
    n = name.replace("\\", "/").lstrip("./")
    # Match as prefix (handles PIL/_avif.cp312-win_amd64.pyd etc.)
    return any(n.startswith(s) for s in _strip_binaries)

_before = len(a.binaries)
a.binaries = [b for b in a.binaries if not _should_strip(b[0])]
_removed = _before - len(a.binaries)
if _removed:
    print(f"[spec] Stripped {_removed} unwanted binaries")

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="UltraStarImporter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    icon="static/favicon.ico",
    console=False,  # No console window - pure desktop app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
