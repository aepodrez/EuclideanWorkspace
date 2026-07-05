#!/usr/bin/env python3
"""
backfill_ollama.py

Backfills missing company parquets using local Ollama (qwen3:8b).

For each company in the universe, checks S3 for an existing 10-K and 10-Q parquet.
For each missing one, fetches the most recent filing from EDGAR and runs the mapper
with Ollama instead of OpenRouter.

Resumable: already-written parquets are skipped automatically (S3 check).
Progress is also logged to /tmp/backfill_progress.jsonl.

Usage:
    python3 backfill_ollama.py [--workers N] [--limit N] [--annual-only] [--quarterly-only]
"""
from __future__ import annotations

import argparse
import csv
import io
import itertools
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Path setup — reuse the Lambda's mapper directly
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "Euclidean/DataIngressModel/lambdas/edgar_ai_worker",
)
sys.path.insert(0, LAMBDA_DIR)
import xbrl_ai_mapper as mapper  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
S3_BUCKET      = "euclidean-pipeline-954976294836"
UNIVERSE_KEY   = "universe/universe.csv"
EDGAR_IDENTITY = "EuclideanResearch podreze03@gmail.com"
LOOKBACK_DAYS  = 1825  # 5 years

# LLM server config — override via env vars.
# LLM_ENDPOINTS: comma-separated list of base URLs (without /v1/chat/completions).
# If set, overrides LLM_BASE_URL and round-robins across all endpoints.
# Example: LLM_ENDPOINTS=http://host.docker.internal:8080,http://192.168.1.50:8080
LLM_MODEL    = os.environ.get("LLM_MODEL", "mlx-community/Qwen3-32B-4bit")

_raw_endpoints = os.environ.get("LLM_ENDPOINTS", "")
if _raw_endpoints:
    _LLM_ENDPOINTS = [e.rstrip("/") + "/v1/chat/completions" for e in _raw_endpoints.split(",") if e.strip()]
else:
    LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://host.docker.internal:8080/v1/chat/completions")
    _LLM_ENDPOINTS = [LLM_BASE_URL]

_endpoint_idx = itertools.cycle(range(len(_LLM_ENDPOINTS)))
_endpoint_lock = threading.Lock()

def _next_endpoint() -> str:
    with _endpoint_lock:
        return _LLM_ENDPOINTS[next(_endpoint_idx)]

PROGRESS_FILE  = "/tmp/backfill_progress.jsonl"
QUEUE_CACHE    = "/tmp/backfill_queue.json"
QUEUE_CACHE_S3 = "backfill/queue_cache.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

s3 = boto3.client("s3")

# Serialise all local MLX calls so at most one inference request is in-flight
# at a time. Workers can still run concurrently for EDGAR/S3 I/O; only the LLM
# step is gated. This prevents the KV-cache OOM that occurs when two large
# prompts (~32K tokens each) are sent to MLX simultaneously.
_mlx_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Local LLM caller — monkey-patches mapper._invoke_kimi
# ---------------------------------------------------------------------------
def _stream_llm_raw(prompt: str, max_tokens: int = 32000, retries: int = 3) -> str:
    """Stream a completion from the local LLM and return the raw text.

    Uses a per-line timeout (LLM_LINE_TIMEOUT) so a stalled stream aborts instead
    of hanging the whole socket. Returns content if present, else the reasoning
    channel. Raises on repeated failure.
    """
    with _mlx_lock:
     return _stream_llm_raw_inner(prompt, max_tokens, retries)


