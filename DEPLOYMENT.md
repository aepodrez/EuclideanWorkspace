# Deployment: CI/CD via AWS CDK

This describes how code changes become running infrastructure across the four
Euclidean repos (`AlphaModel`, `DataIngressModel`, `EuclideanInfra`,
`UniverseModel`), the two newer repos (`ExecutionModel`,
`PortfolioConstructionModel`), and this outer workspace. There is no manual
deploy step for any of them — a push to `main` is the entire deploy trigger.

**As of 2026-07-06, all six repos deploy via AWS CDK (Python), not Terraform
Cloud.** Every live resource was adopted into CloudFormation with `cdk import`
(zero recreation); all six Terraform Cloud workspaces are now emptied,
auto-apply-disabled, and locked. See `cdk-migration/` at the workspace root
for the migration tooling (import-mapping generator, import+converge recipe,
TFC-release script) if you ever need to understand how the cutover was done.

## The single-track model (replaces the old two-track TFC design)

Every repo with infrastructure runs **one linear GitHub Actions pipeline** per
push to `main`:

1. **Build** — package the Lambda zip / build and push the container image to
   ECR (unchanged from before).
2. **`cdk deploy`** — synthesizes and applies the repo's `infra/` CDK app,
   passing `-c imageTag=$GITHUB_SHA` so the freshly-pushed image tag flows
   straight into the Lambda/ECS resource definitions in the same step.

Because build and deploy are now sequential steps in the *same* job (instead
of two independently-triggered systems — GitHub Actions and a Terraform Cloud
VCS-connected workspace), **the old "bootstrap race" (a Terraform apply
running before the image existed) can no longer happen.** That failure mode
documented in earlier revisions of this file is obsolete.

## Per-repo CDK app layout (`infra/`)

```
<repo>/infra/
  app.py                 # cdk.App(); instantiates the repo's stack(s)
  cdk.json                # app = "python3 app.py"; context (account/region)
  requirements.txt        # aws-cdk-lib, constructs
  stacks/
    <name>_stack.py       # one Stack subclass per stack
  import/
    mapping.json          # historical: used once for the cdk import cutover
```

`DataIngressModel` is the one exception worth knowing about: it's
**spec-driven**. `infra/specs/*.json` holds the full live estate (extracted
once from the old Terraform state by `infra/tools/extract_specs.py`), and five
stacks loop over those specs instead of hardcoding each resource:

| Stack | CFN stack name | Contents |
|---|---|---|
| `EuclideanDataIngressShared` | `euclidean-data-ingress-shared` | 6 ECR repos, 2 shared IAM roles, ECS log groups, 5 Fargate task defs |
| `EuclideanDataIngressMarketData` | `euclidean-data-ingress-market-data` | ~44 dispatcher Lambdas + their EventBridge schedules |
| `EuclideanDataIngressIbes` | `euclidean-data-ingress-ibes` | 15 IBES Lambdas + schedules |
| `EuclideanDataIngressPredictors` | `euclidean-data-ingress-predictors-lambdas` | 133 predictor Lambdas |
| `EuclideanDataIngressEdgar` | `euclidean-data-ingress-edgar` | poller/worker/aggregator Lambdas, SNS fan-out, SQS queues+DLQs, event-source mappings |

Splitting across 5 stacks keeps each well under CloudFormation's 500-resource
hard cap and shrinks blast radius per deploy. `deploy-ecs.yml`'s final step is
`cdk deploy --all -c imageTag=$GITHUB_SHA`, which updates all five in one CI
run.

**Secrets** (FRED/FINRA/BEA/Refinitiv/OpenRouter API keys) never live in
`specs/*.json` — each is a `{"__env__": "VAR_NAME"}` reference resolved from
`os.environ` at synth time. In CI these come from GitHub Actions secrets; for
a local `cdk diff`/`cdk deploy`, `source infra/specs/secrets.local.env`
(gitignored) first. Synth fails fast with a clear error if a referenced
secret is unset, rather than silently deploying a function with a blanked
credential.

## The CDK bootstrap role trust chain

