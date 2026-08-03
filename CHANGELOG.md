# Changelog

Alle wesentlichen Änderungen dieses Projekts werden hier dokumentiert. Das
Format orientiert sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/)
und die Versionierung an [Semantic Versioning](https://semver.org/lang/de/).

## [Unreleased]

## [0.1.2] - 2026-08-03

### Added

- Added release-tag publishing of tested Docker images to GitHub Container Registry.

### Fixed

- Fixed stale OCI version metadata by deriving it from the project release version.

## [0.1.1] - 2026-08-02

### Added

- Added English, German, Spanish, and Russian user-interface translations.
- Added automatic language detection and persistent language selection.

## [0.1.0] - 2026-08-02

### Added

- Native Windows-Oberfläche mit pywebview und loopbackgebundenem Waitress-Server.
- USDB-Import von Songtext, Cover, Audio und optionalem Video.
- YouTube-Suche und Download über yt-dlp.
- Transaktionale Songordner, SMB-/NAS-sichere Dateioperationen und Bibliotheksscan.
- Docker-/Headless-Modus mit verpflichtender HTTP-Basic-Authentisierung.
- Persistenter Cache, Credential-Store und automatischer USDB-Login.
- GitHub Actions für Tests, Windows-Paket und Docker-Smoke-Test.

### Security

- Same-Origin-Schutz für schreibende lokale APIs.
- Proxy-Sanitizing, Content Security Policy und Traversal-Schutz.
- Sichere ZIP-Grenzen, atomare Konfigurations-/Cache-Schreibvorgänge und
  begrenzte SSE-Verbindungen.
