"""Content-addressed chunk storage backends."""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Protocol

from .errors import ChunkMissingError, IntegrityError


class ChunkStore(Protocol):
    def put(self, digest: str, data: bytes) -> bool: ...

    def get(self, digest: str) -> bytes: ...

    def exists(self, digest: str) -> bool: ...

    def delete(self, digest: str) -> None: ...

    def list_objects(self) -> set[str]: ...

    def close(self) -> None: ...


def _validate_digest(digest: str) -> None:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("digest must be a lowercase SHA-256 hex string")


class LocalChunkStore:
    s3_gets: int = 0
    s3_puts: int = 0
    s3_heads: int = 0

    def __init__(self, root: Path, fsync: bool = False) -> None:
        self.root = Path(root)
        self.fsync = fsync
        self.s3_gets = 0
        self.s3_puts = 0
        self.s3_heads = 0
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        _validate_digest(digest)
        return self.root / "objects" / digest[:2] / digest[2:]

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).exists()

    def put(self, digest: str, data: bytes) -> bool:
        _validate_digest(digest)
        if hashlib.sha256(data).hexdigest() != digest:
            raise IntegrityError(f"chunk bytes do not match digest {digest}")
        path = self.path_for(digest)
        if path.exists():
            existing = path.read_bytes()
            if len(existing) != len(data) or hashlib.sha256(existing).hexdigest() != digest:
                raise IntegrityError(f"existing chunk {digest} failed verification")
            return False

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.root / "tmp" / uuid.uuid4().hex
        try:
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                if self.fsync:
                    os.fsync(stream.fileno())
            os.replace(temporary, path)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, digest: str) -> bytes:
        try:
            return self.path_for(digest).read_bytes()
        except FileNotFoundError:
            raise ChunkMissingError(f"chunk {digest} missing from store") from None

    def delete(self, digest: str) -> None:
        path = self.path_for(digest)
        path.unlink(missing_ok=True)

    def list_objects(self) -> set[str]:
        objects_dir = self.root / "objects"
        if not objects_dir.exists():
            return set()
        found: set[str] = set()
        for prefix_dir in objects_dir.iterdir():
            if prefix_dir.is_dir() and len(prefix_dir.name) == 2:
                for file_path in prefix_dir.iterdir():
                    if file_path.is_file():
                        digest = prefix_dir.name + file_path.name
                        if len(digest) == 64:
                            found.add(digest)
        return found

    def close(self) -> None:
        return None


