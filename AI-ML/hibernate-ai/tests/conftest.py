# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared pytest fixtures for HibernateAI Lambda tests.

The Lambda modules import each other by bare module name (``from rules import ...``),
because at runtime the handler directory is on ``sys.path``. We replicate that here by
inserting ``lambdas/widget`` at the front of ``sys.path`` before any test imports them.

All AWS access is mocked with ``moto`` — no test may touch a real account. A guard
fixture sets bogus credentials so a misconfigured test fails closed instead of
reaching AWS.
"""
import os
import sys
from pathlib import Path

import boto3
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WIDGET_DIR = REPO_ROOT / "lambdas" / "widget"

# Make the Lambda modules importable by bare name, exactly like the Lambda runtime.
sys.path.insert(0, str(WIDGET_DIR))


@pytest.fixture(autouse=True)
def _aws_safety_env(monkeypatch):
    """Fail closed: bogus creds + a fixed region so nothing can reach a real account."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("STATE_TABLE", "HibernateAIState")


@pytest.fixture
def aws(_aws_safety_env):
    """Start moto's mock for the AWS services HibernateAI uses.

    Yields nothing; use the ``ec2``/``rds``/``state_table`` fixtures for clients.
    """
    from moto import mock_aws

    with mock_aws():
        yield


@pytest.fixture
def ec2(aws):
    return boto3.client("ec2", region_name="us-east-1")


@pytest.fixture
def rds(aws):
    return boto3.client("rds", region_name="us-east-1")


@pytest.fixture
def dynamodb(aws):
    return boto3.resource("dynamodb", region_name="us-east-1")


@pytest.fixture
def state_table(dynamodb):
    """Create the HibernateAIState table with the real key schema (project_name, str)."""
    table = dynamodb.create_table(
        TableName="HibernateAIState",
        KeySchema=[{"AttributeName": "project_name", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "project_name", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    return table


@pytest.fixture
def default_vpc(ec2):
    """A VPC + subnet so run_instances works under moto."""
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]
    subnet = ec2.create_subnet(VpcId=vpc["VpcId"], CidrBlock="10.0.1.0/24")["Subnet"]
    return {"vpc_id": vpc["VpcId"], "subnet_id": subnet["SubnetId"]}
