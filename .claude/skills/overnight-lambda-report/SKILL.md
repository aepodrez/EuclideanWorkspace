---
name: overnight-lambda-report
description: Report on recent scheduled and triggered AWS activity across the Euclidean pipeline, including every market-data, IBES, universe, EDGAR, and other invoked Euclidean Lambda plus scheduled ECS/Fargate and Step Functions work. Identify exactly which overnight runs require reruns and which failures already recovered. Use for overnight/daily status, what ran, missed schedules, failures, rerun/redrive decisions, 13F health, or pipeline output counts.
---

# Overnight Euclidean AWS Activity Report

Produce an evidence-based health report for the previous processing window. Discover the
live topology every time: deployed functions and schedules change, and a static inventory
must never define the scope.

## Environment

- Region: `us-east-1`
- Account: `954976294836`
- Data bucket: `s3://euclidean-pipeline-954976294836`
- Lambda logs: `/aws/lambda/<function-name>`
- EventBridge cron expressions use UTC unless EventBridge Scheduler specifies a timezone.

Use read-only AWS calls. Start with `aws sts get-caller-identity`; never print credentials or
secret values. Default to the last 24 hours so the window includes evening option jobs as
well as the 05:00-12:00 UTC pipeline. State the exact UTC start and end. Honor a user-supplied
window instead.

## Scope: discover, do not assume

### Lambda

List every deployed function and include:

1. Every `euclidean-md-*`, `euclidean-ibes-*`, `euclidean-universe-*`, and
   `euclidean-edgar-*` function, whether directly scheduled or indirectly triggered.
2. Every other `euclidean-*` function with an invocation, error, or throttle in the window.
3. Every Lambda targeted by an enabled Euclidean EventBridge rule or EventBridge Scheduler
   schedule that was due in the window, even if it had zero invocations.

This deliberately covers all current and future market-data families. Current examples
include CRSP, Fama-French, VIX, FX, market returns, q-factor, signal master, security
identifiers, 13F shards and merge, option snapshots and metrics, acquisitions, segments,
PIT raw, Compustat short interest, company names, IPO dates, Treasury bills, GNP, BEA,
Bali-Hovak, Corwin-Schultz, Pastor-Stambaugh, and IBES actuals/recommendations/link jobs.
Treat this list only as orientation, never as the inventory.

Discover indirect Lambda triggers with `lambda list-event-source-mappings`. Workers such as
SQS consumers may correctly run without their own cron rule. A zero-run worker is expected
when its upstream queue received no messages.

### EventBridge, ECS, and Step Functions

Enumerate all enabled scheduled rules with `events list-rules`, then call
`events list-targets-by-rule` for each. Retain a rule when its name, description, target ARN,
task definition, or target input identifies Euclidean. Classify every target ARN as Lambda,
ECS, Step Functions, or other. Also enumerate EventBridge Scheduler with
`scheduler list-schedule-groups`, `scheduler list-schedules`, and `scheduler get-schedule`;
the account may currently have none.

Do not omit scheduled non-Lambda targets or Euclidean ECS tasks launched indirectly or
manually during the window. Known ECS/Fargate schedule examples currently include:

- `euclidean-build-ff-portfolios-annual`
- `euclidean-ibes-eps-monthly`
- `euclidean-usaspending-quarterly`

Treat these names as examples only. Discover cluster ARN, task-definition ARN, schedule,
state, role, and target input from the live target configuration.

Include scheduled Step Functions targets such as `euclidean-pipeline`; their executions can
fan out to many predictor, model, Lambda, or ECS jobs.

## Workflow

### 1. Build the live schedule and target map

Record each enabled Euclidean schedule, its expression/timezone, target type, and target ARN.
Evaluate whether at least one firing fell inside the report window. AWS EventBridge cron has
six fields (`minutes hours day-of-month month day-of-week year`) and `?` means unspecified.
Handle lists, ranges, steps, named weekdays/months, and `rate(...)`; do not infer cadence from
the function name. For an expression that cannot be evaluated confidently, show the exact
expression and label it `possibly due` rather than calling it missed or expected.

For each rule, query `AWS/Events` metrics `Invocations` and `FailedInvocations` over the same
window. A failed target delivery is a real failure even when the Lambda or ECS task never
started.

### 2. Batch metrics for all in-scope Lambdas

Use CloudWatch `get_metric_data` with `Sum` for `Invocations`, `Errors`, and `Throttles` for
the full window. Batch at most 500 metric queries per request and follow `NextToken`.

Classify each Lambda:

- `FAILED`: `Errors > 0`, timeout/OOM in REPORT, or failed async delivery.
- `DEGRADED`: invocation succeeded but logs show dropped records, partial/budget-capped work,
  retry exhaustion, or output freshness/completeness concerns.