def _stream_llm_raw_inner(prompt: str, max_tokens: int = 32000, retries: int = 3) -> str:
    endpoint = _next_endpoint()
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt + "\n\n/no_think"}],
        "temperature": 0,
        "stream": True,   # stream tokens to avoid HTTP timeout on long reasoning chains
        "max_tokens": max_tokens,
    }
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Per-line timeout: if Qwen3 stops streaming for > N seconds between SSE lines, abort.
    # socket timeout=60 alone isn't sufficient because tiny keepalive bytes reset it
    # without delivering a complete newline-terminated SSE chunk.
    # M2 Pro / slower hosts need more headroom — override via LLM_LINE_TIMEOUT env var.
    _LINE_TIMEOUT = int(os.environ.get("LLM_LINE_TIMEOUT", "180"))

    chunks: list[str] = []
    reasoning_chunks: list[str] = []
    for attempt in range(1, retries + 1):
        try:
            chunks = []
            reasoning_chunks = []
            with urllib.request.urlopen(req, timeout=_LINE_TIMEOUT) as resp:
                line_q: queue.Queue = queue.Queue()
                stop_evt = threading.Event()

                def _reader(r=resp, q=line_q, ev=stop_evt):
                    try:
                        for raw in r:
                            if ev.is_set():
                                break
                            q.put(raw)
                        q.put(None)
                    except Exception as exc:
                        q.put(exc)

                t = threading.Thread(target=_reader, daemon=True)
                t.start()
                try:
                    while True:
                        try:
                            item = line_q.get(timeout=_LINE_TIMEOUT)
                        except queue.Empty:
                            stop_evt.set()
                            raise TimeoutError(f"No SSE line in {_LINE_TIMEOUT}s — stream stalled")
                        if item is None:
                            break
                        if isinstance(item, Exception):
                            raise item
                        line = item.decode("utf-8").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        if delta.get("content"):
                            chunks.append(delta["content"])
                        if delta.get("reasoning"):
                            reasoning_chunks.append(delta["reasoning"])
                finally:
                    stop_evt.set()
            break
        except Exception as exc:
            if attempt == retries:
                raise
            wait = 5 * attempt
            log.warning("LLM attempt %d failed: %s — retrying in %ds", attempt, exc, wait)
            time.sleep(wait)

    content   = "".join(chunks)
    reasoning = "".join(reasoning_chunks)

    # Log the thinking chain for debugging
    think_match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
    thinking_text = think_match.group(1).strip() if think_match else reasoning
    if thinking_text:
        log.info("Reasoning (%d chars): %s…", len(thinking_text), thinking_text[:500])

    return (content or reasoning).strip()


def _call_local_llm(prompt: str) -> tuple[str, dict]:
    text = _stream_llm_raw(prompt, max_tokens=32000)
    return LLM_MODEL, mapper._parse_llm_json(text)


def _extract_json_object(text: str) -> dict:
    """Best-effort extraction of a single JSON object from an LLM response.

    Handles markdown fences, leading/trailing prose, and trailing commas that
    trip strict json.loads. Raises ValueError if nothing parseable is found.
    """
    if not text:
        raise ValueError("empty response")
    # Strip a <think>…</think> block if the model emitted one inline.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Strip markdown code fences.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Grab the outermost {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object found in: {text[:200]}")
    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Drop trailing commas (",}" / ",]") and retry once.
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        return json.loads(repaired)


