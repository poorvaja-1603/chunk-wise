"""Idempotently set up CloudWatch dashboard and alarms for ChunkWise."""
from __future__ import annotations

import json
import os
import sys

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    print("Error: boto3 is required to set up CloudWatch. Install with: pip install boto3", file=sys.stderr)
    sys.exit(1)

DASHBOARD_NAME = "ChunkWise"
ALARM_NAME = "ChunkWise-IntegrityErrors"
NAMESPACE = "ChunkWise"


def get_region() -> str:
    return os.environ.get("CHUNKWISE_AWS_REGION", "us-east-1")


def create_dashboard_body(region: str) -> str:
    widgets = [
        {
            "type": "metric",
            "x": 0,
            "y": 0,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Deduplication Ratio",
                "view": "timeSeries",
                "stacked": False,
                "region": region,
                "metrics": [
                    [NAMESPACE, "DedupRatio", "Mode", "real", "Backend", "aws", {"stat": "Average", "label": "Real AWS Dedup Ratio"}],
                    ["...", "Mode", "simulated", "Backend", "local", {"stat": "Average", "label": "Simulated Dedup Ratio"}],
                ],
                "period": 60,
                "yAxis": {"left": {"min": 1.0}},
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 0,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Throughput (Chunks Processed / Second)",
                "view": "timeSeries",
                "region": region,
                "metrics": [
                    [{"expression": "m1 / 60", "label": "Real Chunks/s", "id": "e1"}],
                    [NAMESPACE, "ChunksProcessed", "Mode", "real", "Backend", "aws", {"id": "m1", "visible": False, "stat": "Sum"}],
                    [{"expression": "m2 / 60", "label": "Simulated Chunks/s", "id": "e2"}],
                    [NAMESPACE, "ChunksProcessed", "Mode", "simulated", "Backend", "local", {"id": "m2", "visible": False, "stat": "Sum"}],
                ],
                "period": 60,
            },
        },
        {
            "type": "metric",
            "x": 0,
            "y": 6,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "New vs Duplicate Chunks",
                "view": "timeSeries",
                "stacked": True,
                "region": region,
                "metrics": [
                    [NAMESPACE, "NewChunks", "Mode", "real", "Backend", "aws", {"stat": "Sum", "label": "New Chunks (Stored)"}],
                    [NAMESPACE, "DuplicateChunks", "Mode", "real", "Backend", "aws", {"stat": "Sum", "label": "Duplicate Chunks (Deduped)"}],
                ],
                "period": 60,
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 6,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Latencies (Put, Index Lookup, S3 Put)",
                "view": "timeSeries",
                "region": region,
                "metrics": [
                    [NAMESPACE, "PutLatencyMs", "Mode", "real", "Backend", "aws", {"stat": "p50", "label": "Put Latency p50"}],
                    ["...", {"stat": "p95", "label": "Put Latency p95"}],
                    [NAMESPACE, "IndexLookupLatencyMs", "Mode", "real", "Backend", "aws", {"stat": "p50", "label": "DynamoDB Lookup p50"}],
                    [NAMESPACE, "S3PutLatencyMs", "Mode", "real", "Backend", "aws", {"stat": "p50", "label": "S3 Put p50"}],
                ],
                "period": 60,
                "yAxis": {"left": {"label": "Milliseconds"}},
            },
        },
        {
            "type": "metric",
            "x": 0,
            "y": 12,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Errors and Integrity Violations",
                "view": "timeSeries",
                "region": region,
                "metrics": [
                    [NAMESPACE, "Errors", "Mode", "real", "Backend", "aws", {"stat": "Sum", "label": "Errors"}],
                    [NAMESPACE, "IntegrityErrors", "Mode", "real", "Backend", "aws", {"stat": "Sum", "label": "Integrity Errors", "color": "#d62728"}],
                ],
                "period": 60,
            },
        },
        {
            "type": "metric",
            "x": 12,
            "y": 12,
            "width": 12,
            "height": 6,
            "properties": {
                "title": "Real vs Simulated Comparison (Upload Volume)",
                "view": "timeSeries",
                "region": region,
                "metrics": [
                    [NAMESPACE, "UploadBytes", "Mode", "real", "Backend", "aws", {"stat": "Sum", "label": "Real AWS Bytes"}],
                    [NAMESPACE, "UploadBytes", "Mode", "simulated", "Backend", "local", {"stat": "Sum", "label": "Simulated Bytes"}],
                ],
                "period": 60,
                "yAxis": {"left": {"label": "Bytes"}},
            },
        },
    ]
    return json.dumps({"widgets": widgets}, separators=(",", ":"))


def setup_cloudwatch() -> int:
    region = get_region()
    print(f"Configuring CloudWatch in region: {region}")
    print("Prerequisites: Ensure AWS credentials with LabRole or appropriate permissions are active in your environment.")

    try:
        cw = boto3.client("cloudwatch", region_name=region)

        # 1. Dashboard setup
        dashboard_body = create_dashboard_body(region)
        print(f"Creating or updating CloudWatch dashboard '{DASHBOARD_NAME}'...")
        cw.put_dashboard(DashboardName=DASHBOARD_NAME, DashboardBody=dashboard_body)
        print(f"Successfully created dashboard '{DASHBOARD_NAME}'.")

        # 2. Alarm setup
        print(f"Creating or updating CloudWatch alarm '{ALARM_NAME}'...")
        cw.put_metric_alarm(
            AlarmName=ALARM_NAME,
            AlarmDescription="Alert triggered when ChunkWise detects any data corruption or integrity verification failure.",
            MetricName="IntegrityErrors",
            Namespace=NAMESPACE,
            Statistic="Sum",
            Period=60,
            EvaluationPeriods=1,
            Threshold=0.0,
            ComparisonOperator="GreaterThanThreshold",
            TreatMissingData="notBreaching",
        )
        print(f"Successfully created alarm '{ALARM_NAME}'.")
        print("\nSetup complete! You can view your dashboard at:")
        print(f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#dashboards:name={DASHBOARD_NAME}")
        return 0

    except NoCredentialsError:
        print("\nError: AWS credentials not found.", file=sys.stderr)
        print("Please export AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_SESSION_TOKEN.", file=sys.stderr)
        return 1

    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        message = error.response.get("Error", {}).get("Message", "")
        print(f"\nAWS ClientError: [{code}] {message}", file=sys.stderr)

        if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
            print("\nPermission Denied: Your IAM role or Learner Lab LabRole does not permit this action.", file=sys.stderr)
            print("Note for AWS Academy Learner Lab:", file=sys.stderr)
            print("  - LabRole has fixed policy boundaries that may restrict cloudwatch:PutDashboard or cloudwatch:PutMetricAlarm.", file=sys.stderr)
            print("  - Metric emission (cloudwatch:PutMetricData) is generally permitted.", file=sys.stderr)
            print("  - Verify with your AWS Academy course administrator if dashboard creation is disabled in LabRole.", file=sys.stderr)
        return 2

    except Exception as general_error:
        print(f"\nUnexpected error during CloudWatch setup: {general_error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(setup_cloudwatch())

