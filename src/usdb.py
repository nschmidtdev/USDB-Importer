"""USDB scraping: detail pages, .txt downloads, cover downloads, search.

All network functions return (result, error) tuples where error is None on
success. Uses BeautifulSoup for HTML parsing.
"""

import re
from urllib.parse import urljoin, urlparse, parse_qs

try:
    import requests as req_lib
    from bs4 import BeautifulSoup
except ImportError:
    print("pip install requests beautifulsoup4")
    raise

from config import USDB_BASE, USDB_DETAIL


# === URL regex (used by worker validation) ===

USDB_URL_RE = re.compile(
    r"^https?://(?:www\.)?usdb\.animux\.de/", re.IGNORECASE
)


# === ID extraction ===

def extract_usdb_id(url):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "id" in qs:
        song_id = qs["id"][0]
        if not song_id.isdigit():
            return None
        return song_id
    m = re.search(r"id=(\d+)", url)
    return m.group(1) if m else None


# === YouTube URL normalization ===

_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}


def _canonical_youtube_url(raw_url):
    """Return a strict watch URL or None for malformed/untrusted input."""
    if not isinstance(raw_url, str):
        return None
    candidate = raw_url.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None

    host = (parsed.hostname or "").lower()
    video_id = None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host in _YOUTUBE_HOSTS:
        if parsed.path.rstrip("/") == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif parsed.path.startswith("/embed/"):
            video_id = parsed.path[len("/embed/"):].split("/", 1)[0]

    if not video_id or not _YOUTUBE_VIDEO_ID_RE.fullmatch(video_id):
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


# === Detail page fetch + parse ===

def fetch_detail(song_id, cookie_string, session=None):
    if session is None:
        session = req_lib.Session()
    url = USDB_BASE + USDB_DETAIL.format(song_id)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_string,
    }
    try:
        resp = session.get(url, headers=headers, timeout=15)
    except Exception as e:
        return None, str(e)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    soup = BeautifulSoup(resp.text, "html.parser")
    if "Datensatz nicht gefunden" in soup.get_text():
        return None, "Nicht eingeloggt oder ID existiert nicht"
    return parse_detail(soup, song_id), None


def parse_detail(soup, song_id):
    """Extract metadata from a USDB detail page.
    USDB layout: first unnamed table row = [Artist] [Title],
    subsequent rows have German labels (Sprache, Jahr, etc.).
    YouTube is an iframe embed. Cover is data/cover/<id>.jpg."""
    data = {"usdb_id": song_id}

    # --- Artist + Title from page <title> tag ---
    # Format: "USDB - <Artist> - <Title>"
    page_title = ""
    if soup.title:
        page_title = soup.title.get_text(strip=True)
    if page_title.startswith("USDB - "):
        rest = page_title[7:]  # strip "USDB - "
        if " - " in rest:
            parts = rest.rsplit(" - ", 1)
            data["artist"] = parts[0].strip()
            data["title"] = parts[1].strip()
        else:
            data["title"] = rest.strip()

    # German label -> internal field
    label_map = {
        "sprache": "language",
        "jahr": "year", "year": "year",
        "genre": "genre",
        "edition": "edition",
        "goldene noten": "golden_notes",
        "songcheck": "songcheck",
        "bpm": "bpm",
        "aufrufe": "views",
        "hochgeladen von": "creator",
        "song editiert von:": "editor",
    }

    # Find the detail table: it has rows like [Artist]=[Title], [Sprache]=[English]...
    # Skip navigation tables (first table is usually the Options menu)
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        # Skip tables that are just navigation
        first_row_text = ""
        if rows:
            first_cells = rows[0].find_all("td")
            if first_cells:
                first_row_text = first_cells[0].get_text(strip=True).lower()

        if "options" in first_row_text and "start" in first_row_text:
            continue

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            label_raw = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)
            if not label_raw:
                continue

            label_lower = label_raw.rstrip(":").lower()

            if label_lower in label_map:
                data[label_map[label_lower]] = value
            elif label_lower in ("artist", "interpret") and "artist" not in data:
                data["artist"] = value
            elif label_lower in ("title", "titel") and "title" not in data:
                data["title"] = value
            # No more guessing from unlabeled rows - title tag is authoritative

    # YouTube: accept only canonical URLs with a strict 11-character video ID.
    for iframe in soup.find_all("iframe"):
        youtube_url = _canonical_youtube_url(iframe.get("src", ""))
        if youtube_url:
            data["youtube_url"] = youtube_url
            break

    # Fallback: check links with the same strict validation.
    if "youtube_url" not in data:
        for a in soup.find_all("a", href=True):
            youtube_url = _canonical_youtube_url(a["href"])
            if youtube_url:
                data["youtube_url"] = youtube_url
                break

    # Cover: data/cover/<id>.jpg
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "data/cover/" in src or ("cover" in src.lower() and src.endswith((".jpg", ".png", ".jpeg"))):
            data["cover_url"] = urljoin(USDB_BASE, src)
            break

    if "artist" not in data and "title" not in data:
        return None
    return data