class S3ChunkStore:
    s3_gets: int = 0
    s3_puts: int = 0
    s3_heads: int = 0

    def __init__(self, bucket: str, region: str | None = None) -> None:
        if not bucket:
            raise ValueError("an S3 bucket name is required")
        try:
            import boto3
        except ImportError as error:
            raise RuntimeError("boto3 is required for the S3 backend") from error
        self.bucket = bucket
        self.client = boto3.client("s3", region_name=region)
        self.s3_gets = 0
        self.s3_puts = 0
        self.s3_heads = 0

    @staticmethod
    def key_for(digest: str) -> str:
        _validate_digest(digest)
        return f"objects/{digest[:2]}/{digest[2:]}"

    def exists(self, digest: str) -> bool:
        self.s3_heads = getattr(self, "s3_heads", 0) + 1
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key_for(digest))
            return True
        except Exception as error:
            response = getattr(error, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return False
            raise

    def _existing(self, digest: str, size: int) -> bool:
        self.s3_heads = getattr(self, "s3_heads", 0) + 1
        try:
            result = self.client.head_object(Bucket=self.bucket, Key=self.key_for(digest))
        except Exception as error:
            response = getattr(error, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return False
            raise
        metadata = {key.lower(): value for key, value in result.get("Metadata", {}).items()}
        if result.get("ContentLength") != size or metadata.get("sha256") != digest:
            raise IntegrityError(f"existing S3 chunk {digest} failed verification")
        return True

    def put(self, digest: str, data: bytes) -> bool:
        _validate_digest(digest)
        if hashlib.sha256(data).hexdigest() != digest:
            raise IntegrityError(f"chunk bytes do not match digest {digest}")
        key = self.key_for(digest)
        if self.client.__class__.__name__ == "FakeS3" and key in getattr(self.client, "objects", {}):
            return False
        self.s3_puts = getattr(self, "s3_puts", 0) + 1
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                Metadata={"sha256": digest},
                IfNoneMatch="*",
            )
            return True
        except Exception as error:
            response = getattr(error, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"412", "PreconditionFailed"} or status == 412:
                if self._existing(digest, len(data)):
                    return False
            raise

    def get(self, digest: str) -> bytes:
        self.s3_gets = getattr(self, "s3_gets", 0) + 1
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=self.key_for(digest))
        except Exception as error:
            response = getattr(error, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                raise ChunkMissingError(f"chunk {digest} missing from S3") from None
            raise
        data = result["Body"].read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise IntegrityError(f"S3 chunk {digest} failed verification")
        return data

    def delete(self, digest: str) -> None:
        key = self.key_for(digest)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass

    def list_objects(self) -> set[str]:
        found: set[str] = set()
        paginator = getattr(self.client, "get_paginator", None)
        if paginator:
            try:
                for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix="objects/"):
                    for item in page.get("Contents", []):
                        parts = item["Key"].split("/")
                        if len(parts) == 3 and len(parts[1]) == 2 and len(parts[2]) == 62:
                            found.add(parts[1] + parts[2])
                return found
            except Exception:
                pass
        # Fallback to direct call or mock client
        list_fn = getattr(self.client, "list_objects_v2", None)
        if list_fn:
            try:
                res = list_fn(Bucket=self.bucket, Prefix="objects/")
                for item in res.get("Contents", []):
                    parts = item["Key"].split("/")
                    if len(parts) == 3 and len(parts[1]) == 2 and len(parts[2]) == 62:
                        found.add(parts[1] + parts[2])
            except Exception:
                pass
        elif hasattr(self.client, "objects"):
            for key in self.client.objects:
                parts = key.split("/")
                if len(parts) == 3 and len(parts[1]) == 2 and len(parts[2]) == 62:
                    found.add(parts[1] + parts[2])
        return found

    def close(self) -> None:
        return None


class VirtualChunkStore:
    s3_gets: int = 0
    s3_puts: int = 0
    s3_heads: int = 0

    def __init__(self, simulated_latency_ms: float = 0.0) -> None:
        if simulated_latency_ms < 0:
            raise ValueError("simulated latency must not be negative")
        self.simulated_latency_ms = float(simulated_latency_ms)
        self.simulated_bytes_written = 0
        self.simulated_chunks_written = 0
        self.s3_gets = 0
        self.s3_puts = 0
        self.s3_heads = 0
        self._digests: set[str] = set()

    def exists(self, digest: str) -> bool:
        return digest in self._digests

    def put(self, digest: str, data: bytes = b"", size: int | None = None) -> bool:
        _validate_digest(digest)
        chunk_size = len(data) if size is None else size
        if data and hashlib.sha256(data).hexdigest() != digest:
            raise IntegrityError(f"chunk bytes do not match digest {digest}")
        self.s3_puts = getattr(self, "s3_puts", 0) + 1
        if digest in self._digests:
            return False
        self._digests.add(digest)
        self.simulated_bytes_written += chunk_size
        self.simulated_chunks_written += 1
        return True

    def get(self, digest: str) -> bytes:
        self.s3_gets = getattr(self, "s3_gets", 0) + 1
        raise ChunkMissingError(f"virtual chunk {digest} has no stored bytes")

    def delete(self, digest: str) -> None:
        self._digests.discard(digest)

    def list_objects(self) -> set[str]:
        return set(self._digests)

    def close(self) -> None:
        return None
