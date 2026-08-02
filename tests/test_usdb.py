from bs4 import BeautifulSoup

from usdb import parse_detail


def _detail_with(link_html):
    return BeautifulSoup(
        '<html><head><title>USDB - Artist - Title</title></head><body>'
        + link_html
        + '</body></html>',
        'html.parser',
    )


def test_parse_detail_canonicalizes_youtube_links():
    detail = parse_detail(
        _detail_with('<a href="https://youtu.be/dQw4w9WgXcQ?t=3">Video</a>'),
        '42',
    )
    assert detail['youtube_url'] == 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'


def test_parse_detail_rejects_executable_youtube_attribute_data():
    detail = parse_detail(
        _detail_with('<a href="https://youtube.com/watch?v=bad%27);alert(1)//">Video</a>'),
        '42',
    )
    assert 'youtube_url' not in detail