# === .txt download ===

def fetch_txt(song_id, cookie_string, session=None):
    """Download the UltraStar .txt content from USDB via POST with wd=1."""
    if session is None:
        session = req_lib.Session()
    url = USDB_BASE + f"index.php?link=gettxt&id={song_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_string,
    }
    try:
        resp = session.post(url, headers=headers, data={"wd": "1"}, timeout=15)
    except Exception as e:
        return None, str(e)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text()
    if "not logged in" in page_text.lower() or "nicht eingeloggt" in page_text.lower():
        return None, "Nicht eingeloggt! Bitte neu einloggen."
    for ta in soup.find_all("textarea"):
        content = ta.get_text()
        if "#ARTIST" in content or "#TITLE" in content:
            import html as html_mod
            content = html_mod.unescape(content)
            content = content.replace("\r\n", "\n").replace("\r", "\n")
            return content, None
    return None, "Kein .txt verfügbar (evtl. noch nicht hochgeladen oder nicht eingeloggt)"


# === Cover download ===

def fetch_cover(song_id, cookie_string, session=None):
    """Download cover image bytes from USDB."""
    if session is None:
        session = req_lib.Session()
    url = USDB_BASE + f"data/cover/{song_id}.jpg"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_string,
    }
    try:
        resp = session.get(url, headers=headers, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 100:
            return resp.content, None
        return None, f"HTTP {resp.status_code} oder leer"
    except Exception as e:
        return None, str(e)


# === Search ===

def search_usdb(cookie, interpret="", title="", edition="", limit=50):
    """Search USDB for songs. Returns list of dicts."""
    session = req_lib.Session()
    params = {"link": "list"}
    if interpret:
        params["interpret"] = interpret
    if title:
        params["title"] = title
    if edition:
        params["edition"] = edition
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie,
    }
    try:
        resp = session.get(USDB_BASE + "index.php", params=params,
                           headers=headers, timeout=15)
    except Exception as e:
        return [], f"Netzwerkfehler: {e}"
    if resp.status_code != 200:
        return [], f"HTTP {resp.status_code}"
    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text().lower()
    if "not logged in" in page_text or "nicht eingeloggt" in page_text:
        return [], "Nicht eingeloggt! Bitte neu einloggen."

    results = []
    seen_ids = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"id=(\d+)", a["href"])
        if not m or "detail" not in a["href"]:
            continue
        song_id = m.group(1)
        if song_id in seen_ids:
            continue
        row = a.find_parent("tr")
        if not row:
            continue
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        texts = [c.get_text(strip=True) for c in cells]
        # USDB list: [Artist, Title, Edition, Language, ...Views, Rating]
        entry = {
            "usdb_id": song_id,
            "artist": texts[0] if len(texts) > 0 else "",
            "title": texts[1] if len(texts) > 1 else "",
            "edition": texts[2] if len(texts) > 2 else "",
            "language": texts[3] if len(texts) > 3 else "",
            "url": f"https://usdb.animux.de/index.php?link=detail&id={song_id}",
        }
        # Clean: remove duplicate text from nested links
        if entry["artist"] and entry["title"]:
            seen_ids.add(song_id)
            results.append(entry)
        if len(results) >= limit:
            break
    return results, None
