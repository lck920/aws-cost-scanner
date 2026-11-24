"""Delete everything create_waste.py made (found by the Project=cost-scanner-demo tag).

Usage: python scripts/cleanup_waste.py [region]
"""
import sys

import boto3

REGION = sys.argv[1] if len(sys.argv) > 1 else "ap-southeast-1"
TAG_FILTER = [{"Name": "tag:Project", "Values": ["cost-scanner-demo"]}]

ec2 = boto3.client("ec2", region_name=REGION)

for v in ec2.describe_volumes(Filters=TAG_FILTER)["Volumes"]:
    ec2.delete_volume(VolumeId=v["VolumeId"])
    print(f"Deleted volume {v['VolumeId']}")

for a in ec2.describe_addresses(Filters=TAG_FILTER)["Addresses"]:
    ec2.release_address(AllocationId=a["AllocationId"])
    print(f"Released Elastic IP {a['PublicIp']} ({a['AllocationId']})")

print("Demo waste cleaned up.")