- `HEALTHY`: ran, emitted its expected output, and had no terminal failure.
- `MISSED`: an enabled schedule was due but invocations are zero.
- `EXPECTED NO-RUN`: no firing was due, or an indirect worker had no upstream work.
- `UNSCHEDULED/TRIGGERED`: ran via SQS, chaining, Step Functions, or manual invocation.

Do not call an unscheduled worker missed. Do not call a monthly/quarterly job healthy merely
because it had zero errors; classify it by whether its schedule was due.

### 3. Inspect logs for every invoked Lambda

Use `logs filter-log-events` over the whole window, not only the newest stream. Collect every
`REPORT` line to capture run time, billed duration, peak memory, and terminal `Status` /
`Error Type`. Correlate failures by RequestId and retrieve surrounding events, including the
full exception class and operation that failed.

For successful market-data and ingestion runs, extract concrete production summaries such
as `done:`, `Done:`, `completed`, `Wrote`, `Saved`, `uploaded`, row/record/ticker/filing/
manager counts, date ranges, and S3 keys. Family-specific examples:

- Universe: `Total SEC-registered tickers`, `Tickers on major US exchanges`,
  `filter_common_stocks`, `Wrote universe.csv`.
- EDGAR: `Done [incremental]`, `map_filing`, `wrote parquet`, aggregator `Done: {...}`.
- Sharded jobs: show every shard separately, then the merge. Check that all expected shard
  outputs were present and quote merged row/entity counts.
- 13F: quote per-shard raw rows and managers, unprocessed/budget-limited filings, combined
  rows/managers, and whether the final transformed artifact was written.
- IBES/options: show shard completion and merge/metric output; flag a merge that ran before
  all expected shards completed.

Application log lines labeled `ERROR` are not automatically Lambda failures. For example,
an unavailable/delisted ticker warning can be non-fatal if the handler subsequently writes
outputs and its REPORT is successful. Report these as data-quality warnings, naming affected
symbols/counts, while keeping them distinct from terminal failures.

### 4. Root-cause Lambda failures and retries

For every `Errors > 0`, inspect all attempts in the window. Report:

- number of invocations and failed attempts;
- first and last attempt times;
- exception/error type and failing operation;
- whether retry attempts reused the same event/RequestId;
- whether any final output was written;
- downstream impact.

Known signatures:

- `Runtime.OutOfMemory`: peak memory equals the configured limit. A merge may successfully
  concatenate shards and then die during transformation/output. Raising memory is a short-term
  mitigation; chunked/streaming aggregation is the durable fix.
- `[Errno 28] No space left on device`: Lambda `/tmp` exhausted, sometimes followed by a
  timeout. Increase ephemeral storage or stream/clean intermediate data.
- `Status: timeout`: the invocation hit its configured timeout; do not infer success from
  earlier progress messages.
- SSM `TooManyUpdates` on `PutParameter`: concurrent cursor writes; transient for the service,
  but the affected item was not processed. Recommend serialization, backoff, or idempotency.

### 5. Inspect ECS/Fargate launches and results

For every in-scope scheduled ECS target that was due, and every Euclidean ECS task actually
launched during the window, verify both launch and task outcome:

1. Check its EventBridge rule `Invocations` / `FailedInvocations`.
2. Query all CloudTrail `RunTask` events within the window, retain Euclidean clusters/task
   definitions/startedBy values, and recover task ARNs. This catches Step Functions and
   manual launches too. CloudTrail is required because stopped tasks may no longer appear
   in `ecs list-tasks`.
3. Also list RUNNING and STOPPED tasks on each discovered cluster and call `ecs describe-tasks`.
4. Report `createdAt`, `startedAt`, `stoppedAt`, `lastStatus`, `stopCode`, `stoppedReason`, and
   every essential container's `exitCode` and `reason`.
5. Read the task definition's `awslogs` configuration, then inspect the corresponding
   CloudWatch log stream for output counts and the root error.

Classify exit code 0 as healthy only when expected output evidence exists. A due rule with
failed delivery or no corresponding `RunTask` is failed/missed. Clearly note when AWS task
retention prevents outcome verification rather than guessing.

### 6. Inspect scheduled Step Functions

For every due Euclidean state-machine target, use `stepfunctions list-executions` and
`describe-execution`. For failed, timed-out, or aborted executions, use
`get-execution-history` to identify the failed state, error, cause, and Lambda/ECS child.
Report successful executions too, including start/stop time. Avoid double-counting child
Lambda failures in totals; link them to the parent execution.

### 7. Reconcile outputs and schedules

Cross-check these sources before reporting:

