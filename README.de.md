[English](README.md) | **Deutsch**

# UltraStar Importer

![Lizenz: GPL-3.0-only](https://img.shields.io/badge/Lizenz-GPL--3.0--only-blue.svg)
![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg)
![Plattform](https://img.shields.io/badge/Desktop-Windows-0078D4.svg)
![Status](https://img.shields.io/badge/Status-Beta-orange.svg)

Ein lokaler, quelloffener Importer für UltraStar-Songs aus der
[USDB](https://usdb.animux.de/). Er lädt Song-Metadaten und UltraStar-TXT-Dateien,
sucht passende YouTube-Medien und erzeugt vollständige, NAS-taugliche Songordner.

> [!WARNING]
> **Eigenverantwortliche Nutzung:** Verwende das Tool ausschließlich für
> Inhalte, die du herunterladen und nutzen darfst. Das Projekt liefert keine
> Medien mit, räumt keine Rechte an Inhalten Dritter ein und übernimmt keine
> Gewährleistung für Datenverlust, Kontosperren oder Rechtsverletzungen. Lies
> den vollständigen [Haftungs- und Nutzungshinweis](DISCLAIMER.md).

UltraStar Importer ist ein **unabhängiges, inoffizielles Projekt** und steht in
keiner Verbindung zu USDB, Animux, YouTube, Google oder UltraStar Deluxe.

## Status

Die aktuelle öffentliche Zielversion ist **0.1.1 (Beta)**. Der Kernworkflow ist
automatisiert getestet, dennoch können Änderungen an USDB oder YouTube einzelne
Funktionen jederzeit beeinträchtigen. Vor einem großen Import sind Backups des
Zielordners empfohlen.

## Funktionen

- **USDB-Import:** Metadaten, UltraStar-TXT und Cover
- **YouTube-Medien:** Suche sowie Audio-/Video-Download über yt-dlp
- **Qualität:** Audioformat und Bitrate, Videoformat und Maximalauflösung
- **Nachbearbeitung:** EBU-R128-Normalisierung und Audio-Tags
- **Cover-Verarbeitung:** Größenlimit, Rotation und Auto-Kontrast
- **Bibliothek:** vorhandene Songordner scannen und fehlende Assets reparieren
- **Queue:** sequenzielle oder parallele Verarbeitung mit Live-Fortschritt
- **Sicherheit:** Credential Store, Same-Origin-Schutz, Proxy-Sanitizing,
  Pfadgrenzen und atomare Schreibvorgänge
- **NAS/SMB:** transaktionale Staging-Ordner und SMB-verträgliche Moves
- **Zwei Betriebsarten:** native Windows-App oder authentisierter Docker-Server

## Screenshots

### USDB direkt durchsuchen

Die integrierte, sanitierte USDB-Ansicht erlaubt die Suche, ohne zwischen App
und Browser wechseln zu müssen. Detailseiten können direkt in die Queue
übernommen werden.

![Integrierte USDB-Suche im UltraStar Importer](docs/images/Suche.png)

### Importe in der Queue verwalten

USDB-Links lassen sich gesammelt hinzufügen. Statuskarten und Live-Fortschritt
zeigen den Zustand jedes Imports.

![Song-Queue mit Statusübersicht und Worker-Steuerung](docs/images/Queue.png)

### Sitzung und Auto-Login konfigurieren

Die Einstellungen verwalten Browser-Login, gespeicherte Zugangsdaten und den
Output-Ordner. Geheimnisse werden nicht in der normalen Projektkonfiguration
gespeichert.

![Einstellungen für USDB-Sitzung und Auto-Login](docs/images/Cookie.png)

Ein fertiger Songordner sieht beispielsweise so aus:

```text
Songs/
└── Artist - Title/
    ├── Artist - Title.txt
    ├── Artist - Title.mp3
    ├── Artist - Title.mp4
    ├── Artist - Title [CO].jpg
    └── _links
```

`_links` besitzt absichtlich keine `.txt`-Endung, da UltraStar alle TXT-Dateien
im Songordner als Songdefinition interpretiert.

## Voraussetzungen

### Windows-Desktop

- Windows 10 oder neuer
- Microsoft Edge WebView2 Runtime
- [FFmpeg](https://ffmpeg.org/download.html) im `PATH`

Die Release-EXE enthält Python und die Python-Abhängigkeiten, **aber kein
FFmpeg-Binary**.

### Aus dem Quellcode

- Python 3.12
- FFmpeg im `PATH`
- Git

## Installation aus dem Quellcode

Das Repository über GitHub herunterladen oder klonen und anschließend in den
Projektordner wechseln:

```bash
cd ultrastar-importer
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

Linux/macOS können den Headless-Server verwenden; die pywebview-Desktop-App ist
in diesem Projekt nur für Windows paketiert.

## Verwendung der Desktop-App

1. Unter **Einstellungen → Browser-Login** bei USDB anmelden.
2. Einen Output-Ordner auswählen.
3. USDB-Detail-URLs oder numerische Song-IDs in die Queue einfügen.
4. Gewünschte Audio-/Videoqualität und Workerzahl konfigurieren.
5. Queue starten und den Fortschritt beobachten.
6. Ergebnisse im Reiter **Bibliothek** prüfen oder reparieren.

USDB-Sitzung und optionale Login-Daten werden unter Windows über den Credential
Manager gespeichert. Die normale `config.json` enthält keine Cookies oder
Passwörter.

## Docker / Headless-Server

> [!CAUTION]
> Der Server ist für lokalen oder kontrollierten privaten Betrieb gedacht.
> **Keine direkte Portweiterleitung ins Internet.** HTTP Basic Auth verschlüsselt
> keine Zugangsdaten und muss außerhalb des lokalen Rechners hinter einem
> HTTPS-Reverse-Proxy betrieben werden.

Konfiguration und beschreibbare Host-Verzeichnisse anlegen:

```bash
cp .env.example .env
mkdir -p data output
```

Anschließend `USDB_USERNAME` und `USDB_PASSWORD` in `.env` durch eigene, starke
Werte ersetzen und starten:

```bash
docker compose up --build -d
```

Die Oberfläche ist danach unter <http://127.0.0.1:5776> erreichbar. `/health`
ist absichtlich ohne Authentisierung erreichbar und gibt ausschließlich einen
minimalen Status zurück.

Persistente Verzeichnisse:

```text
./data    -> /app/data
./output  -> /app/output
```

Der Compose-Standard bindet nur an `127.0.0.1`. Für einen LAN-Zugriff darf
`USDB_BIND_ADDRESS=0.0.0.0` ausschließlich zusammen mit einem korrekt
konfigurierten HTTPS-Reverse-Proxy gesetzt werden.

### Docker Engine in WSL2

Docker Desktop ist nicht erforderlich. Mit einer Docker Engine in WSL2:

```bash
wsl.exe -d Ubuntu-24.04 -- bash -lc \
  'cd /mnt/d/PFAD/ZUM/ultrastar-importer && docker compose up --build -d'
```

## Daten und Datenschutz

Desktop-Daten liegen standardmäßig hier:

```text
%LOCALAPPDATA%\UltraStarImporter
```

Dazu können Konfiguration, Cache und Logs gehören. Fertige Songs liegen im
gewählten Output-Ordner. Folgende Inhalte dürfen niemals in Issues oder Commits
hochgeladen werden:

- Cookies und Zugangsdaten
- `config.json`, `server-secrets.json` oder `.env`
- Logs mit personenbezogenen Daten
- heruntergeladene Songs, Covers, Audio- oder Videodateien

## Sicherheit

- Schreibende lokale APIs prüfen die erlaubte Origin.
- Der Servermodus verlangt HTTP Basic Auth; `/health` ist die einzige Ausnahme.
- Externe USDB-Seiten werden vor der Anzeige sanitisiert und mit einer CSP
  versehen.
- Datei- und ZIP-Operationen validieren kanonische Pfade und Größenlimits.
- Konfiguration, Secrets und Cache werden atomar geschrieben.
- Volle oder langsame SSE-Clients werden begrenzt und getrennt.
- Downloads entstehen zunächst in Staging-Verzeichnissen; unvollständige
  Importe werden bereinigt.

Sicherheitsprobleme bitte gemäß [SECURITY.md](SECURITY.md) privat melden.

## Architektur

```text
main.py                     Windows-/pywebview-Entry-Point
server.py                   Docker-/Headless-Entry-Point
src/app.py                  Flask-API, Proxy und SSE
src/worker.py               Queue und Importpipeline
src/usdb.py                 USDB-Parsing und Downloads
src/youtube.py              yt-dlp-Integration
src/songs.py                Song-TXT und transaktionale Ordner
src/config.py               Pfade, Konfiguration und Credentials
static/index.html           lokale Weboberfläche
tests/                      automatisierte Tests
```

## Entwicklung und Tests

Entwicklungsabhängigkeiten installieren:

```bash
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

Tests und Syntaxprüfung:

```bash
set PYTHONPATH=
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m compileall -q main.py server.py src usdb_importer.py
```

Windows-EXE bauen:

```bash
.venv\Scripts\python -m PyInstaller --clean --noconfirm ultrastar-importer.spec
```

Der Build erzeugt `dist/UltraStarImporter.exe`; CI erzwingt eine Obergrenze von
25 MiB und paketiert EXE, Lizenz, Disclaimer und Drittanbieterhinweise zusammen.
Pushes und Pull Requests erhalten ein temporäres Actions-Artefakt; ein passender
`v*`-Tag veröffentlicht ZIP und SHA-256-Prüfsumme zusätzlich als GitHub-Release-Assets.

Weitere Details stehen in [CONTRIBUTING.md](CONTRIBUTING.md) und
[docs/RELEASING.md](docs/RELEASING.md).

## Bekannte Grenzen

- USDB und YouTube besitzen keine stabilen, von diesem Projekt kontrollierten
  APIs; Änderungen der Webseiten können Parser oder Login vorübergehend
  brechen.
- Das Stoppen wartet gegebenenfalls auf eine bereits laufende externe
  yt-dlp-/FFmpeg-Operation.
- Der Docker-Server stellt kein eigenes TLS bereit.
- Eine automatische inhaltliche Rechteprüfung heruntergeladener Medien ist
  technisch nicht möglich.

## Lizenz

Copyright © 2026 UltraStar Importer contributors.

Dieses Projekt ist freie Software unter der
[GNU General Public License Version 3, ausschließlich dieser Version
(GPL-3.0-only)](LICENSE). Du darfst es unter diesen Bedingungen verwenden,
verändern und weitergeben. Bei der Weitergabe von Binärversionen oder
modifizierten Fassungen müssen die GPL-Bedingungen und der korrespondierende
Quellcode eingehalten werden.

Die Software kommt **ohne jede Gewährleistung**, soweit gesetzlich zulässig.
Drittanbieterkomponenten und ihre Lizenztexte sind in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) und
[THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt) dokumentiert.
