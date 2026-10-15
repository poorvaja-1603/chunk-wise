"""Same-origin HTTP API and dashboard server for ChunkWise."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from . import Engine
from .errors import ChunkWiseError, IdempotencyConflictError
from .ui import STATIC_FILES, STATIC_DIR

FILE_ID_PATTERN = re.compile(r"^f-[a-f0-9]{12}$")
CHUNKERS = {"cdc", "fixed"}
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class ChunkWiseHTTPServer(HTTPServer):
    def __init__(self, address: tuple[str, int], home: Path, max_upload_bytes: int):
        self.home = Path(home).expanduser().resolve()
        self.max_upload_bytes = max_upload_bytes
        super().__init__(address, ChunkWiseRequestHandler)


class ChunkWiseRequestHandler(BaseHTTPRequestHandler):
    server: ChunkWiseHTTPServer

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            self._send_bytes(200, (STATIC_DIR / filename).read_bytes(), content_type)
            return
        if path == "/api/health":
            self._send_json(200, {"ok": True})
            return
        if path == "/api/config":
            self._send_json(200, {"max_upload_bytes": self.server.max_upload_bytes})
            return
        if path == "/api/stats":
            with Engine(self.server.home) as engine:
                self._send_json(200, asdict(engine.stats()))
            return
        if path == "/api/files":
            with Engine(self.server.home) as engine:
                files = [
                    {
                        "file_id": row.file_id,
                        "name": row.name,
                        "size": row.size,
                        "chunker": row.chunker,
                        "created_at": row.created_at,
                        "chunks": len(engine.index.manifest(row.file_id)),
                    }
                    for row in engine.index.list_files()
                ]
            self._send_json(200, {"files": files})
            return

        match = re.fullmatch(r"/api/files/([^/]+)/download", path)
        if match:
            file_id = unquote(match.group(1))
            if not FILE_ID_PATTERN.fullmatch(file_id):
                self._send_error_json(404, "File not found")
                return
            try:
                with Engine(self.server.home) as engine:
                    row = engine.index.get_file(file_id)
                    content = engine.get_bytes(file_id)
                filename = quote(row.name, safe="")
                self._send_bytes(
                    200,
                    content,
                    "application/octet-stream",
                    {"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
                )
            except ChunkWiseError as error:
                self._send_error_json(404, str(error))
            return
        self._send_error_json(404, "Not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/files":
            self._upload()
            return
        match = re.fullmatch(r"/api/files/([^/]+)/verify", path)
        if match:
            file_id = unquote(match.group(1))
            if not FILE_ID_PATTERN.fullmatch(file_id):
                self._send_error_json(404, "File not found")
                return
            try:
                with Engine(self.server.home) as engine:
                    size = engine.verify(file_id)
                self._send_json(200, {"file_id": file_id, "verified": True, "size": size})
            except ChunkWiseError as error:
                self._send_error_json(422, str(error))
            return
        self._send_error_json(404, "Not found")

    def _upload(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_error_json(400, "A valid Content-Length is required")
            return
        if length < 0:
            self._send_error_json(400, "Content-Length must not be negative")
            return
        if length > self.server.max_upload_bytes:
            self._send_error_json(413, "File exceeds the configured upload limit")
            return
        chunker = self.headers.get("X-Chunker", "cdc")
        if chunker not in CHUNKERS:
            self._send_error_json(400, "Chunker must be cdc or fixed")
            return
        idempotency_key = self.headers.get("Idempotency-Key", "")
        if not idempotency_key or len(idempotency_key) > 200:
            self._send_error_json(400, "A valid Idempotency-Key header is required")
            return
        filename = self._filename(self.headers.get("X-File-Name", "upload.bin"))
        data = self.rfile.read(length)
        if len(data) != length:
            self._send_error_json(400, "Upload ended before the declared content length")
            return
        try:
            with Engine(self.server.home) as engine:
                result = engine.put_idempotent_bytes(data, filename, idempotency_key, chunker)
            self._send_json(201, asdict(result))
        except IdempotencyConflictError as error:
            self._send_error_json(409, str(error))
        except (ChunkWiseError, ValueError) as error:
            self._send_error_json(400, str(error))

    @staticmethod
    def _filename(value: str) -> str:
        name = unquote(value).replace("\\", "/").split("/")[-1]
        name = "".join(character for character in name if character >= " " and character != "\x7f")
        name = name.strip(" .")[:255]
        return name or "upload.bin"

    def _send_json(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} {self.address_string()} {format % args}", file=sys.stderr)


def main() -> int:
    home = Path(os.environ.get("CHUNKWISE_HOME", ".chunkwise"))
    host = os.environ.get("CHUNKWISE_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("CHUNKWISE_PORT", "8080")))
    max_upload = int(os.environ.get("CHUNKWISE_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES)))
    if not 0 < max_upload <= 2**31 - 1:
        raise SystemExit("CHUNKWISE_MAX_UPLOAD_BYTES must be between 1 and 2147483647")
    server = ChunkWiseHTTPServer((host, port), home, max_upload)
    print(f"ChunkWise dashboard listening on http://{host}:{server.server_port} (home: {server.home})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())