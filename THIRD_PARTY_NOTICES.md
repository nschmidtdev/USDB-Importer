# Third-party notices

UltraStar Importer wird unter **GPL-3.0-only** veröffentlicht. Das Windows-Paket
enthält außerdem freie Software anderer Urheber. Vollständige, aus der
gestesteten Build-Umgebung extrahierte Lizenztexte stehen in
[`THIRD_PARTY_LICENSES.txt`](THIRD_PARTY_LICENSES.txt).

## Direkte Runtime-Abhängigkeiten

| Komponente | getestete Version | Lizenz | Projekt |
|---|---:|---|---|
| Flask | 3.1.3 | BSD-3-Clause | https://palletsprojects.com/p/flask/ |
| Requests | 2.34.2 | Apache-2.0 | https://requests.readthedocs.io/ |
| Beautiful Soup | 4.15.0 | MIT | https://www.crummy.com/software/BeautifulSoup/ |
| yt-dlp | 2026.7.4 | Unlicense | https://github.com/yt-dlp/yt-dlp |
| Mutagen | 1.48.1 | GPL-2.0-or-later | https://github.com/quodlibet/mutagen |
| ffmpeg-normalize | 1.41.1 | MIT | https://github.com/slhck/ffmpeg-normalize |
| Pillow | 12.3.0 | MIT-CMU | https://python-pillow.org/ |
| Waitress | 3.0.2 | ZPL-2.1 | https://github.com/Pylons/waitress |
| keyring | 25.7.0 | MIT | https://github.com/jaraco/keyring |
| browser-cookie3 | 0.20.1 | LGPL-3.0 | https://github.com/borisbabic/browser_cookie3 |
| pywebview | 6.2.1 | BSD-3-Clause | https://pywebview.flowrl.com/ |

Die GPL-2.0-or-later-Lizenz von Mutagen ist mit GPLv3 kompatibel. Die LGPL-3.0
von browser-cookie3 erlaubt die Kombination unter den dort genannten
Bedingungen. Das Release-Paket enthält die jeweiligen Lizenztexte.

Bei zwei älteren transitiven Paketen wurden unvollständige Wheel-Metadaten gegen
die Upstream-Quellen geprüft: WMI 1.5.1 steht unter MIT (Copyright Tim Golden),
`proxy_tools` 0.1.0 tatsächlich unter BSD-3-Clause. Die korrigierten Original-
beziehungsweise Standardtexte stehen in `THIRD_PARTY_LICENSES.txt`.

## Build-Werkzeug

PyInstaller 6.21.0 steht unter GPL-2.0-or-later mit einer ausdrücklichen
Bootloader-Ausnahme für daraus erzeugte Programme. PyInstaller ist kein
Runtime-Paket der Anwendung; sein Bootloader ist Bestandteil der Windows-EXE.

## FFmpeg

FFmpeg wird nicht in die Windows-EXE eingebettet und muss dort separat
installiert werden. Das Docker-Image installiert das von Debian bereitgestellte
`ffmpeg`-Paket. Dessen konkrete Lizenzzusammensetzung hängt von der
Debian-Buildkonfiguration ab. Copyright- und Lizenzinformationen befinden sich
im Container unter `/usr/share/doc/ffmpeg/`; der zugehörige Debian-Quellcode ist
über https://sources.debian.org/src/ffmpeg/ verfügbar.

## Aktualisierung

`THIRD_PARTY_LICENSES.txt` wird aus der geprüften virtuellen Umgebung erzeugt.
Bei Änderungen an Abhängigkeiten müssen diese Datei und diese Übersicht vor
einem Binary-Release neu erzeugt und geprüft werden.
