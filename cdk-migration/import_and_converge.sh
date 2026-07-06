#!/usr/bin/env bash
# Import a repo's live AWS resources into its CDK CloudFormation stack, then
# converge (apply tags + reconcile). Encodes the proven recipe:
#   - import runs with `-c import_mode=1` so no Tags are in the changeset
#     (CloudFormation import rejects the Tags property)
#   - the follow-up `cdk deploy` (tags on) applies tags + cosmetic reconciles
# Usage:
#   import_and_converge.sh <infra_dir> <StackConstructId> [extra -c context ...]
set -euo pipefail

INFRA_DIR="$1"; STACK_ID="$2"; shift 2
EXTRA_CTX=("$@")

SC=/tmp/claude-1000/-workspaces-EuclideanWorkspace/5c98ea2a-d9bc-4bd4-89a0-c316cce98e23/scratchpad
GEN=/workspaces/EuclideanWorkspace/cdk-migration/gen_import_mapping.py
export PATH="$SC/cdkvenv/bin:$PATH"
export CDK_DEFAULT_ACCOUNT=954976294836 CDK_DEFAULT_REGION=us-east-1
CDK="npx --yes aws-cdk@2"

cd "$INFRA_DIR"
mkdir -p import

echo "### [1/5] synth (import mode: tags suppressed)"
$CDK synth --quiet "${EXTRA_CTX[@]}" -c import_mode=1 >/dev/null

echo "### [2/5] generate import mapping"
python3 "$GEN" "cdk.out/${STACK_ID}.template.json" --account 954976294836 --region us-east-1 -o import/mapping.json

echo "### [3/5] cdk import"
$CDK import "$STACK_ID" --resource-mapping import/mapping.json --force "${EXTRA_CTX[@]}" -c import_mode=1 2>&1 \
  | grep -iE "importing using|import complete|IMPORT_COMPLETE|✅|❌|failed|cannot" || true

echo "### [4/5] converge deploy (tags on)"
$CDK deploy "$STACK_ID" --require-approval never "${EXTRA_CTX[@]}" 2>&1 \
  | grep -iE "UPDATE_COMPLETE .*CloudFormation::Stack|✅|❌|failed|Deployment time" || true

echo "### [5/5] verify diff (expect: no differences)"
$CDK diff "$STACK_ID" "${EXTRA_CTX[@]}" 2>&1 | grep -iE "no differences|differences:|\[~\]|\[-\]|\[+\]" | head -20
