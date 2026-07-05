# Lambda Infrastructure

How the ~95 deployed Lambda functions across the Euclidean pipeline are built,
dispatched, scheduled, and kept idempotent. See `DEPLOYMENT.md` for how code
actually reaches these functions.

## The core pattern: one image, many functions, a JOB env var

Almost every Lambda group in this system follows the same shape: **one shared
container image** containing a dispatcher module plus a directory of
independent scripts, deployed as **N separate `aws_lambda_function` resources**
that all point at the same `image_uri` but differ only in an environment
variable that tells the dispatcher which script to run. This avoids building
a separate image per job while still getting per-job memory/timeout/schedule
tuning and independent CloudWatch log groups/metrics.

There are five such dispatcher-based image groups, all in
`DataIngressModel/lambdas/`:

| Image | Dispatcher | Env var | # functions | Scripts live in |
|---|---|---|---|---|
| `euclidean-market-data` | `market_data/lambda_entry.py` | `JOB` | ~45 | `DataDownloads/*.py` |
| `euclidean-ibes` | `ibes/ibes_entry.py` | `JOB` | 14 | `DataDownloads/IBES*.py` |
| `euclidean-predictors` | `predictors/lambda_entry.py` | `PREDICTOR` | 67 | `Predictors/*.py` |

Plus two purpose-built (non-dispatcher) Lambdas that are event-driven rather
than cron-driven: `euclidean-edgar-filing-poller` (scheduled) and
`euclidean-edgar-ai-worker`/`euclidean-edgar-ai-aggregator` (SQS/cron), and two
single-purpose Lambdas outside DataIngressModel entirely: AlphaModel's and
UniverseModel's (the latter zip-packaged, not container images — see
`DEPLOYMENT.md`).

## Dispatcher handler flow (market_data / ibes — nearly identical)

1. Reads `JOB` (or the Step-Function-style `event["job"]` override) and looks
   it up in a `JOB_SPECS: dict[str, dict]` table hardcoded in the dispatcher.
2. Sets `CWD` to `/tmp/DataDownloads` so the scripts' relative paths
   (`../pyData/Intermediate`, `../Static`) resolve under `/tmp` (Lambda's only
   writable filesystem).
3. Downloads `universe.csv` from S3, plus (unless `force` is set) the job's own
   prior **outputs** (so incremental/append logic has something to append to),
   plus any declared **inputs** (other pipeline files the script reads, e.g.
   `QFactorModel` needs `dailyCRSP.parquet` + `dailyFF.parquet` +
   `m_aCompustat.parquet`).
4. Optionally loads secrets from SSM (`needs_fred` → `FRED_API_KEY`) or a
   shared portfolio file (`needs_portfolio`).
5. `importlib.import_module`s the target script and calls its `main()` (or
   just imports it, for scripts that do their work at import time, e.g. `VIX`).
6. Uploads whatever output files the script produced back to S3 under
   `pyData/Intermediate/`.

### Idempotency contract

Every job is one of two classes, and this classification is deliberate — it's
what makes "just re-run it" always safe:

- **Class A (full recompute)**: the script overwrites its output key with a
  freshly computed result every run. Re-running is a no-op if nothing changed
  upstream (`vix`, `fama-french-*`, `gnp`, `treasury-bill-3m`,
  `security-identifiers`, `corwin-schultz`, `q-factor-model`, `ibes-crsp-link`).
- **Class B (incremental)**: the script downloads its own prior output,
  computes only a recent window, appends, and `drop_duplicates(keep="last")`
  on a stable key (`crsp-daily`, `crsp-monthly`, `market-returns[-daily]`, the
  two IBES daily scripts). The incremental window is derived from
  existing-output-max-date + wall clock, so a same-day re-run is safe — the
  merge absorbs the overlap.

### Sharding convention

Jobs that iterate the full ~5,700-ticker universe per-ticker (CRSP daily
prices, options snapshots, IBES recommendations/actuals) would blow past
Lambda's 900s hard cap running serially, so they're split across N parallel
Lambda functions, each processing `tickers[k::N]`:

- Each shard is its own `JOB_SPECS` entry with a distinct output filename
  suffix (`dailyCRSP_s0.parquet` … `_s5.parquet`, `IBES_Recommendations_s0.parquet`
  … `_s5.parquet`).
- The shard index/count are **not** in `JOB_SPECS` — they're injected per
  Lambda via `extra_env` in the Terraform job map (e.g.
  `AP_CRSP_SHARD_INDEX`/`AP_CRSP_SHARD_COUNT` for CRSP, `AP_IBES_SHARD_INDEX`/
  `_COUNT` for IBES, `AP_OPT_SHARD_INDEX`/`_COUNT` for options), which the
  script itself reads to slice its ticker list.
- A separate **merge job**, scheduled 10-20 minutes after the shards, reads
  every shard's output, concatenates, dedups on a stable key, and writes the
  canonical file (`CRSPDailyMerge`, `IBESRecommendationsMerge`,
  `IBESUnadjustedActualsMerge`). Merge jobs are written to be tolerant of a
  missing shard (a single shard timeout doesn't nuke the canonical file — it
  just contributes stale data for that slice until the next run).

### Other JOB_SPECS flags worth knowing

- `cache_subdir` / `cache_readonly`: syncs a whole S3 prefix down to `/tmp`
  before the run (and back up after, unless `cache_readonly`). Used for the
  ever-growing options-snapshot history that multiple signal jobs read but
  don't want to re-upload every time.
