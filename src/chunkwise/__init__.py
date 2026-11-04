"""ChunkWise: content-defined chunk-level storage deduplication prototype."""

from .chunker import ChunkParams
from .engine import Engine, PutResult, Stats
from .index import ChunkIndex, DynamoIndex, MemoryIndex, SqliteIndex
from .store import ChunkStore, LocalChunkStore, S3ChunkStore, VirtualChunkStore

__version__ = "0.1.0"
__all__ = [
    "ChunkIndex",
    "ChunkParams",
    "ChunkStore",
    "DynamoIndex",
    "Engine",
    "LocalChunkStore",
    "MemoryIndex",
    "PutResult",
    "S3ChunkStore",
    "SqliteIndex",
    "Stats",
    "VirtualChunkStore",
    "__version__",
]
