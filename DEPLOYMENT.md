# Deployment: CI/CD and Terraform Auto-Apply

This describes how code changes become running infrastructure across the four
Euclidean repos (`AlphaModel`, `DataIngressModel`, `EuclideanInfra`,
`UniverseModel`) and this outer workspace. There is no manual deploy step for
any of them — a push to `main` is the entire deploy trigger.

## The two-track model

Every repo with infrastructure runs **two independent pipelines** off the same
push, and both must succeed for a change to actually take effect:

1. **GitHub Actions (code track)** — builds container images / packages Lambda
   zips and pushes the *artifact* (ECR image tag, S3 zip). It never touches
   AWS resource definitions (IAM roles, schedules, ECR repos themselves).
2. **Terraform Cloud (infra track)** — a Terraform Cloud workspace is
   VCS-connected directly to each repo (no `cloud {}`/backend block exists in
   any repo's `.tf` files — the connection lives entirely in Terraform Cloud's
   own workspace settings). Push to `main` triggers an auto-plan, and
   auto-apply is enabled, so the plan applies without a human clicking
   anything.

These two tracks are unordered relative to each other. That has one sharp
edge, described below.

## The bootstrap race (first deploy of a new Lambda)

Lambda resources are defined with `image_uri = "<ecr-repo>:latest"`. If
Terraform Cloud's apply runs *before* CI has ever pushed an image to that ECR
repo, `terraform apply` fails outright — you cannot create a Lambda function
against a tag that doesn't exist yet. This is a known, expected failure mode
the first time a new Lambda/image is introduced (e.g. when the IBES Lambda
image was added in this session): push once, let CI build+push the image,
then re-run (or wait for) the next Terraform Cloud apply. After that first
successful apply, every subsequent push updates the same function in place and
the race no longer applies (the function already exists; CI's
`update-function-code` call keeps it warm independent of Terraform).

## Per-repo breakdown

### DataIngressModel

**GitHub Actions** (`.github/workflows/deploy-ecs.yml`, one job, several steps):
- Builds and pushes 3 ECS Fargate images: `Shipping/ECS/data/Dockerfile`
  (`euclidean/data-ingress-data`), `Shipping/ECS/refinitiv/Dockerfile`
  (`euclidean/data-ingress-refinitiv`), `Shipping/ECS/predictors/Dockerfile`
  (`euclidean/data-ingress-predictors`).
- Builds and pushes the **market-data Lambda image** (`euclidean-market-data`):
  stages a curated list of `DataDownloads/*.py` + `utils/*.py` + `config.py`
  into `lambdas/market_data/` as the build context, builds, pushes, then loops
  over every `euclidean-md-*` Lambda function calling
  `aws lambda update-function-code` (tolerant of `ResourceNotFoundException`
  for functions Terraform hasn't created yet).
- Builds and pushes the **IBES Lambda image** (`euclidean-ibes`): same
  stage/build/push/refresh pattern, scoped to the 5 IBES scripts.
- Builds and pushes the **daily-predictor Lambda image**
  (`euclidean-predictors`): stages `Predictors/`, `utils/`, `SignalMasterTable.py`,
  `config.py`; pushes only if the ECR repo already exists (Terraform Cloud owns
  creating it, CI can't); refreshes all 67 `euclidean-pred-*` functions.
- Registers new ECS task-definition revisions for the 3 Fargate task families
  (`euclidean-data-ingress-{downloads,compustat-annual,compustat-quarterly,refinitiv,predictors}`)
  pointing at the freshly pushed image digests.

**Terraform Cloud**: creates/updates the ECR repos, the ~90 Lambda function
resources across `market_data_lambdas.tf`, `ibes_lambdas.tf`,
`predictor_lambdas.tf`, `edgar_ai_worker.tf`, `edgar_ai_aggregator.tf`,
`edgar_filing_poller.tf`; their IAM roles; every EventBridge schedule; the ECS
task definitions/execution+task roles; SQS queues + DLQs + redrive policies
(`sqs.tf`, `edgar_fanout.tf`).

### AlphaModel

Small repo, single Lambda. `.github/workflows/deploy-ecs.yml` (name is a
holdover, it does not touch ECS) builds the root `Dockerfile` → pushes to
`euclidean/alpha-model` → calls `aws lambda update-function-code` on the one
function. Terraform (`lambda.tf`, `ecr.tf`, `iam.tf`) owns the function
resource, its role, and the ECR repo. There is no EventBridge schedule here —
this Lambda is invoked by the monthly decision Step Function
(`EuclideanInfra/terraform/step_function.tf`), not on a cron of its own.

*(Note: this repo also has an abandoned `Shipping/ECS/` subtree from an
earlier planned ECS deployment target that was never wired to any Terraform
resource or CI step — removed as dead code; see git history.)*

### UniverseModel

Two Lambdas, both **zip-packaged** (`archive_file` data source +
`filename`/`source_code_hash` on `aws_lambda_function`), not container images.
This means there is **no CI build step at all** for these — Terraform Cloud
zips the Python source directly from the repo checkout and uploads it on
`terraform apply`. Push to `main` → Terraform Cloud applies → new code is live.
Both functions are scheduled daily 05:30 UTC via `universe_lambda.tf`'s
`aws_cloudwatch_event_rule`/`_target`.

*(Note: this repo previously also had an ECS/Fargate task definition + a
`build-and-push-ecr.yml` CI workflow for it, with no EventBridge/Step Function
ever invoking that task — confirmed fully orphaned and removed.)*

### EuclideanInfra

No Lambdas or containers of its own — this is the shared substrate: VPC,
subnets, security groups, the shared ECS cluster (`aws_ecs_cluster.main`), the
monthly decision Step Function (`step_function.tf`), and cross-repo scheduled
ECS RunTask triggers for task definitions that live in sibling repos (e.g.
`usaspending_ecs.tf`, `ibes_eps_ecs.tf` — both reference DataIngressModel's
task families by naming convention: `${var.project_name}-<name>${local.env_suffix}`,
and grant `iam:PassRole`/`ecs:RunTask` scoped to that ARN pattern). No CI
pipeline exists here — it's Terraform-only, auto-applied on push.

## Cross-repo naming convention

Every repo constructs AWS resource names the same way:
`${var.project_name}-<resource>${local.env_suffix}`, where `env_suffix` is
empty for prod and `-<environment>` for any other `DEPLOY_ENVIRONMENT`. This is
what lets `EuclideanInfra` (and the Step Function inside it) reference Lambda
ARNs and ECS task families defined in sibling repos purely by string
construction — there is no Terraform remote-state data source linking the
repos together. **This is also the sharp edge from the memory note on
cross-repo SSM-parameter ordering**: shared values (S3 bucket name/ARN, VPC id,
subnet ids, security group id, SNS topic ARN) are published by
`EuclideanInfra` as SSM parameters and read by the other repos via
`data "aws_ssm_parameter"` — if `EuclideanInfra`'s apply hasn't run yet when a
sibling repo's apply runs, the sibling's plan can fail or read a stale value.
There's no ordering guarantee between the four Terraform Cloud workspaces.

## This outer workspace repo

`/workspaces/EuclideanWorkspace` itself is a separate git repo
(`git@github.com:aepodrez/EuclideanWorkspace.git`) with the four repos above
checked out as git submodules under `Euclidean/`. It has no CI/CD and no
Terraform of its own — it's purely the local dev container + cross-cutting
reference docs (this file included). Committing here does not trigger any
deploy; the submodule pointers only move when someone explicitly bumps them.
