"""Create demo 'waste' for the scanner to find: one unattached 1 GiB volume + one unused Elastic IP.

Costs about $0.005/hour while it exists. Run cleanup_waste.py when done.
Usage: python scripts/create_waste.py [region]
"""
import sys

import boto3

REGION = sys.argv[1] if len(sys.argv) > 1 else "ap-southeast-1"
TAG = {"Key": "Project", "Value": "cost-scanner-demo"}

ec2 = boto3.client("ec2", region_name=REGION)
az = ec2.describe_availability_zones()["AvailabilityZones"][0]["ZoneName"]

vol = ec2.create_volume(AvailabilityZone=az, Size=1, VolumeType="gp3",
                        TagSpecifications=[{"ResourceType": "volume", "Tags": [TAG]}])
print(f"Created volume {vol['VolumeId']} (1 GiB gp3) in {az}")

eip = ec2.allocate_address(Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": [TAG]}])
print(f"Allocated Elastic IP {eip['PublicIp']} ({eip['AllocationId']})")

print("This costs about $0.005/hour. Run: python scripts/cleanup_waste.py")
