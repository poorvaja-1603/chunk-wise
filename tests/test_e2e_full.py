import http.client
import json
import tempfile
import threading
import time
from pathlib import Path
import pytest

from chunkwise.http_api import ChunkWiseHTTPServer


def test_full_platform_end_to_end(tmp_path):
    server = ChunkWiseHTTPServer(("127.0.0.1", 0), tmp_path / "home", 10_000_000)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)

        # 1. Dashboard HTML root
        conn.request("GET", "/")
        res = conn.getresponse()
        assert res.status == 200
        html = res.read().decode("utf-8")
        assert "Welcome to ChunkWise" in html
        assert "Poorvaja" not in html
        assert "Cognito" not in html

        # 2. Config & Health
        conn.request("GET", "/api/config")
        res = conn.getresponse()
        assert res.status == 200
        cfg = json.loads(res.read())
        assert cfg["backend"] in {"local", "aws"}

        # 3. Upload file 1 (CDC)
        data1 = b"Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 500
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data1)),
            "X-File-Name": "test_corpus.txt",
            "X-Chunker": "cdc",
            "Idempotency-Key": "idemp-upload-1",
        }
        conn.request("POST", "/api/files", body=data1, headers=headers)
        res = conn.getresponse()
        assert res.status == 201
        up1 = json.loads(res.read())
        fid1 = up1["file_id"]
        assert up1["chunks"] > 0
        assert up1["new_chunks"] == up1["chunks"]

        # 4. Chunks manifest
        conn.request("GET", f"/api/files/{fid1}/chunks")
        res = conn.getresponse()
        assert res.status == 200
        chunks1 = json.loads(res.read())["chunks"]
        assert len(chunks1) > 0
        assert "hash" in chunks1[0]
        assert "offset" in chunks1[0]

        # 5. Download and verify content fidelity
        conn.request("GET", f"/api/files/{fid1}/download")
        res = conn.getresponse()
        assert res.status == 200
        downloaded = res.read()
        assert downloaded == data1

        # 6. Verify cryptographic integrity
        conn.request("POST", f"/api/files/{fid1}/verify")
        res = conn.getresponse()
        assert res.status == 200
        assert json.loads(res.read())["verified"] is True

        # 7. Deduplication test (Upload duplicate file with new idempotency key)
        headers["X-File-Name"] = "test_corpus_duplicate.txt"
        headers["Idempotency-Key"] = "idemp-upload-2"
        conn.request("POST", "/api/files", body=data1, headers=headers)
        res = conn.getresponse()
        assert res.status == 201
        up2 = json.loads(res.read())
        assert up2["new_chunks"] == 0
        assert up2["chunks"] == up1["chunks"]

        # 8. Fixed-size chunking test
        data2 = b"Fixed chunking dataset stream " * 300
        headers_fixed = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data2)),
            "X-File-Name": "fixed_data.bin",
            "X-Chunker": "fixed",
            "Idempotency-Key": "idemp-upload-3",
        }
        conn.request("POST", "/api/files", body=data2, headers=headers_fixed)
        res = conn.getresponse()
        assert res.status == 201
        up3 = json.loads(res.read())
        assert up3["chunker"] == "fixed"

        # 9. Policy management
        conn.request("GET", "/api/v1/policies")
        res = conn.getresponse()
        assert res.status == 200
        policies_data = json.loads(res.read())
        assert len(policies_data["policies"]) >= 4

        body = json.dumps({"policy": "space_saver"}).encode("utf-8")
        conn.request(
            "POST",
            "/api/v1/policies/select",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        res = conn.getresponse()
        assert res.status == 200
        active_pol = json.loads(res.read())["active"]
        assert active_pol["policy_id"] == "policy_space_saver_v1"

        # 10. Stats check
        conn.request("GET", "/api/stats")
        res = conn.getresponse()
        assert res.status == 200
        st = json.loads(res.read())
        assert st["files"] == 3
        assert st["logical_bytes"] == (len(data1) * 2) + len(data2)
        assert st["dedup_ratio"] > 1.0
        assert st["storage_saved_pct"] > 0

        # 11. Simulation lifecycle
        sim_body = json.dumps({
            "stage_seconds": 1,
            "duplicate_ratio": 0.6,
            "seed": 42,
        }).encode("utf-8")
        conn.request(
            "POST",
            "/api/simulations",
            body=sim_body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(sim_body))},
        )
        res = conn.getresponse()
        assert res.status == 202
        sim_id = json.loads(res.read())["simulation_id"]

        conn.request("GET", f"/api/simulations/{sim_id}")
        res = conn.getresponse()
        assert res.status == 200
        sim_data = json.loads(res.read())
        assert "status" in sim_data
        assert sim_data["status"] in {"running", "completed"}
        assert "rows" in sim_data

        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2)
