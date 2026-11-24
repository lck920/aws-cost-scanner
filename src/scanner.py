"""AWS Cost & Unused Resource Scanner.

Runs as a Lambda once a day: scans every enabled region for idle resources,
estimates their monthly cost, saves a JSON report to S3 and emails a summary
via SNS. Deletes nothing unless DRY_RUN=false.
"""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

HOURS_PER_MONTH = 730
# Approximate on-demand USD prices, based on us-east-1. Real prices vary by region
# (Singapore is ~10-30% higher). ponytail: hardcoded table; switch to the AWS Pricing API if accuracy matters.
EBS_PER_GB = {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05}
SNAPSHOT_PER_GB = 0.05
EIP_HOURLY = 0.005
LB_HOURLY = {"application": 0.0225, "network": 0.0225, "gateway": 0.0125, "classic": 0.025}
NAT_HOURLY = 0.045

SNAPSHOT_MAX_AGE = timedelta(days=30)
NAT_IDLE_WINDOW = timedelta(days=7)
GRACE_PERIOD = timedelta(days=7)
IGNORE_TAG = "cost-scanner:ignore"
FLAG_TAG = "cost-scanner:first-flagged"

TITLES = {
    "ebs_volume": "Unattached EBS volumes",
    "elastic_ip": "Unassociated Elastic IPs",
    "snapshot": "EBS snapshots older than 30 days",
    "stopped_instance": "Stopped EC2 instances (EBS cost only)",
    "load_balancer": "Load balancers with no targets",
    "classic_load_balancer": "Classic load balancers with no instances",
    "nat_gateway": "Idle NAT gateways (no traffic for 7 days)",
}


def tags_of(tag_list):
    return {t["Key"]: t["Value"] for t in tag_list or []}


def finding(type_, region, id_, monthly_usd, tags, **detail):
    return {"type": type_, "region": region, "id": id_, "monthly_usd": round(monthly_usd, 2),
            "tags": tags_of(tags), "detail": detail}


# ---------- detectors: each returns a list of findings for one region ----------

def unattached_volumes(s, region, now):
    ec2 = s.client("ec2", region_name=region)
    pages = ec2.get_paginator("describe_volumes").paginate(Filters=[{"Name": "status", "Values": ["available"]}])
    return [finding("ebs_volume", region, v["VolumeId"], v["Size"] * EBS_PER_GB.get(v["VolumeType"], 0.10),
                    v.get("Tags"), size_gib=v["Size"], volume_type=v["VolumeType"])
            for p in pages for v in p["Volumes"]]


def unassociated_eips(s, region, now):
    ec2 = s.client("ec2", region_name=region)
    return [finding("elastic_ip", region, a["AllocationId"], EIP_HOURLY * HOURS_PER_MONTH, a.get("Tags"),
                    public_ip=a["PublicIp"])
            for a in ec2.describe_addresses()["Addresses"] if "AssociationId" not in a]


def old_snapshots(s, region, now):
    ec2 = s.client("ec2", region_name=region)
    pages = ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"])
    # VolumeSize overestimates: snapshots are incremental, so real cost is usually lower.
    return [finding("snapshot", region, sn["SnapshotId"], sn["VolumeSize"] * SNAPSHOT_PER_GB, sn.get("Tags"),
                    size_gib=sn["VolumeSize"], created=sn["StartTime"].date().isoformat())
            for p in pages for sn in p["Snapshots"] if now - sn["StartTime"] > SNAPSHOT_MAX_AGE]


def stopped_instances(s, region, now):
    ec2 = s.client("ec2", region_name=region)
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}])
    out = []
    for p in pages:
        for r in p["Reservations"]:
            for i in r["Instances"]:
                # ponytail: one describe_volumes call per stopped instance; batch it if you have hundreds.
                vols = ec2.describe_volumes(Filters=[{"Name": "attachment.instance-id",
                                                      "Values": [i["InstanceId"]]}])["Volumes"]
                cost = sum(v["Size"] * EBS_PER_GB.get(v["VolumeType"], 0.10) for v in vols)
                out.append(finding("stopped_instance", region, i["InstanceId"], cost, i.get("Tags"),
                                   instance_type=i["InstanceType"], volumes=[v["VolumeId"] for v in vols]))
    return out


