"""Command-line interface for ChunkWise."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from .engine import Engine
from .errors import ChunkMissingError, ChunkWiseError, IntegrityError


def _home(argument: str | None) -> Path:
    return Path(argument or os.environ.get("CHUNKWISE_HOME", ".chunkwise"))


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


def main(argv: list[str] | None = None) -> int:
    _auto_load_dotenv()
    parser = argparse.ArgumentParser(prog="chunkwise")
    parser.add_argument(
        "--home",
        default=None,
        help="store directory (default: $CHUNKWISE_HOME or ./.chunkwise)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    put_parser = commands.add_parser("put", help="store a file")
    put_parser.add_argument("path")
    put_parser.add_argument("--chunker", choices=("cdc", "fixed"), default="cdc")

    get_parser = commands.add_parser("get", help="restore a file")
    get_parser.add_argument("file_id")
    get_parser.add_argument("-o", "--out", required=True)

    verify_parser = commands.add_parser("verify", help="verify a stored file")
    verify_parser.add_argument("file_id")

    commands.add_parser("stats", help="print storage accounting")

    delete_parser = commands.add_parser("delete", help="delete a file and tombstone zero-ref chunks")
    delete_parser.add_argument("file_id")

    gc_parser = commands.add_parser("gc", help="garbage collect expired tombstoned chunks")
    gc_parser.add_argument("--grace-seconds", type=float, default=86400.0)

    fsck_parser = commands.add_parser("fsck", help="verify refcounts, detect drift and orphan chunks")
    fsck_parser.add_argument("--repair", action="store_true", default=False)
    fsck_parser.add_argument("--max-pending-age", type=float, default=3600.0)

    arguments = parser.parse_args(argv)
    try:
        with Engine(_home(arguments.home)) as engine:
            if arguments.command == "put":
                result = engine.put_path(arguments.path, arguments.chunker)
                print(json.dumps(asdict(result), indent=2))
            elif arguments.command == "get":
                output = Path(arguments.out)
                temporary = output.with_name(output.name + ".part")
                try:
                    with temporary.open("wb") as stream:
                        size = engine.get(arguments.file_id, stream)
                    os.replace(temporary, output)
                finally:
                    temporary.unlink(missing_ok=True)
                print(f"restored {size} bytes to {output}")
            elif arguments.command == "verify":
                print(f"ok: {engine.verify(arguments.file_id)} bytes verified")
            elif arguments.command == "stats":
                print(json.dumps(asdict(engine.stats()), indent=2))
            elif arguments.command == "delete":
                result = engine.delete(arguments.file_id)
                print(json.dumps(result, indent=2))
            elif arguments.command == "gc":
                result = engine.gc(grace_seconds=arguments.grace_seconds)
                print(json.dumps(result, indent=2))
            elif arguments.command == "fsck":
                result = engine.fsck(
                    repair=arguments.repair,
                    max_pending_age_seconds=arguments.max_pending_age,
                )
                print(json.dumps(result, indent=2))
                if not result["ok"] and not arguments.repair:
                    return 2
    except (IntegrityError, ChunkMissingError) as error:
        print(f"INTEGRITY ERROR: {error}", file=sys.stderr)
        return 2
    except (ChunkWiseError, OSError, ValueError, sqlite3.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0
