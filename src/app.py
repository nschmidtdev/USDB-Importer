#!/usr/bin/env python3
"""
USDB Song Importer - Web UI Backend
Flask backend with SSE for live status, cookie management, link queue.

Refactored into focused modules:
  utils.py    — pure helpers (sanitize_filename, normalize, JSON I/O)
  config.py   — paths, constants, credential management, load/save_config
  state.py    — shared State singleton, SSE broadcast, queue snapshot
  usdb.py     — USDB scraping (fetch_detail/txt/cover, search, parse_detail)
  youtube.py  — yt-dlp audio/video download, URL validation
  songs.py    — build_song_folder, parse_ultrastar_txt, scan_local, matching
  worker.py   — process_single/process_queue, validate_queue_url/delay

This module wires everything together via Flask routes and re-exports
the commonly used names so existing callers (tests, server.py) keep working.
"""

import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
import html as html_lib
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from flask import Flask, request, jsonify, Response, send_file, redirect

try:
    import requests as req_lib
    from bs4 import BeautifulSoup
except ImportError:
    print("pip install requests beautifulsoup4 flask")
    raise

# --- Module imports ---
from utils import (
    _file_size_mb, _atomic_write_json, _load_json_safe,
    sanitize_filename, normalize,
)
from config import (
    CODE_DIR, DATA_DIR, CONFIG_FILE,
    USDB_BASE, USDB_DETAIL, CONFIG_DEFAULTS,
    COOKIE_SERVICE, COOKIE_ACCOUNT,
    get_cookie, set_cookie,
    get_login_credentials, set_login_credentials, has_login_credentials,
    auto_login, load_config,
    get_active_port,
)
from usdb import (
    USDB_URL_RE, extract_usdb_id,
    fetch_detail, fetch_txt, fetch_cover, parse_detail,
    search_usdb,
)
from youtube import (
    is_valid_youtube_url, download_youtube_audio, download_youtube_video,
)
from songs import (
    validate_output_path, _patch_txt_content, build_song_folder,
    parse_ultrastar_txt, scan_local, build_match_report,
)
from worker import (
    save_usdb_cache, load_usdb_cache,
    process_single, process_queue,
    validate_queue_url, validate_delay,
)
from state import state, sse_broadcast, get_queue_snapshot, SSE_CLOSE
from status import SongStatus
from version import __version__


# === Flask app ===

app = Flask(__name__,
            static_folder=str(CODE_DIR / "static"),
            template_folder=str(CODE_DIR / "static"))


# --- save_config wrapper (parameterless, matches original API) ---
from config import save_config as _save_config_impl

def save_config():
    """Persist current state.config to disk."""
    _save_config_impl(state.config)


# === Request authentication and cross-origin guard ===

def _server_mode_enabled():
    return os.environ.get("USDB_SERVER_MODE", "").strip().lower() in {"1", "true", "yes"}


def _origin_is_allowed(origin):
    """Allow same-origin browsers; non-browser clients are authenticated separately."""
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
        expected = urlparse(request.host_url)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.scheme == expected.scheme
        and parsed.hostname == expected.hostname
        and parsed.port == expected.port
    )


def _server_credentials_valid():
    username = os.environ.get("USDB_USERNAME", "")
    password = os.environ.get("USDB_PASSWORD", "")
    supplied = request.authorization
    if not username or not password or supplied is None:
        return False
    return (
        secrets.compare_digest(supplied.username or "", username)
        and secrets.compare_digest(supplied.password or "", password)
    )


@app.before_request
def protect_requests():
    if request.path == "/health":
        return None
    if _server_mode_enabled() and not _server_credentials_valid():
        return Response(
            "Authentifizierung erforderlich",
            401,
            {"WWW-Authenticate": 'Basic realm="UltraStar Importer"'},
        )
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _origin_is_allowed(request.headers.get("Origin")):
        return jsonify({"ok": False, "error": "Cross-Origin-Anfrage abgelehnt"}), 403


# === Health and SSE endpoints ===

@app.route("/health")
def health():
    return jsonify({"ok": True, "version": __version__})


_LEGAL_DOCUMENTS = {
    "license": "LICENSE",
    "disclaimer": "DISCLAIMER.md",
    "third-party-notices": "THIRD_PARTY_NOTICES.md",
    "third-party-licenses": "THIRD_PARTY_LICENSES.txt",
}


@app.route("/legal/<slug>")
def legal_document(slug):
    """Serve only release-bundled legal documents from a fixed allowlist."""
    filename = _LEGAL_DOCUMENTS.get(slug)
    if filename is None:
        return Response("Nicht gefunden", status=404, mimetype="text/plain")
    path = CODE_DIR / filename
    if not path.is_file():
        return Response("Nicht gefunden", status=404, mimetype="text/plain")
    return send_file(path, mimetype="text/plain")


@app.route("/stream")
def stream():
    q = queue.Queue(maxsize=256)
    max_clients = max(1, int(os.environ.get("USDB_MAX_SSE_CLIENTS", "16")))
    with state.sse_lock:
        if len(state.sse_clients) >= max_clients:
            return jsonify({"ok": False, "error": "Zu viele Live-Verbindungen"}), 503
        state.sse_clients.append(q)
    q.put_nowait(json.dumps({"type": "queue", "data": get_queue_snapshot()},
                            ensure_ascii=False))

    def event_stream():
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    if msg is SSE_CLOSE:
                        break
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with state.sse_lock:
                if q in state.sse_clients:
                    state.sse_clients.remove(q)
    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# === API Routes ===

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/queue", methods=["GET"])
def api_queue():
    return jsonify(get_queue_snapshot())


@app.route("/api/queue/add", methods=["POST"])
def api_queue_add():
    data = request.json or {}
    urls = data.get("urls", "")

    # Normalize urls to a list of candidates.
    if isinstance(urls, str):
        url_list = [u.strip() for u in urls.splitlines() if u.strip()]
    elif isinstance(urls, list):
        url_list = urls
    else:
        return jsonify({"ok": False, "error": "'urls' muss ein String oder eine Liste sein"}), 400

    errors = []
    clean_urls = []
    for raw in url_list:
        clean, err = validate_queue_url(raw)
        if err:
            errors.append(f"{raw!r}: {err}")
        else:
            clean_urls.append(clean)

    added = 0
    with state.queue_lock:
        existing_urls = {q["url"] for q in state.queue}
        for url in clean_urls:
            if url in existing_urls:
                continue
            item = {
                "id": state.queue_next_id,
                "url": url,
                "status": SongStatus.PENDING.value,
                "progress": "Warteschlange",
                "artist": "", "title": "",
                "youtube_url": "", "usdb_id": None,
            }
            state.queue_next_id += 1
            state.queue.append(item)
            added += 1
    sse_broadcast("queue", get_queue_snapshot())
    if errors:
        return jsonify({"added": added, "errors": errors}), 400
    return jsonify({"added": added})


@app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear():
    # Task 3: only remove pending items; leave processing/done/error items.
    with state.queue_lock:
        state.queue = [q for q in state.queue if q["status"] != SongStatus.PENDING.value]
    sse_broadcast("queue", get_queue_snapshot())
    return jsonify({"ok": True})


@app.route("/api/queue/remove/<int:item_id>", methods=["POST"])
def api_queue_remove(item_id):
    with state.queue_lock:
        item = next((q for q in state.queue if q["id"] == item_id), None)
        if item is None:
            return jsonify({"ok": False, "error": "Queue-Item nicht gefunden"}), 404
        if item["status"] != SongStatus.PENDING.value:
            return jsonify({"ok": False, "error": "Nur wartende Items duerfen entfernt werden"}), 409
        state.queue.remove(item)
    sse_broadcast("queue", get_queue_snapshot())
    return jsonify({"ok": True})


@app.route("/api/queue/retry/<int:item_id>", methods=["POST"])
def api_queue_retry(item_id):
    with state.queue_lock:
        item = next((q for q in state.queue if q["id"] == item_id), None)
        if item is None:
            return jsonify({"ok": False, "error": "Queue-Item nicht gefunden"}), 404
        if item["status"] != SongStatus.ERROR.value:
            return jsonify({"ok": False, "error": "Nur fehlgeschlagene Items duerfen erneut gestartet werden"}), 409
        item["status"] = SongStatus.PENDING.value
        item["progress"] = "Warteschlange"
    sse_broadcast("queue", get_queue_snapshot())
    return jsonify({"ok": True})


