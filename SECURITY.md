# Security Policy

## Supported versions

Bis zum ersten stabilen Release wird ausschließlich der aktuelle Stand des
Default-Branches unterstützt. Ältere Builds können bekannte Sicherheitslücken
enthalten.

## Schwachstellen melden

Bitte veröffentliche Sicherheitslücken, Zugangsdaten oder Exploit-Details nicht
in einem öffentlichen Issue. Nutze nach Veröffentlichung des Repositories
GitHubs **Private vulnerability reporting** unter **Security → Advisories → New
draft security advisory**.

Eine gute Meldung enthält:

- betroffene Version oder Commit,
- reproduzierbare, nicht destruktive Schritte,
- erwartetes und tatsächliches Verhalten,
- mögliche Auswirkungen und
- einen Fix-Vorschlag, falls vorhanden.

Entferne Cookies, Passwörter, lokale Pfade und personenbezogene Daten aus Logs
und Screenshots.

## Deployment-Hinweis

Der Docker-Server ist standardmäßig nur an `127.0.0.1` gebunden. HTTP Basic
Auth bietet ohne TLS keine Vertraulichkeit. Setze `USDB_BIND_ADDRESS=0.0.0.0`
nur hinter einem korrekt konfigurierten HTTPS-Reverse-Proxy. Eine direkte
Freigabe des Ports ins Internet wird nicht unterstützt.
