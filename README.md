# ChunkWise: Content-Aware Storage Deduplication Engine

Content-defined, chunk-level storage deduplication with local and AWS backends,
a dashboard/API, recovery tools, CloudWatch observability, and a capped real/virtual upload-scaling simulation.

## Project files

- `src/chunkwise/chunker.py`: Rabin-style content-defined and fixed-size chunking.
- `src/chunkwise/store.py` and `src/chunkwise/index.py`: local, S3, SQLite, DynamoDB, and virtual backend implementations.
- `src/chunkwise/engine.py`: stable `Engine` API, upload pipeline, restore, verification, delete, gc, fsck, and accounting.
- `src/chunkwise/observability.py`: CloudWatch metrics, statistic sets, structured JSON operational logging, and metrics aggregation.
- `src/chunkwise/http_api.py` and `src/chunkwise/static/`: dashboard/API, `/api/metrics` endpoint, and simulation charts.
- `src/chunkwise/simulation.py`: real-phase cap, process-based chunking, and seeded virtual load generator.
- `scripts/cloudwatch_setup.py`: idempotent provisioning for CloudWatch dashboard and integrity alarms.
- `docs/plan.md` and `docs/deployment.md`: implementation spec and deployment configuration.
- `docs/report_updates.md`: report text, conformance tables, and benchmark figures.

## Run the prototype

Requires Python 3.11+.

```powershell
pip install -e .
chunkwise put path\to\file
chunkwise stats
chunkwise get YOUR_FILE_ID -o restored.bin
chunkwise verify YOUR_FILE_ID
chunkwise delete YOUR_FILE_ID
chunkwise gc --grace-seconds 0
chunkwise fsck --repair
```

The default backend stores data in `./.chunkwise`. Restore checks every chunk
hash and the whole-file checksum before publishing the output.

## Backends and scope

The local filesystem/SQLite backend is available for development and tests. Set
`CHUNKWISE_BACKEND=aws`, `CHUNKWISE_S3_BUCKET`, and
`CHUNKWISE_DYNAMODB_TABLE` to use S3 and an on-demand DynamoDB table. boto3 uses
the default AWS credential chain, including the AWS Academy Learner Lab role.

Chunk bytes are stored before DynamoDB references are committed. Writes follow
a two-phase commit: the file is written with status pending, manifest rows and
chunk references are written, and the file record flips to committed. Read operations
only observe committed files. Concurrency protection uses lease claims for new
chunks. Recovery and lifecycle tools include `chunkwise fsck`, `chunkwise delete`,
and `chunkwise gc`.

## Storage dashboard and API

Run the same-origin dashboard and HTTP API with `chunkwise-web`. The dashboard
uses the configured ChunkWise backend for live statistics, file uploads, listing,
verified restore, and file verification. Upload retries use a persistent
idempotency key stored transactionally with the file record.

```sh
pip install -e .
CHUNKWISE_HOME="$PWD/.chunkwise" PORT=8080 chunkwise-web
```

An in-process metrics endpoint is available at `/api/metrics`.

See [docs/deployment.md](docs/deployment.md) for environment variables,
persistent-volume requirements, and CloudWatch setup.

## CloudWatch observability

Enable CloudWatch telemetry by setting `CHUNKWISE_CLOUDWATCH=1`. Run the setup
script to provision the dashboard and alarm:

```powershell
python scripts\cloudwatch_setup.py
```

## Live load simulation

Upload a file in the dashboard and use **View upload flow** to open its file
context. From `/simulation`, run a seeded 1x, 10x, and 100x virtual workload and watch
per-second charts update. The virtual run uses `MemoryIndex` and
`VirtualChunkStore`, makes zero AWS calls, and is labelled simulated.

`chunkwise-simulate` remains available for capped real AWS warm-ups followed
by virtual stages. It writes JSON Lines metrics separately from
`results/results.json`.
