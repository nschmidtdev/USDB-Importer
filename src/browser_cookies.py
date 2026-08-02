"""Browser cookie extraction for USDB session.

Clean-room implementation using browser_cookie3. Reads cookies from
Chrome, Firefox, Edge, Brave, Opera, or Chromium and extracts the
USDB session cookie (PHPSESSID) from the usdb.animux.de domain.

This allows "Login with browser" — the user logs into USDB in their
normal browser, and this module pulls the session cookie directly
without manual copy-paste.
"""

from __future__ import annotations

from typing import Optional

# USDB domain for cookie filtering
USDB_DOMAIN = "usdb.animux.de"


def extract_usdb_cookie_from_browser(browser: str = "chrome") -> tuple[Optional[str], Optional[str]]:
    """Extract the USDB PHPSESSID cookie from a browser's cookie store.

    ``browser``: "chrome" | "firefox" | "edge" | "brave" | "opera" | "chromium"
    Returns (cookie_string, error).
    """
    try:
        import browser_cookie3 as bc3
    except ImportError:
        return None, "browser_cookie3 nicht installiert (pip install browser_cookie3)"

    loader = _BROWSER_LOADERS.get(browser.lower())
    if loader is None:
        return None, f"Unbekannter Browser: {browser}. Erlaubt: {', '.join(_BROWSER_LOADERS.keys())}"

    try:
        cookie_jar = loader(bc3)
    except Exception as e:
        return None, f"Cookie-Store von {browser} konnte nicht gelesen werden: {e}"

    # Filter for USDB domain
    phpsessid = None
    all_cookies = []
    for cookie in cookie_jar:
        # browser_cookie3 returns http.cookiejar.Cookie objects
        domain = (cookie.domain or "").lstrip(".")
        if USDB_DOMAIN not in domain:
            continue
        all_cookies.append(cookie)
        if cookie.name == "PHPSESSID":
            phpsessid = cookie.value

    if not all_cookies:
        return None, f"Keine USDB-Cookies in {browser} gefunden. Bitte zuerst in {browser} bei USDB einloggen."

    if not phpsessid:
        return None, f"USDB-Cookies gefunden, aber keine PHPSESSID. Session abgelaufen?"

    # Build cookie string
    parts = [f"{c.name}={c.value}" for c in all_cookies]
    cookie_str = "; ".join(parts)
    return cookie_str, None


def list_available_browsers() -> list[str]:
    """Return browsers that browser_cookie3 can try to read.

    This does NOT check whether the browser is installed — it only
    reports which backends browser_cookie3 supports. Actual cookie
    reading may still fail if the browser is not installed or the
    user is not logged in.
    """
    return list(_BROWSER_LOADERS.keys())


def try_all_browsers() -> tuple[Optional[str], dict]:
    """Try all supported browsers until one yields a USDB cookie.

    Returns (cookie_string, results_dict) where results_dict maps
    browser → (success, message).
    """
    results = {}
    for browser in _BROWSER_LOADERS:
        cookie, err = extract_usdb_cookie_from_browser(browser)
        if cookie:
            results[browser] = (True, "USDB-Cookie gefunden")
            return cookie, results
        results[browser] = (False, err or "Kein Cookie")
    return None, results


# === Browser loader functions ===
# Each takes browser_cookie3 module as argument and returns a CookieJar.
# Wrapped in lambdas so they're only called when needed (not at import time).

def _load_chrome(bc3):
    return bc3.chrome(domain_name=USDB_DOMAIN)


def _load_firefox(bc3):
    return bc3.firefox(domain_name=USDB_DOMAIN)


def _load_edge(bc3):
    return bc3.edge(domain_name=USDB_DOMAIN)


def _load_brave(bc3):
    return bc3.brave(domain_name=USDB_DOMAIN)


def _load_opera(bc3):
    return bc3.opera(domain_name=USDB_DOMAIN)


def _load_chromium(bc3):
    return bc3.chromium(domain_name=USDB_DOMAIN)


_BROWSER_LOADERS = {
    "chrome": _load_chrome,
    "firefox": _load_firefox,
    "edge": _load_edge,
    "brave": _load_brave,
    "opera": _load_opera,
    "chromium": _load_chromium,
}