def unused_load_balancers(s, region, now):
    elbv2 = s.client("elbv2", region_name=region)
    out = []
    for p in elbv2.get_paginator("describe_load_balancers").paginate():
        for lb in p["LoadBalancers"]:
            arn = lb["LoadBalancerArn"]
            try:
                tgs = elbv2.describe_target_groups(LoadBalancerArn=arn)["TargetGroups"]
            except ClientError as e:
                if e.response["Error"]["Code"] != "TargetGroupNotFound":
                    raise
                tgs = []
            if any(elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"]
                   for tg in tgs):
                continue
            tags = elbv2.describe_tags(ResourceArns=[arn])["TagDescriptions"][0]["Tags"]
            out.append(finding("load_balancer", region, arn, LB_HOURLY.get(lb["Type"], 0.0225) * HOURS_PER_MONTH,
                               tags, name=lb["LoadBalancerName"], lb_type=lb["Type"]))

    elb = s.client("elb", region_name=region)
    for p in elb.get_paginator("describe_load_balancers").paginate():
        for lb in p["LoadBalancerDescriptions"]:
            if lb["Instances"]:
                continue
            name = lb["LoadBalancerName"]
            tags = elb.describe_tags(LoadBalancerNames=[name])["TagDescriptions"][0]["Tags"]
            out.append(finding("classic_load_balancer", region, name, LB_HOURLY["classic"] * HOURS_PER_MONTH,
                               tags, name=name))
    return out


def idle_nat_gateways(s, region, now):
    ec2 = s.client("ec2", region_name=region)
    cw = s.client("cloudwatch", region_name=region)
    out = []
    for p in ec2.get_paginator("describe_nat_gateways").paginate(
            Filter=[{"Name": "state", "Values": ["available"]}]):
        for nat in p["NatGateways"]:
            if now - nat["CreateTime"] < NAT_IDLE_WINDOW:
                continue  # too new to judge
            stats = cw.get_metric_statistics(
                Namespace="AWS/NATGateway", MetricName="BytesOutToDestination",
                Dimensions=[{"Name": "NatGatewayId", "Value": nat["NatGatewayId"]}],
                StartTime=now - NAT_IDLE_WINDOW, EndTime=now, Period=86400, Statistics=["Sum"])
            if sum(d["Sum"] for d in stats["Datapoints"]) == 0:
                out.append(finding("nat_gateway", region, nat["NatGatewayId"], NAT_HOURLY * HOURS_PER_MONTH,
                                   nat.get("Tags"), vpc=nat["VpcId"]))
    return out


DETECTORS = [unattached_volumes, unassociated_eips, old_snapshots, stopped_instances,
             unused_load_balancers, idle_nat_gateways]


def scan_region(region, now):
    s = boto3.session.Session()  # one session per thread: boto3 sessions aren't thread-safe
    findings, errors = [], []
    for detect in DETECTORS:
        try:
            findings += detect(s, region, now)
        except ClientError as e:  # e.g. AccessDenied in one region shouldn't kill the whole scan
            errors.append({"region": region, "check": detect.__name__, "error": str(e)})
    return [f for f in findings if f["tags"].get(IGNORE_TAG) != "true"], errors


def enabled_regions():
    return [r["RegionName"] for r in boto3.client("ec2").describe_regions()["Regions"]]


# ---------- delete mode: tag first, delete only after the grace period ----------

DELETERS = {
    "ebs_volume": lambda r, i: boto3.client("ec2", region_name=r).delete_volume(VolumeId=i),
    "elastic_ip": lambda r, i: boto3.client("ec2", region_name=r).release_address(AllocationId=i),
    "snapshot": lambda r, i: boto3.client("ec2", region_name=r).delete_snapshot(SnapshotId=i),
    "nat_gateway": lambda r, i: boto3.client("ec2", region_name=r).delete_nat_gateway(NatGatewayId=i),
    "load_balancer": lambda r, i: boto3.client("elbv2", region_name=r).delete_load_balancer(LoadBalancerArn=i),
    "classic_load_balancer": lambda r, i: boto3.client("elb", region_name=r).delete_load_balancer(
        LoadBalancerName=i),
    # stopped_instance is deliberately missing: terminating is irreversible, so it's report-only.
}


