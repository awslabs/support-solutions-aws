# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Constants for the Well-Architected Review MCP Server for Amazon SageMaker."""

from typing import Literal, TypeAlias


# Well-Architected Pillars
PILLAR_SECURITY = 'Security'
PILLAR_RELIABILITY = 'Reliability'
PILLAR_PERFORMANCE = 'Performance Efficiency'
PILLAR_COST = 'Cost Optimization'
PILLAR_OPS_EXCELLENCE = 'Operational Excellence'
PILLAR_SUSTAINABILITY = 'Sustainability'

ALL_PILLARS = [
    PILLAR_SECURITY,
    PILLAR_RELIABILITY,
    PILLAR_PERFORMANCE,
    PILLAR_COST,
    PILLAR_OPS_EXCELLENCE,
    PILLAR_SUSTAINABILITY,
]

# Severity levels
SEVERITY_HIGH = 'HIGH'
SEVERITY_MEDIUM = 'MEDIUM'
SEVERITY_LOW = 'LOW'

# Resource types
RESOURCE_ENDPOINT = 'endpoint'
RESOURCE_TRAINING_JOB = 'training_job'
RESOURCE_NOTEBOOK = 'notebook_instance'
RESOURCE_MODEL = 'model'

RESOURCE_TYPES: TypeAlias = Literal[
    'endpoint',
    'training_job',
    'notebook_instance',
    'model',
]

# Validation operations
VALIDATE_OPERATION = 'validate'
LIST_OPERATION = 'list'
PILLAR_INFO_OPERATION = 'pillar_info'

VALIDATION_OPERATIONS: TypeAlias = Literal[
    'validate',
    'validate_all',
    'list',
    'pillar_info',
]

SUPPORTED_REGIONS: TypeAlias = Literal[
    'ap-northeast-1',
    'ap-south-1',
    'ap-southeast-1',
    'ap-southeast-2',
    'ca-central-1',
    'eu-central-1',
    'eu-north-1',
    'eu-west-1',
    'eu-west-2',
    'sa-east-1',
    'us-east-1',
    'us-east-2',
    'us-west-1',
    'us-west-2',
]