- `companyfacts_tar`: restores/persists the EDGAR companyfacts cache as a
  single tarball (`build_ff_portfolios`), plus a deadline-timer thread that
  flushes the partial cache ~60s before the Lambda's hard timeout, since a
  full companyfacts download can't finish in one 900s invocation and SEC's
  ~10 req/s limit means this genuinely takes multiple days of runs to
  self-heal to completion.
- `skip_portfolio_build`: guards daily/monthly factor jobs from accidentally
  triggering a full portfolio rebuild (mass EDGAR re-download) just because
  the portfolio file happened to be missing on a cold path.
- `needs_refinitiv` (ibes dispatcher only): asserts
  `REFINITIV_APP_KEY`/`USERNAME`/`PASSWORD` are present before running —
  fails fast with a clear error instead of a confusing SDK-level failure.

## Why IBES got its own image instead of joining market-data

The Refinitiv Data SDK (`refinitiv-data==1.6.2`) requires a specific pinned
install (`httpx==0.23.3` + `--no-deps` + a curated transitive dependency list —
a plain `pip install refinitiv-data` pulls in `scipy` and an incompatible
`httpx`) and carries live third-party credentials. Rather than add ~140MB and
production credentials to the shared market-data image (used by ~45
functions), IBES got a dedicated image + IAM role, mirroring how
`edgar_ai_worker` is also its own image.

## Event-driven Lambdas (no schedule, triggered by SQS)

`euclidean-edgar-ai-worker` is the one Lambda in this system that isn't
cron-triggered — it's an SQS consumer (event source mapping on
`euclidean-edgar-filings`, `BatchSize=1`, `MaximumConcurrency=2`,
`FunctionResponseTypes=[ReportBatchItemFailures]`). Flow:

```
edgar-filing-poller (cron 06:30 daily)
  → finds new filings via SEC's daily index
  → sends one SQS message per filing: {cik, ticker, sic, form_type, accession_number, report_date}
edgar-ai-worker (SQS-triggered, up to 2 concurrent)
  → fetches XBRL facts, maps them to Compustat fields via an LLM
    (Nemotron/OpenRouter in production; local MLX/Ollama for manual backfills)
  → writes a per-filing parquet to S3
  → on success, the Lambda returns cleanly and SQS auto-deletes the message
  → on failure, reports a batchItemFailure; after maxReceiveCount
    delivery attempts, SQS routes the message to euclidean-edgar-filings-dlq
edgar-ai-aggregator (cron 08:00 daily)
  → merges all per-filing parquets into a_aCompustat.parquet / m_aCompustat.parquet / etc.
```

The `segments`/`acquisitions` aggregator+worker pairs follow the same
poller→SQS→worker→aggregator shape (their own queues + DLQs, defined in
`edgar_fanout.tf`), sharing the market-data image but selecting the SQS-worker
entrypoint via `image_config.command = ["sqs_worker.lambda_handler"]` instead
of the default `lambda_entry.lambda_handler`.

**DLQ `maxReceiveCount` note**: a low value here means even a single slow-but-
ultimately-successful run (Nemotron occasionally takes several minutes on a
token-limit retry) can lose the race against SQS's visibility timeout and land
in the DLQ despite the underlying work actually succeeding. Check the DLQ
before assuming a stranded message represents lost data — the S3 output may
already exist.

## Scheduling

Every cron-driven Lambda is wired identically in Terraform: an
`aws_cloudwatch_event_rule` (the cron expression) → `aws_cloudwatch_event_target`
(pointing at the Lambda ARN) → `aws_lambda_permission` (letting
`events.amazonaws.com` invoke it). For the dispatcher-based images this is
almost always expressed as a `for_each` over a single Terraform `locals` map
(`market_data_jobs`, `ibes_jobs`, `predictor_jobs`) so adding a new scheduled
job is a one-line map entry, not three new resource blocks.

Approximate overnight ordering (UTC), illustrating the dependency chain
between jobs that read each other's output:

```
05:15  security-identifiers          (ticker/cik/permno crosswalk — must be first)
05:30  vix, crsp-daily-s0..s5, universe-downloader→sic-worker
05:40  crsp-daily-merge               (10 min after shards)
05:45  market-returns-daily
06:00  ibes-recommendations-s0..s5 (daily), corwin-schultz (monthly 1st)
06:15  fama-french-daily, ibes-recommendations-merge, ibes-actuals-s0..s5 (Mon-Fri)
06:20  q-factor-model
06:30  edgar-filing-poller
06:35  ibes-actuals-merge (Mon-Fri)
07:00  ibes-crsp-link (monthly 1st — after crsp-monthly + IBES EPS)
08:00  edgar-ai-aggregator
08:15  signal-master-daily
08:30  overnight-report (self-check SMS), monthly decision Step Function (1st only)
```

Cross-repo scheduling (a Terraform-managed EventBridge rule in one repo
triggering a resource defined in another) exists too — e.g.
`EuclideanInfra/terraform/ibes_eps_ecs.tf` schedules a monthly `ecs:RunTask`
against a task family (`euclidean-data-ingress-refinitiv`) whose task
definition is actually registered by DataIngressModel's CI. There's no
Terraform remote-state link for this — it's pure naming-convention string
construction plus an IAM policy scoped to that specific ARN pattern.
