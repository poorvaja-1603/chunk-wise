"""Same-origin HTTP API and dashboard server for ChunkWise."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

try:
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    class BotoCoreError(Exception): pass
    class ClientError(Exception): pass

from . import Engine
from .errors import ChunkWiseError, IdempotencyConflictError
from .simulation import HybridRunner
from .ui import STATIC_FILES, STATIC_DIR

FILE_ID_PATTERN = re.compile(r"^f-[a-f0-9]{12}$")
SIMULATION_ID_PATTERN = re.compile(r"^[a-f0-9]{12}$")
CHUNKERS = {"cdc", "fixed"}
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class SimulationJobManager:
    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}

    def start(
        self,
        stage_seconds: int,
        duplicate_ratio: float,
        seed: int,
        source_file_id: str | None,
    ) -> str | None:
        with self._lock:
            if any(job["status"] == "running" for job in self._jobs.values()):
                return None
            job_id = uuid.uuid4().hex[:12]
            self._jobs[job_id] = {
                "status": "running",
                "stage_seconds": stage_seconds,
                "duplicate_ratio": duplicate_ratio,
                "seed": seed,
                "source_file_id": source_file_id,
                "rows": [],
                "result": None,
                "error": None,
            }
        thread = threading.Thread(
            target=self._run,
            args=(job_id, stage_seconds, duplicate_ratio, seed),
            daemon=True,
            name=f"chunkwise-simulation-{job_id}",
        )
        thread.start()
        return job_id

    def _append_row(self, job_id: str, row: dict) -> None:
        with self._lock:
            self._jobs[job_id]["rows"].append(row)

    def _run(
        self,
        job_id: str,
        stage_seconds: int,
        duplicate_ratio: float,
        seed: int,
    ) -> None:
        try:
            runner = HybridRunner(
                home=self.home,
                metrics_path=self.home / "simulations" / f"{job_id}.jsonl",
                seed=seed,
                duplicate_ratio=duplicate_ratio,
                stage_seconds=stage_seconds,
                realtime=True,
                metrics_callback=lambda row: self._append_row(job_id, row),
            )
            result = runner.run()
            with self._lock:
                self._jobs[job_id]["status"] = "completed"
                self._jobs[job_id]["result"] = result
        except Exception as error:
            print(f"Simulation {job_id} failed: {error}", file=sys.stderr)
            with self._lock:
                self._jobs[job_id]["status"] = "failed"
                self._jobs[job_id]["error"] = str(error)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else dict(job, rows=list(job["rows"]))


class ChunkWiseHTTPServer(HTTPServer):
    def __init__(self, address: tuple[str, int], home: Path, max_upload_bytes: int):
        self.home = Path(home).expanduser().resolve()
        self.max_upload_bytes = max_upload_bytes
        self.simulations = SimulationJobManager(self.home)
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
            backend = os.environ.get("CHUNKWISE_BACKEND", "local").strip().lower()
            self._send_json(200, {
                "max_upload_bytes": self.server.max_upload_bytes,
                "backend": backend,
                "index_label": "DynamoDB Metadata Index" if backend == "aws" else "SQLite Metadata Index",
                "store_label": "S3 Chunk Store" if backend == "aws" else "Content-Addressed Chunk Store",
            })
            return
        match = re.fullmatch(r"/api/simulations/([a-f0-9]{12})", path)
        if match:
            job = self.server.simulations.get(match.group(1))
            if job is None:
                self._send_error_json(404, "Simulation not found")
            else:
                self._send_json(200, job)
            return
        if path == "/api/stats":
            try:
                with Engine(self.server.home) as engine:
                    value = asdict(engine.stats())
            except (BotoCoreError, ClientError, sqlite3.Error, OSError, ValueError) as error:
                self._log_backend_error(error)
                self._send_error_json(503, "Storage backend unavailable")
                return
            self._send_json(200, value)
            return
        if path == "/api/metrics":
            from .observability import get_metrics
            self._send_json(200, get_metrics().to_dict())
            return
        if path == "/api/files":
            try:
                with Engine(self.server.home) as engine:
                    files = []
                    for row in engine.index.list_files():
                        manifest = engine.index.manifest(row.file_id)
                        chunk_hashes = [e.chunk_hash for e in manifest]
                        files.append({
                            "file_id": row.file_id,
                            "name": row.name,
                            "size": row.size,
                            "chunker": row.chunker,
                            "created_at": row.created_at,
                            "chunks": len(chunk_hashes),
                            "unique_chunks": len(set(chunk_hashes)),
                        })
            except (BotoCoreError, ClientError, sqlite3.Error, OSError, ValueError) as error:
                self._log_backend_error(error)
                self._send_error_json(503, "Storage backend unavailable")
                return
            self._send_json(200, {"files": files})
            return

        match = re.fullmatch(r"/api/files/([^/]+)/chunks", path)
        if match:
            file_id = unquote(match.group(1))
            if not FILE_ID_PATTERN.fullmatch(file_id):
                self._send_error_json(404, "File not found")
                return
            try:
                with Engine(self.server.home) as engine:
                    row = engine.index.get_file(file_id)
                    entries = engine.index.manifest(file_id)
                    chunks = []
                    for e in entries:
                        ref = engine.index.refcount(e.chunk_hash) or 1
                        codec = "none"
                        # Check local sidecar or S3 metadata if available
                        meta_fn = getattr(engine.store, "_meta_path", None)
                        if meta_fn and meta_fn(e.chunk_hash).exists():
                            try:
                                meta_data = json.loads(meta_fn(e.chunk_hash).read_text(encoding="utf-8"))
                                codec = meta_data.get("codec", "none")
                            except Exception:
                                pass
                        chunks.append({
                            "seq": e.seq,
                            "hash": e.chunk_hash,
                            "offset": e.byte_offset,
                            "length": e.length,
                            "ref_count": ref,
                            "codec": codec,
                            "is_duplicate": ref > 1,
                        })
                self._send_json(200, {
                    "file_id": file_id,
                    "name": row.name,
                    "size": row.size,
                    "total_chunks": len(chunks),
                    "chunks": chunks,
                })
            except ChunkWiseError as error:
                self._send_error_json(404, str(error))
            except (BotoCoreError, ClientError, sqlite3.Error, OSError, ValueError) as error:
                self._log_backend_error(error)
                self._send_error_json(503, "Storage backend unavailable")
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
            except (BotoCoreError, ClientError, sqlite3.Error, OSError, ValueError) as error:
                self._log_backend_error(error)
                self._send_error_json(503, "Storage backend unavailable")
            return
        self._send_error_json(404, "Not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/simulations":
            self._start_simulation()
            return
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
            except (BotoCoreError, ClientError, sqlite3.Error, OSError, ValueError) as error:
                self._log_backend_error(error)
                self._send_error_json(503, "Storage backend unavailable")
            return
        self._send_error_json(404, "Not found")

    def _start_simulation(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_error_json(400, "A valid Content-Length is required")
            return
        if length < 0 or length > 4096:
            self._send_error_json(400, "Simulation settings must be a small JSON object")
            return
        try:
            options = json.loads(self.rfile.read(length))
            if not isinstance(options, dict):
                raise ValueError
            stage_seconds = options.get("stage_seconds", 2)
            duplicate_ratio = options.get("duplicate_ratio", 0.6)
            seed = options.get("seed", 1)
            source_file_id = options.get("source_file_id")
            if (
                isinstance(stage_seconds, bool)
                or not isinstance(stage_seconds, int)
                or not 1 <= stage_seconds <= 30
            ):
                raise ValueError
            if duplicate_ratio not in (0.3, 0.6, 0.9):
                raise ValueError
            if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
                raise ValueError
            if source_file_id is not None and (
                not isinstance(source_file_id, str)
                or not FILE_ID_PATTERN.fullmatch(source_file_id)
            ):
                raise ValueError
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
            self._send_error_json(400, "Invalid simulation settings")
            return
        job_id = self.server.simulations.start(
            stage_seconds,
            duplicate_ratio,
            seed,
            source_file_id,
        )
        if job_id is None:
            self._send_error_json(409, "A simulation is already running")
            return
        self._send_json(202, {"simulation_id": job_id, "status": "running"})

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
            if length > 0:
                self.rfile.read(min(length, 1024 * 1024))
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
        except (BotoCoreError, ClientError, sqlite3.Error, OSError) as error:
            self._log_backend_error(error)
            self._send_error_json(503, "Storage backend unavailable")

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

    @staticmethod
    def _log_backend_error(error: Exception) -> None:
        print(f"Storage backend error: {error}", file=sys.stderr)

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
            "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} {self.address_string()} {format % args}", file=sys.stderr)


def _auto_load_dotenv() -> None:
    if os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return
    candidates = (
        Path(".env"),
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",
    )
    for env_file in candidates:
        if env_file.is_file():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    os.environ[key] = val
                    os.environ[key.upper()] = val
            except Exception:
                pass
            break


def main() -> int:
    _auto_load_dotenv()
    home = Path(os.environ.get("CHUNKWISE_HOME", ".chunkwise"))
    host = os.environ.get("CHUNKWISE_HOST", "0.0.0.0")
    explicit_port = os.environ.get("PORT") or os.environ.get("CHUNKWISE_PORT")
    preferred_port = int(explicit_port) if explicit_port else 8080
    max_upload = int(os.environ.get("CHUNKWISE_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES)))
    if not 0 < max_upload <= 2**31 - 1:
        raise SystemExit("CHUNKWISE_MAX_UPLOAD_BYTES must be between 1 and 2147483647")

    candidate_ports = [8080, 8081, 8082, 8000, 5000] if preferred_port == 8080 else [preferred_port]
    server = None
    for p in candidate_ports:
        try:
            server = ChunkWiseHTTPServer((host, p), home, max_upload)
            break
        except (PermissionError, OSError) as err:
            if p == candidate_ports[-1]:
                print(f"Failed to bind to {host}:{p}: {err}", file=sys.stderr)
                raise

    if server is None:
        return 1

    display_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    backend = os.environ.get("CHUNKWISE_BACKEND", "local").strip().lower()
    print(f"ChunkWise dashboard listening on http://{display_host}:{server.server_port} (backend: {backend}, home: {server.home})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())