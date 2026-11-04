import os
import uuid
import pytest

from chunkwise import Engine

LIVE_TESTS_ENABLED = os.environ.get("CHUNKWISE_LIVE_TESTS", "0").strip() == "1"


@pytest.mark.skipif(
    not LIVE_TESTS_ENABLED,
    reason="Live AWS tests are opt-in via CHUNKWISE_LIVE_TESTS=1",
)
def test_live_aws_pipeline(tmp_path):
    # Ensure AWS backend environment variables are defined
    bucket = os.environ.get("CHUNKWISE_S3_BUCKET")
    table = os.environ.get("CHUNKWISE_DYNAMODB_TABLE")
    if not bucket or not table:
        pytest.skip("CHUNKWISE_S3_BUCKET and CHUNKWISE_DYNAMODB_TABLE must be set for live tests")

    payload = b"ChunkWise live AWS integration test content " + uuid.uuid4().bytes * 100
    home = tmp_path / "live_home"

    with Engine(home) as engine:
        # 1. Put file
        filename = f"live-test-{uuid.uuid4().hex[:8]}.bin"
        put_result = engine.put_bytes(payload, filename)
        file_id = put_result.file_id
        assert put_result.size == len(payload)
        assert put_result.chunks > 0

        # 2. Restore file
        restored = engine.get_bytes(file_id)
        assert restored == payload

        # 3. Verify
        verified_size = engine.verify(file_id)
        assert verified_size == len(payload)

        # 4. FSCK
        fsck_report = engine.fsck(repair=False)
        assert fsck_report["ok"] is True
        assert len(fsck_report["drift_chunks"]) == 0

        # 5. Delete
        del_result = engine.delete(file_id)
        assert del_result["deleted"] is True
        assert del_result["file_id"] == file_id

        # 6. GC (with grace_seconds=0 to immediately clean up tombstoned chunks)
        gc_result = engine.gc(grace_seconds=0.0)
        assert gc_result["deleted_chunks"] >= 0