@app.route("/api/worker/start", methods=["POST"])
def api_worker_start():
    with state.worker_lock:
        if state.worker_thread is not None and state.worker_thread.is_alive():
            return jsonify({"ok": False, "error": "Worker laeuft oder wird noch angehalten"}), 409
        state.worker_generation += 1
        generation = state.worker_generation
        state.worker_stop_event = threading.Event()
        state.worker_running = True
        state.worker_thread = threading.Thread(target=process_queue, args=(generation, state.worker_stop_event), daemon=True)
        state.worker_thread.start()
    return jsonify({"ok": True})


@app.route("/api/worker/stop", methods=["POST"])
def api_worker_stop():
    with state.worker_lock:
        if not state.worker_running:
            return jsonify({"ok": True, "message": "Worker laeuft nicht"})
        state.worker_stop_event.set()
    return jsonify({"ok": True, "message": "Nach dem aktuellen Arbeitsschritt wird angehalten"})


@app.route("/api/worker/status", methods=["GET"])
def api_worker_status():
    with state.worker_lock:
        running = state.worker_running
        stopping = running and state.worker_stop_event.is_set()
    return jsonify({"running": running, "stopping": stopping})


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.json or {}
    cookie = get_cookie()
    if not cookie or len(cookie) < 10:
        return jsonify({"ok": False, "error": "Kein Cookie! Bitte unter Einstellungen einloggen."}), 400
    interpret = str(data.get("interpret", "")).strip()[:100]
    title = str(data.get("title", "")).strip()[:100]
    edition = str(data.get("edition", "")).strip()[:100]
    if not interpret and not title and not edition:
        return jsonify({"ok": False, "error": "Mindestens ein Suchfeld ausfuellen."}), 400
    results, err = search_usdb(cookie, interpret, title, edition)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "count": len(results), "songs": results})


@app.route("/api/song/detail/<int:song_id>", methods=["GET"])
def api_song_detail(song_id):
    """Lightweight detail lookup: returns YouTube link + cover + meta for a
    single USDB song. Used by the proxy JS to pre-fill list rows."""
    cookie = get_cookie()
    if not cookie or len(cookie) < 10:
        return jsonify({"ok": False, "error": "Kein Cookie"}), 400
    detail, err = fetch_detail(str(song_id), cookie)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if not detail:
        return jsonify({"ok": False, "error": "Detail nicht gefunden"}), 404
    return jsonify({
        "ok": True,
        "usdb_id": detail.get("usdb_id", str(song_id)),
        "youtube_url": detail.get("youtube_url", ""),
        "cover_url": detail.get("cover_url", ""),
        "artist": detail.get("artist", ""),
        "title": detail.get("title", ""),
    })


# === USDB Proxy (for embedded browsing) ===

