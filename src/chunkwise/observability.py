"""CloudWatch observability and structured logging for ChunkWise."""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

NAMESPACE = "ChunkWise"
DEFAULT_INTERVAL_SECONDS = 30.0
MAX_QUEUE_SIZE = 2000


def is_enabled() -> bool:
    return os.environ.get("CHUNKWISE_CLOUDWATCH", "0").strip() == "1"


def get_region() -> str | None:
    return os.environ.get("CHUNKWISE_AWS_REGION") or None


def emit_log(event_type: str, **kwargs: Any) -> None:
    if not is_enabled():
        return
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **kwargs,
    }
    line = json.dumps(entry, separators=(",", ":"))
    print(line, file=sys.stdout, flush=True)


def _stat_values(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"SampleCount": 0.0, "Sum": 0.0, "Minimum": 0.0, "Maximum": 0.0}
    return {
        "SampleCount": float(len(values)),
        "Sum": float(sum(values)),
        "Minimum": float(min(values)),
        "Maximum": float(max(values)),
    }


@dataclass
class InProcessMetrics:
    upload_bytes: int = 0
    chunks_processed: int = 0
    new_chunks: int = 0
    duplicate_chunks: int = 0
    errors: int = 0
    integrity_errors: int = 0
    fsck_drift_chunks: int = 0
    s3_requests: int = 0
    dynamo_requests: int = 0
    put_latencies_ms: list[float] = field(default_factory=list)
    index_lookup_latencies_ms: list[float] = field(default_factory=list)
    s3_put_latencies_ms: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "upload_bytes": self.upload_bytes,
            "chunks_processed": self.chunks_processed,
            "new_chunks": self.new_chunks,
            "duplicate_chunks": self.duplicate_chunks,
            "dedup_ratio": (
                round(self.chunks_processed / max(1, self.new_chunks), 2)
                if self.new_chunks > 0
                else 1.0
            ),
            "errors": self.errors,
            "integrity_errors": self.integrity_errors,
            "fsck_drift_chunks": self.fsck_drift_chunks,
            "s3_requests": self.s3_requests,
            "dynamo_requests": self.dynamo_requests,
            "put_count": len(self.put_latencies_ms),
            "avg_put_latency_ms": (
                round(sum(self.put_latencies_ms) / len(self.put_latencies_ms), 2)
                if self.put_latencies_ms
                else 0.0
            ),
        }


