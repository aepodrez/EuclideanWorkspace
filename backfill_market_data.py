#!/usr/bin/env python3
"""
backfill_market_data.py

Runs market-data download jobs locally using the same lambda_entry.py dispatcher
that the overnight Lambdas use. Writes results directly to S3.

Usage:
    python3 backfill_market_data.py                         # all jobs (incremental)
    python3 backfill_market_data.py crsp_daily              # specific job (incremental)
    python3 backfill_market_data.py --force crsp_monthly    # force full re-download
"""
import os
import sys
import time
import shutil

sys.path.insert(0, "/workspaces/EuclideanWorkspace/Euclidean/EuclideanInfra/lambdas/market_data")

DIM       = "/workspaces/EuclideanWorkspace/Euclidean/DataIngressModel"
S3_BUCKET = "euclidean-pipeline-954976294836"

os.environ["S3_BUCKET"]     = S3_BUCKET
os.environ["PYDATA_PREFIX"] = "pyData/Intermediate"
os.environ["UNIVERSE_KEY"]  = "universe/universe.csv"
os.environ["MD_CODE_DIR"]   = f"{DIM}/DataDownloads"
os.environ["MD_UTILS_DIR"]  = f"{DIM}/utils"
# os.environ["FRED_API_KEY"] = "your_key_here"

# Parse --force flag
args = sys.argv[1:]
force = "--force" in args
if force:
    args = [a for a in args if a != "--force"]

# Pre-download universe before any script imports it (scripts read UNIVERSE_CSV at import time)
import boto3 as _boto3
os.makedirs("/tmp/pyData/Intermediate", exist_ok=True)
print("Downloading universe.csv from S3...")
_boto3.client("s3").download_file(S3_BUCKET, os.environ["UNIVERSE_KEY"], "/tmp/universe.csv")
os.environ["UNIVERSE_CSV"] = "/tmp/universe.csv"
print(f"Universe ready: {sum(1 for _ in open('/tmp/universe.csv'))-1} companies")

import lambda_entry

ALL_JOBS = list(lambda_entry.JOB_SPECS.keys())
jobs = args if args else ALL_JOBS

if force:
    print(f"⚠️  --force: skipping existing S3 files for full re-download")

print(f"Running {len(jobs)} job(s): {jobs}\n")

for job in jobs:
    if job not in lambda_entry.JOB_SPECS:
        print(f"Unknown job '{job}' — skipping. Valid: {ALL_JOBS}")
        continue
    print(f"\n{'='*60}")
    print(f"JOB: {job}")
    print(f"{'='*60}")

    # --force: remove cached tmp outputs so the script sees no prior data
    if force:
        for fname in lambda_entry.JOB_SPECS[job]["outputs"]:
            p = f"/tmp/pyData/Intermediate/{fname}"
            if os.path.exists(p):
                os.remove(p)
                print(f"  cleared {p}")

    t0 = time.time()
    try:
        result = lambda_entry.lambda_handler({"job": job, "force": force}, {})
        print(f"\n✓ {job} done in {time.time()-t0:.0f}s — {result}")
    except Exception as exc:
        print(f"\n✗ {job} FAILED after {time.time()-t0:.0f}s: {exc}")