PROXY_INJECT_JS = r"""
<script>
(function() {
    // Intercept window.location assignments to keep navigation inside the proxy
    // USDB JS uses things like: window.location.href = '?link=detail&id=123'
    // The base tag approach escapes the proxy, so we rewrite assets instead.
    // We patch it so relative ?link=... URLs go to /proxy?...
    var origLocation = window.location;

    function proxyUrl(url) {
        if (!url) return url;
        if (url.indexOf('?link=') === 0 || url.indexOf('index.php?') === 0) {
            return '/proxy?' + url.replace(/^(\?|index\.php\?)/, '');
        }
        return url;
    }

    // Override the location setter
    try {
        var loc = window.location;
        var origHref = Object.getOwnPropertyDescriptor(Window.prototype, 'location');
        // Can't override location directly, but we can intercept beforeunload
    } catch(e) {}

    // Intercept clicks on elements that use JS navigation (onclick="window.location=...")
    document.addEventListener('click', function(e) {
        // Let it happen naturally - the onclick will fire
        // But we also listen for location changes via a MutationObserver-free approach
    }, true);

    // Patch window.location.href via a beforeunload handler
    // Actually: rewrite all onclick handlers that contain window.location
    function rewriteOnClicks() {
        var elements = document.querySelectorAll('[onclick]');
        elements.forEach(function(el) {
            var oc = el.getAttribute('onclick');
            if (oc && oc.indexOf('window.location') >= 0) {
                // Replace ?link=... with /proxy?link=...
                var newOc = oc.replace(/window\.location(\.href)?\s*=\s*['"]?\?link=/g,
                    "window.location.href='/proxy?link=");
                newOc = newOc.replace(/window\.location(\.href)?\s*=\s*['"]?index\.php\?link=/g,
                    "window.location.href='/proxy?link=");
                if (newOc !== oc) {
                    el.setAttribute('onclick', newOc);
                }
            }
        });
    }

    // Override USDB's show_detail function to navigate through proxy
    function overrideShowDetail() {
        if (typeof window.show_detail === 'function') {
            var origShowDetail = window.show_detail;
            window.show_detail = function(id) {
                window.location.href = '/proxy?link=detail&id=' + id;
            };
        }
    }

    // Add floating "Add to Queue" button on detail pages
    function checkDetailPage() {
        var m = window.location.search.match(/[?&]link=detail&id=(\d+)/);
        if (!m) { removeQueueBtn(); removeYtBadge(); return; }
        var songId = m[1];
        if (document.getElementById('usdb-proxy-queue-btn')) return;
        var btn = document.createElement('button');
        btn.id = 'usdb-proxy-queue-btn';
        btn.innerHTML = '➕ Zur Queue hinzufügen';
        btn.style.cssText = 'position:fixed!important;bottom:20px!important;right:20px!important;'
            + 'z-index:999999!important;padding:14px 28px!important;font-size:16px!important;'
            + 'background:#f59e0b!important;color:#000!important;border:none!important;'
            + 'border-radius:10px!important;cursor:pointer!important;font-weight:bold!important;'
            + 'box-shadow:0 4px 16px rgba(0,0,0,0.4)!important;';
        btn.onclick = function() {
            btn.textContent = 'Lädt...';
            fetch('/api/queue/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({urls: 'https://usdb.animux.de/index.php?link=detail&id=' + songId})
            }).then(function(r) { return r.json(); })
              .then(function(data) {
                if (data.added > 0) {
                    btn.textContent = '✔ Hinzugefügt!';
                    btn.style.background = '#22c55e';
                } else {
                    btn.textContent = 'Bereits in Queue';
                    btn.style.background = '#8b8b94';
                }
                btn.disabled = true;
              })
              .catch(function() {
                  btn.textContent = 'Fehler';
                  btn.style.background = '#ef4444';
              });
        };
        document.body.appendChild(btn);
        checkYoutubeLink();
    }
    function removeQueueBtn() {
        var b = document.getElementById('usdb-proxy-queue-btn');
        if (b) b.remove();
    }

    // YouTube link detection on detail pages
    function findYoutubeLink() {
        // 1. Check for YouTube embeds (iframe)
        var iframes = document.querySelectorAll('iframe[src]');
        for (var i = 0; i < iframes.length; i++) {
            var src = iframes[i].getAttribute('src') || '';
            var vm = src.match(/youtube\.com\/embed\/([\w-]{11})/);
            if (vm) return 'https://www.youtube.com/watch?v=' + vm[1];
        }
        // 2. Check for YouTube links (anchor tags)
        var links = document.querySelectorAll('a[href]');
        for (var j = 0; j < links.length; j++) {
            var href = links[j].getAttribute('href') || '';
            var wm = href.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]{11})/);
            if (wm) return 'https://www.youtube.com/watch?v=' + wm[1];
        }
        // 3. Check for plain text YouTube URLs in page text
        var pageText = document.body ? document.body.innerHTML : '';
        var tm = pageText.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([\w-]{11})/);
        if (tm) return 'https://www.youtube.com/watch?v=' + tm[1];
        return null;
    }

    function checkYoutubeLink() {
        removeYtBadge();
        var ytUrl = findYoutubeLink();
        var badge = document.createElement('div');
        badge.id = 'usdb-proxy-yt-badge';
        if (ytUrl) {
            badge.innerHTML = '✔ YouTube verfügbar';
            badge.style.cssText = 'position:fixed!important;bottom:72px!important;right:20px!important;'
                + 'z-index:999998!important;padding:10px 20px!important;font-size:14px!important;'
                + 'background:#22c55e!important;color:#fff!important;border:none!important;'
                + 'border-radius:8px!important;font-weight:bold!important;'
                + 'box-shadow:0 2px 12px rgba(0,0,0,0.3)!important;cursor:pointer!important;';
            badge.title = ytUrl;
            badge.onclick = function() { window.open(ytUrl, '_blank'); };
        } else {
            badge.innerHTML = '✖ Kein YouTube-Link';
            badge.style.cssText = 'position:fixed!important;bottom:72px!important;right:20px!important;'
                + 'z-index:999998!important;padding:10px 20px!important;font-size:14px!important;'
                + 'background:#ef4444!important;color:#fff!important;border:none!important;'
                + 'border-radius:8px!important;font-weight:bold!important;'
                + 'box-shadow:0 2px 12px rgba(0,0,0,0.3)!important;';
        }
        document.body.appendChild(badge);
    }
    function removeYtBadge() {
        var b = document.getElementById('usdb-proxy-yt-badge');
        if (b) b.remove();
    }
    // Check on load and on navigation (debounced — MutationObserver fires
    // on every DOM change including our own, so we coalesce rapid bursts)
    var _observerTimer = null;
    function onMutations() {
        if (_observerTimer) return;
        _observerTimer = setTimeout(function() {
            _observerTimer = null;
            checkDetailPage();
            checkListPage();
        }, 200);
    }
    var observer = new MutationObserver(onMutations);
    document.addEventListener('DOMContentLoaded', function() {
        rewriteOnClicks();
        overrideShowDetail();
        checkDetailPage();
        checkListPage();
        if (document.body) observer.observe(document.body, {childList: true, subtree: true});
    });
    // Also rewrite onclicks immediately and after slight delay (for dynamically added content)
    rewriteOnClicks();
    overrideShowDetail();
    setTimeout(function() { rewriteOnClicks(); overrideShowDetail(); checkListPage(); }, 500);
    setTimeout(function() { rewriteOnClicks(); overrideShowDetail(); checkListPage(); }, 1500);
    setTimeout(function() { checkListPage(); }, 3000);
    checkDetailPage();

    // ======== LIST PAGE SCAN: YouTube badges + Queue buttons ========
    var _listScanActive = false;
    var _listPageDone = false;       // scan completed for current page
    var _listPageSig = '';           // page signature (url + row count) to detect new searches
    var _listDetailCache = {};       // songId → {yt_url, status}
    var _listScanQueue = [];
    var _listScanIdx = 0;
    var _listScanTotal = 0;

    function isListPage() {
        var s = window.location.search;
        // link=list (search results) or link=browse (default landing, also shows song table)
        return /[?&]link=(list|browse)\b/.test(s) || s.indexOf('link=') === -1 && s === '';
    }

    function getListSongRows() {
        // USDB song rows: <tr> containing an <a href="?link=detail&id=123">
        var rows = [];
        var links = document.querySelectorAll('a[href]');
        for (var i = 0; i < links.length; i++) {
            var href = links[i].getAttribute('href') || '';
            var m = href.match(/id=(\d+)/);
            if (m && /detail/.test(href)) {
                var tr = links[i].closest('tr');
                if (tr && rows.indexOf(tr) === -1) rows.push(tr);
            }
        }
        return rows;
    }

    function ensureListScanColumn() {
        // Add a header cell if the table has a thead
        var tables = document.querySelectorAll('table');
        for (var t = 0; t < tables.length; t++) {
            var ths = tables[t].querySelectorAll('thead th');
            if (ths.length > 0 && !tables[t].querySelector('th.usi-scan-col')) {
                var th = document.createElement('th');
                th.className = 'usi-scan-col';
                th.textContent = 'YT/Queue';
                th.style.cssText = 'font-size:11px;padding:4px 6px;text-align:center;white-space:nowrap;color:#aaa;';
                ths[ths.length - 1].parentNode.appendChild(th);
            }
        }
    }

    function addListScanCell(tr, songId) {
        if (tr.querySelector('.usi-list-cell')) return null;
        // Add a new <td> at the end of the row
        var td = document.createElement('td');
        td.className = 'usi-list-cell';
        td.setAttribute('data-song-id', songId);
        td.style.cssText = 'text-align:center;white-space:nowrap;padding:4px 6px;vertical-align:middle;';
        td.innerHTML = '<span style="font-size:11px;color:#888;">…</span>';
        // Try to append as last cell
        tr.appendChild(td);
        return td;
    }

    function updateListCell(td, songId) {
        var cached = _listDetailCache[songId];
        var yt = cached && cached.youtube_url ? cached.youtube_url : '';
        var renderKey = cached ? ('ready:' + yt) : 'loading';
        // DIRTY CHECK: only update DOM if content actually changed.
        if (td.getAttribute('data-usi-rendered') === renderKey) return;
        td.setAttribute('data-usi-rendered', renderKey);
        td.replaceChildren();

        if (!cached) {
            var loading = document.createElement('span');
            loading.style.cssText = 'font-size:11px;color:#666;';
            loading.textContent = '…';
            td.appendChild(loading);
            return;
        }

        var badge = document.createElement('span');
        badge.style.cssText = 'display:inline-block;padding:2px 6px;font-size:10px;font-weight:bold;border-radius:4px;';
        if (yt) {
            badge.title = yt;
            badge.textContent = 'YT';
            badge.style.cssText += 'background:#22c55e;color:#fff;cursor:pointer;';
            badge.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();
                window.open(yt, '_blank', 'noopener,noreferrer');
            });
        } else {
            badge.title = 'Kein YouTube-Link';
            badge.textContent = '—';
            badge.style.cssText += 'background:#444;color:#888;';
        }
        td.appendChild(badge);
        td.appendChild(document.createTextNode(' '));

        var btn = document.createElement('button');
        btn.setAttribute('data-queue-id', songId);
        btn.style.cssText = 'padding:2px 8px;font-size:11px;font-weight:bold;cursor:pointer;background:#f59e0b;color:#000;border:none;border-radius:4px;';
        btn.textContent = '➕';
        btn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            queueFromList(songId, btn);
        });
        td.appendChild(btn);
    }

    function queueFromList(songId, btn) {
        var orig = btn.innerHTML;
        btn.innerHTML = '…';
        btn.disabled = true;
        fetch('/api/queue/add', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({urls: 'https://usdb.animux.de/index.php?link=detail&id=' + songId})
        }).then(function(r) { return r.json(); })
          .then(function(data) {
              if (data.added > 0) {
                  btn.innerHTML = '✔';
                  btn.style.background = '#22c55e';
              } else {
                  btn.innerHTML = '—';
                  btn.style.background = '#555';
              }
          })
          .catch(function() {
              btn.innerHTML = orig;
              btn.disabled = false;
          });
    }

    // --- Progress bar ---
    function ensureListProgressBar() {
        var bar = document.getElementById('usi-list-progress');
        if (bar) return;
        bar = document.createElement('div');
        bar.id = 'usi-list-progress';
        bar.style.cssText = 'position:sticky;top:0;z-index:999997;'
            + 'background:rgba(20,20,25,0.92);padding:6px 12px;'
            + 'border-bottom:1px solid #333;font-size:11px;color:#ccc;'
            + 'display:flex;align-items:center;gap:8px;';
        bar.innerHTML =
            '<span id="usi-lp-text" style="white-space:nowrap;">Scanne…</span>'
            + '<div style="flex:1;height:6px;background:#333;border-radius:3px;overflow:hidden;">'
            + '<div id="usi-lp-fill" style="width:0%;height:100%;background:linear-gradient(90deg,#f59e0b,#fbbf24);'
            + 'border-radius:3px;transition:width 0.3s ease;"></div>'
            + '</div>';
        // Insert at top of body
        if (document.body) document.body.insertBefore(bar, document.body.firstChild);
    }

    function updateListProgress(done, total) {
        var fill = document.getElementById('usi-lp-fill');
        var text = document.getElementById('usi-lp-text');
        if (fill) {
            var pct = total > 0 ? Math.round(done / total * 100) : 0;
            fill.style.width = pct + '%';
        }
        if (text) {
            if (done >= total) {
                var ytCount = 0;
                for (var k in _listDetailCache) { if (_listDetailCache[k].youtube_url) ytCount++; }
                text.textContent = '✓ ' + done + '/' + total + ' gescannt · ' + ytCount + ' mit YouTube';
                var bar = document.getElementById('usi-list-progress');
                if (bar) {
                    // Auto-fade after 4 seconds
                    setTimeout(function() {
                        if (bar) {
                            bar.style.transition = 'opacity 0.5s';
                            bar.style.opacity = '0';
                            setTimeout(function() {
                                if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
                            }, 600);
                        }
                    }, 4000);
                }
            } else {
                text.textContent = 'Scanne ' + done + '/' + total + ' …';
            }
        }
    }

    function removeListProgressBar() {
        var bar = document.getElementById('usi-list-progress');
        if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
    }

    // --- Sequential background scan ---
    function startListScan() {
        if (_listScanActive) return;
        var rows = getListSongRows();
        if (rows.length === 0) return;

        _listScanActive = true;
        _listScanQueue = [];
        _listScanIdx = 0;
        _listScanTotal = 0;

        ensureListScanColumn();
        ensureListProgressBar();

        for (var i = 0; i < rows.length; i++) {
            var tr = rows[i];
            // Extract song ID from the detail link inside the row
            var a = tr.querySelector('a[href*="detail"]');
            if (!a) continue;
            var m = (a.getAttribute('href') || '').match(/id=(\d+)/);
            if (!m) continue;
            var songId = m[1];
            if (_listDetailCache[songId]) {
                // Already cached — just update the cell
                var td = addListScanCell(tr, songId);
                if (td) updateListCell(td, songId);
                continue;
            }
            addListScanCell(tr, songId);
            _listScanQueue.push({tr: tr, songId: songId});
        }

        _listScanTotal = _listScanQueue.length;
        if (_listScanTotal === 0) {
            // All cached — update existing cells, remove progress bar
            refreshAllListCells();
            removeListProgressBar();
            _listScanActive = false;
            _listPageDone = true;
            return;
        }

        updateListProgress(0, _listScanTotal);
        scanNextListItem();
    }

    function scanNextListItem() {
        if (_listScanIdx >= _listScanTotal) {
            _listScanActive = false;
            _listPageDone = true;
            updateListProgress(_listScanTotal, _listScanTotal);
            return;
        }
        var item = _listScanQueue[_listScanIdx];
        var songId = item.songId;

        fetch('/api/song/detail/' + songId)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.ok) {
                    _listDetailCache[songId] = {
                        youtube_url: data.youtube_url || '',
                        cover_url: data.cover_url || ''
                    };
                } else {
                    _listDetailCache[songId] = {youtube_url: '', cover_url: '', error: true};
                }
                refreshListCell(item.tr, songId);
                _listScanIdx++;
                updateListProgress(_listScanIdx, _listScanTotal);
                // Small delay to avoid hammering USDB
                setTimeout(scanNextListItem, 300);
            })
            .catch(function() {
                _listDetailCache[songId] = {youtube_url: '', cover_url: '', error: true};
                refreshListCell(item.tr, songId);
                _listScanIdx++;
                updateListProgress(_listScanIdx, _listScanTotal);
                setTimeout(scanNextListItem, 300);
            });
    }

    function refreshListCell(tr, songId) {
        var td = tr.querySelector('.usi-list-cell');
        if (td) updateListCell(td, songId);
    }

    function refreshAllListCells() {
        var rows = getListSongRows();
        for (var i = 0; i < rows.length; i++) {
            var a = rows[i].querySelector('a[href*="detail"]');
            if (!a) continue;
            var m = (a.getAttribute('href') || '').match(/id=(\d+)/);
            if (!m) continue;
            refreshListCell(rows[i], m[1]);
        }
    }

    function checkListPage() {
        if (!isListPage()) {
            _listPageDone = false;
            _listPageSig = '';
            removeListProgressBar();
            return;
        }
        // Don't run if a scan is already in progress
        if (_listScanActive) return;

        // Compute page signature: URL + row count
        // This detects when a new search has been executed
        var rows = getListSongRows();
        var sig = window.location.search + '|' + rows.length;
        if (sig === _listPageSig && _listPageDone) return; // Already scanned this exact page
        _listPageSig = sig;
        _listPageDone = false;

        if (rows.length > 0) {
            startListScan();
        }
    }

    // --- Volume control: apply saved volume to all media + nested iframes ---
    function applyVolume() {
        var vol = parseFloat(localStorage.getItem('usi_volume'));
        if (isNaN(vol)) vol = 0.3; // default 30%
        try {
            document.querySelectorAll('video, audio').forEach(function(m) { m.volume = vol; });
        } catch(e) {}
    }
    applyVolume();
    // Re-apply when new media is added
    var volObserver = new MutationObserver(applyVolume);
    if (document.body) volObserver.observe(document.body, {childList: true, subtree: true});
    // Listen for volume changes from parent (postMessage)
    window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'usi_set_volume') {
            try {
                document.querySelectorAll('video, audio').forEach(function(m) { m.volume = e.data.volume; });
            } catch(err) {}
        }
    });
})();
</script>
"""


