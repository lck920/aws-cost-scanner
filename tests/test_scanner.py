"""Tests run against moto: a fake, in-memory AWS. No real account is touched."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import scanner  # noqa: E402

REGION = "ap-southeast-1"
NOW = datetime.now(timezone.utc)
LATER = NOW + timedelta(days=31)  # lets us "age" resources without waiting


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                 "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield


def ec2():
    return boto3.client("ec2", region_name=REGION)


def scan(now=NOW):
    findings, errors = scanner.scan_region(REGION, now)
    assert errors == []
    return findings


def of_type(findings, t):
    return [f for f in findings if f["type"] == t]


def test_unattached_volume_and_eip():
    vol = ec2().create_volume(AvailabilityZone=f"{REGION}a", Size=10, VolumeType="gp3")["VolumeId"]
    eip = ec2().allocate_address(Domain="vpc")["AllocationId"]
    f = scan()
    assert [(x["id"], x["monthly_usd"]) for x in of_type(f, "ebs_volume")] == [(vol, 0.80)]
    assert [(x["id"], x["monthly_usd"]) for x in of_type(f, "elastic_ip")] == [(eip, 3.65)]


def test_snapshot_flagged_only_when_old():
    vol = ec2().create_volume(AvailabilityZone=f"{REGION}a", Size=20)["VolumeId"]
    snap = ec2().create_snapshot(VolumeId=vol)["SnapshotId"]
    by_id = lambda now: {x["id"]: x for x in of_type(scan(now), "snapshot")}  # moto ships its own AMI snapshots
    assert snap not in by_id(NOW)
    assert by_id(LATER)[snap]["monthly_usd"] == 1.00


def test_stopped_instance_reports_its_volume_cost():
    ami = ec2().describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    stopped, running = [i["InstanceId"] for i in ec2().run_instances(
        ImageId=ami, MinCount=2, MaxCount=2, InstanceType="t3.micro")["Instances"]]
    ec2().stop_instances(InstanceIds=[stopped])
    hits = of_type(scan(), "stopped_instance")
    assert [h["id"] for h in hits] == [stopped]
    assert hits[0]["monthly_usd"] > 0 and hits[0]["detail"]["volumes"]


def test_load_balancers_without_targets():
    vpc = ec2().create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = [ec2().create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24",
                                   AvailabilityZone=f"{REGION}{az}")["Subnet"]["SubnetId"]
               for i, az in enumerate("ab")]
    boto3.client("elbv2", region_name=REGION).create_load_balancer(Name="empty-alb", Subnets=subnets)
    boto3.client("elb", region_name=REGION).create_load_balancer(
        LoadBalancerName="empty-clb", AvailabilityZones=[f"{REGION}a"],
        Listeners=[{"Protocol": "HTTP", "LoadBalancerPort": 80, "InstancePort": 80}])
    f = scan()
    assert of_type(f, "load_balancer")[0]["detail"]["name"] == "empty-alb"
    assert of_type(f, "classic_load_balancer")[0]["id"] == "empty-clb"


def test_idle_nat_gateway_after_window():
    vpc = ec2().create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2().create_subnet(VpcId=vpc, CidrBlock="10.0.0.0/24")["Subnet"]["SubnetId"]
    nat = ec2().create_nat_gateway(SubnetId=subnet, AllocationId=ec2().allocate_address(Domain="vpc")[
        "AllocationId"])["NatGateway"]["NatGatewayId"]
    assert of_type(scan(NOW), "nat_gateway") == []  # too new to judge
    assert [x["id"] for x in of_type(scan(LATER), "nat_gateway")] == [nat]


def test_ignore_tag_skips_resource():
    ec2().create_volume(AvailabilityZone=f"{REGION}a", Size=1, TagSpecifications=[{
        "ResourceType": "volume", "Tags": [{"Key": scanner.IGNORE_TAG, "Value": "true"}]}])
    assert scan() == []


def test_dry_run_never_deletes():
    vol = ec2().create_volume(AvailabilityZone=f"{REGION}a", Size=1)["VolumeId"]
    f = of_type(scan(), "ebs_volume")[0]
    assert scanner.act(f, LATER, dry_run=True) == "none (dry run)"
    assert ec2().describe_volumes(VolumeIds=[vol])["Volumes"]


def test_delete_mode_flags_then_deletes_after_grace_period():
    vol = ec2().create_volume(AvailabilityZone=f"{REGION}a", Size=1)["VolumeId"]
    # day 0: tagged, not deleted
    assert scanner.act(of_type(scan(), "ebs_volume")[0], NOW, dry_run=False).startswith("flagged")
    # day 3: still pending
    assert scanner.act(of_type(scan(), "ebs_volume")[0], NOW + timedelta(days=3), False).startswith("pending")
    assert ec2().describe_volumes(VolumeIds=[vol])["Volumes"]
    # day 8: gone
    assert scanner.act(of_type(scan(), "ebs_volume")[0], NOW + timedelta(days=8), False) == "deleted"
    assert of_type(scan(), "ebs_volume") == []


def test_stopped_instances_are_never_deleted():
    assert scanner.act({"type": "stopped_instance", "tags": {}}, NOW, dry_run=False) == "report-only"


def test_handler_writes_s3_and_publishes(monkeypatch):
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="reports", CreateBucketConfiguration={"LocationConstraint": REGION})
    topic = boto3.client("sns", region_name=REGION).create_topic(Name="t")["TopicArn"]
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setenv("TOPIC_ARN", topic)
    monkeypatch.delenv("DRY_RUN", raising=False)  # unset must mean dry run
    monkeypatch.setattr(scanner, "enabled_regions", lambda: [REGION])
    ec2().allocate_address(Domain="vpc")

    out = scanner.handler()

    assert out["dry_run"] is True and out["findings"] == 1 and out["monthly_waste_usd"] == 3.65
    key = s3.list_objects_v2(Bucket="reports")["Contents"][0]["Key"]
    report = json.loads(s3.get_object(Bucket="reports", Key=key)["Body"].read())
    assert report["findings"][0]["type"] == "elastic_ip"
    email = scanner.format_email(report)
    assert email.startswith("ESTIMATED MONTHLY WASTE: $3.65") and "Unassociated Elastic IPs (1)" in email
