"""Clear all S3 objects and DynamoDB items for ChunkWise."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Auto-load .env
env_file = Path(".env")
if env_file.is_file():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip("'\"")
        os.environ[key] = val
        os.environ[key.upper()] = val

bucket_name = os.environ.get("CHUNKWISE_S3_BUCKET", "chunk-wise")
table_name = os.environ.get("CHUNKWISE_DYNAMODB_TABLE", "chunkwise-index")
region = os.environ.get("CHUNKWISE_AWS_REGION", "us-east-1")

print(f"Connecting to AWS in region {region}...")
print(f"Target S3 Bucket: {bucket_name}")
print(f"Target DynamoDB Table: {table_name}")

try:
    import boto3
except ImportError:
    print("boto3 is required. Run: pip install boto3", file=sys.stderr)
    sys.exit(1)

s3 = boto3.client("s3", region_name=region)
dynamo = boto3.client("dynamodb", region_name=region)

# 1. Clear S3 Objects
print("\n--- Clearing S3 Objects ---")
paginator = s3.get_paginator("list_objects_v2")
deleted_s3 = 0
for page in paginator.paginate(Bucket=bucket_name, Prefix="objects/"):
    objects = page.get("Contents", [])
    if objects:
        delete_keys = [{"Key": obj["Key"]} for obj in objects]
        s3.delete_objects(Bucket=bucket_name, Delete={"Objects": delete_keys})
        for obj in objects:
            print(f"  Deleted S3: {obj['Key']}")
            deleted_s3 += 1

if deleted_s3 == 0:
    print("  No objects found in S3 bucket.")
else:
    print(f"  Successfully deleted {deleted_s3} S3 object(s).")

# 2. Clear DynamoDB Items
print("\n--- Clearing DynamoDB Items ---")
scan_paginator = dynamo.get_paginator("scan")
deleted_dynamo = 0
for page in scan_paginator.paginate(TableName=table_name):
    items = page.get("Items", [])
    for item in items:
        pk = item["pk"]
        sk = item["sk"]
        dynamo.delete_item(TableName=table_name, Key={"pk": pk, "sk": sk})
        pk_val = pk.get("S", "")
        sk_val = sk.get("S", "")
        print(f"  Deleted DynamoDB item: pk={pk_val}, sk={sk_val}")
        deleted_dynamo += 1

if deleted_dynamo == 0:
    print("  No items found in DynamoDB table.")
else:
    print(f"  Successfully deleted {deleted_dynamo} DynamoDB item(s).")

# 3. Clear Local .chunkwise directory if present
local_home = Path(".chunkwise")
if local_home.exists():
    import shutil
    shutil.rmtree(local_home, ignore_errors=True)
    local_home.mkdir(parents=True, exist_ok=True)
    print("\n--- Local Storage Cleared ---")
    print("  Reset .chunkwise/ local database and cache.")

print("\nStorage purge complete! S3 and DynamoDB are now fresh and ready for new uploads.")

