# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Network-context capture + pre-restore drift validation.

While a resource is hibernated its captured network context (security groups, subnet,
EIP) can drift — a SG or subnet may be deleted by other automation. Restoring blindly
either fails hard (run_instances throws) or silently lands the instance in the wrong
network. This module validates the captured context against the live account BEFORE
restore and returns a reconciliation plan instead of failing silently.
"""
import boto3


def validate_restore_network(config, ec2_client=None):
    """Check that a saved EC2 config's network context still exists.

    Returns a dict:
      {
        "ok": bool,                       # True if nothing drifted
        "drift": [str, ...],              # human-readable drift findings
        "reconciled": {                   # a params patch the caller can apply
            "SecurityGroupIds": [...],    # only surviving SGs (may be [])
            "SubnetId": <id or None>,     # None if the subnet is gone
            "drop_private_ip": bool,      # drop static IP if subnet changed/gone
        }
      }
    Read-only; performs no mutations.
    """
    ec2 = ec2_client or boto3.client("ec2")
    drift = []
    saved_sgs = list(config.get("security_group_ids") or [])
    saved_subnet = config.get("subnet_id") or None

    # Security groups
    surviving_sgs = saved_sgs
    if saved_sgs:
        try:
            resp = ec2.describe_security_groups(GroupIds=saved_sgs)
            found = {g["GroupId"] for g in resp.get("SecurityGroups", [])}
        except Exception:
            # describe with an invalid id raises; fall back to a filtered lookup.
            found = set()
            try:
                resp = ec2.describe_security_groups(
                    Filters=[{"Name": "group-id", "Values": saved_sgs}])
                found = {g["GroupId"] for g in resp.get("SecurityGroups", [])}
            except Exception as e:
                drift.append(f"could not verify security groups: {e}")
        missing = [s for s in saved_sgs if s not in found]
        if missing:
            drift.append(f"security group(s) no longer exist: {', '.join(missing)}")
            surviving_sgs = [s for s in saved_sgs if s in found]

    # Subnet
    subnet_ok = True
    if saved_subnet:
        try:
            resp = ec2.describe_subnets(SubnetIds=[saved_subnet])
            subnet_ok = bool(resp.get("Subnets"))
        except Exception:
            subnet_ok = False
        if not subnet_ok:
            drift.append(f"subnet {saved_subnet} no longer exists")

    reconciled = {
        "SecurityGroupIds": surviving_sgs,
        "SubnetId": saved_subnet if subnet_ok else None,
        # If the subnet is gone, a captured static private IP is meaningless.
        "drop_private_ip": (not subnet_ok) and bool(config.get("private_ip")),
    }
    return {"ok": not drift, "drift": drift, "reconciled": reconciled}
