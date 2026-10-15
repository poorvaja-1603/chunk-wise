# ChunkWise: Content-Aware Storage Deduplication Engine

System-design micro-project package: local prototype, professional architecture and data-flow diagrams, feasibility report, project report, and Breakscale virtual-lab materials.

## Project files

- `src/dedup_engine.py` — standard-library prototype with a 48-byte Rabin rolling window, 2/8/64 KiB min/average/max chunk bounds, SHA-256 content IDs, verified chunk storage, ordered manifests, whole-file integrity checks, local reference counts, and optional upload idempotency keys.
- `docs/architecture.svg` — polished layered architecture diagram with dotted system boundaries.
- `docs/data-flow.svg` — upload/deduplication and restore flow with commit ordering and integrity handling.
- `docs/architecture.md` — component responsibilities, metadata records, and system invariants.
- `docs/feasibility-report.md` and `docs/project-report.md` — course-ready reports.
- `lab/breakscale-lab-guide.md` — topology, scenarios, recording, and native-file export/attachment steps.
- `lab/breakscale-lab-design.json` — portable topology brief; it is not a native Breakscale project.
- `lab/experiment-tracker.xlsx` — worksheet for entering observed simulator results and attaching/linking the native lab.

## Run the prototype

Requires Python 3.10+ and uses only the standard library.

```powershell
python src/dedup_engine.py add path\to\file --idempotency-key upload-001
python src/dedup_engine.py restore YOUR_FILE_ID restored.bin
python src/dedup_engine.py stats
```

Omit `--idempotency-key` for a new logical file record each time. The default local repository is `./dedup_repository`. Restore checks both every chunk hash and the whole-file checksum before publishing the output.

## Scope note

The prototype is a local, single-process teaching implementation. It demonstrates the core content addressing and commit ordering, but it does not implement a distributed KV service, conditional writes/CAS, real concurrent-writer coordination, API authentication, cloud durability, file deletion, or executable GC/recovery workers. The design document describes those production-system components. Breakscale is for relative service-capacity experiments; it does not calculate real file-level dedupe savings.

## Breakscale file

Breakscale's native project file must be exported from its canvas as `.breakscale`. The guide provides the node/link plan. Save the native file in `lab` and add its path or link to the experiment workbook; embed it there if your instructor requires an attachment.
