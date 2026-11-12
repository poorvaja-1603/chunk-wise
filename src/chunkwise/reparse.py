"""Utilities for safe handling of filesystem reparse points.

This module implements the Bhoomi-side safety checks described in the project
plan: detect reparse entries, prevent traversal outside a sandbox root, and
create a link only when the caller explicitly requests it.
"""
from __future__ import annotations

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
        # Windows reparse metadata is exposed via the file attribute bit. We do not
        # depend on pywin32 being installed in this environment: if the attribute is
        # visible via ``stat`` we treat it as a reparse. The caller can still inspect
        # ``target_path`` once the real Windows API is available.
        try:
            attrs = os.stat(path).st_mode
            if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:  # type: ignore[attr-defined]
                info["is_reparse"] = True
                info["tag_type"] = "unknown"
                info["target_path"] = str(Path(path).resolve())
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


def create_reparse_link(source_path: str, target: str, link_type: str) -> bool:
    """Create a safe symlink/junction-like pointer.

    The function accepts a source destination path and a target path. It verifies
    that the target stays inside the destination root before creating a link.
    Returns ``True`` when the link is created successfully and ``False`` otherwise.
    """
    if link_type not in REPARSE_TAGS:
        raise ValueError(f"unsupported reparse type: {link_type!r}")

    source = Path(source_path)
    source.mkdir(parents=True, exist_ok=True) if source.is_dir() else source.parent.mkdir(parents=True, exist_ok=True)

    root_dir = str(source.parent.resolve(strict=False))
    safe_target = safe_resolve_target(target, root_dir)

    if source.exists() or source.is_symlink():
        return False

    try:
        if link_type in {"junction", "mount_point"}:
            # Posix platforms cannot create a true NTFS junction, but a directory
            # symlink is the closest portable equivalent that keeps the API stable.
            os.symlink(safe_target, str(source), target_is_directory=True)
            return True
        os.symlink(safe_target, str(source), target_is_directory=Path(safe_target).is_dir())
        return True
    except (NotImplementedError, OSError, ValueError):
        return False


__all__ = [
    "REPARSE_TAGS",
    "inspect_path",
    "safe_resolve_target",
    "create_reparse_link",
]