@app.route("/favicon.ico")
def favicon():
    return send_file(CODE_DIR / "static" / "favicon.ico")


@app.route("/proxy", methods=["GET", "POST"])
def usdb_proxy():
    """Proxy USDB pages with cookie injected, rewrite links to go through proxy."""
    cookie = get_cookie()
    if not cookie or len(cookie) < 10:
        return (
            "<html><body><div style='font-family:sans-serif;padding:40px;text-align:center;color:#f59e0b;'>"
            "<h2>Nicht eingeloggt</h2><p>Bitte zuerst unter Einstellungen einloggen.</p>"
            "<p><a href='/' style='color:#f59e0b;'>Zur Einstellungen</a></p></div></body></html>"
        ), 200

    # Build the USDB URL from query params
    usdb_params = dict(request.args)
    link = usdb_params.pop("link", "browse")
    # POST form data (USDB search forms use POST) — sent in the body, not the URL
    post_data = dict(request.form) if request.method == "POST" else {}
    # If POST contains a link value, use it
    if "link" in post_data:
        link = post_data.pop("link")

    # --- Search persistence: redirect POST search → GET with all params in URL ---
    # USDB search forms POST to /proxy?link=list with interpret/title/edition in
    # the body.  The body is ephemeral — it never enters the iframe history, so
    # pressing "back" from a detail page loses the filter.  Redirect to a GET
    # URL that carries the same params; the browser follows 303 automatically.
    if request.method == "POST" and link == "list":
        qs = urlencode({"link": "list", **post_data})
        return redirect("/proxy?" + qs, code=303)

    # Build query string ONLY from the link param and remaining GET query args.
    # urlencode handles encoding properly (no raw concatenation / injection).
    get_qs = urlencode({"link": link, **usdb_params})
    usdb_url = USDB_BASE + "index.php?" + get_qs

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie,
    }
    try:
        # Use POST if original was POST (USDB expects POST for search);
        # pass form fields in the body via data= (previously dropped).
        if request.method == "POST" and post_data:
            resp = req_lib.post(usdb_url, headers=headers, data=post_data, timeout=15)
        else:
            resp = req_lib.get(usdb_url, headers=headers, timeout=15)
    except Exception as e:
        # Escape the exception text to avoid reflected XSS in the error page.
        safe_err = html_lib.escape(str(e))
        return f"<html><body><p>Proxy error: {safe_err}</p></body></html>", 502

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    # The proxied document is rendered under the application's own origin.
    # Never allow remote USDB code to inherit access to privileged local APIs.
    for active in soup.find_all(["script", "base", "iframe", "frame", "object", "embed"]):
        active.decompose()
    for meta in soup.find_all("meta"):
        if str(meta.get("http-equiv", "")).strip().lower() == "refresh":
            meta.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
        for attr in ("href", "src", "action", "formaction"):
            value = tag.get(attr)
            if not isinstance(value, str):
                continue
            lowered = value.strip().lower()
            if lowered.startswith(("javascript:", "vbscript:")):
                del tag.attrs[attr]
            elif lowered.startswith("data:") and not (
                attr == "src" and lowered.startswith("data:image/")
            ):
                del tag.attrs[attr]

    # Do NOT use <base> — it breaks /proxy links by resolving them against usdb.animux.de.

    for tag in soup.find_all(["img", "link", "script", "source", "video"]):
        for attr in ("src", "href"):
            val = tag.get(attr)
            if not val:
                continue
            if val.startswith("//"):
                tag[attr] = "https:" + val
            elif val.startswith("/"):
                tag[attr] = USDB_BASE.rstrip("/") + val
            elif val.startswith("http"):
                pass  # already absolute
            elif not val.startswith("data:") and not val.startswith("#"):
                # Relative path
                tag[attr] = urljoin(USDB_BASE, val)

    # Rewrite all links: href="index.php?..." / "?link=..." -> href="/proxy?..."
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("index.php?"):
            a["href"] = "/proxy?" + href[len("index.php?"):]
        elif href.startswith("?link="):
            a["href"] = "/proxy?" + href[1:]
        elif href.startswith("http") and "usdb.animux.de" in href and "index.php" in href:
            if "?" in href:
                a["href"] = "/proxy?" + href.split("?", 1)[1]

    # Rewrite form actions and ensure link parameter survives GET submissions
    for form in soup.find_all("form"):
        action = form.get("action", "")
        method = (form.get("method") or "get").lower()

        if "gettxt" in action:
            # Don't proxy txt download forms
            form["action"] = "/proxy?link=gettxt" if not action.startswith("?") else "/proxy?" + action.lstrip("?")
        elif action.startswith("?link=") or action.startswith("index.php"):
            query = action.lstrip("?").replace("index.php?", "")
            # Extract the link value
            link_match = re.search(r"link=(\w+)", query)
            form["action"] = "/proxy"

            # For GET forms: add hidden field so link= survives form serialization
            if method == "get" and link_match:
                link_val = link_match.group(1)
                # Remove existing hidden link field if any
                for existing in form.find_all("input", {"name": "link"}):
                    existing.decompose()
                hidden = soup.new_tag("input", attrs={
                    "type": "hidden", "name": "link", "value": link_val
                })
                form.insert(0, hidden)
            elif method == "post" and link_match:
                form["action"] = "/proxy?" + query
        elif not action:
            # Default action: point to proxy
            form["action"] = "/proxy"
            # Check if this is a search/browse form on the browse page
            if method == "get":
                # Ensure link=list is preserved
                if not form.find("input", {"name": "link"}):
                    hidden = soup.new_tag("input", attrs={
                        "type": "hidden", "name": "link", "value": "list"
                    })
                    form.insert(0, hidden)

    # Inject our JS
    inject_soup = BeautifulSoup(PROXY_INJECT_JS, "html.parser")
    if soup.body:
        soup.body.append(inject_soup)

    response = Response(str(soup), mimetype="text/html")
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://usdb.animux.de; "
        "img-src 'self' data: https://usdb.animux.de; "
        "media-src 'self' https://usdb.animux.de; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; "
        "frame-src 'none'; object-src 'none'; frame-ancestors 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify({
        "has_cookie": bool(get_cookie()),
        "local_path": state.config.get("local_path", ""),
        "delay": state.config.get("delay", 0.5),
        "output_path": state.config.get("output_path", ""),
        "data_path": str(DATA_DIR),
        "code_dir": str(CODE_DIR),
        # Audio quality
        "audio_format": state.config.get("audio_format", "mp3"),
        "audio_bitrate": state.config.get("audio_bitrate", 192),
        "audio_normalize": state.config.get("audio_normalize", False),
        "audio_normalize_strength": state.config.get("audio_normalize_strength", "loudnorm"),
        # Video quality
        "video_resolution": state.config.get("video_resolution", 1080),
        "video_format": state.config.get("video_format", "mp4"),
        # Image processing
        "cover_resize": state.config.get("cover_resize", 0),
        "cover_autofix": state.config.get("cover_autofix", False),
        # Meta-tag behavior
        "use_meta_tags": state.config.get("use_meta_tags", True),
        # Concurrency
        "max_workers": state.config.get("max_workers", 1),
    })


