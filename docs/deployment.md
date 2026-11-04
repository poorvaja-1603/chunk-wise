# ChunkWise Deployment

ChunkWise supports a local filesystem/SQLite configuration and an AWS
configuration using S3 for chunk bytes and an on-demand DynamoDB table for
metadata. The AWS backend uses boto3's default credential chain. In AWS Academy
Learner Lab, start the lab and use its provided role credentials; do not copy
access keys into source files or commit them.

The HTTP API has no built-in authentication or multi-user isolation. Keep it
private, use an SSH tunnel, or restrict access through the security group or an
authenticated proxy. Do not expose an unauthenticated storage dashboard to the
public internet.

## Local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
$env:CHUNKWISE_HOME = "$PWD\.chunkwise"
$env:CHUNKWISE_HOST = "127.0.0.1"
$env:PORT = "8080"
chunkwise-web
```

Open `http://127.0.0.1:8080`. Local mode uses the standard library and SQLite.
The local data directory must be persistent if it contains files you want to
keep.

## AWS backend

Install the package and set your AWS configuration variables:

```powershell
pip install -e .
$env:CHUNKWISE_BACKEND = "aws"
$env:CHUNKWISE_S3_BUCKET = "your-existing-bucket"
$env:CHUNKWISE_DYNAMODB_TABLE = "chunkwise-index"
$env:CHUNKWISE_AWS_REGION = "us-east-1"
$env:CHUNKWISE_CLOUDWATCH = "1"
$env:CHUNKWISE_HOME = "$PWD\.chunkwise"
$env:PORT = "8080"
chunkwise-web
```

For local development, keep non-secret settings together in PowerShell.
Never put AWS credentials into version-controlled files.

`CHUNKWISE_AWS_REGION` defaults to the standard boto3 provider chain when omitted.
The configured S3 bucket must already exist. The DynamoDB table is created
on demand with `PAY_PER_REQUEST` billing and `pk` / `sk` string keys, plus a
`status-index` global secondary index for committed file queries.

### Required IAM Actions

The active AWS credentials or Learner Lab LabRole must permit:

- S3:
  - `s3:GetObject`
  - `s3:PutObject`
  - `s3:DeleteObject`
  - `s3:ListBucket`
- DynamoDB:
  - `dynamodb:CreateTable`
  - `dynamodb:DescribeTable`
  - `dynamodb:GetItem`
  - `dynamodb:BatchGetItem`
  - `dynamodb:PutItem`
  - `dynamodb:UpdateItem`
  - `dynamodb:DeleteItem`
  - `dynamodb:BatchWriteItem`
  - `dynamodb:Query` (on the primary table and on the `status-index` GSI)
- CloudWatch (when `CHUNKWISE_CLOUDWATCH=1`):
  - `cloudwatch:PutMetricData`
  - `cloudwatch:PutDashboard`
  - `cloudwatch:PutMetricAlarm`

Use the lab's existing role and policies. If LabRole denies dashboard creation,
metric emission via `PutMetricData` remains functional.

## CloudWatch setup

To create the CloudWatch dashboard and alarm for integrity monitoring:

```powershell
python scripts\cloudwatch_setup.py
```

## CLI commands

```powershell
# Upload with content-defined chunking
chunkwise put path\to\file

# Upload with fixed-size chunking
chunkwise put path\to\file --chunker fixed

# Print storage accounting summary
chunkwise stats

# Restore a file to output path
chunkwise get FILE_ID -o restored.bin

# Verify chunk and whole-file integrity
chunkwise verify FILE_ID

# Delete a file and mark zero-ref chunks as tombstoned
chunkwise delete FILE_ID

# Garbage collect expired tombstoned chunks
chunkwise gc --grace-seconds 86400

# FSCK drift verification and repair
chunkwise fsck --repair
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CHUNKWISE_BACKEND` | `local` | `local`, `aws`, or `virtual` storage and index backend. |
| `CHUNKWISE_HOME` | `./.chunkwise` | Local data directory and AWS-mode temporary working directory. |
| `CHUNKWISE_S3_BUCKET` | unset | S3 bucket name when backend is `aws`. |
| `CHUNKWISE_DYNAMODB_TABLE` | unset | DynamoDB index table name when backend is `aws`. |
| `CHUNKWISE_AWS_REGION` | boto3 default | Region for AWS clients (e.g. `us-east-1`). |
| `CHUNKWISE_CLOUDWATCH` | `0` | Enable CloudWatch telemetry (`1` to enable). |
| `CHUNKWISE_UPLOAD_CONCURRENCY` | `8` | Maximum concurrent thread pool workers for chunk uploads. |
| `CHUNKWISE_HOST` | `0.0.0.0` | HTTP bind address for `chunkwise-web`. |
| `PORT` | `8080` | HTTP listen port for `chunkwise-web`. |
| `CHUNKWISE_MAX_UPLOAD_BYTES` | `104857600` | Maximum upload request size (100 MiB). |

## Live load simulation

Run the capped real/virtual workload separately from the storage benchmark:

```powershell
chunkwise-simulate `
  --real-files .\sample-a.bin .\sample-b.bin `
  --real-worker-count 2 `
  --metrics .\simulation\metrics.jsonl `
  --stage-seconds 10
```