- schedule due/not due;
- EventBridge target delivery;
- Lambda metrics and REPORT lines;
- ECS task/container outcome;
- Step Functions execution status;
- expected S3/output evidence from logs.

CloudWatch metrics can lag several minutes. If the report ends close to a scheduled firing,
label the job `pending/metric lag possible` and inspect logs before declaring it missed.

### 8. Determine required reruns

Make an explicit rerun decision for every failed, missed, degraded, partial, or stale run.
Do not equate an error metric with a required rerun: correlate all retries, final output,
the immutable current manifest, and downstream timing first.

A rerun is required when the processing window ended without a complete trustworthy output,
including when:

- the final attempt failed, timed out, or was missed;
- a shard set or merge remained incomplete;
- a successful handler published materially partial, stale, or unusable data;
- a fix deployed after the run must be exercised before a downstream job's next firing; or
- a recovered upstream output arrived only after a downstream consumer ran, so that consumer
  must also be rerun.

A rerun is not required when a later retry completed the same work, promoted a complete
current manifest, and every relevant downstream consumer either used that output or will
use it at its next firing. Warnings for individual unavailable/delisted symbols do not by
themselves require a full rerun when the final artifact is complete.

For each required rerun, report:

- target name and target type;
- original run ID and UTC attempt time;
- concrete reason and affected output/current pointer;
- exact rerun scope and order, including prerequisite jobs and downstream consumers;
- whether to use the same event or a fresh run ID;
- the deadline before the next dependent schedule; and
- readiness state: `ready now`, `wait for prerequisite`, or `blocked`.

Respect immutable dataset semantics. Never recommend rerunning only a merge against a sealed
run when its bytes would change. For a sharded immutable dataset, specify a fresh run ID and
the complete shard-then-merge sequence unless an exact idempotent retry is valid.

### 9. Include manifest-backed AI data-quality reviews

Read current immutable dataset manifests beneath
`s3://euclidean-pipeline-954976294836/pyData/Intermediate/_runs/<dataset>/`.
Discover datasets with `list-objects-v2` using the `_runs/` prefix and `/` delimiter; do not
maintain a static dataset list. For each dataset, read `current.json`, then the exact
`manifest_key` it identifies, and verify `manifest_sha256` before trusting the contents.
Only include manifests whose `created_at` falls inside the report window.

Extract the top-level `quality.ai` response and every sharded response at
`shards[].quality.ai`. Report the AI status, mode, model, verdict, confidence, summary, and
each anomaly's file, code, severity, evidence text, and cited `evidence_ids`. Group healthy
shards compactly, but show every warning, failure, unavailable review, invalid citation, or
manifest-integrity problem explicitly. AI reviews are advisory unless their recorded mode is
`enforce`: do not describe an advisory AI `fail` as a blocked publication, but do classify it
as a data-quality issue requiring review. Distinguish `disabled` from `unavailable`.

Do not send raw dataset rows elsewhere during reporting. The manifest already contains the
pseudonymized evidence that was sent to the LLM; quote only the minimum evidence necessary
to explain an anomaly.

## Reporting format

Lead with an overall status and a **Real failures** section; never bury failures below healthy
tables. Immediately follow it with **Reruns required**. List every run that must be rerun in
dependency order with its scope, reason, readiness, and deadline. If none require rerunning,
write `None — all failed attempts recovered or produced complete outputs.` Do not leave this
section implicit.

Then provide compact tables grouped by:

1. Universe
2. EDGAR/data ingress
3. Market data (subgroup large fleets such as CRSP, 13F, IBES, and options)
4. ECS/Fargate and Step Functions
5. Other invoked Euclidean functions

Immediately after real infrastructure failures, include an **AI data quality** section with
pass/warn/fail/unavailable/disabled counts. A non-pass response must include its dataset/run,
shard when applicable, confidence, concise summary, and cited anomaly evidence. Include this
section in both interactive reports and the scheduled SNS/email report.

Each invoked or due target gets a row with target name, target type, scheduled/actual UTC
time, invocation/attempt count, status, duration/memory or ECS exit code, and concrete result.
Large healthy shard fleets may use one summary row only if it explicitly says every shard ran
and provides a concise per-shard count list. Failed or missing shards always get individual
rows.

Put non-due monthly/quarterly jobs in a short **Expected no-runs** section grouped by cadence;
do not flood the main tables with them. End with **Action items** ranked by severity:

1. full-failure daily jobs or failed orchestration/output;
2. missed schedules or failed EventBridge delivery;
3. partial/backlogged work and dangerous memory/disk headroom;
4. transient races and data-quality warnings.

Always quote concrete counts and affected output keys. Distinguish raw shard artifacts from a
successful final merged product. Include the same **Reruns required** section in both the
interactive report and the scheduled SNS/email report.