@app.route("/api/settings", methods=["PUT"])
def api_settings_put():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON-Objekt erwartet"}), 400
    for key in ("local_path", "output_path"):
        if key in data:
            if not isinstance(data[key], str):
                return jsonify({"ok": False, "error": f"{key} muss ein String sein"}), 400
            state.config[key] = data[key].strip()
    if "data_path" in data:
        if not isinstance(data["data_path"], str):
            return jsonify({"ok": False, "error": "data_path muss ein String sein"}), 400
        requested_data_path = data["data_path"].strip()
        if requested_data_path:
            try:
                Path(requested_data_path).expanduser().resolve().mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError) as exc:
                return jsonify({"ok": False, "error": f"Datenpfad nicht verwendbar: {exc}"}), 400
        state.config["data_path"] = requested_data_path
    if "cookie" in data:
        if not isinstance(data["cookie"], str):
            return jsonify({"ok": False, "error": "cookie muss ein String sein"}), 400
        set_cookie(data["cookie"].strip())
    if "delay" in data:
        d, err = validate_delay(data["delay"])
        if err:
            return jsonify({"ok": False, "error": err}), 400
        state.config["delay"] = d
    # --- Audio quality ---
    if "audio_format" in data:
        fmt = str(data["audio_format"]).strip().lower()
        if fmt not in ("mp3", "m4a", "opus", "vorbis"):
            return jsonify({"ok": False, "error": "audio_format ungültig"}), 400
        state.config["audio_format"] = fmt
    if "audio_bitrate" in data:
        try:
            br = int(data["audio_bitrate"])
            if br < 64 or br > 320:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "audio_bitrate muss 64-320 sein"}), 400
        state.config["audio_bitrate"] = br
    if "audio_normalize" in data:
        state.config["audio_normalize"] = bool(data["audio_normalize"])
    if "audio_normalize_strength" in data:
        method = str(data["audio_normalize_strength"]).strip().lower()
        if method not in ("loudnorm", "replaygain"):
            return jsonify({"ok": False, "error": "audio_normalize_strength ungültig"}), 400
        state.config["audio_normalize_strength"] = method
    # --- Video quality ---
    if "video_resolution" in data:
        try:
            res = int(data["video_resolution"])
            if res not in (360, 480, 720, 1080, 1440, 2160):
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "video_resolution ungültig"}), 400
        state.config["video_resolution"] = res
    if "video_format" in data:
        fmt = str(data["video_format"]).strip().lower()
        if fmt not in ("mp4", "webm", "mkv"):
            return jsonify({"ok": False, "error": "video_format ungültig"}), 400
        state.config["video_format"] = fmt
    # --- Image processing ---
    if "cover_resize" in data:
        try:
            state.config["cover_resize"] = int(data["cover_resize"])
            if state.config["cover_resize"] < 0:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "cover_resize muss >= 0 sein"}), 400
    if "cover_autofix" in data:
        state.config["cover_autofix"] = bool(data["cover_autofix"])
    # --- Meta tags ---
    if "use_meta_tags" in data:
        state.config["use_meta_tags"] = bool(data["use_meta_tags"])
    # --- Concurrency ---
    if "max_workers" in data:
        try:
            w = int(data["max_workers"])
            if w < 1 or w > 10:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "max_workers muss 1-10 sein"}), 400
        state.config["max_workers"] = w
    save_config()
    return jsonify({"ok": True})


@app.route("/api/cookie/transfer", methods=["POST"])
def api_cookie_transfer():
    token = request.headers.get("X-Transfer-Token", "")
    with state.worker_lock:
        expected = state.login_transfer_token
        if not expected or not secrets.compare_digest(token, expected):
            return jsonify({"ok": False, "error": "Ungueltiger Login-Transfer"}), 403
        state.login_transfer_token = None
    cookie = (request.get_json(silent=True) or {}).get("cookie", "")
    if not isinstance(cookie, str) or not cookie.strip():
        return jsonify({"ok": False, "error": "Kein Cookie erhalten"}), 400
    set_cookie(cookie.strip())
    sse_broadcast("cookie_set", {"ok": True})
    return jsonify({"ok": True})


@app.route("/api/cookie/forget", methods=["POST"])
def api_cookie_forget():
    set_cookie("")
    sse_broadcast("cookie_set", {"ok": False})
    return jsonify({"ok": True})


@app.route("/api/credentials", methods=["GET"])
def api_credentials_get():
    user, _ = get_login_credentials()
    return jsonify({
        "has_credentials": bool(user),
        "username": user,
    })


