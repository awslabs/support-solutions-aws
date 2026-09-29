# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os

from setuptools import setup, find_packages

_here = os.path.abspath(os.path.dirname(__file__))
_readme = os.path.join(_here, "README.md")
_long_description = ""
if os.path.exists(_readme):
    with open(_readme, encoding="utf-8") as fp:
        _long_description = fp.read()

setup(
    name="bedrock-readiness-only",
    version="1.2.0",
    description="Bedrock Readiness (readiness-only) -- Observability, Architecture, "
                 "Quota & Capacity, Cost Optimization, Model Fitness, Security, "
                 "Guardrails, and Data Governance assessment of a live account "
                 "and/or an architecture diagram, using read-only API calls only.",
    long_description=_long_description,
    long_description_content_type="text/markdown",
    author="AWS",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        # Floor, not a pin: several checks call APIs newer than 1.35 --
        # bedrock:GetAccountDataRetention, ListEnforcedGuardrailsConfiguration,
        # ListInferenceProfiles, ListModelInvocationJobs, and
        # xray:GetTraceSegmentDestination. On an older SDK those checks
        # degrade to ERROR (the scan still completes; see AccountScanner._safe_call),
        # but they cannot actually run. >=1.40 is the floor where all of them exist.
        "boto3>=1.40.0",
    ],
    extras_require={
        "yaml": ["pyyaml==6.0.2"],
        "agentcore": ["strands-agents==0.1.5"],
        # MCP server delivery channel (Kiro / Claude Code / Cursor / Q Developer)
        "mcp": ["mcp>=1.2.0"],
    },
    entry_points={
        "console_scripts": [
            "bedrock-readiness=bedrock_readiness.cli:main",
            # Convenience launcher so an MCP client config can point at a
            # command instead of a file path. Equivalent to
            # `bedrock-readiness mcp`.
            "bedrock-readiness-mcp=bedrock_readiness.delivery.mcp_server:mcp.run",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Quality Assurance",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
