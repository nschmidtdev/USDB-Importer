#!/usr/bin/env python3
"""Production server entry point for Docker/LAN deployments."""

import os
import sys
from pathlib import Path

# Enable request authentication before importing the Flask application.
os.environ["USDB_SERVER_MODE"] = "1"

username = os.environ.get("USDB_USERNAME", "").strip()
password = os.environ.get("USDB_PASSWORD", "")
if not username or not password:
    print(
        "USDB_USERNAME und USDB_PASSWORD müssen im Servermodus gesetzt sein.",
        file=sys.stderr,
    )
    raise SystemExit(2)

src_dir = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(src_dir))

from waitress import serve  # noqa: E402
from app import app, load_usdb_cache, save_config, state  # noqa: E402


if __name__ == "__main__":
    host = os.environ.get("USDB_HOST", "0.0.0.0")
    port = int(os.environ.get("USDB_PORT", "5776"))
    configured_output = os.environ.get("USDB_OUTPUT_PATH", "/app/output").strip()
    if configured_output and not state.config.get("output_path"):
        state.config["output_path"] = configured_output
        save_config()
    load_usdb_cache()
    print(f"UltraStar Importer auf http://{host}:{port}")
    serve(app, host=host, port=port, threads=32)