@app.route("/api/credentials", methods=["PUT"])
def api_credentials_put():
    data = request.json or {}
    user = str(data.get("username", "")).strip()
    raw_password = data.get("password", "")
    if not isinstance(raw_password, str):
        return jsonify({"ok": False, "error": "Ungueltige Eingabe"}), 400
    password = raw_password
    if user and not password:
        _, password = get_login_credentials()
    try:
        set_login_credentials(user, password)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/credentials/delete", methods=["POST"])
def api_credentials_delete():
    try:
        set_login_credentials("", "")
    except RuntimeError:
        pass
    return jsonify({"ok": True})


@app.route("/api/auto-login", methods=["POST"])
def api_auto_login():
    cookie, err = auto_login()
    if cookie:
        sse_broadcast("cookie_set", {"ok": True})
        return jsonify({"ok": True, "message": "Auto-Login erfolgreich"})
    return jsonify({"ok": False, "error": err or "Auto-Login fehlgeschlagen"}), 400


@app.route("/api/cookie/from-browser", methods=["POST"])
def api_cookie_from_browser_extract():
    """Extract USDB cookie directly from a browser's cookie store."""
    if _server_mode_enabled():
        return jsonify({"ok": False, "error": "Browser-Cookies sind im Headless-Servermodus nicht verfügbar."}), 409
    from browser_cookies import extract_usdb_cookie_from_browser, try_all_browsers, list_available_browsers
    data = request.get_json(silent=True) or {}
    browser = str(data.get("browser", "")).strip().lower()

    if browser:
        cookie, err = extract_usdb_cookie_from_browser(browser)
        if cookie:
            set_cookie(cookie)
            sse_broadcast("cookie_set", {"ok": True})
            return jsonify({"ok": True, "message": f"Cookie aus {browser} übernommen"})
        return jsonify({"ok": False, "error": err or f"Kein Cookie in {browser} gefunden"}), 400
    else:
        # Try all browsers
        cookie, results = try_all_browsers()
        if cookie:
            set_cookie(cookie)
            sse_broadcast("cookie_set", {"ok": True})
            # Find which browser worked
            found = [b for b, (ok, _) in results.items() if ok]
            return jsonify({"ok": True, "message": f"Cookie aus {found[0]} übernommen",
                            "results": {b: msg for b, (_, msg) in results.items()}})
        return jsonify({"ok": False, "error": "Kein USDB-Cookie in keinem Browser gefunden",
                        "results": {b: msg for b, (_, msg) in results.items()}}), 400


@app.route("/api/cookie/browsers", methods=["GET"])
def api_cookie_browsers():
    """List supported browsers for cookie extraction."""
    if _server_mode_enabled():
        return jsonify({"browsers": [], "server_mode": True})
    from browser_cookies import list_available_browsers
    return jsonify({"browsers": list_available_browsers()})


@app.route("/api/login-window", methods=["POST"])
def api_login_window():
    """Launch a login window with a one-time, server-issued transfer token."""
    if _server_mode_enabled():
        return jsonify({
            "ok": False,
            "error": "Browser-Login ist im Headless-Servermodus nicht verfügbar; bitte USDB-Zugangsdaten speichern und Auto-Login verwenden.",
        }), 409
    with state.worker_lock:
        state.login_transfer_token = secrets.token_urlsafe(32)
        token = state.login_transfer_token
    try:
        env = {**os.environ, "USDB_TRANSFER_TOKEN": token}

        if getattr(sys, "frozen", False):
            # The main EXE already owns pywebview's event loop. Create another
            # window in that loop; never call webview.start() a second time.
            import login_window as lw
            lw.create_login_window(token)
        else:
            # Running as script: spawn login_window.py as subprocess
            login_script = str(Path(__file__).parent / "login_window.py")
            subprocess.Popen([sys.executable, login_script, "--transfer-token", token],
                             cwd=str(Path(__file__).parent), env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True})
    except Exception as exc:
        with state.worker_lock:
            state.login_transfer_token = None
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/cookie/test", methods=["POST"])
def api_cookie_test():
    supplied = (request.get_json(silent=True) or {}).get("cookie", "")
    cookie = supplied if isinstance(supplied, str) and supplied else get_cookie()

    # Test: can we actually download a .txt? (stricter than detail page)
    txt_result, txt_err = fetch_txt("3045", cookie)
    if txt_result:
        # Also get detail for song name
        detail, _ = fetch_detail("3045", cookie)
        song_name = "?"
        if detail:
            song_name = detail.get("artist", "?") + " - " + detail.get("title", "?")
        return jsonify({"ok": True, "song": song_name,
                        "message": "Cookie gueltig - .txt-Download funktioniert!"})
    else:
        return jsonify({"ok": False,
                        "error": txt_err or "Cookie ungueltig oder abgelaufen"})


# === Output management ===

@app.route("/api/output/list", methods=["GET"])
def api_output_list():
    """List all song folders in the output directory."""
    output_base = state.config.get("output_path", "")
    if not output_base:
        output_base = str(DATA_DIR / "output")
    base = Path(output_base)
    if not base.exists():
        return jsonify({"ok": True, "songs": [], "output_path": str(base)})

    songs = []
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        all_files = [f.name for f in folder.iterdir() if f.is_file()]
        suffixes = {Path(f).suffix.lower() for f in all_files}
        audio_exts = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".flac"}
        video_exts = {".mp4", ".avi", ".webm", ".mkv", ".mov", ".mpg", ".mpeg", ".ts"}
        song = {
            "folder": folder.name,
            "path": str(folder),
            "has_txt": ".txt" in suffixes,
            "has_mp3": bool(suffixes & audio_exts),
            "has_video": bool(suffixes & video_exts),
            "has_cover": any("[CO]" in f for f in all_files),
            "size_mb": round(sum(f.stat().st_size for f in folder.iterdir() if f.is_file()) / 1048576, 1),
        }
        # Parse "Artist - Title" from folder name for grouping
        if " - " in folder.name:
            parts = folder.name.split(" - ", 1)
            song["artist"] = parts[0].strip()
            song["title"] = parts[1].strip()
        else:
            song["artist"] = "Unbekannt"
            song["title"] = folder.name
        songs.append(song)
    return jsonify({"ok": True, "songs": songs, "output_path": str(base)})


def _resolve_output_folder(output_base, folder_name):
    """Resolve a user-supplied output folder without allowing base escape."""
    base = Path(output_base).resolve()
    target = (base / folder_name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


@app.route("/api/output/cover", methods=["GET"])
def api_output_cover():
    """Serve the cover image from a song folder."""
    folder_name = request.args.get("folder", "").strip()
    if not folder_name:
        return "", 400

    output_base = state.config.get("output_path", "")
    if not output_base:
        output_base = str(DATA_DIR / "output")
    folder = _resolve_output_folder(output_base, folder_name)
    if folder is None:
        return "", 400
    if not folder.is_dir():
        return "", 404

    for f in folder.iterdir():
        if f.is_file() and "[CO]" in f.name and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            return send_file(str(f), mimetype="image/jpeg", conditional=True)
    return "", 404


@app.route("/api/output/preview", methods=["GET"])
def api_output_preview():
    """Stream the audio file from a song folder for preview playback.
    Supports HTTP Range requests for seeking."""
    folder_name = request.args.get("folder", "").strip()
    if not folder_name:
        return jsonify({"ok": False, "error": "Ordnername fehlt"}), 400

    output_base = state.config.get("output_path", "")
    if not output_base:
        output_base = str(DATA_DIR / "output")
    folder = _resolve_output_folder(output_base, folder_name)
    if folder is None:
        return jsonify({"ok": False, "error": "Pfad-Traversale verweigert"}), 400
    if not folder.is_dir():
        return jsonify({"ok": False, "error": "Ordner nicht gefunden"}), 404

    audio_exts = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".flac"}
    audio_file = None
    for f in folder.iterdir():
        if f.is_file() and f.suffix.lower() in audio_exts:
            audio_file = f
            break
    if not audio_file:
        return jsonify({"ok": False, "error": "Keine Audiodatei gefunden"}), 404

    return send_file(
        str(audio_file),
        mimetype="audio/mpeg",
        conditional=True,  # enables Range support
    )