def add_flag_tag(f, value):
    tag = [{"Key": FLAG_TAG, "Value": value}]
    if f["type"] == "load_balancer":
        boto3.client("elbv2", region_name=f["region"]).add_tags(ResourceArns=[f["id"]], Tags=tag)
    elif f["type"] == "classic_load_balancer":
        boto3.client("elb", region_name=f["region"]).add_tags(LoadBalancerNames=[f["id"]], Tags=tag)
    else:
        boto3.client("ec2", region_name=f["region"]).create_tags(Resources=[f["id"]], Tags=tag)


def act(f, now, dry_run):
    if dry_run:
        return "none (dry run)"
    if f["type"] not in DELETERS:
        return "report-only"
    try:
        flagged = f["tags"].get(FLAG_TAG)
        if not flagged:
            add_flag_tag(f, now.date().isoformat())
            return f"flagged, deletes after {(now + GRACE_PERIOD).date()}"
        due = datetime.fromisoformat(flagged).replace(tzinfo=timezone.utc) + GRACE_PERIOD
        if now < due:
            return f"pending, deletes after {due.date()}"
        DELETERS[f["type"]](f["region"], f["id"])
        return "deleted"
    except (ClientError, ValueError) as e:  # e.g. snapshot still used by an AMI
        return f"error: {e}"


# ---------- report ----------

def format_email(report):
    lines = [f"ESTIMATED MONTHLY WASTE: ${report['total_monthly_usd']:.2f}",
             f"Scanned {report['regions_scanned']} regions in {report['duration_s']}s · "
             f"Mode: {'DRY RUN (nothing deleted)' if report['dry_run'] else 'DELETE'}", ""]
    for type_, title in TITLES.items():
        items = [f for f in report["findings"] if f["type"] == type_]
        if not items:
            continue
        lines.append(f"── {title} ({len(items)}) ── ${sum(f['monthly_usd'] for f in items):.2f}/mo")
        for f in items:
            detail = " ".join(f"{k}={v}" for k, v in f["detail"].items())
            lines.append(f"  {f['region']}  {f['id']}  {detail}  ${f['monthly_usd']:.2f}  [{f['action']}]")
        lines.append("")
    if report["errors"]:
        lines.append(f"── Errors ({len(report['errors'])}) ──")
        lines += [f"  {e['region']} {e['check']}: {e['error']}" for e in report["errors"]]
        lines.append("")
    lines += ["Prices are approximate (us-east-1 on-demand), not your actual bill.",
              f"Full report: s3://{report['s3_location']}"]
    return "\n".join(lines)


def handler(event=None, context=None):
    started = time.time()
    now = datetime.now(timezone.utc)
    dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"  # anything but "false" stays safe
    bucket = os.environ["REPORT_BUCKET"]

    regions = enabled_regions()
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda r: scan_region(r, now), regions))
    findings = sorted((f for fs, _ in results for f in fs), key=lambda f: -f["monthly_usd"])
    errors = [e for _, es in results for e in es]
    for f in findings:
        f["action"] = act(f, now, dry_run)

    key = f"reports/{now:%Y-%m-%dT%H%M%SZ}.json"
    report = {
        "scanned_at": now.isoformat(), "dry_run": dry_run, "regions_scanned": len(regions),
        "duration_s": round(time.time() - started, 1),
        "total_monthly_usd": round(sum(f["monthly_usd"] for f in findings), 2),
        "s3_location": f"{bucket}/{key}", "findings": findings, "errors": errors,
    }
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=json.dumps(report, indent=2, default=str),
                                  ContentType="application/json")
    boto3.client("sns").publish(
        TopicArn=os.environ["TOPIC_ARN"],
        Subject=f"AWS waste report: ${report['total_monthly_usd']:.2f}/month across {len(findings)} resources",
        Message=format_email(report))
    log.info(json.dumps({"findings": len(findings), "total": report["total_monthly_usd"], "errors": len(errors)}))
    return {"findings": len(findings), "monthly_waste_usd": report["total_monthly_usd"],
            "dry_run": dry_run, "report": f"s3://{bucket}/{key}"}
