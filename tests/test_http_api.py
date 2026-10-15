import http.client
import json
import threading
from contextlib import contextmanager

from chunkwise import Engine
from chunkwise.http_api import ChunkWiseHTTPServer


@contextmanager
def running_server(tmp_path, max_upload_bytes=1_000_000):
    server = ChunkWiseHTTPServer(("127.0.0.1", 0), tmp_path / "private-home", max_upload_bytes)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return result


def test_http_dashboard_upload_list_download_verify_and_stats(tmp_path):
    content = (b"chunkwise dashboard integration " * 1200)
    with running_server(tmp_path) as server:
        status, page, headers = request(server, "GET", "/")
        assert status == 200
        assert b"Storage overview" in page
        assert headers["Content-Security-Policy"]

        status, body, _ = request(
            server,
            "POST",
            "/api/files",
            content,
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(content)),
                "X-File-Name": "quarterly%20report.txt",
                "X-Chunker": "cdc",
                "Idempotency-Key": "upload-test-1",
            },
        )
        assert status == 201
        upload = json.loads(body)
        assert upload["name"] == "quarterly report.txt"
        file_id = upload["file_id"]

        status, body, _ = request(server, "GET", "/api/files")
        assert status == 200
        files = json.loads(body)["files"]
        assert len(files) == 1
        assert files[0]["file_id"] == file_id
        assert files[0]["chunker"] == "cdc"

        status, body, _ = request(server, "GET", "/api/stats")
        stats = json.loads(body)
        assert status == 200
        assert stats["logical_bytes"] == len(content)
        assert stats["files"] == 1

        status, restored, headers = request(server, "GET", f"/api/files/{file_id}/download")
        assert status == 200
        assert restored == content
        assert "attachment" in headers["Content-Disposition"]

        status, body, _ = request(server, "POST", f"/api/files/{file_id}/verify")
        assert status == 200
        assert json.loads(body)["verified"] is True


def test_http_upload_retry_is_idempotent_and_conflicting_content_is_rejected(tmp_path):
    with running_server(tmp_path) as server:
        headers = {
            "Content-Type": "application/octet-stream",
            "X-File-Name": "retry.bin",
            "X-Chunker": "fixed",
            "Idempotency-Key": "stable-key",
        }
        payload = b"same upload" * 400
        first_status, first_body, _ = request(server, "POST", "/api/files", payload, headers)
        retry_status, retry_body, _ = request(server, "POST", "/api/files", payload, headers)
        assert first_status == retry_status == 201
        assert json.loads(first_body)["file_id"] == json.loads(retry_body)["file_id"]
        assert json.loads(retry_body)["new_bytes"] == 0

        status, body, _ = request(server, "POST", "/api/files", b"different payload", headers)
        assert status == 409
        assert "different file content" in json.loads(body)["error"]

        with Engine(server.home) as engine:
            assert engine.stats().files == 1


def test_http_accepts_an_empty_file_supported_by_the_engine(tmp_path):
    with running_server(tmp_path) as server:
        status, body, _ = request(server, "POST", "/api/files", b"", {
            "Content-Length": "0",
            "X-File-Name": "empty.bin",
            "X-Chunker": "cdc",
            "Idempotency-Key": "empty-key",
        })
        assert status == 201
        result = json.loads(body)
        assert result["size"] == 0
        assert result["chunks"] == 0
        assert result["file_id"].startswith("f-")


def test_http_rejects_bad_requests_and_never_serves_storage_paths(tmp_path):
    with running_server(tmp_path, max_upload_bytes=8) as server:
        status, body, _ = request(server, "POST", "/api/files", b"123456789", {
            "Content-Length": "9",
            "X-Chunker": "cdc",
            "Idempotency-Key": "too-large",
        })
        assert status == 413
        assert "upload limit" in json.loads(body)["error"]

        status, body, _ = request(server, "POST", "/api/files", b"x", {
            "Content-Length": "1",
            "X-Chunker": "invented",
            "Idempotency-Key": "bad-chunker",
        })
        assert status == 400
        assert "cdc or fixed" in json.loads(body)["error"]

        status, body, _ = request(server, "POST", "/api/files", b"x", {
            "Content-Length": "1",
            "X-Chunker": "cdc",
        })
        assert status == 400
        assert "Idempotency-Key" in json.loads(body)["error"]

        server.home.mkdir(parents=True, exist_ok=True)
        (server.home / "secret.txt").write_text("private", encoding="utf-8")
        status, body, _ = request(server, "GET", "/secret.txt")
        assert status == 404
        assert b"private" not in body


def test_http_verification_reports_corrupt_chunk(tmp_path):
    with running_server(tmp_path) as server:
        payload = b"verify me" * 600
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
            "X-File-Name": "integrity.bin",
            "X-Chunker": "cdc",
            "Idempotency-Key": "integrity-key",
        }
        status, body, _ = request(server, "POST", "/api/files", payload, headers)
        assert status == 201
        file_id = json.loads(body)["file_id"]
        with Engine(server.home) as engine:
            entry = engine.index.manifest(file_id)[0]
            path = engine.store.path_for(entry.chunk_hash)
            raw = bytearray(path.read_bytes())
            raw[0] ^= 0xFF
            path.write_bytes(raw)
        status, body, _ = request(server, "POST", f"/api/files/{file_id}/verify")
        assert status == 422
        assert "failed verification" in json.loads(body)["error"]