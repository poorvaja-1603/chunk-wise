"""Tests for recovery tools: fsck, delete, and garbage collection."""
import hashlib
import time

from chunkwise import Engine
from chunkwise.cli import main
from chunkwise.errors import FileNotFoundInIndexError


def test_delete_decrements_refs_and_tombstones(tmp_path):
    home = tmp_path / "del_home"
    data = b"delete test content " * 100
    with Engine(home) as engine:
        result = engine.put_bytes(data, "test.bin")
        manifest = engine.index.manifest(result.file_id)
        first_chunk = manifest[0].chunk_hash
        assert engine.index.refcount(first_chunk) == 1

        # Delete the file
        del_res = engine.delete(result.file_id)
        assert del_res["deleted"] is True
        assert first_chunk in del_res["tombstoned_chunks"]
        assert engine.index.refcount(first_chunk) == 0

        # File is no longer accessible
        try:
            engine.get_bytes(result.file_id)
            assert False, "should have raised FileNotFoundInIndexError"
        except FileNotFoundInIndexError:
            pass


def test_gc_reclaims_expired_tombstoned_chunks(tmp_path):
    home = tmp_path / "gc_home"
    data = b"gc test content " * 100
    with Engine(home) as engine:
        result = engine.put_bytes(data, "gc.bin")
        manifest = engine.index.manifest(result.file_id)
        digest = manifest[0].chunk_hash

        engine.delete(result.file_id)
        # Immediate GC with 10-second grace period should NOT delete it yet
        gc_res1 = engine.gc(grace_seconds=10.0)
        assert gc_res1["deleted_chunks"] == 0
        assert engine.store.exists(digest) is True

        # GC with 0 grace period should delete it immediately
        gc_res2 = engine.gc(grace_seconds=0.0)
        assert gc_res2["deleted_chunks"] > 0
        assert gc_res2["reclaimed_bytes"] > 0
        assert engine.store.exists(digest) is False


def test_fsck_detects_and_repairs_refcount_drift(tmp_path):
    home = tmp_path / "fsck_home"
    data = b"fsck test content " * 100
    with Engine(home) as engine:
        result = engine.put_bytes(data, "fsck.bin")
        manifest = engine.index.manifest(result.file_id)
        digest = manifest[0].chunk_hash

        # Intentionally tamper with refcount to simulate drift
        with engine.index._tx() as c:
            c.execute("UPDATE chunks SET ref_count = 999 WHERE hash = ?", (digest,))

        report = engine.fsck(repair=False)
        assert report["ok"] is False
        assert digest in report["drift_chunks"]
        assert report["drift_chunks"][digest] == (999, 1)

        # Repair drift
        repair_report = engine.fsck(repair=True)
        assert digest in repair_report["drift_chunks"]

        # Check that refcount is fixed
        assert engine.index.refcount(digest) == 1
        clean_report = engine.fsck(repair=False)
        assert clean_report["ok"] is True


def test_fsck_detects_orphan_chunks_in_store(tmp_path):
    home = tmp_path / "orphan_home"
    with Engine(home) as engine:
        orphan_data = b"orphan bytes"
        orphan_digest = hashlib.sha256(orphan_data).hexdigest()
        engine.store.put(orphan_digest, orphan_data)

        report = engine.fsck(repair=False)
        assert report["ok"] is False
        assert orphan_digest in report["orphan_chunks"]


def test_cli_delete_gc_fsck(tmp_path, capsys):
    home = tmp_path / "cli_recovery"
    src = tmp_path / "in.bin"
    src.write_bytes(b"cli recovery payload " * 200)

    assert main(["--home", str(home), "put", str(src)]) == 0
    import json
    out = json.loads(capsys.readouterr().out)
    file_id = out["file_id"]

    assert main(["--home", str(home), "fsck"]) == 0
    capsys.readouterr()

    assert main(["--home", str(home), "delete", file_id]) == 0
    del_out = json.loads(capsys.readouterr().out)
    assert del_out["deleted"] is True

    assert main(["--home", str(home), "gc", "--grace-seconds", "0"]) == 0
    gc_out = json.loads(capsys.readouterr().out)
    assert gc_out["deleted_chunks"] > 0

