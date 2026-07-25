#!/usr/bin/env python3
"""
USDB Song Importer
==================
Liest Metadaten von usdb.animux.de Detailseiten (mit Session-Cookie)
und führt sie mit lokalen UltraStar-Dateien zusammen.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Abhaengigkeiten fehlen. Installiere mit:")
    print("  pip install requests beautifulsoup4")
    sys.exit(1)

USDB_BASE = "https://usdb.animux.de/"
USDB_DETAIL = "index.php?link=detail&id={}"


# ---------------------------------------------------------------------------
# USDB Scraper
# ---------------------------------------------------------------------------

def fetch_detail(song_id, cookie_string, session=None):
    """Fetch a single USDB detail page and parse metadata."""
    if session is None:
        session = requests.Session()

    url = USDB_BASE + USDB_DETAIL.format(song_id)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_string,
    }

    try:
        resp = session.get(url, headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"  Fehler bei ID {song_id}: {e}")
        return None

    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text()

    if "Datensatz nicht gefunden" in text:
        return None

    return parse_detail(soup, song_id)


def parse_detail(soup, song_id):
    """Extract metadata from a USDB detail page."""
    data = {"usdb_id": song_id}

    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            label = cells[0].get_text(strip=True).rstrip(":").lower()
            value = cells[1].get_text(strip=True)
            if not label:
                continue

            field_map = {
                "artist": "artist",
                "titel": "title",
                "title": "title",
                "language": "language",
                "sprache": "language",
                "edition": "edition",
                "golden notes": "golden_notes",
                "rating": "rating",
                "year": "year",
                "jahr": "year",
                "genre": "genre",
                "creator": "creator",
                "views": "views",
            }
            if label in field_map:
                data[field_map[label]] = value

    # YouTube-Link suchen
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "youtube.com/watch" in href or "youtu.be/" in href:
            data["youtube_url"] = href
            break

    if "youtube_url" not in data:
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            if "youtube" in src:
                data["youtube_url"] = src
                break

    # Cover-Bild suchen
    for img in soup.find_all("img"):
        src = img.get("src", "")
        alt = img.get("alt", "").lower()
        if "cover" in src.lower() or alt == "cover":
            data["cover_url"] = urljoin(USDB_BASE, src)
            break

    if "artist" not in data and "title" not in data:
        return None

    return data


def scan_usdb(cookie_string, start_id=1, end_id=100, delay=0.5, output_file="usdb_songs.json"):
    """Scan USDB for songs in a range of IDs."""
    results = []
    session = requests.Session()
    found = 0
    skipped = 0

    print(f"Scanne USDB IDs {start_id}-{end_id} ...")

    for song_id in range(start_id, end_id + 1):
        data = fetch_detail(song_id, cookie_string, session)
        if data:
            results.append(data)
            found += 1
            yt = " [YT]" if "youtube_url" in data else ""
            print(f"  [{song_id:>6}] OK  {data.get('artist', '?')} - {data.get('title', '?')}{yt}")
        else:
            skipped += 1

        if song_id % 50 == 0:
            print(f"  ... {song_id}/{end_id} gescannt ({found} gefunden)")

        time.sleep(delay)

    print(f"\nFertig: {found} Songs gefunden, {skipped} uebersprungen.")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Ergebnis gespeichert: {output_file}")

    return results


# ---------------------------------------------------------------------------
# Local UltraStar Scanner
# ---------------------------------------------------------------------------

def parse_ultrastar_txt(filepath):
    """Parse an UltraStar .txt file and extract metadata."""
    data = {"file": str(filepath)}
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
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


def scan_local(path, output_file="local_songs.json"):
    """Scan a local directory for UltraStar song folders."""
    results = []
    base = Path(path)

    if not base.exists():
        print(f"Pfad nicht gefunden: {path}")
        return []

    print(f"Scanne lokales Verzeichnis: {path}")

    count = 0
    for txt_file in sorted(base.rglob("*.txt")):
        parsed = parse_ultrastar_txt(txt_file)
        if "artist" in parsed or "title" in parsed:
            folder = txt_file.parent
            assets = {
                "has_mp3": any(folder.glob("*.mp3")),
                "has_video": (any(folder.glob("*.mp4"))
                              or any(folder.glob("*.avi"))
                              or any(folder.glob("*.webm"))),
                "has_cover": (any(folder.glob("*cover*"))
                              or any(folder.glob("*[Cc][Oo]*"))),
                "has_background": (any(folder.glob("*bg*"))
                                   or any(folder.glob("*background*"))),
            }
            parsed["assets"] = assets
            parsed["folder"] = str(folder)
            results.append(parsed)
            count += 1
            print(f"  [{count:>4}] {parsed.get('artist', '?')} - {parsed.get('title', '?')}")

    print(f"\nFertig: {count} lokale Songs gefunden.")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Ergebnis gespeichert: {output_file}")

    return results


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def normalize(s):
    """Normalize a string for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def match_songs(usdb_file="usdb_songs.json", local_file="local_songs.json",
                output="match_report.json"):
    """Match USDB songs with local songs and produce a report."""
    with open(usdb_file, "r", encoding="utf-8") as f:
        usdb_songs = json.load(f)
    with open(local_file, "r", encoding="utf-8") as f:
        local_songs = json.load(f)

    report = {
        "matched": [],
        "usdb_only": [],
        "local_only": [],
        "missing_youtube": [],
        "missing_video": [],
        "missing_cover": [],
        "missing_mp3": [],
    }

    local_index = {}
    for ls in local_songs:
        key = normalize(ls.get("artist", "")) + "|" + normalize(ls.get("title", ""))
        local_index[key] = ls

    usdb_keys = set()

    for us in usdb_songs:
        key = normalize(us.get("artist", "")) + "|" + normalize(us.get("title", ""))
        usdb_keys.add(key)
        local_match = local_index.get(key)

        if local_match:
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

    print("\n" + "=" * 60)
    print("MATCH REPORT")
    print("=" * 60)
    print(f"  USDB-Songs gesamt:   {len(usdb_songs)}")
    print(f"  Lokale Songs gesamt: {len(local_songs)}")
    print(f"  Matched:            {len(report['matched'])}")
    print(f"  Nur USDB:           {len(report['usdb_only'])}")
    print(f"  Nur lokal:          {len(report['local_only'])}")
    print(f"  Fehlendes Video:    {len(report['missing_video'])}")
    print(f"  Fehlendes Cover:    {len(report['missing_cover'])}")
    print(f"  Fehlendes MP3:      {len(report['missing_mp3'])}")
    print(f"  Fehlender YT-Link:  {len(report['missing_youtube'])}")
    print("=" * 60)

    with open(output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nReport gespeichert: {output}")

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def get_cookie_interactive():
    """Get cookie via getpass (never via CLI argument)."""
    import getpass
    print("USDB Session-Cookie eingeben (wird nicht angezeigt):")
    return getpass.getpass("Cookie: ")


def main():
    parser = argparse.ArgumentParser(description="USDB Song Importer")
    subparsers = parser.add_subparsers(dest="command")

    p_usdb = subparsers.add_parser("scan-usdb", help="USDB ID-Range durchsuchen")
    p_usdb.add_argument("--cookie-file", default=None,
                        help="Datei mit Session-Cookie (alternativ: interaktive Eingabe)")
    p_usdb.add_argument("--start", type=int, default=1)
    p_usdb.add_argument("--end", type=int, default=100)
    p_usdb.add_argument("--delay", type=float, default=0.5)
    p_usdb.add_argument("--output", default="usdb_songs.json")

    p_single = subparsers.add_parser("scan-one", help="Einzelne USDB-ID abrufen")
    p_single.add_argument("--cookie-file", default=None,
                          help="Datei mit Session-Cookie (alternativ: interaktive Eingabe)")
    p_single.add_argument("--id", type=int, required=True)

    p_local = subparsers.add_parser("scan-local", help="Lokale UltraStar-Songs scannen")
    p_local.add_argument("--path", required=True, help="Pfad zum UltraStar-Ordner")
    p_local.add_argument("--output", default="local_songs.json")

    p_match = subparsers.add_parser("match", help="USDB und lokale Songs abgleichen")
    p_match.add_argument("--usdb", default="usdb_songs.json")
    p_match.add_argument("--local", default="local_songs.json")
    p_match.add_argument("--output", default="match_report.json")

    args = parser.parse_args()

    # Cookie aus Datei oder interaktiv (niemals als CLI-Argument)
    if args.command in ("scan-usdb", "scan-one"):
        if getattr(args, "cookie_file", None):
            with open(args.cookie_file, "r", encoding="utf-8") as f:
                cookie = f.read().strip()
        else:
            cookie = get_cookie_interactive()
    else:
        cookie = None

    if args.command == "scan-usdb":
        scan_usdb(cookie, args.start, args.end, args.delay, args.output)
    elif args.command == "scan-one":
        data = fetch_detail(args.id, cookie)
        if data:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print("Song nicht gefunden oder nicht eingeloggt.")
    elif args.command == "scan-local":
        scan_local(args.path, args.output)
    elif args.command == "match":
        match_songs(args.usdb, args.local, args.output)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
