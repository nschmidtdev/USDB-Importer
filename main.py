#!/usr/bin/env python3
"""Native desktop launcher for the Flask application.

The web UI is served by Waitress on a random loopback-only port and displayed
inside pywebview. Closing the window requests a clean worker shutdown and
closes the local HTTP server.
"""

import os
import socket
import sys
import threading
import time

import webview
from waitress import create_server


# Ensure relative paths are anchored at the application directory.
if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))
else:
    project_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_dir)
    sys.path.insert(0, os.path.join(project_dir, "src"))


def find_free_port():
    """Return an unused loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def create_desktop_server(port):
    """Initialize persistent state and create the loopback Waitress server."""
    os.environ["USDB_PORT"] = str(port)
    import app as flask_app

    flask_app.load_usdb_cache()
    return create_server(
        flask_app.app,
        host="127.0.0.1",
        port=port,
        threads=16,
    )


def request_worker_shutdown(timeout=10):
    """Ask the active queue worker to stop and wait for it to finish."""
    from state import state

    state.worker_stop_event.set()
    worker_thread = state.worker_thread
    if worker_thread is None or worker_thread is threading.current_thread():
        return True
    if worker_thread.is_alive():
        worker_thread.join(timeout=timeout)
    return not worker_thread.is_alive()


def wait_for_server(port, timeout=10):
    """Wait until the local server is accepting connections."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


def main():
    port = find_free_port()
    server = create_desktop_server(port)
    server_thread = threading.Thread(
        target=server.run,
        name="ultrastar-http",
        daemon=True,
    )
    server_thread.start()

    if not wait_for_server(port):
        print("ERROR: Local server failed to start")
        server.close()
        server_thread.join(timeout=5)
        return 1

    try:
        webview.create_window(
            title="UltraStar Importer",
            url=f"http://127.0.0.1:{port}",
            width=1200,
            height=800,
            min_size=(800, 600),
            text_select=True,
        )
        webview.start()
    finally:
        request_worker_shutdown()
        server.close()
        server_thread.join(timeout=5)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
