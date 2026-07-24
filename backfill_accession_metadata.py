#!/usr/bin/env python3
"""
backfill_accession_metadata.py

For every parquet in data-ingress/filings/ that lacks an 'accession' metadata tag,
look up the accession number from the EDGAR submissions API and write it back
via s3.copy_object (the only way to update S3 metadata in place).

Safe to re-run — skips objects that already have accession metadata.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

S3_BUCKET      = "euclidean-pipeline-954976294836"
EDGAR_IDENTITY = "EuclideanResearch podreze03@gmail.com"

s3 = boto3.client("s3")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": EDGAR_IDENTITY})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            if attempt == 3:
                raise
            time.sleep(2 * attempt)


def _list_parquets() -> list[dict]:
    """List all parquets under data-ingress/filings/ with their current metadata."""
    results = []
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in ("data-ingress/filings/annual/", "data-ingress/filings/quarterly/"):
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".parquet"):
                    results.append({"key": key})
    log.info("Found %d parquets total", len(results))
    return results


def _needs_update(key: str) -> tuple[bool, dict]:
    """Return (needs_update, existing_metadata)."""
    try:
        head = s3.head_object(Bucket=S3_BUCKET, Key=key)
        meta = head.get("Metadata", {})
        return "accession" not in meta or not meta["accession"], meta
    except ClientError:
        return False, {}


def _find_accession(cik: str, report_date: str, folder: str) -> str | None:
    """Query EDGAR submissions API to find the accession for this CIK + report_date."""
    cik_padded = str(int(cik)).zfill(10)
    try:
        data = _get_json(f"https://data.sec.gov/submissions/CIK{cik_padded}.json")
    except Exception as e:
        log.warning("EDGAR fetch failed for CIK %s: %s", cik, e)
        return None

    recent      = data.get("filings", {}).get("recent", {})
    accessions  = recent.get("accessionNumber", [])
    forms       = recent.get("form", [])
    report_dates = recent.get("reportDate", [])

    # Accepted forms per folder
    annual_forms    = {"10-K", "10-K/A", "20-F", "20-F/A"}
    quarterly_forms = {"10-Q", "10-Q/A"}
    accepted = annual_forms if folder == "annual" else quarterly_forms

    # Find all matches for this report_date, prefer most recent (amendments)
    matches = []
    for acc, form, rd in zip(accessions, forms, report_dates):
        if form in accepted and rd == report_date:
            matches.append(acc)

    # EDGAR returns newest-first so matches[0] is the latest (amendment if any)
    return matches[0] if matches else None


def _update_metadata(key: str, existing_meta: dict, accession: str) -> None:
    """Copy object to itself with updated metadata (only way to update S3 metadata)."""
    new_meta = {**existing_meta, "accession": accession}
    s3.copy_object(
        Bucket=S3_BUCKET,
        CopySource={"Bucket": S3_BUCKET, "Key": key},
        Key=key,
        Metadata=new_meta,
        MetadataDirective="REPLACE",
        ContentType="application/octet-stream",
    )


def _process(obj: dict) -> dict:
    key    = obj["key"]
    parts  = key.split("/")
    # key format: data-ingress/filings/{folder}/{cik}/{date}.parquet
    folder      = parts[2]
    cik         = parts[3]
    report_date = parts[4].replace(".parquet", "")

    needs_update, existing_meta = _needs_update(key)
    if not needs_update:
        return {"key": key, "status": "skip"}

    accession = _find_accession(cik, report_date, folder)
    if not accession:
        return {"key": key, "status": "no_match"}

    _update_metadata(key, existing_meta, accession)
    return {"key": key, "status": "ok", "accession": accession}


def main():
    parquets = _list_parquets()

    updated = skipped = no_match = errors = 0
    # Use modest concurrency — EDGAR rate limit is 10 req/s
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_process, obj): obj for obj in parquets}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                result = fut.result()
            except Exception as exc:
                log.error("Error on %s: %s", futs[fut]["key"], exc)
                errors += 1
                continue

            status = result["status"]
            if status == "ok":
                updated += 1
            elif status == "skip":
                skipped += 1
            elif status == "no_match":
                no_match += 1
                log.warning("No accession found for %s", result["key"])

            if i % 100 == 0:
                log.info("Progress: %d/%d — updated=%d skipped=%d no_match=%d errors=%d",
                         i, len(parquets), updated, skipped, no_match, errors)

    log.info("Done. updated=%d skipped=%d no_match=%d errors=%d", updated, skipped, no_match, errors)


if __name__ == "__main__":
    main()
