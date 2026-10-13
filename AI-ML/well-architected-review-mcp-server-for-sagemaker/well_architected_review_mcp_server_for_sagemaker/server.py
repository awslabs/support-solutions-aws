# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Well-Architected Review MCP Server for Amazon SageMaker implementation.

This module implements the Well-Architected Review MCP Server for Amazon SageMaker, which provides tools
for validating Amazon SageMaker resources against all six AWS Well-Architected Framework
pillars through the Model Context Protocol (MCP).

Environment Variables:
    AWS_REGION: AWS region to use for AWS API calls
    AWS_PROFILE: AWS profile to use for credentials
    FASTMCP_LOG_LEVEL: Log level (default: WARNING)
"""

import argparse
import os
import sys
from well_architected_review_mcp_server_for_sagemaker.wa_validation_handler import (
    WellArchitectedValidationHandler,
)
from loguru import logger
from mcp.server.fastmcp import FastMCP


SERVER_INSTRUCTIONS = """
# Well-Architected Review MCP Server for Amazon SageMaker

This MCP server validates SageMaker workloads against all six AWS Well-Architected Framework pillars:
Security, Reliability, Performance Efficiency, Cost Optimization, Operational Excellence, and Sustainability.

## IMPORTANT: Use MCP Tools for SageMaker Validation

Always use the MCP tools provided by this server for Well-Architected validation of SageMaker resources.

## Available MCP Tools

### 1. Resource Validation: `validate_sagemaker_resource`
**Primary tool for validating individual SageMaker resources**

Validates a single resource (endpoint, training job, notebook instance, or model)
against all six Well-Architected pillars and returns findings with severity levels
and actionable recommendations.

### 2. Batch Endpoint Validation: `validate_all_endpoints`
**Validate all endpoints in a region at once**

Scans all SageMaker endpoints in a region and returns aggregated findings.

### 3. Resource Discovery: `list_sagemaker_resources`
**Discover SageMaker resources available for validation**

Lists endpoints, training jobs, notebook instances, and models in a region.

### 4. Pillar Reference: `get_pillar_details`
**Get information about a specific Well-Architected pillar**

Returns the pillar description and all validation checks performed.

## Common Workflows

### 1. Validate a specific endpoint
```
list_sagemaker_resources(region_name='us-east-1')
validate_sagemaker_resource(resource_type='endpoint', resource_name='my-endpoint', region_name='us-east-1')
```

### 2. Audit all endpoints in a region
```
validate_all_endpoints(region_name='us-east-1')
```

### 3. Validate a training job
```
validate_sagemaker_resource(resource_type='training_job', resource_name='my-training-job')
```

### 4. Review pillar checks
```
get_pillar_details(pillar='security')
```

## Best Practices

- **Start with discovery**: Use `list_sagemaker_resources` to find resources before validating
- **Regional awareness**: Specify the correct region for all operations
- **Prioritize findings**: Address HIGH severity findings first, then MEDIUM, then LOW
- **Regular audits**: Run `validate_all_endpoints` periodically to catch configuration drift

## Important Notes

- All operations are **read-only** — no resources are modified
- Use `--allow-sensitive-data-access` flag to enable access to detailed resource configurations
- Findings include actionable recommendations aligned with AWS best practices
"""

SERVER_DEPENDENCIES = [
    'pydantic',
    'loguru',
    'boto3',
]

# Global reference to the MCP server instance for testing purposes
mcp = None


def create_server(transport: str = 'stdio'):
    """Create and configure the MCP server instance.

    Args:
        transport: The MCP transport that will be used to run the server.
            'stdio' (default) requires no network configuration — the client
            spawns this process and communicates over stdin/stdout.
            'streamable-http' binds to a host/port for network-reachable
            deployments such as Amazon Bedrock AgentCore Runtime, which
            requires the server to listen on 0.0.0.0:8000 and expose POST /mcp
            in stateless mode.
    """
    server_kwargs = {
        'instructions': SERVER_INSTRUCTIONS,
        'dependencies': SERVER_DEPENDENCIES,
    }

    if transport == 'streamable-http':
        # Required by Amazon Bedrock AgentCore Runtime's MCP protocol contract:
        # host 0.0.0.0, port 8000, stateless HTTP so the platform-issued
        # Mcp-Session-Id header is accepted rather than rejected.
        server_kwargs['host'] = '0.0.0.0'
        server_kwargs['port'] = 8000
        server_kwargs['stateless_http'] = True

    return FastMCP(
        'well-architected-review-mcp-server-for-sagemaker',
        **server_kwargs,
    )


def main():
    """Run the MCP server with CLI argument support."""
    global mcp

    # Configure loguru logging
    logger.remove()
    logger.add(sys.stderr, level=os.getenv('FASTMCP_LOG_LEVEL', 'WARNING'))

    parser = argparse.ArgumentParser(
        description='A Model Context Protocol (MCP) server for SageMaker Well-Architected validation'
    )
    parser.add_argument(
        '--allow-sensitive-data-access',
        action=argparse.BooleanOptionalAction,
        default=False,
        help='Enable sensitive data access (required for reading detailed resource configurations)',
    )
    parser.add_argument(
        '--transport',
        choices=['stdio', 'streamable-http'],
        default='stdio',
        help=(
            "MCP transport to use. 'stdio' (default) is for local IDE/CLI use "
            "(e.g. Kiro, Cursor) where the client spawns this process directly. "
            "'streamable-http' is for hosted environments such as Amazon Bedrock "
            'AgentCore Runtime, which requires a network-reachable server.'
        ),
    )

    args = parser.parse_args()
    allow_sensitive_data_access = args.allow_sensitive_data_access

    mode_info = []
    if not allow_sensitive_data_access:
        mode_info.append('restricted sensitive data access mode')

    mode_str = ' in ' + ', '.join(mode_info) if mode_info else ''
    logger.info(
        f'Starting Well-Architected Review MCP Server for Amazon SageMaker{mode_str} '
        f'(transport={args.transport})'
    )

    # Create the MCP server instance
    mcp = create_server(transport=args.transport)

    # Initialize handler — all tools are registered, access control is handled within
    WellArchitectedValidationHandler(mcp, allow_sensitive_data_access)

    # Run server
    mcp.run(transport=args.transport)

    return mcp


if __name__ == '__main__':
    main()
