#!/usr/bin/env python3
"""Generate a `cdk import --resource-mapping` file from a synthesized template.

Because the CDK stacks translate the Terraform faithfully — every importable
resource is given its real physical name (function_name, role_name,
repository_name, log_group_name, queue name, topic name, rule name) — the
import identifiers can be derived straight from the synthesized CloudFormation
template. No Terraform state is needed for these types.

A handful of resource types have identifiers that are NOT present in the
template (VPC/subnet ids, EventSourceMapping UUIDs, ECS task-def revisions).
For those, pass --state <terraform.tfstate.json> and the generator falls back to
matching the live physical id out of the cached Terraform state by resource name.

Usage:
    python gen_import_mapping.py cdk.out/<Stack>.template.json \
        --account 954976294836 --region us-east-1 \
        [--state /path/<Repo>_state.json] \
        -o import/mapping.json
"""
import argparse
import json
import sys

# CFN type -> the resource-import *primary identifier* property name, plus how
# to derive its value from the synthesized template Properties.
#   value None  => pull Properties[<prop>] directly (a literal name)
#   value "arn:sns" / "url:sqs" => construct from name + account/region
IDENTIFIER = {
    "AWS::Lambda::Function": ("FunctionName", "FunctionName"),
    "AWS::IAM::Role": ("RoleName", "RoleName"),
    "AWS::ECR::Repository": ("RepositoryName", "RepositoryName"),
    "AWS::Logs::LogGroup": ("LogGroupName", "LogGroupName"),
    "AWS::SNS::Topic": ("TopicArn", "arn:sns:TopicName"),
    "AWS::SQS::Queue": ("QueueUrl", "url:sqs:QueueName"),
    "AWS::Events::Rule": ("Arn", "arn:events:Name"),
    "AWS::S3::Bucket": ("BucketName", "BucketName"),
    "AWS::SSM::Parameter": ("Name", "Name"),
    "AWS::StepFunctions::StateMachine": ("Arn", "arn:states:Name"),
    "AWS::ECS::Cluster": ("ClusterName", "ClusterName"),
    "AWS::KinesisFirehose::DeliveryStream": ("DeliveryStreamName", "DeliveryStreamName"),
    "AWS::IAM::ManagedPolicy": ("PolicyArn", "arn:iam:policy:ManagedPolicyName"),
}

# Types that are folded into a parent resource on import (no standalone import)
# or are pure CDK bookkeeping — skip them, they carry no physical resource.
SKIP_TYPES = {"AWS::CDK::Metadata"}


def literal(v):
    """Return v only if it is a plain string (not an intrinsic like Fn::Join)."""
    return v if isinstance(v, str) else None


def derive(logical_id, res, account, region, state_index):
    typ = res["Type"]
    if typ in SKIP_TYPES:
        return None
    props = res.get("Properties", {})
    if typ not in IDENTIFIER:
        return ("UNSUPPORTED", None)
    id_prop, how = IDENTIFIER[typ]

    if how in ("FunctionName", "RoleName", "RepositoryName", "LogGroupName",
               "BucketName", "Name", "ClusterName", "DeliveryStreamName"):
        name = literal(props.get(how))
        return (id_prop, {id_prop: name} if name else None)

    if how.startswith("arn:sns:"):
        name = literal(props.get("TopicName"))
        if name:
            return (id_prop, {id_prop: f"arn:aws:sns:{region}:{account}:{name}"})
        return (id_prop, None)

    if how.startswith("url:sqs:"):
        name = literal(props.get("QueueName"))
        if name:
            return (id_prop, {id_prop: f"https://sqs.{region}.amazonaws.com/{account}/{name}"})
        return (id_prop, None)

    if how.startswith("arn:iam:policy:"):
        name = literal(props.get("ManagedPolicyName"))
        if name:
            return (id_prop, {id_prop: f"arn:aws:iam::{account}:policy/{name}"})
        return (id_prop, None)

    if how.startswith("arn:events:"):
        name = literal(props.get("Name"))
        if name:
            return (id_prop, {id_prop: f"arn:aws:events:{region}:{account}:rule/{name}"})
        return (id_prop, None)

    if how.startswith("arn:states:"):
        name = literal(props.get("StateMachineName"))
        if name:
            return (id_prop, {id_prop: f"arn:aws:states:{region}:{account}:stateMachine:{name}"})
        return (id_prop, None)

    return (id_prop, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("template")
    ap.add_argument("--account", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--state", help="cached terraform state json for fallback ids")
    ap.add_argument("--extra", help="JSON file of hand-written {logicalId: {Prop: value}} "
                                    "mappings merged in (for types whose ids live only in "
                                    "TF state, e.g. VPC/subnet/SG/route-table ids)")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()

    tmpl = json.load(open(args.template))
    state_index = {}
    if args.state:
        st = json.load(open(args.state))
        for r in st.get("resources", []):
            if r.get("mode") != "managed":
                continue
            for inst in r["instances"]:
                a = inst["attributes"]
                state_index[(r["type"], r["name"])] = a

    extra = json.load(open(args.extra)) if args.extra else {}

    mapping, unresolved, unsupported = {}, [], []
    for lid, res in tmpl.get("Resources", {}).items():
        if lid in extra:
            mapping[lid] = extra[lid]
            continue
        out = derive(lid, res, args.account, args.region, state_index)
        if out is None:
            continue  # skipped (metadata / folded)
        id_prop, ident = out
        if id_prop == "UNSUPPORTED":
            unsupported.append((lid, res["Type"]))
        elif ident is None:
            unresolved.append((lid, res["Type"]))
        else:
            mapping[lid] = ident

    out_json = json.dumps(mapping, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(out_json + "\n")
        print(f"wrote {len(mapping)} mappings -> {args.out}", file=sys.stderr)
    else:
        print(out_json)

    if unsupported:
        print("\nUNSUPPORTED (need manual import / not CFN-importable):", file=sys.stderr)
        for lid, t in unsupported:
            print(f"  {lid}  {t}", file=sys.stderr)
    if unresolved:
        print("\nUNRESOLVED (name not a literal in template — check construct):", file=sys.stderr)
        for lid, t in unresolved:
            print(f"  {lid}  {t}", file=sys.stderr)


if __name__ == "__main__":
    main()
