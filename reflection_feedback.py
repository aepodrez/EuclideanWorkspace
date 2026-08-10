#!/usr/bin/env python3
"""Validate mapper reflections against SEC facts and publish approved context."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pyarrow.parquet as pq


WORKSPACE = Path(__file__).resolve().parent
MAPPER_DIR = WORKSPACE / "Euclidean/DataIngressModel/lambdas/edgar_ai_worker"
sys.path.insert(0, str(MAPPER_DIR))
import xbrl_ai_mapper as mapper  # noqa: E402


BUCKET = os.environ.get("S3_BUCKET", "euclidean-pipeline-954976294836")
REFLECTION_PREFIX = "mapping-insights/"
CONTEXT_PREFIX = "mapper-context/approved"
STATE_PATH = WORKSPACE / "local-runs/reflection_feedback_state.json"
REPORT_PATH = WORKSPACE / "local-runs/reflection_feedback_report.json"
CONTEXT_PATH = WORKSPACE / "local-runs/approved_mapper_context.json"
FEEDBACK_SCHEMA_VERSION = 3

# Exact-accession SEC facts compared with official Compustat on CIK + fiscal date.
# Only pairs with strong value agreement after Compustat's million-unit scaling may
# influence parser instructions. Other observations remain available for audit but
# are not promoted merely because the tag exists and sounds semantically plausible.
COMPUSTAT_VALIDATED_FIELD_TAGS: set[tuple[str, str]] = {
    ("ap", "AccountsPayableCurrent"),
    ("at", "Assets"),
    ("ceq", "StockholdersEquity"),
    ("dltis", "ProceedsFromIssuanceOfLongTermDebt"),
    ("drc", "ContractWithCustomerLiabilityCurrent"),
    ("drc", "DeferredRevenueCurrent"),
    ("gdwl", "Goodwill"),
    ("lt", "Liabilities"),
    ("seq", "StockholdersEquity"),
    ("txdi", "DeferredIncomeTaxExpenseBenefit"),
    ("txt", "IncomeTaxExpenseBenefit"),
}

# Every checked-in Compustat field is eligible for evidence-backed promotion.
# Most canonical tags are named directly in COMPUSTAT_FIELDS. These aliases fill
# the few terse legacy definitions that describe a concept without spelling its
# XBRL name. This remains a semantic allowlist, not unrestricted model advice.
FIELD_TAG_ALIASES: dict[str, set[str]] = {
    "at": {"Assets"},
    "act": {"AssetsCurrent"},
    "rect": {
        "AccountsReceivableNetCurrent",
        "LoansAndLeasesReceivableNetReportedAmount",
    },
    "invt": {"InventoryNet"},
    "ivst": {"ShortTermInvestments", "MarketableSecuritiesCurrent"},
    "xpp": {"PrepaidExpenseCurrent", "PrepaidExpensesCurrent"},
    "aco": {"OtherAssetsCurrent"},
    "intan": {"FiniteLivedIntangibleAssetsNet", "IntangibleAssetsNetExcludingGoodwill"},
    "gdwl": {"Goodwill"},
    "ivao": {"LongTermInvestments", "OtherInvestmentsNoncurrent"},
    "lt": {"Liabilities"},
    "lct": {"LiabilitiesCurrent"},
    "lt_noncurrent": {"LiabilitiesNoncurrent"},
    "lo": {"OtherLiabilitiesNoncurrent"},
    "dlc": {"ShortTermBorrowings", "LongTermDebtCurrent"},
    "ap": {"AccountsPayableCurrent", "AccountsPayableTradeCurrent"},
    "xacc": {"AccruedLiabilitiesCurrent", "OtherAccruedLiabilitiesCurrent"},
    "re": {"RetainedEarningsAccumulatedDeficit"},
    "revt_interest": {"InterestAndDividendIncomeOperating"},
    "revt_noninterest": {"NoninterestIncome"},
    "xsga": {"SellingGeneralAndAdministrativeExpense"},
    "xrd": {"ResearchAndDevelopmentExpense"},
    "xad": {"AdvertisingExpense"},
    "nopi": {"NonoperatingIncomeExpense", "OtherNonoperatingIncomeExpense"},
    "oibdp": {"OperatingIncomeBeforeDepreciationAndAmortization"},
    "sale": {"Revenue", "Revenues"},
    "txt": {"IncomeTaxExpenseBenefit"},
    "scstkc": {"ProceedsFromIssuanceOfCommonStock", "ProceedsFromStockOptionsExercised"},
    "dltis": {"ProceedsFromIssuanceOfLongTermDebt"},
    "dltr": {"RepaymentsOfLongTermDebt"},
    "ni": {"NetIncomeLoss", "ProfitLoss"},
    "deposits": {"Deposits"},
    "fatl": {"Land"},
}

REJECTED_ADVICE = {
    "cash_tax_flow_as_tax_payable": r"IncomeTaxesPaidNet.{0,100}(?:txp|Taxes Payable)",
    "operating_lease_as_finance_lease": r"OperatingLeaseLiabilityNoncurrent.{0,100}dltt_finlease",
    "total_equity_as_preferred_stock": r"StockholdersEquity.{0,100}pstk",
    "aoci_as_deferred_tax_balance": r"AccumulatedOtherComprehensiveIncomeLossNetOfTax.{0,100}txditc",
}

FIELD_RE = re.compile(
    r"(?<![a-z0-9_])(?:[`\"'])?("
    + "|".join(sorted(map(re.escape, mapper.COMPUSTAT_FIELDS), key=len, reverse=True))
    + r")(?:[`\"'])?(?![a-z0-9_])"
)
TAG_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{5,}\b")
NEGATIVE_SEMANTIC_MARKER_RE = re.compile(
    r"\b(?:forbidden|never|do not|don't|not|excludes?|excluding|different from)\b",
    flags=re.IGNORECASE,
)


def _definition_supports_tag(field: str, tag: str) -> bool:
    """Return whether the authoritative field definition endorses this exact tag."""
    if tag in FIELD_TAG_ALIASES.get(field, set()):
        return True
    if tag.isupper() or sum(character.isupper() for character in tag) < 2:
        return False
    definition = mapper.COMPUSTAT_FIELDS.get(field, "")
    for match in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(tag)}(?![A-Za-z0-9])", definition):
        # A definition may name both preferred and explicitly forbidden tags.
        # Inspect only the sentence/clause leading into this occurrence.
        clause_start = max(
            definition.rfind(".", 0, match.start()),
            definition.rfind("\n", 0, match.start()),
        )
        prefix = definition[clause_start + 1:match.start()]
        if not NEGATIVE_SEMANTIC_MARKER_RE.search(prefix):
            return True
    return False


def _is_promotable(field: str, tag: str) -> bool:
    """Require both semantic compatibility and official Compustat agreement."""
    return (
        (field, tag) in COMPUSTAT_VALIDATED_FIELD_TAGS
        and _definition_supports_tag(field, tag)
    )


def _survives_mapper_guards(field: str, tag: str, facts: dict[str, float]) -> bool:
    """Exercise the production sanitizer before retaining reflection evidence."""
    mapping = {field: tag}
    try:
        values = mapper.extract_values(facts, mapping)
    except (KeyError, TypeError, ValueError):
        return False
    return mapping.get(field) == tag and field in values


def _load_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _reflection_metadata(text: str) -> dict:
    values = {}
    for key in ("cik", "accession", "timestamp"):
        match = re.search(rf"(?m)^-\s+{key}:\s*(.+?)\s*$", text)
        if match:
            values[key] = match.group(1).strip()
    heading = re.search(
        r"(?m)^# Mapping reflection:\s+(\S+)\s+(\S+)\s+(\d{4}-\d{2}-\d{2})\s*$",
        text,
    )
    if heading:
        values.update(
            {"ticker": heading.group(1), "form_type": heading.group(2), "report_date": heading.group(3)}
        )
    return values


def _candidate_pairs(text: str) -> set[tuple[str, str]]:
    match = re.search(
        r"(?ms)^## Accuracy Issues\s*(.*?)(?=^## |\Z)",
        text,
    )
    if not match:
        return set()
    candidates = set()
    for bullet in re.split(r"(?m)^\s*-\s+", match.group(1)):
        field_match = FIELD_RE.search(bullet)
        if not field_match:
            continue
        field = field_match.group(1)
        for tag in TAG_RE.findall(bullet):
            candidates.add((field, tag))
    return candidates


def _filing_key(meta: dict) -> str:
    folder = "annual" if meta.get("form_type") in {"10-K", "20-F"} else "quarterly"
    return (
        f"data-ingress/filings/{folder}/{meta['cik']}/{meta['report_date']}.parquet"
    )


def _stored_mapping(s3, meta: dict) -> dict:
    key = _filing_key(meta)
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    accession = obj.get("Metadata", {}).get("accession")
    if accession and accession != meta["accession"]:
        raise ValueError("stored filing accession differs from reflection")
    table = pq.read_table(io.BytesIO(obj["Body"].read()), columns=["_ai_mapping"])
    raw = table.column("_ai_mapping")[0].as_py()
    value = json.loads(raw or "{}")
    return value if isinstance(value, dict) else {}


def _validate_reflection(s3, key: str, text: str) -> dict:
    meta = _reflection_metadata(text)
    required = {"cik", "accession", "form_type", "report_date"}
    if not required <= meta.keys():
        return {"key": key, "status": "rejected", "reason": "missing_metadata"}
    candidates = _candidate_pairs(text)
    if not candidates:
        return {"key": key, "status": "no_candidate"}
    try:
        mapping = _stored_mapping(s3, meta)
        facts = mapper.fetch_xbrl_facts(
            meta["cik"], meta["accession"], meta["form_type"],
        )
    except Exception as exc:
        return {
            "key": key,
            "status": "deferred",
            "reason": f"{type(exc).__name__}: {exc}"[:300],
        }

    accepted = []
    rejected = []
    for field, tag in sorted(candidates):
        existing = mapping.get(field)
        existing_tags = set(existing if isinstance(existing, list) else [existing])
        if not _definition_supports_tag(field, tag):
            rejected.append({"field": field, "tag": tag, "reason": "unsupported_by_field_definition"})
        elif not _is_promotable(field, tag):
            rejected.append({"field": field, "tag": tag, "reason": "not_validated_against_compustat"})
        elif tag in existing_tags:
            rejected.append({"field": field, "tag": tag, "reason": "already_mapped"})
        elif tag not in facts:
            rejected.append({"field": field, "tag": tag, "reason": "absent_from_sec_facts"})
        elif not _survives_mapper_guards(field, tag, facts):
            rejected.append({"field": field, "tag": tag, "reason": "rejected_by_mapper_guards"})
        else:
            accepted.append({
                "field": field,
                "tag": tag,
                "cik": meta["cik"],
                "accession": meta["accession"],
                "form_type": meta["form_type"],
                "report_date": meta["report_date"],
                "reflection_key": key,
            })
    return {
        "key": key,
        "status": "validated" if accepted else "rejected",
        "accepted": accepted,
        "rejected": rejected,
    }


def _build_context(observations: dict, min_filings: int, min_ciks: int) -> dict:
    rules = []
    for pair, item in sorted(observations.items()):
        evidence = list(item.get("evidence", {}).values())
        ciks = {row["cik"] for row in evidence}
        if len(evidence) < min_filings or len(ciks) < min_ciks:
            continue
        field, tag = pair.split("|", 1)
        rules.append({
            "field": field,
            "tag": tag,
            "evidence_count": len(evidence),
            "distinct_ciks": len(ciks),
            "accession_examples": sorted(row["accession"] for row in evidence)[-5:],
            "validation": "exact_accession_sec_fact+stored_mapping_omission+semantic_allowlist",
        })
    digest = hashlib.sha256(
        json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return {
        "schema_version": 1,
        "version": digest,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rules": rules,
        "promotion_thresholds": {
            "minimum_filings": min_filings,
            "minimum_distinct_ciks": min_ciks,
        },
    }


def _publish_context(s3, context: dict, previous_version: str | None) -> bool:
    CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(context, indent=2, sort_keys=True) + "\n").encode()
    CONTEXT_PATH.write_bytes(body)
    if context["version"] == previous_version:
        return False
    version_key = f"{CONTEXT_PREFIX}/versions/{context['version']}.json"
    try:
        s3.put_object(
            Bucket=BUCKET,
            Key=version_key,
            Body=body,
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except Exception as exc:
        if "PreconditionFailed" not in str(exc):
            raise
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{CONTEXT_PREFIX}/current.json",
        Body=body,
        ContentType="application/json",
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--max-reflections", type=int, default=500)
    parser.add_argument("--max-validations", type=int, default=100)
    parser.add_argument("--min-filings", type=int, default=3)
    parser.add_argument("--min-ciks", type=int, default=2)
    args = parser.parse_args()

    state = _load_json(STATE_PATH, {})
    observations = {
        pair: item
        for pair, item in state.get("observations", {}).items()
        if "|" in pair and _is_promotable(*pair.split("|", 1))
    }
    s3 = boto3.client("s3", region_name="us-east-1")
    all_objects = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=REFLECTION_PREFIX
    ):
        all_objects.extend(page.get("Contents", []))
    all_objects.sort(key=lambda item: item["LastModified"])
    objects_by_key = {item["Key"]: item for item in all_objects}
    watermark = None if args.rebuild else state.get("last_modified")
    if watermark:
        cutoff = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
        new_objects = [item for item in all_objects if item["LastModified"] > cutoff]
    else:
        new_objects = all_objects[-args.max_reflections:]

    schema_upgrade = state.get("feedback_schema_version") != FEEDBACK_SCHEMA_VERSION
    if args.rebuild or schema_upgrade:
        # Candidate eligibility changed, so previously ignored reflections must
        # be replayed. Bounded processing below keeps each watchdog pass cheap.
        pending_keys = [item["Key"] for item in all_objects]
    else:
        pending_keys = state.get("pending_reflections", [])
    available_keys = list(dict.fromkeys(
        [key for key in pending_keys if key in objects_by_key]
        + [item["Key"] for item in new_objects]
    ))
    selected_keys = available_keys[:args.max_reflections]
    unprocessed_keys = available_keys[args.max_reflections:]
    objects = [objects_by_key[key] for key in selected_keys]

    def read(item: dict) -> tuple[dict, str]:
        body = s3.get_object(Bucket=BUCKET, Key=item["Key"])["Body"].read()
        return item, body.decode("utf-8", errors="replace")

    texts = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        texts.extend(pool.map(read, objects))

    invalid_advice = Counter()
    malformed = 0
    candidate_texts = []
    for item, text in texts:
        if not all(
            f"## {name}" in text
            for name in ("Assumptions", "Unmapped Gaps", "Accuracy Issues", "Prompt Improvements")
        ):
            malformed += 1
        for code, pattern in REJECTED_ADVICE.items():
            if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
                invalid_advice[code] += 1
        if _candidate_pairs(text):
            candidate_texts.append((item["Key"], text))

    validation_batch = candidate_texts[:args.max_validations]
    pending_next = unprocessed_keys + [
        key for key, _ in candidate_texts[args.max_validations:]
    ]
    validation_results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_validate_reflection, s3, key, text)
            for key, text in validation_batch
        ]
        for future in as_completed(futures):
            validation_results.append(future.result())

    rejection_reasons = Counter()
    accepted_count = 0
    for result in validation_results:
        if result["status"] == "deferred":
            rejection_reasons["deferred_sec_validation"] += 1
            pending_next.append(result["key"])
        for rejected in result.get("rejected", []):
            rejection_reasons[rejected["reason"]] += 1
        for evidence in result.get("accepted", []):
            accepted_count += 1
            pair = f"{evidence['field']}|{evidence['tag']}"
            item = observations.setdefault(pair, {"evidence": {}})
            item["evidence"][evidence["accession"]] = evidence

    previous_context = _load_json(CONTEXT_PATH, {})
    context = _build_context(observations, args.min_filings, args.min_ciks)
    changed = _publish_context(s3, context, previous_context.get("version"))
    now = datetime.now(timezone.utc).isoformat()
    report = {
        "generated_at": now,
        "feedback_schema_version": FEEDBACK_SCHEMA_VERSION,
        "eligible_fields": len(mapper.COMPUSTAT_FIELDS),
        "new_reflections": len(objects),
        "candidate_reflections": len(candidate_texts),
        "validation_attempts": len(validation_results),
        "validated_candidates": accepted_count,
        "pending_reflections": len(set(pending_next)),
        "invalid_advice": dict(sorted(invalid_advice.items())),
        "validation_rejections": dict(sorted(rejection_reasons.items())),
        "malformed_reflections": malformed,
        "approved_rules": len(context["rules"]),
        "context_version": context["version"],
        "context_changed": changed,
        "promotion_policy": (
            "official Compustat value agreement + authoritative definition + mapper hard "
            "guards + exact accession SEC fact + stored mapping omission + "
            "recurrence across filings and issuers"
        ),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    latest = (
        new_objects[-1]["LastModified"].isoformat()
        if new_objects
        else state.get("last_modified")
    )
    STATE_PATH.write_text(
        json.dumps(
            {
                "last_modified": latest,
                "checked_at": now,
                "feedback_schema_version": FEEDBACK_SCHEMA_VERSION,
                "observations": observations,
                "pending_reflections": sorted(set(pending_next)),
                "context_version": context["version"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(
        f"reflection_feedback new={len(objects)} candidates={len(candidate_texts)} "
        f"attempted={len(validation_results)} validated={accepted_count} "
        f"pending={len(set(pending_next))} approved={len(context['rules'])} "
        f"context={context['version']} changed={str(changed).lower()} "
        f"invalid_advice={sum(invalid_advice.values())}"
    )


if __name__ == "__main__":
    main()
