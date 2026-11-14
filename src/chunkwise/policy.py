"""Optimization policy profiles for ChunkWise.

This module implements the Bhoomi-side policy engine described in the project
plan: preset profiles, validation, and the active policy selection API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

DEFAULT_POLICY = "balanced"


@dataclass(frozen=True)
class PolicyProfile:
    """Configuration profile applied during chunk ingest."""

    name: str
    policy_id: str
    description: str
    compression: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": True,
            "codec": "zlib",
            "level": 6,
            "min_saving_ratio": 0.05,
        }
    )
    chunking: dict[str, Any] = field(
        default_factory=lambda: {"min_kb": 2, "avg_kb": 8, "max_kb": 64}
    )
    reparse_policy: dict[str, Any] = field(
        default_factory=lambda: {"record_only": True, "allow_link_traversal": False}
    )
    concurrency: dict[str, Any] = field(
        default_factory=lambda: {"upload_threads": 4}
    )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PolicyProfile":
        return cls(
            name=str(data.get("name", data.get("policy_name", "Default"))),
            policy_id=str(data.get("policy_id", data.get("id", "policy_default_v1"))),
            description=str(data.get("description", "")),
            compression=dict(data.get("compression", {})),
            chunking=dict(data.get("chunking", {})),
            reparse_policy=dict(data.get("reparse_policy", {})),
            concurrency=dict(data.get("concurrency", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "policy_id": self.policy_id,
            "description": self.description,
            "compression": self.compression,
            "chunking": self.chunking,
            "reparse_policy": self.reparse_policy,
            "concurrency": self.concurrency,
        }


PROFILES: dict[str, PolicyProfile] = {
    "balanced": PolicyProfile(
        name="Balanced",
        policy_id="policy_balanced_v1",
        description="Standard trade-off between CPU consumption and storage footprint.",
        compression={
            "enabled": True,
            "codec": "zlib",
            "level": 6,
            "min_saving_ratio": 0.05,
        },
        chunking={"min_kb": 2, "avg_kb": 8, "max_kb": 64},
        reparse_policy={"record_only": True, "allow_link_traversal": False},
        concurrency={"upload_threads": 4},
    ),
    "space_saver": PolicyProfile(
        name="Space Saver",
        policy_id="policy_space_saver_v1",
        description="Maximum storage reduction at higher compression cost.",
        compression={
            "enabled": True,
            "codec": "zlib",
            "level": 9,
            "min_saving_ratio": 0.02,
        },
        chunking={"min_kb": 2, "avg_kb": 8, "max_kb": 64},
        reparse_policy={"record_only": True, "allow_link_traversal": False},
        concurrency={"upload_threads": 4},
    ),
    "fast_restore": PolicyProfile(
        name="Fast Restore",
        policy_id="policy_fast_restore_v1",
        description="Lower compression cost and faster restore throughput.",
        compression={
            "enabled": True,
            "codec": "zlib",
            "level": 1,
            "min_saving_ratio": 0.10,
        },
        chunking={"min_kb": 2, "avg_kb": 8, "max_kb": 64},
        reparse_policy={"record_only": True, "allow_link_traversal": False},
        concurrency={"upload_threads": 4},
    ),
    "disabled": PolicyProfile(
        name="No Compression",
        policy_id="policy_disabled_v1",
        description="Compression is fully disabled for all uploads.",
        compression={
            "enabled": False,
            "codec": "none",
            "level": 0,
            "min_saving_ratio": 1.0,
        },
        chunking={"min_kb": 2, "avg_kb": 8, "max_kb": 64},
        reparse_policy={"record_only": True, "allow_link_traversal": False},
        concurrency={"upload_threads": 4},
    ),
}

_ACTIVE_POLICY = DEFAULT_POLICY


def normalize_policy_name(name: str | None) -> str:
    if name is None:
        return DEFAULT_POLICY
    key = str(name).strip().lower().replace(" ", "_")
    if key in PROFILES:
        return key
    if key == "space_saver":
        return "space_saver"
    if key == "fast_restore":
        return "fast_restore"
    if key in {"none", "disabled", "no_compression"}:
        return "disabled"
    raise ValueError(f"unknown policy '{name}'")


def list_policies() -> list[dict[str, Any]]:
    return [PROFILES[name].to_dict() for name in ("balanced", "space_saver", "fast_restore", "disabled")]


def get_policy(name: str | None = None) -> dict[str, Any]:
    key = normalize_policy_name(name)
    return PROFILES[key].to_dict()


def get_active_policy() -> dict[str, Any]:
    return PROFILES[_ACTIVE_POLICY].to_dict()


def select_policy(name: str | None = None) -> dict[str, Any]:
    global _ACTIVE_POLICY
    key = normalize_policy_name(name)
    _ACTIVE_POLICY = key
    import os

    os.environ["CHUNKWISE_COMPRESSION_PROFILE"] = key
    return PROFILES[key].to_dict()


set_active_policy = select_policy


def validate_policy(policy: Mapping[str, Any]) -> PolicyProfile:
    if not isinstance(policy, Mapping):
        raise TypeError("policy must be a mapping")
    return PolicyProfile.from_dict(policy)


__all__ = [
    "DEFAULT_POLICY",
    "PROFILES",
    "PolicyProfile",
    "get_policy",
    "get_active_policy",
    "list_policies",
    "normalize_policy_name",
    "select_policy",
    "set_active_policy",
    "validate_policy",
]
