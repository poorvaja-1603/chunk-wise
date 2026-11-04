import json

from chunkwise.chunker import ChunkParams
from chunkwise.engine import UploadPipeline
from chunkwise.index import MemoryIndex
from chunkwise.simulation import HybridRunner, _read_spool, _spool_chunks
from chunkwise.store import VirtualChunkStore


PARAMS = ChunkParams(min_size=8, avg_size=16, max_size=32, window=4)


def _runner(metrics_path, **overrides):
    options = {
        "home": metrics_path.parent / "home",
        "metrics_path": metrics_path,
        "params": PARAMS,
        "seed": 42,
        "duplicate_ratio": 0.6,
        "stage_seconds": 1,
        "base_rps": 0.01,
        "chunks_per_file": 2,
        "chunker": "fixed",
        "realtime": False,
    }
    options.update(overrides)
    return HybridRunner(**options)


def test_virtual_phase_makes_no_aws_backend_calls(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHUNKWISE_BACKEND", "aws")

    def fail_if_called(self):
        raise AssertionError("AWS backend was initialized in the virtual phase")

    monkeypatch.setattr(HybridRunner, "_real_pipeline", fail_if_called)
    result = _runner(tmp_path / "metrics.jsonl").run()

    rows = [json.loads(line) for line in (tmp_path / "metrics.jsonl").read_text().splitlines()]
    assert result["virtual"]["label"] == "simulated"
    assert rows and all(row["mode"] == "virtual" for row in rows)
    assert all(row["simulated"] is True and row["label"] == "simulated" for row in rows)
    assert "SIMULATED " in capsys.readouterr().out


def test_same_seed_produces_identical_virtual_metrics(tmp_path, capsys):
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    _runner(first_path).run()
    _runner(second_path).run()
    capsys.readouterr()

    first = [json.loads(line) for line in first_path.read_text().splitlines()]
    second = [json.loads(line) for line in second_path.read_text().splitlines()]
    assert first == second


def test_real_cap_switches_to_virtual_without_exceeding_cap(tmp_path, capsys):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(bytes(range(32)))
    second.write_bytes(bytes(range(32, 64)))
    pipeline = UploadPipeline(MemoryIndex(), VirtualChunkStore(), PARAMS)
    runner = _runner(
        tmp_path / "metrics.jsonl",
        cap_bytes=40,
        cap_chunks=10,
        real_pipeline_factory=lambda: pipeline,
    )

    result = runner.run([first, second])
    rows = [json.loads(line) for line in (tmp_path / "metrics.jsonl").read_text().splitlines()]
    capsys.readouterr()

    assert result["switched_to_virtual"] is True
    assert result["real_bytes_written"] <= 40
    assert result["real_chunks_written"] <= 10
    assert rows[0]["mode"] == "real"
    assert all(row["mode"] == "virtual" for row in rows[1:])


def test_real_chunking_spool_roundtrips_chunks(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefghijklmnop" * 2)
    spool = tmp_path / "chunks.spool"

    spool_name, count, size, duration = _spool_chunks(
        str(source), str(spool), PARAMS, "fixed"
    )

    assert count == 2
    assert size == source.stat().st_size
    assert duration >= 0
    assert list(_read_spool(spool_name)) == [b"abcdefghijklmnop"] * 2


def test_real_phase_can_parallelize_chunking_workers(tmp_path, capsys):
    paths = [tmp_path / "one.bin", tmp_path / "two.bin"]
    paths[0].write_bytes(bytes(range(32)))
    paths[1].write_bytes(bytes(range(32, 64)))
    pipeline = UploadPipeline(MemoryIndex(), VirtualChunkStore(), PARAMS)
    runner = _runner(
        tmp_path / "metrics.jsonl",
        cap_bytes=1024,
        cap_chunks=100,
        real_worker_count=2,
        real_pipeline_factory=lambda: pipeline,
    )

    result = runner.run(paths)
    rows = [json.loads(line) for line in (tmp_path / "metrics.jsonl").read_text().splitlines()]
    capsys.readouterr()

    assert result["switched_to_virtual"] is False
    assert rows[0]["worker_count"] == 2
    assert rows[0]["chunking_chunks_per_second"] > 0


def test_virtual_generator_fingerprint_only_and_deterministic():
    from chunkwise.simulation import VirtualLoadGenerator
    gen1 = VirtualLoadGenerator(PARAMS, seed=123, duplicate_ratio=0.5, chunks_per_file=5)
    gen2 = VirtualLoadGenerator(PARAMS, seed=123, duplicate_ratio=0.5, chunks_per_file=5)
    name1, chunks1 = gen1.next_upload()
    name2, chunks2 = gen2.next_upload()
    assert name1 == name2
    assert chunks1 == chunks2
    assert len(chunks1) == 5
    for digest, size in chunks1:
        assert isinstance(digest, str)
        assert len(digest) == 64
        assert int(digest, 16) >= 0
        assert PARAMS.min_size <= size <= PARAMS.max_size


def test_virtual_mode_makes_zero_boto3_calls(tmp_path, monkeypatch):
    import boto3
    def forbidden_client(*args, **kwargs):
        raise AssertionError("boto3.client should never be called in virtual mode")
    monkeypatch.setattr(boto3, "client", forbidden_client)

    runner = _runner(tmp_path / "metrics.jsonl")
    result = runner.run()
    assert result["virtual"]["label"] == "simulated"