def _get_llm_reflection(ticker: str, form_type: str, report_date: str, mapping: dict, xbrl_facts: dict) -> str:
    """Ask the LLM to reflect on mapping accuracy, assumptions, and gaps.

    Returns a free-form markdown document (string). We deliberately do NOT ask the
    model for JSON: the reflections constantly quote XBRL tag names and Compustat
    codes (e.g. "che", "ib"), and those inner double-quotes break strict JSON
    parsing ("Expecting ',' delimiter"). Plain markdown can never fail to parse and
    is exactly the document form we want to read back later.
    """
    # `mapping` is the tag→Compustat assignment dict the LLM produced.
    mapped_fields = {k: v for k, v in mapping.items() if v}
    unmapped_fields = [k for k, v in mapping.items() if not v]

    # Which XBRL tags were actually available, and which of those went unused by
    # the mapping. This is the real signal: a field is only a genuine "gap" if no
    # suitable tag existed; if a tag existed but wasn't assigned, that's a mapper
    # miss worth flagging for a prompt fix.
    available_tags = sorted(xbrl_facts.keys())
    assigned_tags = set()
    for v in mapped_fields.values():
        if isinstance(v, list):
            assigned_tags.update(v)
        elif isinstance(v, str):
            assigned_tags.add(v)
    unused_tags = sorted(t for t in available_tags if t not in assigned_tags)

    # Compustat field DEFINITIONS the mapper works from. Without these the
    # reflection model guesses field meanings from the 2-letter codes (e.g.
    # assuming `lt` means "long-term debt" when it means total Liabilities),
    # which produced a large volume of spurious "accuracy issues". Restrict to
    # the fields actually in play to keep the prompt focused.
    relevant = set(mapping.keys())
    field_defs = {
        k: v for k, v in mapper.COMPUSTAT_FIELDS.items() if k in relevant
    }

    # Fields the post-processor DERIVES downstream from accounting identities
    # even when no tag is assigned — do NOT flag these as gaps/misses.
    derived_fields = [
        "lt (= at - seq)", "lt_noncurrent (= lt - lct)", "gp (= sale - cogs)",
        "seq (= ceq when no preferred/NCI)", "ceq (= seq - pstk)",
        "ib (= pi - txt)", "ni (= ib when no discontinued/extraordinary items)",
        "oibdp (= oiadp + dp — operating income before depreciation, i.e. EBITDA)",
        "dltt (= base long-term-debt tag + dltt_oplease + dltt_finlease — lease "
        "liabilities are folded IN, do not flag dltt as missing lease components)",
    ]

    reflection_prompt = f"""You just completed an XBRL-to-Compustat mapping for {ticker} {form_type} {report_date}.

Compustat field DEFINITIONS (authoritative — use these exact meanings, do NOT
guess a field's meaning from its short code):
{json.dumps(field_defs, indent=2)}

These Compustat fields are DERIVED automatically downstream from accounting
identities even if no XBRL tag is assigned — do NOT report them as gaps or
misses when their inputs are present:
{json.dumps(derived_fields, indent=2)}

The {len(available_tags)} XBRL tags AVAILABLE in this filing were:
{json.dumps(available_tags, indent=2)}

The tag→Compustat assignments you produced ({len(mapped_fields)} of {len(mapping)} fields mapped):
{json.dumps(mapped_fields, indent=2)}

Compustat fields left NULL: {json.dumps(unmapped_fields)}

XBRL tags that were available but you did NOT assign to any field:
{json.dumps(unused_tags, indent=2)}

Ground every observation in the lists above — do NOT claim a tag or field is
"missing" if it appears in the AVAILABLE list, and do NOT claim a field is a gap
if it is in the DERIVED list and its inputs were mapped. Write a short markdown
reflection using EXACTLY these four sections (keep it concise — a few bullets each):

## Assumptions
2-3 key assumptions you made (e.g. how you handled ambiguous tags, whether you preferred aggregate vs component tags).

## Unmapped Gaps
Top 3 NULL Compustat fields for which NO suitable tag existed in the AVAILABLE list AND which are not auto-derived, and why. If a usable tag did exist but you missed it, put it under Accuracy Issues instead.

## Accuracy Issues
Cases where a usable tag WAS available (cite it from the unused-tags list) but you left the field null or mapped it wrong, plus any accounting-identity violations. Use the field DEFINITIONS above — do not flag a mapping as wrong based on a misremembered field meaning.

## Prompt Improvements
2-3 specific improvements to the mapping prompt/constraints that would have caught the misses above on future filings.

Output ONLY the markdown document. Do not output JSON."""

    try:
        if getattr(_use_nemotron, "active", False) and OPENROUTER_API_KEY:
            # Nemotron worker: use OpenRouter for the reflection too so the local
            # MLX server isn't hit from every worker simultaneously.
            _, parsed = _call_openrouter_real(NEMOTRON_MODEL, reflection_prompt, OPENROUTER_API_KEY)
            # reflection_prompt asks for plain markdown not JSON — _parse_llm_json
            # will likely fail, so fall back to the raw text from the API.
            try:
                raw_resp = mapper._call_openrouter.__wrapped_raw__ if hasattr(mapper._call_openrouter, "__wrapped_raw__") else None
            except Exception:
                raw_resp = None
            # Simpler: call the OpenRouter HTTP layer directly for plain text
            import urllib.request as _ur
            _body = json.dumps({
                "model": NEMOTRON_MODEL,
                "messages": [{"role": "user", "content": reflection_prompt}],
                "max_tokens": 2000,
                "temperature": 0,
            }).encode()
            _req = _ur.Request(
                mapper._OPENROUTER_API_URL,
                data=_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "HTTP-Referer": "https://github.com/aepodrez/EuclideanInfra",
                    "X-Title": "Euclidean XBRL Mapper",
                },
                method="POST",
            )
            with _ur.urlopen(_req, timeout=120) as _r:
                _resp = json.loads(_r.read())
            text = (_resp["choices"][0]["message"].get("content") or "").strip()
        else:
            # Local worker: use the MLX server as before.
            text = _stream_llm_raw(reflection_prompt, max_tokens=2000, retries=2)
        text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", text.strip())
        return text.strip()
    except Exception as exc:
        log.warning("Failed to get reflection from LLM: %s — continuing without it", exc)
        return ""


