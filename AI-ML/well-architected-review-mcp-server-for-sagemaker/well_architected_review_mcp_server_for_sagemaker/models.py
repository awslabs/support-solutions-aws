# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data models for the Well-Architected Review MCP Server for Amazon SageMaker.

These are the structured data shapes the validators and tools build. Tools
return their results as ``mcp.types.CallToolResult`` (human-readable text plus
``structuredContent``); these models describe the structured payload rather
than the MCP result envelope itself.
"""

from pydantic import BaseModel, Field
from typing import Optional


class Finding(BaseModel):
    """A single Well-Architected finding."""

    pillar: str = Field(..., description='Well-Architected pillar name')
    severity: str = Field(..., description='Severity level: HIGH, MEDIUM, or LOW')
    resource: str = Field(..., description='Resource name or identifier')
    check: str = Field(..., description='Check identifier')
    detail: str = Field(..., description='Description of the finding')
    recommendation: str = Field(..., description='Recommended remediation')


class PillarSummary(BaseModel):
    """Summary of findings for a single pillar."""

    HIGH: int = Field(0, description='Number of HIGH severity findings')
    MEDIUM: int = Field(0, description='Number of MEDIUM severity findings')
    LOW: int = Field(0, description='Number of LOW severity findings')


class ResourceSummary(BaseModel):
    """Summary of a SageMaker resource."""

    name: str = Field(..., description='Resource name')
    status: Optional[str] = Field(None, description='Resource status')


class PillarCheck(BaseModel):
    """Description of a single validation check."""

    id: str = Field(..., description='Check identifier')
    description: str = Field(..., description='What this check validates')
