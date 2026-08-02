# Contributing

Danke für dein Interesse am UltraStar Importer.

## Voraussetzungen

- Python 3.12
- FFmpeg im `PATH`
- Git
- unter Windows: WebView2 Runtime

## Lokale Einrichtung

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
```

Unter Linux/macOS lautet der Interpreterpfad `.venv/bin/python`.

## Änderungen

1. Erstelle einen Branch von `master`.
2. Halte Änderungen klein und thematisch fokussiert.
3. Ergänze bei Verhaltensänderungen zuerst einen fehlschlagenden Test.
4. Speichere niemals Cookies, Zugangsdaten, heruntergeladene Medien oder lokale
   Konfigurationen im Repository.
5. Führe vor einem Pull Request die Qualitätsprüfungen aus.

```bash
PYTHONPATH="" .venv/Scripts/python -m pytest -q
PYTHONPATH="" .venv/Scripts/python -m compileall -q main.py server.py src usdb_importer.py
```

Für einen Windows-Build:

```bash
PYTHONPATH="" .venv/Scripts/python -m PyInstaller --clean --noconfirm ultrastar-importer.spec
```

## Lizenz der Beiträge

Mit dem Einreichen eines Beitrags bestätigst du, dass du ihn unter
**GPL-3.0-only** bereitstellen darfst und er unter derselben Lizenz wie das
Projekt veröffentlicht werden kann. Übernimm keinen inkompatibel lizenzierten
Code und dokumentiere neue Drittanbieterabhängigkeiten in
`THIRD_PARTY_NOTICES.md` und `THIRD_PARTY_LICENSES.txt`.

## Verhalten

Sei respektvoll, sachlich und konstruktiv. Belästigung, Diskriminierung und die
Veröffentlichung privater Daten werden nicht toleriert.