# ---------------------------------------------------------------------------
# Optional Nemotron (OpenRouter) worker path
# When OPENROUTER_API_KEY is set and --nemotron-workers > 0, those workers
# bypass the local LLM patch and call OpenRouter directly with nemotron.
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
NEMOTRON_MODEL     = "nvidia/nemotron-3-super-120b-a12b:free"

# Save the real _call_openrouter before patching so Nemotron workers can use it.
_call_openrouter_real = mapper._call_openrouter

# Thread-local: True when the current worker should use Nemotron instead of local LLM.
_use_nemotron = threading.local()


def _invoke_for_filing(prompt: str) -> tuple[str, dict]:
    """Route to local LLM or Nemotron depending on which worker we're in."""
    if getattr(_use_nemotron, "active", False) and OPENROUTER_API_KEY:
        return _call_openrouter_real(NEMOTRON_MODEL, prompt, OPENROUTER_API_KEY)
    return _call_local_llm(prompt)


# Patch before any map_filing calls — unified entry point
mapper._call_openrouter = lambda model, prompt, api_key: _invoke_for_filing(prompt)

# ---------------------------------------------------------------------------
# S3 / EDGAR helpers
# ---------------------------------------------------------------------------
def _s3_key(form_type: str, cik: str, report_date: str) -> str:
    folder = "annual" if "10-K" in form_type or "20-F" in form_type else "quarterly"
    return f"data-ingress/filings/{folder}/{cik}/{report_date}.parquet"


def _parquet_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "403"):
            return False
        raise


def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": EDGAR_IDENTITY, "Accept-Encoding": "gzip"}
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode())
        except Exception as exc:
            if attempt == 3:
                raise
            time.sleep(2 * attempt)


