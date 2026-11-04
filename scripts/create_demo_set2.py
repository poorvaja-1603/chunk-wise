"""Create a second realistic demo dataset (logs and JSON backups) for live testing."""
from __future__ import annotations

import json
from pathlib import Path

out_dir = Path("demo_set2")
out_dir.mkdir(exist_ok=True)

# 1. Server Log Dataset
log_entries = []
for i in range(1, 4001):
    status = 200 if i % 10 != 0 else (404 if i % 25 == 0 else 500)
    user = f"user_{i % 80:03d}"
    endpoint = ["/api/v1/auth", "/api/v1/storage/chunks", "/api/v1/files/upload", "/dashboard", "/healthz"][i % 5]
    log_entries.append(
        f"2026-11-05T00:{i%60:02d}:{(i*7)%60:02d}.{i%1000:03d}Z [INFO] [{user}] request_id=req_{i:06x} method=POST path={endpoint} status={status} latency_ms={12 + (i % 45)}"
    )

log_v1 = "\n".join(log_entries) + "\n"
(out_dir / "server_log_v1.log").write_text(log_v1, encoding="utf-8")
(out_dir / "server_log_v1_duplicate.log").write_text(log_v1, encoding="utf-8")

# Log v2 with 200 new appended entries
appended_entries = [
    f"2026-11-05T01:00:{i%60:02d}.000Z [ALERT] [admin_ops] request_id=req_live_{i:04x} event=BATCH_IMPORT status=200 records_processed={i * 100}"
    for i in range(1, 201)
]
log_v2 = log_v1 + "\n".join(appended_entries) + "\n"
(out_dir / "server_log_v2_appended.log").write_text(log_v2, encoding="utf-8")

# 2. Database Backup Dataset (JSON)
records = []
for i in range(1, 2501):
    records.append({
        "customer_id": f"cust_{i:05d}",
        "company": f"Acme Corporation Branch #{i % 50}",
        "tier": "enterprise" if i % 3 == 0 else "standard",
        "active_buckets": [f"bucket-{i}-primary", f"bucket-{i}-secondary"],
        "metadata": {
            "region": "us-east-1",
            "allocated_storage_gb": 500 + (i % 200) * 10,
            "compliance_flags": ["HIPAA", "SOC2"] if i % 2 == 0 else ["SOC2"],
            "last_audit_epoch": 1793818600 + i * 3600,
        },
    })

json_v1 = json.dumps({"schema_version": "2.4", "records": records}, indent=2)
(out_dir / "database_backup_v1.json").write_text(json_v1, encoding="utf-8")
(out_dir / "database_backup_v1_duplicate.json").write_text(json_v1, encoding="utf-8")

# JSON v2 with a few modified records in the middle
records_v2 = list(records)
for idx in [500, 1000, 1500]:
    records_v2[idx]["tier"] = "premium_vip"
    records_v2[idx]["metadata"]["allocated_storage_gb"] = 99999

json_v2 = json.dumps({"schema_version": "2.4", "records": records_v2}, indent=2)
(out_dir / "database_backup_v2_updated.json").write_text(json_v2, encoding="utf-8")

print("Created demo_set2 files successfully:")
for p in sorted(out_dir.glob("*")):
    print(f"  {p} ({p.stat().st_size:,} bytes)")
