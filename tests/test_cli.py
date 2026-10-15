import json
import random

from chunkwise import Engine
from chunkwise.cli import main


def test_cli_put_get_verify(tmp_path, capsys):
    home = tmp_path / "home"
    source = tmp_path / "in.bin"
    source.write_bytes(random.Random(7).randbytes(300_000))

    assert main(["--home", str(home), "put", str(source)]) == 0
    file_id = json.loads(capsys.readouterr().out)["file_id"]

    destination = tmp_path / "out.bin"
    assert main(["--home", str(home), "get", file_id, "-o", str(destination)]) == 0
    assert destination.read_bytes() == source.read_bytes()
    assert main(["--home", str(home), "verify", file_id]) == 0


def test_cli_returns_2_on_corruption(tmp_path, capsys):
    home = tmp_path / "home"
    source = tmp_path / "in.bin"
    source.write_bytes(random.Random(8).randbytes(200_000))
    assert main(["--home", str(home), "put", str(source)]) == 0
    file_id = json.loads(capsys.readouterr().out)["file_id"]

    with Engine(home) as engine:
        entry = engine.index.manifest(file_id)[0]
        path = engine.store.path_for(entry.chunk_hash)
        raw = bytearray(path.read_bytes())
        raw[0] ^= 0xFF
        path.write_bytes(raw)

    assert main(["--home", str(home), "verify", file_id]) == 2
    assert "INTEGRITY ERROR" in capsys.readouterr().err


def test_cli_missing_chunk_returns_2_and_removes_partial_output(tmp_path, capsys):
    home = tmp_path / "home"
    source = tmp_path / "in.bin"
    source.write_bytes(random.Random(9).randbytes(200_000))
    assert main(["--home", str(home), "put", str(source)]) == 0
    file_id = json.loads(capsys.readouterr().out)["file_id"]
    destination = tmp_path / "out.bin"

    with Engine(home) as engine:
        entry = engine.index.manifest(file_id)[0]
        engine.store.path_for(entry.chunk_hash).unlink()

    assert main(["--home", str(home), "get", file_id, "-o", str(destination)]) == 2
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()
    assert "INTEGRITY ERROR" in capsys.readouterr().err