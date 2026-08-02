import sys
from types import SimpleNamespace

import pytest

import youtube


class _FakeYoutubeDL:
    calls = []

    def __init__(self, options):
        self.options = options
        self.__class__.calls.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def download(self, _urls):
        return 0


@pytest.mark.parametrize(
    ("download", "extra"),
    [
        (youtube.download_youtube_audio, {}),
        (youtube.download_youtube_video, {}),
    ],
)
def test_downloads_allow_only_youtube_extractors(monkeypatch, tmp_path, download, extra):
    _FakeYoutubeDL.calls.clear()
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=_FakeYoutubeDL))

    download("https://www.youtube.com/watch?v=dQw4w9WgXcQ", str(tmp_path / "media"), **extra)

    assert _FakeYoutubeDL.calls
    assert _FakeYoutubeDL.calls[0]["allowed_extractors"] == ["youtube.*"]
