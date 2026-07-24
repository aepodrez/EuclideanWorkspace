#!/usr/bin/env python3
"""Regenerate XBRL mapping-reflection documents from existing parquets.

The original reflection corpus was generated with a bug: map_filing() never
returned the XBRL facts, so every reflection was produced against an empty fact
set ("Total XBRL tags found: 0") and hallucinated its "missing" fields.

This one-off pass reuses the work already in S3: for each filing parquet it
re-fetches the raw XBRL facts (cheap, cached companyfacts JSON), reloads the
tag->Compustat assignments the LLM already produced (`_ai_mapping`), and re-runs
the fixed reflection prompt. It does NOT re-run the expensive mapper LLM call.

Usage:
    python3 regen_reflections.py                 # all filings
    python3 regen_reflections.py --limit 1       # smoke test (one filing)
    python3 regen_reflections.py --accession 0000012208-22-000026
"""
import argparse
import io as _io
import json
from datetime import datetime, timezone

import pyarrow.parquet as pq

import backfill_ollama as bf  # imports mapper, sets up S3 + LLM monkeypatch

s3 = bf.s3
S3_BUCKET = bf.S3_BUCKET
mapper = bf.mapper
log = bf.log

FILINGS_PREFIX = "data-ingress/filings/"


def _list_parquets() -> list[str]:
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=FILINGS_PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                keys.append(obj["Key"])
    return keys


def _read_row(key: str) -> dict:
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    table = pq.read_table(_io.BytesIO(obj["Body"].read()))
    return {k: (v[0] if v else None) for k, v in table.to_pydict().items()}


def _regen_one(key: str) -> str:
    row = _read_row(key)
    meta = s3.head_object(Bucket=S3_BUCKET, Key=key).get("Metadata", {})
    accession = meta.get("accession") or row.get("_accession") or ""
    cik = str(row.get("cik") or "").strip()
    ticker = row.get("ticker") or ""
    form_type = row.get("form_type") or ""
    report_date = row.get("datadate") or "unknown"

    if not (cik and accession):
        return f"SKIP {key}: missing cik/accession"

    try:
        ai_mapping = json.loads(row.get("_ai_mapping") or "{}")
    except Exception:
        ai_mapping = {}

    facts = mapper.fetch_xbrl_facts(cik, accession, form_type)
    reflection = bf._get_llm_reflection(ticker, form_type, report_date, ai_mapping, facts)
    if not reflection:
        return f"SKIP {key}: empty reflection"

    try:
        residuals = json.loads(row.get("_anchor_residuals") or "{}")
    except Exception:
        residuals = {}

    header = (
        f"# Mapping reflection: {ticker} {form_type} {report_date}\n\n"
        f"- cik: {cik}\n"
        f"- accession: {accession}\n"
        f"- timestamp: {datetime.now(timezone.utc).isoformat()}\n"
        f"- xbrl_facts: {len(facts)}\n"
        f"- residuals: {json.dumps(residuals) if residuals else 'n/a'}\n\n"
        f"---\n\n"
    )
    insight_key = f"mapping-insights/{cik}/{accession}.md"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=insight_key,
        Body=(header + reflection).encode(),
        ContentType="text/markdown",
    )
    return f"OK   {ticker} {form_type} {report_date} facts={len(facts)} -> {insight_key}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max filings to process (0 = all)")
    ap.add_argument("--accession", default="", help="only this accession")
    ap.add_argument("--key", default="", help="process exactly this parquet S3 key")
    args = ap.parse_args()

    if args.key:
        print(_regen_one(args.key), flush=True)
        return

    keys = _list_parquets()
    log.info("Found %d filing parquets", len(keys))
    done = 0
    for key in keys:
        if args.accession and args.accession.replace("-", "") not in key and args.accession not in key:
            # accession isn't in the key (key is by report_date); filter via head metadata
            meta = s3.head_object(Bucket=S3_BUCKET, Key=key).get("Metadata", {})
            if meta.get("accession") != args.accession:
                continue
        try:
            print(_regen_one(key), flush=True)
        except Exception as exc:
            print(f"ERR  {key}: {exc}", flush=True)
        done += 1
        if args.limit and done >= args.limit:
            break
    log.info("Regenerated reflections for %d filings", done)


if __name__ == "__main__":
    main()