@app.route("/api/output/delete", methods=["POST"])
def api_output_delete():
    """Delete a song folder from output."""
    data = request.json or {}
    folder_name = str(data.get("folder", "")).strip()
    if not folder_name:
        return jsonify({"ok": False, "error": "Ordnername fehlt"}), 400

    output_base = state.config.get("output_path", "")
    if not output_base:
        output_base = str(DATA_DIR / "output")
    base = Path(output_base)

    # Security: only one direct child song directory may be removed.
    # In particular, "." must never resolve to and delete the output root.
    if folder_name in {".", ".."} or "/" in folder_name or "\\" in folder_name:
        return jsonify({"ok": False, "error": "Ungültiger Ordnername"}), 400
    resolved_base = base.resolve()
    target = (resolved_base / folder_name).resolve()
    if target == resolved_base or target.parent != resolved_base:
        return jsonify({"ok": False, "error": "Pfad-Traversale verweigert"}), 403
    if not target.exists() or not target.is_dir():
        return jsonify({"ok": False, "error": "Ordner nicht gefunden"}), 404

    from smb_utils import safe_rmtree
    safe_rmtree(target)
    return jsonify({"ok": True})


@app.route("/api/data/size", methods=["GET"])
def api_data_size():
    """Calculate total size of data directory + output + cache + logs."""
    data_dir_size = 0
    try:
        for item in DATA_DIR.rglob("*"):
            if item.is_file():
                try:
                    data_dir_size += item.stat().st_size
                except Exception:
                    pass
    except Exception:
        pass

    output_base = Path(state.config.get("output_path", "") or str(DATA_DIR / "output"))
    output_size = 0
    if output_base.exists():
        for item in output_base.rglob("*"):
            if item.is_file():
                try:
                    output_size += item.stat().st_size
                except Exception:
                    pass

    # Cache: usdb_cache.json + any .cache files
    cache_size = _file_size_mb(DATA_DIR / "usdb_cache.json")
    for f in DATA_DIR.glob("*.cache"):
        cache_size += _file_size_mb(f)

    # Logs: scan logs/ dir + any .log files
    log_size = 0.0
    logs_dir = DATA_DIR / "logs"
    if logs_dir.is_dir():
        for f in logs_dir.rglob("*"):
            if f.is_file():
                try:
                    log_size += f.stat().st_size / 1048576
                except Exception:
                    pass
    for f in DATA_DIR.glob("*.log"):
        log_size += _file_size_mb(f)

    # If output is inside DATA_DIR, don't double-count
    if output_base.exists():
        try:
            output_base.relative_to(DATA_DIR)
            data_dir_only = data_dir_size - output_size
        except ValueError:
            data_dir_only = data_dir_size
    else:
        data_dir_only = data_dir_size

    total = data_dir_only + output_size

    return jsonify({
        "ok": True,
        "data_dir": str(DATA_DIR),
        "data_size_mb": round(total / 1048576, 1),
        "output_dir": str(output_base),
        "output_size_mb": round(output_size / 1048576, 1),
        "cache_size_mb": round(cache_size, 1),
        "log_size_mb": round(log_size, 1),
    })


@app.route("/api/data/clear-cache", methods=["POST"])
def api_data_clear_cache():
    """Delete usdb_cache.json."""
    target = DATA_DIR / "usdb_cache.json"
    with state.cache_lock:
        if target.exists():
            target.unlink()
        state.usdb_cache.clear()
    return jsonify({"ok": True})


@app.route("/api/data/clear-logs", methods=["POST"])
def api_data_clear_logs():
    """Delete login_window.log."""
    target = DATA_DIR / "login_window.log"
    freed = 0
    if target.exists():
        freed = target.stat().st_size
        target.unlink()
    return jsonify({"ok": True, "freed_mb": round(freed / 1048576, 2)})


@app.route("/api/data/clear-all", methods=["POST"])
def api_data_clear_all():
    """Delete entire data directory contents (except config.json)."""
    freed = 0
    with state.cache_lock:
        for item in DATA_DIR.iterdir():
            if item.name == "config.json":
                continue
            try:
                if item.is_file():
                    freed += item.stat().st_size
                    item.unlink()
                elif item.is_dir():
                    for f in item.rglob("*"):
                        if f.is_file():
                            freed += f.stat().st_size
                    shutil.rmtree(item, ignore_errors=True)
            except Exception:
                pass
        state.usdb_cache.clear()
    return jsonify({"ok": True, "freed_mb": round(freed / 1048576, 2)})


@app.route("/api/local/scan", methods=["POST"])
def api_local_scan():
    path = (request.json or {}).get("path") or state.config.get("local_path", "")
    if not path or not Path(path).exists():
        return jsonify({"ok": False, "error": f"Pfad nicht gefunden: {path}"})
    songs = scan_local(path)
    state.local_songs = songs
    # Task 5: atomic write
    _atomic_write_json(DATA_DIR / "local_songs.json", songs)
    return jsonify({"ok": True, "count": len(songs), "songs": songs})


@app.route("/api/local/list", methods=["GET"])
def api_local_list():
    return jsonify({"songs": state.local_songs})


def _gather_local_songs():
    """Scan local_path + output_path and merge into one list."""
    songs = []
    seen = set()

    # Scan output_path
    output_base = state.config.get("output_path", "")
    if output_base:
        try:
            for s in scan_local(output_base):
                key = s.get("folder", "")
                if key and key not in seen:
                    seen.add(key)
                    songs.append(s)
        except Exception:
            pass

    # Scan local_path
    local_base = state.config.get("local_path", "")
    if local_base and local_base != output_base:
        try:
            for s in scan_local(local_base):
                key = s.get("folder", "")
                if key and key not in seen:
                    seen.add(key)
                    songs.append(s)
        except Exception:
            pass

    return songs


