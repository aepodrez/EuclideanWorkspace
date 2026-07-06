#!/usr/bin/env bash
# Release a repo's resources from its Terraform Cloud workspace after they've
# been imported into CloudFormation: `terraform state rm` every managed resource
# (NON-destructive — the AWS resources stay), then disable auto-apply + lock the
# workspace so no run can recreate them. Data-source entries are left in state.
# Usage: release_tfc.sh <WorkspaceName>
set -euo pipefail

WS="$1"
export TF_PLUGIN_CACHE_DIR=/home/vscode/.tf-plugin-cache
mkdir -p "$TF_PLUGIN_CACHE_DIR"
TOKEN=$(python3 -c "import json;print(json.load(open('/home/vscode/.terraform.d/credentials.tfrc.json'))['credentials']['app.terraform.io']['token'])")

WD="/home/vscode/tfwork/$WS"; rm -rf "$WD"; mkdir -p "$WD"; cd "$WD"
printf 'terraform {\n  cloud {\n    organization = "euclidean"\n    workspaces { name = "%s" }\n  }\n}\n' "$WS" > main.tf
terraform init -input=false >/dev/null

MANAGED=$(terraform state list 2>/dev/null | grep -v '^data\.' || true)
if [ -n "$MANAGED" ]; then
  echo "$MANAGED" | tr '\n' '\0' | xargs -0 terraform state rm -ignore-remote-version >/dev/null
  echo "state rm: released $(echo "$MANAGED" | wc -l) managed resources"
else
  echo "state rm: no managed resources (already released)"
fi
echo "remaining managed in state: $(terraform state list 2>/dev/null | grep -vc '^data\.' || echo 0)"

WSID=$(curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/vnd.api+json" \
  "https://app.terraform.io/api/v2/organizations/euclidean/workspaces/$WS" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['id'])")
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/vnd.api+json" \
  "https://app.terraform.io/api/v2/workspaces/$WSID" \
  -d '{"data":{"type":"workspaces","attributes":{"auto-apply":false}}}' >/dev/null
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/vnd.api+json" \
  "https://app.terraform.io/api/v2/workspaces/$WSID/actions/lock" \
  -d '{"reason":"Migrated to AWS CDK/CloudFormation; workspace retired"}' >/dev/null || true
echo "$WS ($WSID): auto-apply disabled + locked"
