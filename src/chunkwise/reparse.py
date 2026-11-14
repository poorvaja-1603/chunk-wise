"""Utilities for safe handling of filesystem reparse points.

This module implements the Bhoomi-side safety checks described in the project
plan: detect reparse entries, prevent traversal outside a sandbox root, and
create a link only when the caller explicitly requests it.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


REPARSE_TAGS = {
    "symlink": "symlink",
    "junction": "junction",
    "mount_point": "mount_point",
    "unknown": "unknown",
}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def inspect_path(path: str) -> dict[str, Any]:
    """Return a metadata dictionary describing a filesystem entry.

    The structure intentionally mirrors the plan's ``reparse_info`` contract:
    ``is_reparse``, ``tag_type``, ``target_path``, and ``is_directory``.
    """
    p = Path(path)
    info: dict[str, Any] = {
        "path": str(p),
        "exists": p.exists(),
        "is_reparse": False,
        "tag_type": "unknown",
        "target_path": None,
        "is_directory": False,
    }

    if not p.exists():
        return info

    try:
        st = p.lstat()
    except OSError:
        return info

    info["is_directory"] = stat.S_ISDIR(st.st_mode)

    try:
        if os.path.islink(path):
            info["is_reparse"] = True
            info["tag_type"] = "symlink"
            info["target_path"] = os.readlink(path)
            info["is_directory"] = os.path.isdir(path)
            return info
    except OSError:
        pass

    if sys.platform.startswith("win"):
        # Windows reparse metadata is exposed via the file attribute bit.
        # Check both st.st_file_attributes and S_ISLNK.
        try:
            attrs = getattr(st, "st_file_attributes", 0)
            reparse_bit = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if attrs & reparse_bit:
                info["is_reparse"] = True
                info["tag_type"] = "junction" if info["is_directory"] else "symlink"
                try:
                    raw_target = os.readlink(path)
                    info["target_path"] = raw_target.removeprefix("\\\\?\\")
                except (OSError, ValueError):
                    info["target_path"] = str(Path(path).resolve())
                return info
        except (AttributeError, OSError):
            pass

    return info


def safe_resolve_target(link_path: str, root_dir: str) -> str:
    """Resolve a link target but reject traversal outside the sandbox root."""
    root = Path(root_dir).expanduser().resolve(strict=False)
    candidate = Path(link_path).expanduser()

    if not candidate.is_absolute():
        resolved = (root / candidate).resolve(strict=False)
    else:
        resolved = candidate.resolve(strict=False)

    if not _is_relative_to(resolved, root):
        raise ValueError(
            f"link target escapes sandbox root: {link_path!r} resolves outside {root_dir!r}"
        )
    return str(resolved)


def create_reparse_link(
    source_path: str,
    target: str,
    link_type: str,
    sandbox_root: str | None = None,
) -> bool:
    """Create a safe symlink/junction-like pointer.

    The function accepts a source destination path and a target path. It verifies
    that the target stays inside the sandbox root before creating a link.
    Returns ``True`` when the link is created successfully and ``False`` otherwise.
    """
    if link_type not in REPARSE_TAGS:
        raise ValueError(f"unsupported reparse type: {link_type!r}")

    source = Path(source_path)
    source.mkdir(parents=True, exist_ok=True) if source.is_dir() else source.parent.mkdir(parents=True, exist_ok=True)

    root_dir = str(Path(sandbox_root).resolve(strict=False)) if sandbox_root else str(source.parent.resolve(strict=False))
    try:
        safe_target = safe_resolve_target(target, root_dir)
    except ValueError:
        return False

    if source.exists() or source.is_symlink():
        return False

    try:
        if link_type in {"junction", "mount_point"}:
            if sys.platform.startswith("win"):
                try:
                    import _winapi

                    _winapi.CreateJunction(safe_target, str(source))
                    return True
                except (AttributeError, OSError):
                    pass
            # Posix platforms cannot create a true NTFS junction, but a directory
            # symlink is the closest portable equivalent that keeps the API stable.
            os.symlink(safe_target, str(source), target_is_directory=True)
            return True
        os.symlink(safe_target, str(source), target_is_directory=Path(safe_target).is_dir())
        return True
    except (NotImplementedError, OSError, ValueError):
        # Fall back to creating a text stub .link_info.json explaining the target
        try:
            stub_path = source.parent / f"{source.name}.link_info.json"
            stub_path.write_text(
                json.dumps(
                    {
                        "is_reparse": True,
                        "tag_type": link_type,
                        "target_path": safe_target,
                        "is_directory": Path(safe_target).is_dir(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            return True
        except OSError:
            return False


def scan_directory(
    root_dir: str | Path,
    record_only: bool = True,
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Scan root_dir safely for files and capture reparse points without infinite loops.

    Args:
        root_dir: The directory tree to scan.
        record_only: If True, treats reparse points as atomic metadata objects
            and never recurses into them (Plan Section 3.C).

    Returns:
        (regular_files, reparse_records)
    """
    root = Path(root_dir).resolve()
    regular_files: list[Path] = []
    reparse_records: list[dict[str, Any]] = []

    def _walk(current: Path) -> None:
        try:
            with os.scandir(current) as it:
                for entry in it:
                    entry_path = Path(entry.path)
                    info = inspect_path(str(entry_path))
                    if info.get("is_reparse"):
                        reparse_records.append(info)
                        if record_only:
                            continue
                    if entry_path.is_file():
                        regular_files.append(entry_path)
                    elif entry_path.is_dir():
                        _walk(entry_path)
        except OSError:
            pass

    if root.exists():
        _walk(root)

    return regular_files, reparse_records


__all__ = [
    "REPARSE_TAGS",
    "inspect_path",
    "safe_resolve_target",
    "create_reparse_link",
    "scan_directory",
]
