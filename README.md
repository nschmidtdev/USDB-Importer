# USDB Song Importer

Lokales Tool zum Importieren von UltraStar-Songs aus der USDB (usdb.animux.de).
Lädt Metadaten, Lyrics/Timing (.txt), Cover, Audio (MP3) und Video (MP4) von YouTube.

## Voraussetzungen

- Python 3.10+
- FFmpeg (für MP3-Konvertierung)
- pip install -r requirements.txt

## Installation

```bash
pip install -r requirements.txt
```

FFmpeg wird für die Audio-Extraktion (MP3) benötigt. Unter Windows z.B. via Chocolatey:
```
choco install ffmpeg
```

## Start

```bash
python app.py
```

Dann im Browser öffnen: http://127.0.0.1:5776

## Verwendung

1. **Einstellungen → Browser-Login**: Klick öffnet ein pywebview-Fenster. Bei USDB einloggen. Cookie wird automatisch übernommen.
2. **Einstellungen → Output-Ordner**: Zielordner für fertige Songs setzen.
3. **Queue**: USDB-Links einfügen (eine pro Zeile), Worker starten.
4. Der Worker lädt pro Song: Metadaten, .txt (Lyrics+Timing), Cover, MP3 (via yt-dlp/FFmpeg), Video (MP4).

Jeder Song wird als Ordner erstellt:
```
Output-Ordner/
  Artist - Title/
    ├── Artist - Title.txt          (UltraStar-Format)
    ├── Artist - Title.mp3          (Audio)
    ├── Artist - Title.mp4          (Video, optional)
    └── Artist - Title [CO].jpg     (Cover)
```

## Sicherheit und Dateiverhalten

- Der USDB-Cookie liegt im Windows Credential Manager (`keyring`), niemals in `config.json`, Logs, SSE oder API-GET-Antworten.
- Der Browser-Login verwendet einen einmaligen Übergabe-Token; schreibende API-Aufrufe akzeptieren nur die lokale UI-Origin.
- Der Cookie-Test prüft den echten `.txt`-Download, nicht nur die Detailseite.
- Imports werden zunächst in einem temporären Ordner erzeugt. Fehlt eine erforderliche Audiodatei oder schlägt der Import fehl, bleibt kein fertiger Songordner zurück.
- Existiert der Zielordner bereits, wird der Song sicher übersprungen; vorhandene Dateien werden nicht überschrieben oder gemischt.
- Nutze nur Inhalte, für die du die erforderlichen Rechte oder eine Erlaubnis besitzt.

## Tests

```bash
python -m pytest -v
```

## Hinweis

Verwende dieses Tool nur für Inhalte, für die du die nötigen Rechte oder eine Erlaubnis hast.