def _collect_zip_files(root):
    """Collect bounded, in-tree files for an archive."""
    resolved_root = root.resolve()
    max_files = max(1, int(os.environ.get("USDB_ZIP_MAX_FILES", "10000")))
    max_bytes = max(1, int(os.environ.get("USDB_ZIP_MAX_BYTES", str(20 * 1024 ** 3))))
    files = []
    total = 0
    for item in sorted(root.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        resolved = item.resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            continue
        total += resolved.stat().st_size
        files.append(resolved)
        if len(files) > max_files or total > max_bytes:
            raise ValueError(
                f"ZIP-Limit überschritten ({max_files} Dateien / {max_bytes // 1048576} MiB)"
            )
    return files


def _temporary_zip_response(files, archive_base, download_name):
    """Build on disk instead of duplicating a potentially huge ZIP in RAM."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="ultrastar-", suffix=".zip", dir=DATA_DIR)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in files:
                zf.write(item, item.relative_to(archive_base))
        response = send_file(
            temp_path,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/zip",
        )
        response.call_on_close(lambda: temp_path.unlink(missing_ok=True))
        return response
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


@app.route("/api/output/zip", methods=["GET"])
def api_output_zip():
    """Download the entire output folder as a disk-backed ZIP bundle."""
    output_base = state.config.get("output_path", "") or str(DATA_DIR / "output")
    base = Path(output_base).resolve()
    if not base.exists() or not base.is_dir():
        return jsonify({"ok": False, "error": "Output-Ordner nicht gefunden"}), 404
    try:
        files = _collect_zip_files(base)
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 413

    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _temporary_zip_response(files, base, f"ultrastar_bundle_{stamp}.zip")


@app.route("/api/output/zip-folder", methods=["GET"])
def api_output_zip_folder():
    """Download one direct child song folder as a disk-backed ZIP."""
    folder_name = request.args.get("folder", "").strip()
    if not folder_name:
        return jsonify({"ok": False, "error": "Ordnername fehlt"}), 400
    if folder_name in {".", ".."} or "/" in folder_name or "\\" in folder_name:
        return jsonify({"ok": False, "error": "Ungültiger Ordnername"}), 400

    output_base = state.config.get("output_path", "") or str(DATA_DIR / "output")
    base = Path(output_base).resolve()
    target = (base / folder_name).resolve()
    if target == base or target.parent != base:
        return jsonify({"ok": False, "error": "Pfad-Traversale verweigert"}), 403
    if not target.exists() or not target.is_dir():
        return jsonify({"ok": False, "error": "Ordner nicht gefunden"}), 404
    try:
        files = _collect_zip_files(target)
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 413
    return _temporary_zip_response(files, base, f"{folder_name}.zip")


@app.route("/api/output/fix-covers", methods=["POST"])
def api_output_fix_covers():
    """Scan output for songs missing covers, fetch them from USDB.

    DEPRECATED: Superseded by /api/output/fix-assets which handles all
    missing assets. Kept for backward compatibility.
    """
    # Delegate to fix-assets with cover-only
    return _fix_assets(cover_only=True)


@app.route("/api/output/fix-assets", methods=["POST"])
def api_output_fix_assets():
    """Scan output for songs missing assets (cover/video/mp3), fetch from USDB.

    Accepts optional JSON body:
      - folders: list of folder names to process (default: all)
      - cover: bool (default true)
      - video: bool (default true)
      - mp3:   bool (default true)

    Returns per-song results with what was fixed, skipped, or errored.
    """
    data = request.json or {}
    return _fix_assets(
        folders_filter=data.get("folders"),
        want_cover=data.get("cover", True),
        want_video=data.get("video", True),
        want_mp3=data.get("mp3", True),
    )


def _fix_assets(cover_only=False, folders_filter=None,
                want_cover=True, want_video=True, want_mp3=True):
    """Shared worker for fix-covers and fix-assets routes."""
    if cover_only:
        want_video = False
        want_mp3 = False

    output_base = state.config.get("output_path", "")
    if not output_base:
        output_base = str(DATA_DIR / "output")
    base = Path(output_base)
    if not base.exists():
        return jsonify({"ok": False, "error": "Output-Ordner nicht gefunden"}), 404

    cookie = get_cookie()
    if not cookie:
        return jsonify({"ok": False, "error": "Kein Cookie/Login aktiv"}), 403

    fixed = []
    skipped = []
    errors = []

    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        if folders_filter and folder.name not in folders_filter:
            continue

        all_files = [f for f in folder.iterdir() if f.is_file()]
        suffixes = {f.suffix.lower() for f in all_files}
        audio_exts = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".flac"}
        video_exts = {".mp4", ".avi", ".webm", ".mkv", ".mov", ".mpg", ".mpeg", ".ts"}
        has_cover = any("[CO]" in f.name or "cover" in f.name.lower() for f in all_files)
        has_mp3 = bool(suffixes & audio_exts)
        has_video = bool(suffixes & video_exts)

        # Nothing to do?
        missing = []
        if want_cover and not has_cover: missing.append("cover")
        if want_video and not has_video: missing.append("video")
        if want_mp3 and not has_mp3: missing.append("mp3")
        if not missing:
            continue

        # Extract USDB ID from _links (was links.txt, renamed to avoid UltraStar picking it up as a song)
        links_path = folder / "_links"
        if not links_path.exists():
            # Fallback to old links.txt for backwards compatibility
            links_path = folder / "links.txt"
        if not links_path.exists():
            skipped.append({"folder": folder.name, "reason": "Keine _links/links.txt"})
            continue
        try:
            links_content = links_path.read_text(encoding="utf-8")
        except Exception:
            skipped.append({"folder": folder.name, "reason": "Linkdatei nicht lesbar"})
            continue

        m = re.search(r"id=(\d+)", links_content)
        if not m:
            skipped.append({"folder": folder.name, "reason": "Keine USDB-ID gefunden"})
            continue
        song_id = m.group(1)

        # Determine base_name from .txt file (exclude links.txt)
        txt_files = [t for t in folder.glob("*.txt") if t.name != "links.txt"]
        base_name = txt_files[0].stem if txt_files else folder.name

        song_result = {"folder": folder.name, "song_id": song_id, "fixed": []}

        # --- Fetch metadata from USDB ---
        data_meta, err_meta = fetch_detail(song_id, cookie)
        if not data_meta:
            errors.append({"folder": folder.name, "error": err_meta or "Metadaten-Fehler"})
            continue

        yt_url = data_meta.get("youtube_url", "")

        # --- Cover ---
        if "cover" in missing:
            cover_bytes, cover_err = fetch_cover(song_id, cookie)
            if cover_bytes:
                cover_path = folder / f"{base_name} [CO].jpg"
                try:
                    cover_path.write_bytes(cover_bytes)
                    song_result["fixed"].append("cover")
                except Exception as e:
                    errors.append({"folder": folder.name, "error": f"Cover: {e}"})
            else:
                errors.append({"folder": folder.name, "error": f"Cover: {cover_err or 'leer'}"})

        # --- Video (needs YouTube URL) ---
        if "video" in missing and yt_url:
            try:
                from youtube import download_youtube_video
                vid_target = str(folder / base_name)
                vid_path, vid_err = download_youtube_video(
                    yt_url, vid_target,
                    video_format=state.config.get("video_format", "mp4"),
                    max_height=state.config.get("video_resolution", 1080),
                )
                if vid_path:
                    # Patch the .txt to reference the new video
                    if txt_files:
                        _patch_txt_video(txt_files[0], Path(vid_path).name)
                    song_result["fixed"].append("video")
                else:
                    errors.append({"folder": folder.name, "error": f"Video: {vid_err}"})
            except Exception as e:
                errors.append({"folder": folder.name, "error": f"Video: {e}"})

        # --- MP3 (needs YouTube URL) ---
        if "mp3" in missing and yt_url:
            try:
                from youtube import download_youtube_audio
                aud_target = str(folder / base_name)
                aud_path, aud_err = download_youtube_audio(
                    yt_url, aud_target,
                    audio_format=state.config.get("audio_format", "mp3"),
                    bitrate=state.config.get("audio_bitrate", 192),
                )
                if aud_path:
                    if txt_files:
                        _patch_txt_mp3(txt_files[0], Path(aud_path).name)
                    song_result["fixed"].append("mp3")
                else:
                    errors.append({"folder": folder.name, "error": f"MP3: {aud_err}"})
            except Exception as e:
                errors.append({"folder": folder.name, "error": f"MP3: {e}"})

        if song_result["fixed"]:
            fixed.append(song_result)

    return jsonify({
        "ok": True,
        "fixed": fixed,
        "skipped": skipped,
        "errors": errors,
        "total_fixed": len(fixed),
    })


def _atomic_write_text(path, content):
    """Replace a text file atomically using a sibling temporary file."""
    path = Path(path)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _patch_txt_video(txt_path, video_filename):
    """Patch a song .txt to reference a new video file."""
    content = txt_path.read_text(encoding="utf-8")
    lines = content.split("\n")
    patched = []
    saw_video = False
    for line in lines:
        if line.upper().startswith("#VIDEO:"):
            saw_video = True
            patched.append(f"#VIDEO:{video_filename}")
        else:
            patched.append(line)
    if not saw_video:
        # Insert after the last # header
        insert_at = 0
        for i, line in enumerate(patched):
            if line.startswith("#"):
                insert_at = i + 1
        patched.insert(insert_at, f"#VIDEO:{video_filename}")
        # Ensure VIDEOGAP
        if not any(l.upper().startswith("#VIDEOGAP:") for l in patched):
            patched.insert(insert_at + 1, "#VIDEOGAP:0")
    _atomic_write_text(txt_path, "\n".join(patched))


def _patch_txt_mp3(txt_path, mp3_filename):
    """Patch a song .txt to reference a new audio file."""
    content = txt_path.read_text(encoding="utf-8")
    lines = content.split("\n")
    patched = []
    saw_mp3 = False
    for line in lines:
        if line.upper().startswith("#MP3:"):
            saw_mp3 = True
            patched.append(f"#MP3:{mp3_filename}")
        else:
            patched.append(line)
    if not saw_mp3:
        insert_at = 0
        for i, line in enumerate(patched):
            if line.startswith("#"):
                insert_at = i + 1
        patched.insert(insert_at, f"#MP3:{mp3_filename}")
    _atomic_write_text(txt_path, "\n".join(patched))


@app.route("/api/match", methods=["GET"])
def api_match():
    local = _gather_local_songs()
    report = build_match_report(list(state.usdb_cache.values()), local)
    return jsonify(report)


@app.route("/api/match/download", methods=["GET"])
def api_match_download():
    local = _gather_local_songs()
    report = build_match_report(list(state.usdb_cache.values()), local)
    tmp = DATA_DIR / "match_report.json"
    _atomic_write_json(tmp, report)
    return send_file(tmp, as_attachment=True, download_name="match_report.json")


if __name__ == "__main__":
    load_usdb_cache()
    _ACTIVE_PORT = get_active_port()
    print(f"USDB Importer Web UI auf http://127.0.0.1:{_ACTIVE_PORT}")
    app.run(host="127.0.0.1", port=_ACTIVE_PORT, debug=False, threaded=True)
