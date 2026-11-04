import json
import os
from unittest.mock import MagicMock

from chunkwise import Engine
from chunkwise.chunker import ChunkParams
from chunkwise.observability import (
    ObservabilityTracker,
    emit_log,
    get_metrics,
    get_tracker,
    is_enabled,
    reset_metrics,
)


def test_disabled_by_default_makes_zero_cloudwatch_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("CHUNKWISE_CLOUDWATCH", raising=False)
    assert not is_enabled()

    fake_cw = MagicMock()
    monkeypatch.setattr(get_tracker(), "_get_client", lambda: fake_cw)

    reset_metrics()
    with Engine(tmp_path / "home") as engine:
        result = engine.put_bytes(b"hello world " * 100, "hello.txt")

    get_tracker().flush()
    fake_cw.put_metric_data.assert_not_called()
    assert result.chunks > 0


def test_enabled_sends_expected_metrics_and_dimensions(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUNKWISE_CLOUDWATCH", "1")
    monkeypatch.setenv("CHUNKWISE_BACKEND", "local")
    assert is_enabled()

    sent_batches = []
    fake_cw = MagicMock()
    fake_cw.put_metric_data.side_effect = lambda Namespace, MetricData: sent_batches.append(
        {"Namespace": Namespace, "MetricData": MetricData}
    )
    monkeypatch.setattr(get_tracker(), "_get_client", lambda: fake_cw)

    reset_metrics()
    with Engine(tmp_path / "home") as engine:
        engine.put_bytes(b"test chunk data for cloudwatch " * 200, "sample.bin")

    get_tracker().flush()
    assert len(sent_batches) > 0
    assert sent_batches[0]["Namespace"] == "ChunkWise"
    metric_data = sent_batches[0]["MetricData"]

    metric_names = {m["MetricName"] for m in metric_data}
    assert "UploadBytes" in metric_names
    assert "ChunksProcessed" in metric_names
    assert "NewChunks" in metric_names
    assert "DedupRatio" in metric_names

    for m in metric_data:
        dim_map = {d["Name"]: d["Value"] for d in m["Dimensions"]}
        assert dim_map["Mode"] == "real"
        assert dim_map["Backend"] == "local"


def test_failing_cloudwatch_client_never_breaks_put(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUNKWISE_CLOUDWATCH", "1")
    fake_cw = MagicMock()
    fake_cw.put_metric_data.side_effect = RuntimeError("CloudWatch service unavailable")
    monkeypatch.setattr(get_tracker(), "_get_client", lambda: fake_cw)

    with Engine(tmp_path / "home") as engine:
        result = engine.put_bytes(b"resilient upload data", "resilient.txt")

    assert result.size == len(b"resilient upload data")
    # Flush should not raise even if client raises
    get_tracker().flush()


def test_emit_log_behavior(monkeypatch, capsys):
    monkeypatch.delenv("CHUNKWISE_CLOUDWATCH", raising=False)
    emit_log("test_disabled", file_id="f-123")
    captured = capsys.readouterr()
    assert captured.out == ""

    monkeypatch.setenv("CHUNKWISE_CLOUDWATCH", "1")
    emit_log("test_enabled", file_id="f-456", size=1024)
    captured = capsys.readouterr()
    assert captured.out != ""
    record = json.loads(captured.out.strip())
    assert record["event"] == "test_enabled"
    assert record["file_id"] == "f-456"
    assert record["size"] == 1024
    assert "timestamp" in record


def test_api_metrics_endpoint(tmp_path):
    from http.server import HTTPServer
    from chunkwise.http_api import ChunkWiseRequestHandler
    import urllib.request

    reset_metrics()
    with Engine(tmp_path / "home") as engine:
        engine.put_bytes(b"metrics test bytes " * 50, "test.txt")

    server = HTTPServer(("127.0.0.1", 0), ChunkWiseRequestHandler)
    server.home = tmp_path / "home"
    server.max_upload_bytes = 10 * 1024 * 1024

    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/api/metrics"

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        assert response.status == 200
        data = json.loads(response.read().decode("utf-8"))

    assert "upload_bytes" in data
    assert "chunks_processed" in data
    assert "new_chunks" in data
    assert data["upload_bytes"] > 0
    server.shutdown()

