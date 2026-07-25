#!/usr/bin/env python3
"""
Login Window - Oeffnet einen pywebview Browser fuer USDB-Login.
Der Benutzer loggt sich ein, danach entweder:
- Automatische Erkennung (Login-Form verschwindet)
- Manuell: gelben "Cookies uebernehmen" Button klicken
Cookies werden an den Flask-Server gesendet.

Sicherheit: URL wird vor Cookie-Export validiert (nur usdb.animux.de).
Cookie-Werte werden niemals geloggt.
"""

import sys
import os
import time
import threading
import argparse
import json
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import webview

USDB_LOGIN_URL = "https://usdb.animux.de/?link=login"
USDB_HOST = "usdb.animux.de"
FLASK_API = "http://127.0.0.1:5776/api/cookie/from-browser"

LOG_FILE = Path(__file__).parent / "data" / "login_window.log"
LOG_FILE.parent.mkdir(exist_ok=True)

# Lock to ensure cookies are only exported once per session
_export_lock = threading.Lock()
_export_done = False

# Passed by app.py when the login process is spawned. It is intentionally not
# generated here: Flask can therefore bind exactly one cookie hand-off to its
# own current login request.
TRANSFER_TOKEN = ""


def log(msg):
    """Log a message. NEVER log cookie values."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_usdb_url(url):
    """Verify that a URL belongs to usdb.animux.de over https with no unexpected port."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        # Hostname must match exactly ...
        if parsed.hostname != USDB_HOST:
            return False
        # ... and the transport must be secure ...
        if parsed.scheme != "https":
            return False
        # ... and only the default HTTPS port (or none) is allowed.
        if parsed.port not in (None, 443):
            return False
        return True
    except Exception:
        return False


def origin_for_logging(url):
    """Return scheme://host for a URL, stripping path/query/fragment.

    Used anywhere a URL is logged so that session params or tokens that may
    appear in the path/query are never written to logs.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}"
    except Exception:
        pass
    return "<invalid-url>"


def cookies_to_string(cookies):
    """Convert pywebview cookie objects to a cookie header string."""
    parts = []
    for c in cookies:
        try:
            if hasattr(c, "output"):
                for key, morsel in c.items():
                    parts.append(f"{key}={morsel.value}")
            elif isinstance(c, str):
                if "=" in c:
                    parts.append(c.split(";")[0].strip())
            elif isinstance(c, dict):
                for k, v in c.items():
                    parts.append(f"{k}={v}")
        except Exception as e:
            log(f"Cookie parse error: {e}")
    return "; ".join(parts)


def send_cookies(cookie_string):
    """Send extracted cookies to the Flask server. Cookie value is never logged.

    Sends a one-time X-Transfer-Token header to authenticate the transfer.
    The Flask server (app.py) must verify this header matches the expected
    token before accepting the cookie payload; see TRANSFER_TOKEN docstring.
    """
    try:
        data = json.dumps({"cookie": cookie_string}).encode("utf-8")
        req = urllib.request.Request(
            FLASK_API, data=data,
            headers={
                "Content-Type": "application/json",
                "X-Transfer-Token": TRANSFER_TOKEN,
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        log(f"Cookies an Server gesendet ({resp.status})")
        return True
    except Exception as e:
        log(f"Fehler beim Senden: {e}")
        return False


def grab_and_send(window):
    """Extract cookies from the window and send to server.
    Validates that we're on usdb.animux.de before exporting."""
    global _export_done

    with _export_lock:
        if _export_done:
            log("Export bereits durchgefuehrt, skip.")
            return True

    # Security: verify we're on USDB before grabbing cookies
    current_url = ""
    try:
        current_url = window.get_current_url() or ""
    except Exception:
        pass

    if not is_usdb_url(current_url):
        # Log only the origin (scheme://host), never path/query/fragment,
        # which may carry session params or tokens.
        log(f"URL-Validierung fehlgeschlagen: {origin_for_logging(current_url)}. "
            f"Nur {USDB_HOST} erlaubt.")
        return False

    try:
        cookies = window.get_cookies()
        if not cookies:
            log("Keine Cookies gefunden!")
            return False
        cookie_str = cookies_to_string(cookies)
        # Log only length, never the value
        log(f"Cookie-String extrahiert ({len(cookie_str)} chars)")
        if "PHPSESSID" not in cookie_str:
            log("WARNUNG: Kein PHPSESSID in Cookies!")
            return False

        ok = send_cookies(cookie_str)
        # Only mark the export as done AFTER the server confirms receipt.
        # On failure, leave _export_done False so a retry is possible.
        if ok:
            with _export_lock:
                _export_done = True
        return ok
    except Exception as e:
        log(f"Grab error: {e}")
        return False


def is_logged_in(window):
    """Check if user is logged in by looking for login form absence."""
    try:
        result = window.evaluate_js("""
            (function() {
                var inputs = document.querySelectorAll('input[type="password"]');
                var hasLoginForm = inputs.length > 0;
                var bodyText = document.body ? document.body.innerText.substring(0, 1000) : '';
                var hasLoginLink = bodyText.toLowerCase().indexOf('please login') >= 0;
                return JSON.stringify({
                    hasPasswordFields: hasLoginForm,
                    hasPleaseLogin: hasLoginLink,
                    url: window.location.href
                });
            })();
        """)
        if result:
            info = json.loads(result)
            # Log only structural info, never cookie content
            log(f"Login check: pwd_fields={info['hasPasswordFields']}, "
                f"please_login={info['hasPleaseLogin']}")
            return not info["hasPasswordFields"] and not info["hasPleaseLogin"]
    except Exception as e:
        log(f"is_logged_in JS error: {e}")
    return False


