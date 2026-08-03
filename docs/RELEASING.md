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

1. `src/version.py`, den Standard-Image-Tag in `docker-compose.yml` und
   `.env.example` aktualisieren.
2. `CHANGELOG.md` von `Unreleased` in die neue Version überführen.
3. Tag im Format `vMAJOR.MINOR.PATCH` erstellen. Der Tag muss exakt zur Version
   in `src/version.py` passen.
4. Den Tag zu GitHub pushen. GitHub Actions führt Windows-Tests, EXE-Paketierung
   und den Docker-Smoke-Test aus.
5. Nach grünen Qualitätsgates erstellt die Pipeline das GitHub Release automatisch,
   hängt Windows-ZIP sowie SHA-256-Prüfsumme an und veröffentlicht das Container-
   Image als `ghcr.io/nschmidtdev/usdb-importer:<version>` sowie `latest`.
   Die automatisch erzeugten Quellcodearchive bleiben am selben Release verfügbar.
6. Beim erstmaligen Erstellen des GHCR-Pakets dessen Sichtbarkeit in den
   Paketeinstellungen auf **Public** setzen. Diese Einstellung bleibt für spätere
   Versionen erhalten.
7. Die automatisch erzeugten Release Notes bei Bedarf anhand des Changelogs
   ergänzen.

Das Docker-Image darf nicht als öffentlich erreichbarer Dienst beworben werden.
Für LAN-/Internetbetrieb ist ein HTTPS-Reverse-Proxy erforderlich.