Every repo's CI runs as the `github-cicd` IAM user (access keys, not yet
migrated to GitHub's OIDC provider — `AWS_ROLE_TO_ASSUME` is defined in the
workflow but unset as a secret in every repo today). For `cdk deploy` and any
synth-time SSM parameter lookup (`ssm.StringParameter.value_from_lookup`) to
work, `github-cicd` must be able to assume the CDK bootstrap roles
(`cdk-hnb659fds-{deploy,file-publishing,image-publishing,lookup,cfn-exec}-role-954976294836-us-east-1`).
That permission is granted by a **standalone `AWS::IAM::ManagedPolicy`**
(`euclidean-github-cicd-cdk-assume-roles`, defined in `EuclideanInfra`'s stack
— `github-cicd`'s *inline* policy budget was already at IAM's 2048-byte cap
from two older policies, hence the separate managed policy). Without this,
every repo's `cdk deploy` fails at synth with `ssm:GetParameter ... not
authorized` or `no credentials have been configured`.

## Per-repo breakdown

### DataIngressModel

**GitHub Actions** (`.github/workflows/deploy-ecs.yml`):
- Builds and pushes 3 ECS Fargate images (`euclidean/data-ingress-data`,
  `euclidean/data-ingress-refinitiv`, `euclidean/data-ingress-predictors`).
- Builds and pushes the **market-data Lambda image** (`euclidean-market-data`):
  stages a curated list of `DataDownloads/*.py` + `utils/*.py` + `config.py`,
  builds, pushes `:latest` + `:$GITHUB_SHA`, then loops over every
  `euclidean-md-*` function calling `aws lambda update-function-code`
  (tolerant of both `ResourceNotFoundException` — function not deployed yet —
  and `AccessDeniedException` — a transient ECR-pull-permission propagation
  race right after a fresh push).
- Same pattern for the **IBES Lambda image** (`euclidean-ibes`) and the
  **daily-predictor Lambda image** (`euclidean-predictors`).
- **`cdk deploy --all -c imageTag=$GITHUB_SHA`** — applies all five CDK
  stacks (see table above), which registers new ECS task-definition revisions
  and wires the newly-pushed image tags into the CDK-managed Lambda resources.

Note the market-data/IBES functions are intentionally pinned to `:latest`
(matching the pre-migration behavior and the parallel
`update-function-code` loop), while the ECS task-definition images and the
predictor Lambdas are pinned to the per-commit `$GITHUB_SHA` tag by the CDK
stacks.

### AlphaModel

Single Lambda. `.github/workflows/deploy-infra.yml` builds the root
`Dockerfile` → pushes to `euclidean/alpha-model` → `cdk deploy
-c imageTag=$GITHUB_SHA`, which points `DockerImageFunction` at the new tag
and updates the function in the same step.

### ExecutionModel

Same shape as AlphaModel: one Lambda, `deploy-infra.yml`, `cdk deploy
-c imageTag=$GITHUB_SHA`.

### PortfolioConstructionModel

One ECS Fargate task, invoked on-demand by the monthly decision Step Function
(not on its own schedule). `deploy-infra.yml` builds the image, then
`cdk deploy -c imageTag=$GITHUB_SHA -c alpaca_api_key=... -c
alpaca_api_secret=...` registers a new task-definition revision under the
same family — CDK deliberately does **not** import the task definition itself
(immutable revisions; each converge creates a fresh one, no data loss, no
disruption to the family other repos reference by name).

### UniverseModel

Two Lambdas, **zip-packaged** (`_lambda.Code.from_asset`), not container
images — no image build step. `deploy-infra.yml` just runs `cdk deploy`,
which zips `lambdas/<name>/` as a CDK asset and uploads it directly, same
effect as Terraform's old `archive_file` + apply.

### EuclideanInfra

The foundation: S3 data bucket, VPC/subnets/NAT/IGW/route-tables, the shared
ECS cluster, the SNS notifications topic, the monthly decision Step Function
(embedded verbatim from the live definition), CloudWatch-Logs→Firehose→S3
archival, the `github-cicd` CDK-assume-roles policy (see above), and the 8
SSM parameters that form the cross-repo contract. `deploy-infra.yml` is a
plain `cdk deploy` — no image to build. **Left deliberately unmanaged in AWS**
(documented in the stack's docstring): the SNS email/SMS subscriptions
(CloudFormation cannot import a confirmed-out-of-band email subscription),
routes + route-table associations, the IGW-to-VPC attachment, the 5 S3
folder-marker objects, and the operator IAM user's policy attachment.

## Cross-repo naming convention

Unchanged from the Terraform era: every repo constructs AWS resource names as
`euclidean-<resource>` (no more `-dev` suffix; single-environment now). This
is what lets `EuclideanInfra`'s Step Function and EventBridge rules reference
Lambda ARNs and ECS task families defined in sibling repos purely by string
construction — there's still no CloudFormation cross-stack reference linking
the repos together, same as there was no Terraform remote-state link before.

**Cross-repo SSM contract** (unchanged in shape, now CDK-native): SSM
`StringParameter`s written by `EuclideanInfra`
(`/euclidean/{s3_bucket_name,s3_bucket_arn,vpc_id,private_subnet_ids,
public_subnet_ids,ecs_cluster_arn,ecs_security_group_id,
sns_notifications_arn}`), read by every other repo's stack via
`ssm.StringParameter.value_from_lookup()`. That lookup is cached in each
repo's `infra/cdk.context.json` (gitignored — re-read fresh on every synth,
so this isn't a stale-cache risk the way a committed lockfile would be).
Because these parameters are the **same physical SSM parameters that existed
under Terraform** (imported, never recreated), the cross-repo contract never
broke during the migration — repos could and did cut over in any order.

## This outer workspace repo

`/workspaces/EuclideanWorkspace` itself is a separate git repo
(`git@github.com:aepodrez/EuclideanWorkspace.git`) with the six repos above
checked out as git submodules under `Euclidean/`. It has no CI/CD of its own —
it's purely the local dev container + cross-cutting reference docs (this file
included) + the one-time `cdk-migration/` tooling. Committing here does not
trigger any deploy; the submodule pointers only move when someone explicitly
bumps them.

## Terraform Cloud (retired, not yet decommissioned)

All six TFC workspaces are emptied (`terraform state rm` on every managed
resource), `auto-apply` disabled, and locked — they cannot apply anything even
if someone pushed to a workspace's connected branch. They have **not** been
deleted, and the TFC subscription has not been cancelled — both are pending,
low-urgency manual steps once you're confident the CDK cutover is fully
stable. The TFC API token lives in `~/.terraform.d/credentials.tfrc.json` in
the dev container; revoke it last, after cancelling the subscription.