def _latest_filings(cik: str, forms: tuple[str, ...]) -> list[dict]:
    """
    Return the most recent filing per form-family (annual / quarterly)
    that was filed within the last LOOKBACK_DAYS and whose accession
    belongs to this CIK (not a parent filer).
    """
    cik_padded = str(int(cik)).zfill(10)
    try:
        data = _get_json(f"https://data.sec.gov/submissions/CIK{cik_padded}.json")
    except Exception as e:
        log.warning("EDGAR submissions fetch failed for CIK %s: %s", cik, e)
        return []

    recent      = data.get("filings", {}).get("recent", {})
    accessions  = recent.get("accessionNumber", [])
    form_list   = recent.get("form", [])
    report_dates = recent.get("reportDate", [])
    filed_dates  = recent.get("filingDate", [])

    cutoff  = (datetime.now(timezone.utc).replace(tzinfo=None) -
               __import__("datetime").timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    cik_int = str(int(cik))

    results: list[dict] = []
    for acc, form, report_date, filed_date in zip(accessions, form_list, report_dates, filed_dates):
        if form not in forms:
            continue
        if filed_date < cutoff:
            continue
        if not report_date or not acc:
            continue
        filer_cik = acc.split("-")[0].lstrip("0") or "0"
        if filer_cik != cik_int:
            continue
        results.append({
            "form_type":        form,
            "accession_number": acc,
            "report_date":      report_date,
        })

    # Sort newest-first, return ALL filings in the window
    results.sort(key=lambda f: f["report_date"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Universe loading
# ---------------------------------------------------------------------------
def _load_universe() -> list[dict]:
    obj  = s3.get_object(Bucket=S3_BUCKET, Key=UNIVERSE_KEY)
    text = obj["Body"].read().decode("utf-8", errors="replace")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        cik    = (row.get("cik") or "").strip()
        ticker = (row.get("ticker") or "").strip().upper()
        sic    = (row.get("sic") or "").strip()
        if cik:
            rows.append({"cik": str(int(cik)).zfill(10), "ticker": ticker, "sic": sic})
    return rows


# ---------------------------------------------------------------------------
# Per-filing work
# ---------------------------------------------------------------------------
import io as _io
import pyarrow as pa
import pyarrow.parquet as pq


def _write_parquet(row: dict, key: str) -> None:
    arrays = {}
    for k, v in row.items():
        # Never serialize non-scalar values (e.g. the raw _xbrl_facts dict) into a
        # parquet column — they'd be stringified into an unusable blob.
        if isinstance(v, (dict, list, tuple, set)):
            continue
        if isinstance(v, float):
            arrays[k] = pa.array([v], type=pa.float64())
        else:
            arrays[k] = pa.array([str(v) if v is not None else None], type=pa.string())
    table = pa.table(arrays)
    buf = _io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    buf.seek(0)
    xbrl_status = row.get("_xbrl_status", "ok")
    accession   = row.get("_accession", "")
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=buf.read(),
        ContentType="application/octet-stream",
        Metadata={"xbrl_status": xbrl_status, "accession": accession},
    )


# Accessions that reliably hang the LLM stream — skip permanently.
_SKIP_ACCESSIONS: set[str] = {
    "0000947484-25-000085",  # ACGL/ACGLN 10-Q 2025-09-30 — hangs Qwen3 stream every time
    "0001401521-24-000118",  # ACIC 10-Q 2024-09-30 — hung stream
}

# When True (--reprocess), re-map filings even if a parquet already exists in S3.
# Used to overwrite the whole historical panel after a mapper schema change (new
# fields). Default False keeps the resume-and-skip behaviour.
_REPROCESS: bool = False


def _process_filing(company: dict, filing: dict) -> dict:
    """Run mapper for one filing. Returns a status dict."""
    cik    = company["cik"]
    ticker = company["ticker"]
    sic    = company["sic"]
    form_type  = filing["form_type"]
    accession  = filing["accession_number"]

    if accession in _SKIP_ACCESSIONS:
        log.info("[%s] %s accession %s in skip list — skipping", ticker, form_type, accession)
        return {"ticker": ticker, "cik": cik, "form_type": form_type, "status": "skipped"}

    # If report_date is known (SQS messages include it), check S3 before calling LLM.
    # Skip only if the parquet exists AND the accession matches — if accession differs
    # it's a new filing or amendment and we must reprocess.
    report_date_known = filing.get("report_date", "")
    if report_date_known and not _REPROCESS:
        key = _s3_key(form_type, cik, report_date_known)
        if _parquet_exists(key):
            try:
                head = s3.head_object(Bucket=S3_BUCKET, Key=key)
                existing_accession = head.get("Metadata", {}).get("accession", "")
            except Exception:
                existing_accession = ""
            if existing_accession == accession:
                log.info("[%s] %s %s already in S3 (same accession) — skipping", ticker, form_type, report_date_known)
                return {"ticker": ticker, "cik": cik, "form_type": form_type, "status": "skipped"}
            log.info("[%s] %s %s accession changed — reprocessing", ticker, form_type, report_date_known)
    # The mapper fetches metadata internally; we check S3 after building the key.
    t0 = time.time()
    try:
        row = mapper.map_filing(
            cik=cik,
            accession=accession,
            form_type=form_type,
            ticker=ticker,
            sic=sic,
            openrouter_api_key="",  # unused — patched to Ollama
        )
    except Exception as exc:
        log.error("[%s] %s %s FAILED: %s", ticker, form_type, accession, exc)
        return {"ticker": ticker, "cik": cik, "form_type": form_type, "status": "error", "error": str(exc)}

    row["_accession"] = accession
    report_date = row.get("datadate", "unknown")
    # Pop the raw XBRL facts off the row BEFORE writing parquet — it's a large
    # non-scalar dict that must not become a stringified parquet column. We hold
    # it locally to drive the reflection pass below.
    xbrl_facts = row.pop("_xbrl_facts", {}) or {}
    key = _s3_key(form_type, cik, report_date)
    _write_parquet(row, key)
    elapsed = time.time() - t0
    log.info("[%s] %s %s → s3://%s/%s (%.1fs)", ticker, form_type, report_date, S3_BUCKET, key, elapsed)

    # Capture LLM reflection on mapping accuracy and store as insight document
    try:
        # The tag→Compustat assignments the LLM actually produced (JSON string).
        try:
            ai_mapping = json.loads(row.get("_ai_mapping", "{}"))
        except Exception:
            ai_mapping = {}
        reflection = _get_llm_reflection(
            ticker, form_type, report_date, ai_mapping, xbrl_facts
        )
        if reflection:
            # Store as a markdown document with a small front-matter header so the
            # filing it belongs to is self-describing when read back later.
            try:
                residuals = json.loads(row.get("_anchor_residuals", "{}"))
            except Exception:
                residuals = {}
            header = (
                f"# Mapping reflection: {ticker} {form_type} {report_date}\n\n"
                f"- cik: {cik}\n"
                f"- accession: {accession}\n"
                f"- timestamp: {datetime.now(timezone.utc).isoformat()}\n"
                f"- xbrl_facts: {len(xbrl_facts or {})}\n"
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
            log.info("[%s] Mapping insight stored: s3://%s/%s", ticker, S3_BUCKET, insight_key)
    except Exception as exc:
        log.warning("[%s] Failed to store mapping insight: %s", ticker, exc)

    return {"ticker": ticker, "cik": cik, "form_type": form_type, "status": "ok", "report_date": report_date}


def _log_progress(result: dict) -> None:
    with open(PROGRESS_FILE, "a") as f:
        f.write(json.dumps({**result, "ts": datetime.now().isoformat()}) + "\n")


# ---------------------------------------------------------------------------
# SQS consumer mode
# ---------------------------------------------------------------------------
SQS_QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/954976294836/euclidean-edgar-filings"

def _run_from_sqs(workers: int) -> None:
    """Pull messages directly from SQS, process with local LLM, delete on success."""
    sqs_client = boto3.client("sqs", region_name="us-east-1")
    done = errors = 0

    log.info("Pulling from SQS: %s", SQS_QUEUE_URL)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            resp = sqs_client.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=min(workers, 10),
                WaitTimeSeconds=10,           # long poll
                VisibilityTimeout=1800,       # 30 min — enough for LLM + upload
            )
            messages = resp.get("Messages", [])
            if not messages:
                log.info("Queue empty — done. Written=%d Errors=%d", done, errors)
                break

            futs = {}
            for msg in messages:
                body = json.loads(msg["Body"])
                company = {"cik": body["cik"], "ticker": body["ticker"], "sic": body.get("sic", "")}
                filing  = {"form_type": body["form_type"], "accession_number": body["accession_number"],
                           "report_date": body.get("report_date", "")}
                futs[pool.submit(_process_filing, company, filing)] = msg["ReceiptHandle"]

            for fut in as_completed(futs):
                receipt = futs[fut]
                try:
                    result = fut.result()
                except Exception as exc:
                    result = {"status": "error", "error": str(exc)}

                _log_progress(result)
                if result.get("status") in ("ok", "skipped"):
                    # Delete from queue on success
                    sqs_client.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt)
                    done += 1
                else:
                    errors += 1
                    # Leave in queue — visibility timeout will expire and it'll reappear
                    log.error("Failed: %s — leaving in queue", result.get("error", ""))

                log.info("Progress: %d done / %d errors", done, errors)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Backfill EDGAR parquets via local LLM")
    parser.add_argument("--workers",        type=int, default=2,
                        help="Parallel filing workers (default 2)")
    parser.add_argument("--limit",          type=int, default=0,
                        help="Stop after processing N companies (0 = unlimited)")
    parser.add_argument("--annual-only",    action="store_true")
    parser.add_argument("--quarterly-only", action="store_true")
    parser.add_argument("--from-sqs",       action="store_true",
                        help="Pull work directly from SQS queue instead of scanning EDGAR")
    parser.add_argument("--rescan",         action="store_true",
                        help="Force fresh EDGAR scan even if a cached queue exists")
    parser.add_argument("--reprocess",        action="store_true",
                        help="Re-map filings even if a parquet already exists in S3 "
                             "(overwrite the historical panel after a mapper schema change). "
                             "Combine with --rescan to rebuild the queue including done filings.")
    parser.add_argument("--nemotron-workers", type=int, default=0, metavar="N",
                        help="Number of additional workers that call OpenRouter Nemotron instead "
                             "of the local LLM (requires OPENROUTER_API_KEY env var).")
    args = parser.parse_args()

    global _REPROCESS
    _REPROCESS = args.reprocess
    if _REPROCESS:
        log.info("REPROCESS mode: existing parquets will be overwritten")

    if args.from_sqs:
        _run_from_sqs(workers=args.workers)
        return

    forms: tuple[str, ...]
    if args.annual_only:
        forms = ("10-K", "20-F")
    elif args.quarterly_only:
        forms = ("10-Q",)
    else:
        forms = ("10-K", "20-F", "10-Q")

    work_items: list[tuple[dict, dict]] = []

    if not args.rescan and os.path.exists(QUEUE_CACHE):
        log.info("Loading work queue from local cache: %s", QUEUE_CACHE)
        with open(QUEUE_CACHE) as f:
            cached = json.load(f)
        work_items = [(item["company"], item["filing"]) for item in cached]
        log.info("Work queue: %d filings (from local cache — use --rescan to rebuild)", len(work_items))
    elif not args.rescan and _parquet_exists(QUEUE_CACHE_S3):
        log.info("Loading work queue from S3 cache: s3://%s/%s", S3_BUCKET, QUEUE_CACHE_S3)
        obj = s3.get_object(Bucket=S3_BUCKET, Key=QUEUE_CACHE_S3)
        cached = json.loads(obj["Body"].read())
        work_items = [(item["company"], item["filing"]) for item in cached]
        with open(QUEUE_CACHE, "w") as f:
            json.dump(cached, f)
        log.info("Work queue: %d filings (from S3 cache)", len(work_items))
    else:
        log.info("Loading universe from S3...")
        universe = _load_universe()
        log.info("Universe: %d companies", len(universe))

        edgar_batch_size = 10
        edgar_pause      = 1.0  # EDGAR rate limit: 10 req/s

        log.info("Fetching EDGAR submissions to build work queue...")
        already_done = 0
        for i, company in enumerate(universe):
            if args.limit and len({c["cik"] for c, _ in work_items}) >= args.limit:
                break
            filings = _latest_filings(company["cik"], forms)
            for filing in filings:
                key = _s3_key(filing["form_type"], company["cik"], filing["report_date"])
                if _parquet_exists(key) and not _REPROCESS:
                    already_done += 1
                    continue
                work_items.append((company, filing))

            if (i + 1) % edgar_batch_size == 0:
                time.sleep(edgar_pause)
                log.info("  Scanned %d/%d companies — %d to process, %d already done",
                         i + 1, len(universe), len(work_items), already_done)

        log.info("Work queue: %d filings across %d companies", len(work_items),
                 len({c["cik"] for c, _ in work_items}))

        payload = [{"company": c, "filing": fi} for c, fi in work_items]
        with open(QUEUE_CACHE, "w") as f:
            json.dump(payload, f)
        log.info("Work queue cached to %s", QUEUE_CACHE)
        try:
            s3.put_object(
                Bucket=S3_BUCKET, Key=QUEUE_CACHE_S3,
                Body=json.dumps(payload).encode(), ContentType="application/json")
            log.info("Work queue backed up to s3://%s/%s", S3_BUCKET, QUEUE_CACHE_S3)
        except Exception as e:
            log.warning("Failed to back up queue to S3: %s", e)

    # Apply company limit
    if args.limit:
        # Keep at most limit companies
        seen_ciks: list[str] = []
        filtered: list[tuple[dict, dict]] = []
        for company, filing in work_items:
            if company["cik"] not in seen_ciks:
                seen_ciks.append(company["cik"])
            if len(seen_ciks) <= args.limit:
                filtered.append((company, filing))
        work_items = filtered

    nemotron_workers = getattr(args, "nemotron_workers", 0)
    total_workers    = args.workers + nemotron_workers
    if nemotron_workers and not OPENROUTER_API_KEY:
        log.warning("--nemotron-workers set but OPENROUTER_API_KEY is empty — Nemotron workers will fall back to local LLM")

    def _process_nemotron(company: dict, filing: dict) -> dict:
        _use_nemotron.active = True
        try:
            return _process_filing(company, filing)
        finally:
            _use_nemotron.active = False

    log.info("Processing %d filings (local-workers=%d, nemotron-workers=%d)...",
             len(work_items), args.workers, nemotron_workers)

    done = errors = skipped = 0
    with ThreadPoolExecutor(max_workers=total_workers) as pool:
        futs = {}
        for i, (c, f) in enumerate(work_items):
            # Distribute: every Nth filing goes to a Nemotron worker based on round-robin
            # across the extra worker slots so work is spread evenly.
            fn = _process_nemotron if (nemotron_workers and i % (total_workers) < nemotron_workers) else _process_filing
            futs[pool.submit(fn, c, f)] = (c, f)
        for fut in as_completed(futs):
            try:
                result = fut.result()
            except Exception as exc:
                company, filing = futs[fut]
                result = {
                    "ticker":    company["ticker"],
                    "cik":       company["cik"],
                    "form_type": filing["form_type"],
                    "status":    "error",
                    "error":     str(exc),
                }

            _log_progress(result)
            status = result.get("status")
            if status == "ok":
                done += 1
            elif status == "skipped":
                skipped += 1
            else:
                errors += 1
            log.info("Progress: %d done / %d skipped / %d errors (of %d total)",
                     done, skipped, errors, len(work_items))

    log.info("Done. Written=%d  Skipped=%d  Errors=%d", done, skipped, errors)
    log.info("Progress log: %s", PROGRESS_FILE)


if __name__ == "__main__":
    main()
