# Release-Prozess

## Voraussetzungen

- sauberer Git-Status und grüner Default-Branch,
- Python 3.12 und Abhängigkeiten aus `requirements-dev.txt`,
- Docker Engine für den Container-Smoke-Test,
- keine Secrets oder Nutzerdaten im Repository.

## Qualitätsgate

```bash
PYTHONPATH="" .venv/Scripts/python -m pytest -q
PYTHONPATH="" .venv/Scripts/python -m compileall -q main.py server.py src usdb_importer.py
PYTHONPATH="" .venv/Scripts/python -m PyInstaller --clean --noconfirm ultrastar-importer.spec
```

Die EXE muss kleiner als 25 MiB bleiben. Prüfe anschließend den Start auf einem
sauberen Windows-System mit installierter WebView2 Runtime und FFmpeg.

## Recht und Sicherheit

- `LICENSE`, `DISCLAIMER.md`, `THIRD_PARTY_NOTICES.md` und
  `THIRD_PARTY_LICENSES.txt` müssen im Release-Paket liegen.
- Generiere die Drittanbieter-Lizenzliste nach Dependency-Updates neu.
- Prüfe Working Tree und komplette Git-Historie auf Secrets.
- Liefere keine Config, Cookies, Songs, Audio-/Videodateien oder Logs aus.
- Veröffentliche Binary und korrespondierenden Quellcode am selben Ort.

## Version

1. `src/version.py` aktualisieren.
2. `CHANGELOG.md` von `Unreleased` in die neue Version überführen.
3. Tag im Format `vMAJOR.MINOR.PATCH` erstellen.
4. Das von GitHub Actions erzeugte Windows-ZIP als Release-Asset verwenden.
5. Release Notes aus dem Changelog übernehmen.

Das Docker-Image darf nicht als öffentlich erreichbarer Dienst beworben werden.
Für LAN-/Internetbetrieb ist ein HTTPS-Reverse-Proxy erforderlich.