class ObservabilityTracker:
    def __init__(self, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
        self.interval = interval_seconds
        self.metrics = InProcessMetrics()
        self._lock = threading.Lock()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._client: Any = None
        self._worker_thread: threading.Thread | None = None
        self._running = False

    def _get_client(self) -> Any:
        if self._client is None and is_enabled():
            try:
                import boto3
                self._client = boto3.client("cloudwatch", region_name=get_region())
            except Exception as error:
                print(f"Failed to initialize CloudWatch client: {error}", file=sys.stderr)
        return self._client

    def _ensure_worker(self) -> None:
        if not is_enabled():
            return
        with self._lock:
            if not self._running:
                self._running = True
                self._worker_thread = threading.Thread(
                    target=self._worker_loop,
                    name="chunkwise-cloudwatch-emitter",
                    daemon=True,
                )
                self._worker_thread.start()

    def _worker_loop(self) -> None:
        while self._running:
            time.sleep(self.interval)
            try:
                self.flush()
            except Exception as error:
                print(f"CloudWatch background flush error: {error}", file=sys.stderr)

    def record_upload(
        self,
        bytes_uploaded: int,
        chunks: int,
        new_chunks: int,
        duplicate_chunks: int,
        put_latency_ms: float,
        lookup_latencies: Sequence[float] | None = None,
        s3_put_latencies: Sequence[float] | None = None,
        mode: str = "real",
        backend: str = "local",
        s3_requests: int = 0,
        dynamo_requests: int = 0,
    ) -> None:
        with self._lock:
            self.metrics.upload_bytes += bytes_uploaded
            self.metrics.chunks_processed += chunks
            self.metrics.new_chunks += new_chunks
            self.metrics.duplicate_chunks += duplicate_chunks
            self.metrics.s3_requests += s3_requests
            self.metrics.dynamo_requests += dynamo_requests
            self.metrics.put_latencies_ms.append(put_latency_ms)
            if lookup_latencies:
                self.metrics.index_lookup_latencies_ms.extend(lookup_latencies)
            if s3_put_latencies:
                self.metrics.s3_put_latencies_ms.extend(s3_put_latencies)

        if not is_enabled():
            return

        self._ensure_worker()
        payload = {
            "type": "upload",
            "bytes": bytes_uploaded,
            "chunks": chunks,
            "new_chunks": new_chunks,
            "duplicate_chunks": duplicate_chunks,
            "put_latency_ms": put_latency_ms,
            "lookup_latencies": list(lookup_latencies or []),
            "s3_put_latencies": list(s3_put_latencies or []),
            "mode": mode,
            "backend": backend,
            "s3_requests": s3_requests,
            "dynamo_requests": dynamo_requests,
        }
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            pass

    def record_error(
        self,
        is_integrity: bool = False,
        mode: str = "real",
        backend: str = "local",
    ) -> None:
        with self._lock:
            self.metrics.errors += 1
            if is_integrity:
                self.metrics.integrity_errors += 1

        if not is_enabled():
            return

        self._ensure_worker()
        try:
            self._queue.put_nowait({
                "type": "error",
                "is_integrity": is_integrity,
                "mode": mode,
                "backend": backend,
            })
        except queue.Full:
            pass

    def record_fsck(
        self,
        drift_chunks: int,
        mode: str = "real",
        backend: str = "local",
    ) -> None:
        with self._lock:
            self.metrics.fsck_drift_chunks += drift_chunks

        if not is_enabled():
            return

        self._ensure_worker()
        try:
            self._queue.put_nowait({
                "type": "fsck",
                "drift_chunks": drift_chunks,
                "mode": mode,
                "backend": backend,
            })
        except queue.Full:
            pass

    def flush(self) -> None:
        client = self._get_client()
        if client is None:
            return

        items: list[dict[str, Any]] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break

        if not items:
            return

        # Group by (mode, backend)
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in items:
            key = (item.get("mode", "real"), item.get("backend", "local"))
            grouped.setdefault(key, []).append(item)

        for (mode, backend), records in grouped.items():
            dimensions = [
                {"Name": "Mode", "Value": mode},
                {"Name": "Backend", "Value": backend},
            ]

            upload_bytes = 0
            chunks_processed = 0
            new_chunks = 0
            duplicate_chunks = 0
            errors = 0
            integrity_errors = 0
            fsck_drift = 0
            s3_reqs = 0
            dynamo_reqs = 0
            put_lats: list[float] = []
            lookup_lats: list[float] = []
            s3_put_lats: list[float] = []

            for rec in records:
                rtype = rec["type"]
                if rtype == "upload":
                    upload_bytes += rec["bytes"]
                    chunks_processed += rec["chunks"]
                    new_chunks += rec["new_chunks"]
                    duplicate_chunks += rec["duplicate_chunks"]
                    s3_reqs += rec["s3_requests"]
                    dynamo_reqs += rec["dynamo_requests"]
                    put_lats.append(rec["put_latency_ms"])
                    lookup_lats.extend(rec["lookup_latencies"])
                    s3_put_lats.extend(rec["s3_put_latencies"])
                elif rtype == "error":
                    errors += 1
                    if rec.get("is_integrity"):
                        integrity_errors += 1
                elif rtype == "fsck":
                    fsck_drift += rec["drift_chunks"]

            metric_data: list[dict[str, Any]] = []
            if upload_bytes > 0:
                metric_data.append({
                    "MetricName": "UploadBytes",
                    "Dimensions": dimensions,
                    "Value": float(upload_bytes),
                    "Unit": "Bytes",
                })
            if chunks_processed > 0:
                metric_data.append({
                    "MetricName": "ChunksProcessed",
                    "Dimensions": dimensions,
                    "Value": float(chunks_processed),
                    "Unit": "Count",
                })
            if new_chunks > 0:
                metric_data.append({
                    "MetricName": "NewChunks",
                    "Dimensions": dimensions,
                    "Value": float(new_chunks),
                    "Unit": "Count",
                })
            if duplicate_chunks > 0:
                metric_data.append({
                    "MetricName": "DuplicateChunks",
                    "Dimensions": dimensions,
                    "Value": float(duplicate_chunks),
                    "Unit": "Count",
                })
            if chunks_processed > 0:
                ratio = chunks_processed / max(1, new_chunks)
                metric_data.append({
                    "MetricName": "DedupRatio",
                    "Dimensions": dimensions,
                    "Value": float(ratio),
                    "Unit": "None",
                })
            if errors > 0:
                metric_data.append({
                    "MetricName": "Errors",
                    "Dimensions": dimensions,
                    "Value": float(errors),
                    "Unit": "Count",
                })
            if integrity_errors > 0:
                metric_data.append({
                    "MetricName": "IntegrityErrors",
                    "Dimensions": dimensions,
                    "Value": float(integrity_errors),
                    "Unit": "Count",
                })
            if fsck_drift > 0:
                metric_data.append({
                    "MetricName": "FsckDriftChunks",
                    "Dimensions": dimensions,
                    "Value": float(fsck_drift),
                    "Unit": "Count",
                })
            if s3_reqs > 0:
                metric_data.append({
                    "MetricName": "S3Requests",
                    "Dimensions": dimensions,
                    "Value": float(s3_reqs),
                    "Unit": "Count",
                })
            if dynamo_reqs > 0:
                metric_data.append({
                    "MetricName": "DynamoRequests",
                    "Dimensions": dimensions,
                    "Value": float(dynamo_reqs),
                    "Unit": "Count",
                })

            if put_lats:
                metric_data.append({
                    "MetricName": "PutLatencyMs",
                    "Dimensions": dimensions,
                    "StatisticValues": _stat_values(put_lats),
                    "Unit": "Milliseconds",
                })
            if lookup_lats:
                metric_data.append({
                    "MetricName": "IndexLookupLatencyMs",
                    "Dimensions": dimensions,
                    "StatisticValues": _stat_values(lookup_lats),
                    "Unit": "Milliseconds",
                })
            if s3_put_lats:
                metric_data.append({
                    "MetricName": "S3PutLatencyMs",
                    "Dimensions": dimensions,
                    "StatisticValues": _stat_values(s3_put_lats),
                    "Unit": "Milliseconds",
                })

            # Send in batches of up to 20
            batch_size = 20
            for i in range(0, len(metric_data), batch_size):
                batch = metric_data[i:i + batch_size]
                try:
                    client.put_metric_data(Namespace=NAMESPACE, MetricData=batch)
                except Exception as error:
                    print(f"CloudWatch put_metric_data failed: {error}", file=sys.stderr)

    def stop(self) -> None:
        self._running = False
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
        try:
            self.flush()
        except Exception:
            pass

    def reset(self) -> None:
        with self._lock:
            self.metrics = InProcessMetrics()
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break


_GLOBAL_TRACKER = ObservabilityTracker()


def get_tracker() -> ObservabilityTracker:
    return _GLOBAL_TRACKER


def get_metrics() -> InProcessMetrics:
    return _GLOBAL_TRACKER.metrics


def reset_metrics() -> None:
    _GLOBAL_TRACKER.reset()

