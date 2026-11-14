from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from chunkwise.http_api import ChunkWiseHTTPServer
from chunkwise.policy import (
    PROFILES,
    get_active_policy,
    get_policy,
    list_policies,
    select_policy,
    validate_policy,
)
from chunkwise.reparse import (
    create_reparse_link,
    inspect_path,
    safe_resolve_target,
    scan_directory,
)


@contextmanager
def running_server(tmp_path):
    server = ChunkWiseHTTPServer(("127.0.0.1", 0), tmp_path / "private-home", 1_000_000)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_policy_profiles_are_present():
    assert set(PROFILES) >= {"balanced", "space_saver", "fast_restore", "disabled"}
    assert get_policy("balanced")["policy_id"] == "policy_balanced_v1"
    assert list_policies()[0]["name"] == "Balanced"
    validated = validate_policy(PROFILES["space_saver"].to_dict())
    assert validated.compression["level"] == 9


def test_select_policy_updates_active_value_and_environment():
    selected = select_policy("space_saver")
    assert selected["name"] == "Space Saver"
    assert get_active_policy()["policy_id"] == "policy_space_saver_v1"
    assert os.environ.get("CHUNKWISE_COMPRESSION_PROFILE") == "space_saver"

    # Reset to balanced
    select_policy("balanced")
    assert get_active_policy()["policy_id"] == "policy_balanced_v1"
    assert os.environ.get("CHUNKWISE_COMPRESSION_PROFILE") == "balanced"


def test_safe_resolve_target_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with pytest.raises(ValueError, match="escapes sandbox root"):
            safe_resolve_target("../../etc/passwd", str(root))


def test_create_reparse_link_and_inspect():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        target = root / "target_dir"
        target.mkdir()
        link_path = root / "link_dir"

        success = create_reparse_link(str(link_path), str(target), "junction")
        assert success is True
        info = inspect_path(str(link_path))
        assert info["exists"] is True
        assert info["is_reparse"] is True
        assert info["is_directory"] is True
        assert info["tag_type"] in {"junction", "symlink"}


def test_scan_directory_records_reparse_without_infinite_loop():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        sub1 = root / "sub1"
        sub1.mkdir()
        file1 = sub1 / "data.txt"
        file1.write_text("hello data", encoding="utf-8")

        # Create a loop/pointer to parent inside sub1 safely within the root sandbox
        loop_link = sub1 / "loop_link"
        create_reparse_link(str(loop_link), str(root), "junction", sandbox_root=str(root))

        files, reparse_records = scan_directory(root, record_only=True)
        # file1 must be discovered
        assert any(f.name == "data.txt" for f in files)
        # reparse record must be captured
        assert len(reparse_records) >= 1
        assert reparse_records[0]["is_reparse"] is True


def test_http_api_policy_endpoints(tmp_path):
    with running_server(tmp_path) as server:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)

        # GET /api/v1/policies
        conn.request("GET", "/api/v1/policies")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "policies" in data
        assert len(data["policies"]) >= 4

        # GET /api/v1/policies/active
        conn.request("GET", "/api/v1/policies/active")
        resp = conn.getresponse()
        assert resp.status == 200
        active = json.loads(resp.read())["active"]
        assert active["name"] in {"Balanced", "Space Saver", "Fast Restore", "No Compression"}

        # POST /api/v1/policies/select
        body = json.dumps({"policy": "space_saver"}).encode("utf-8")
        conn.request(
            "POST",
            "/api/v1/policies/select",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        resp = conn.getresponse()
        assert resp.status == 200
        updated = json.loads(resp.read())["active"]
        assert updated["name"] == "Space Saver"
        assert updated["policy_id"] == "policy_space_saver_v1"

        conn.close()
