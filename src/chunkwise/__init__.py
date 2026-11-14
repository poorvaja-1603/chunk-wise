"""ChunkWise: content-defined chunk-level storage deduplication prototype."""

from .chunker import ChunkParams
from .engine import Engine, PutResult, Stats
from .index import ChunkIndex, DynamoIndex, MemoryIndex, SqliteIndex
from .policy import DEFAULT_POLICY, PolicyProfile, PROFILES, get_active_policy, get_policy, list_policies, select_policy
from .reparse import create_reparse_link, inspect_path, safe_resolve_target, scan_directory
from .store import ChunkStore, LocalChunkStore, S3ChunkStore, VirtualChunkStore

__version__ = "0.1.0"
__all__ = [
    "ChunkIndex",
    "ChunkParams",
    "ChunkStore",
    "DEFAULT_POLICY",
    "DynamoIndex",
    "Engine",
    "LocalChunkStore",
    "MemoryIndex",
    "PolicyProfile",
    "PROFILES",
    "PutResult",
    "S3ChunkStore",
    "SqliteIndex",
    "Stats",
    "VirtualChunkStore",
    "create_reparse_link",
    "get_active_policy",
    "get_policy",
    "inspect_path",
    "list_policies",
    "safe_resolve_target",
    "scan_directory",
    "select_policy",
    "__version__",
]
