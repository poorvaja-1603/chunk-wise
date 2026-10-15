"""Allowlisted static assets for the built-in ChunkWise dashboard."""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}