def show_success_banner(window, message):
    """Show a green success banner at the top of the page."""
    try:
        window.evaluate_js(f"""
            (function() {{
                var old = document.getElementById('usdb-success');
                if (old) old.remove();
                var div = document.createElement('div');
                div.id = 'usdb-success';
                div.innerHTML = '{message}';
                div.style.cssText = 'position:fixed!important;top:0!important;left:0!important;'
                    + 'right:0!important;z-index:999999!important;padding:16px!important;'
                    + 'background:#22c55e!important;color:#fff!important;font-size:15px!important;'
                    + 'font-weight:bold!important;text-align:center!important;';
                document.body.appendChild(div);
                setTimeout(function() {{
                    var b = document.getElementById('usdb-success');
                    if (b) b.remove();
                }}, 4000);
            }})();
        """)
    except Exception:
        pass


def inject_button(window):
    """Inject a floating 'Cookies uebernehmen' button into the page."""
    try:
        window.evaluate_js("""
            (function() {
                if (document.getElementById('usdb-grab-btn')) return;
                var btn = document.createElement('button');
                btn.id = 'usdb-grab-btn';
                btn.textContent = '\\\\u2705 Fertig eingeloggt? Cookies \\\\u00fcbernehmen';
                btn.style.cssText = 'position:fixed!important;bottom:20px!important;right:20px!important;'
                    + 'z-index:999999!important;padding:14px 28px!important;font-size:16px!important;'
                    + 'background:#f59e0b!important;color:#000!important;border:none!important;'
                    + 'border-radius:10px!important;cursor:pointer!important;font-weight:bold!important;'
                    + 'box-shadow:0 4px 16px rgba(0,0,0,0.4)!important;'
                    + 'animation:pulse 2s infinite!important;';
                var style = document.createElement('style');
                style.textContent = '@keyframes pulse{0%{transform:scale(1)}50%{transform:scale(1.05)}100%{transform:scale(1)}}';
                document.head.appendChild(style);
                btn.onclick = function() {
                    btn.textContent = '\\\\u23f3 Uebernehme Cookies ...';
                    btn.style.background = '#3b82f6';
                    pywebview.api.grab();
                };
                document.body.appendChild(btn);
            })();
        """)
    except Exception as e:
        log(f"Inject error: {e}")


class JsBridge:
    """JS-Python bridge for the webview page.
    Security: grab() validates URL before exporting cookies."""
    def grab(self):
        log("Manuelle Cookie-Uebernahme ausgeloest.")
        window = webview.active_window()
        if not window:
            return json.dumps({"ok": False, "error": "No window"})

        # Validate URL before any cookie access
        try:
            current_url = window.get_current_url() or ""
        except Exception:
            current_url = ""

        if not is_usdb_url(current_url):
            log(f"Verweigert: URL ist nicht {USDB_HOST} ({origin_for_logging(current_url)})")
            return json.dumps({"ok": False, "error": "invalid_origin"})

        ok = grab_and_send(window)
        if ok:
            show_success_banner(window, "\\u2705 Cookies uebernommen! Fenster schliesst automatisch ...")
            time.sleep(2)
            try:
                window.destroy()
            except Exception:
                pass
        else:
            try:
                window.evaluate_js("""
                    (function() {
                        var btn = document.getElementById('usdb-grab-btn');
                        if (btn) {
                            btn.textContent = '\\\\u274c Fehler - erneut versuchen';
                            btn.style.background = '#ef4444';
                        }
                    })();
                """)
            except Exception:
                pass
        return json.dumps({"ok": ok})


def on_loaded(window):
    """Called on each page load."""
    url = window.get_current_url() or ""
    # Log only the host, not the full URL with potential session params
    try:
        host = urlparse(url).hostname or url
        log(f"Page loaded: {host}")
    except Exception:
        log("Page loaded")

    threading.Timer(1.0, inject_button, args=(window,)).start()
    threading.Timer(2.0, lambda: check_and_grab(window)).start()


def check_and_grab(window):
    """Auto-detect login and grab cookies."""
    try:
        if is_logged_in(window):
            log("AUTO: Login erkannt! Extrahiere Cookies ...")
            time.sleep(1)
            ok = grab_and_send(window)
            if ok:
                show_success_banner(window, "\\u2705 Login erkannt! Cookies automatisch uebernommen.")
                time.sleep(2)
                try:
                    window.destroy()
                except Exception:
                    pass
    except Exception as e:
        log(f"check_and_grab error: {e}")


def main():
    global TRANSFER_TOKEN
    parser = argparse.ArgumentParser()
    parser.add_argument("--transfer-token", required=True, help=argparse.SUPPRESS)
    TRANSFER_TOKEN = parser.parse_args().transfer_token
    log("=" * 50)
    log("Login-Fenster wird geoeffnet ...")

    window = webview.create_window(
        "USDB Login - Song Importer",
        USDB_LOGIN_URL,
        width=900,
        height=700,
        js_api=JsBridge(),
    )
    window.events.loaded += lambda: on_loaded(window)

    log("Fenster erstellt. Warte auf Login ...")

    webview.start()
    log("Login-Fenster geschlossen.")


if __name__ == "__main__":
    main()
