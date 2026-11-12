from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from chunkwise.policy import PROFILES, get_policy, list_policies, select_policy
from chunkwise.reparse import create_reparse_link, inspect_path, safe_resolve_target


def test_policy_profiles_are_present():
    assert set(PROFILES) >= {"balanced", "space_saver", "fast_restore", "disabled"}
    assert get_policy("balanced")["policy_id"] == "policy_balanced_v1"
    assert list_policies()[0]["name"] == "Balanced"


def test_select_policy_updates_active_value():
    selected = select_policy("space_saver")
    assert selected["name"] == "Space Saver"
    assert get_policy("space_saver")["policy_id"] == "policy_space_saver_v1"


def test_safe_resolve_target_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with pytest.raises(ValueError, match="escapes sandbox root"):
            safe_resolve_target("../../etc/passwd", str(root))


def test_create_reparse_link_creates_directory_symlink():
    if os.name == "nt":
        pytest.skip("symlink creation is platform-dependent on Windows")
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        target = root / "target"
        target.mkdir()
        link_path = root / "linkdir"
        assert create_reparse_link(str(link_path), str(target), "junction") is True
        assert link_path.is_symlink()
        info = inspect_path(str(link_path))
        assert info["is_reparse"] is True
        assert info["tag_type"] == "symlink"
