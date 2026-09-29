# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Configuration loader -- parses YAML config and auto-detects settings.
"""

import os
from typing import Optional
from .core.models import Config
from .core.scanner import ScanError


def load_config(
    config_path: Optional[str] = None,
    region: Optional[str] = None,
    profile: Optional[str] = None,
    role_arn: Optional[str] = None,
    output_format: Optional[str] = None,
    output_file: Optional[str] = None,
    dry_run: bool = False,
    verbose: bool = False,
    previous: Optional[str] = None,
) -> Config:
    """Load config from YAML file or create with defaults + CLI overrides."""
    config = Config()

    if config_path and os.path.exists(config_path):
        try:
            import yaml
            with open(config_path, "r") as f:
                data = yaml.safe_load(f) or {}

            config.mode = data.get("mode", "auto")
            config.workload_type = data.get("workload_type", "general")
            config.launch_date = data.get("launch_date")
            config.regions = data.get("regions", ["us-east-1"])
            config.production_estimate = data.get("production_estimate", {})
            config.custom_weights = data.get("custom_weights")
            config.severity_overrides = data.get("severity_overrides", [])
            config.premium_model_markers = data.get("premium_model_markers")
            config.legacy_model_markers = data.get("legacy_model_markers")
            config.diagram = data.get("diagram")
            config.diagram_model_id = data.get("diagram_model_id")

            output_cfg = data.get("output", {})
            config.output_format = output_cfg.get("format", "html")
            config.output_file = output_cfg.get("file", "readiness-report.html")
            config.include_fix_templates = output_cfg.get("include_fix_templates", True)
        except ImportError:
            print("PyYAML not installed -- using defaults. Install: pip install pyyaml")
        except Exception as e:
            print(f"Error reading config: {e} -- using defaults")

    if region:
        config.regions = [r.strip() for r in region.split(",")]
    if profile:
        config.profile = profile
    if role_arn:
        config.role_arn = role_arn
    if output_format:
        config.output_format = output_format
    if output_file:
        config.output_file = output_file
    config.dry_run = dry_run
    config.verbose = verbose
    config.previous_report = previous

    return config


def auto_detect_mode(scan_data: dict) -> str:
    """Auto-detect pre-production vs production from traffic volume."""
    alarms = scan_data.get("cloudwatch", {}).get("alarms", [])
    log_groups = scan_data.get("cloudwatch", {}).get("bedrock_log_groups", [])

    if isinstance(alarms, list) and len(alarms) > 5:
        return "production"
    if isinstance(log_groups, list) and len(log_groups) > 2:
        return "production"
    return "pre-production"


def auto_detect_workload_type(scan_data: dict) -> str:
    """Auto-detect workload type from resources found in the account."""
    kbs = scan_data.get("bedrock", {}).get("knowledge_bases", [])
    runtimes = scan_data.get("agentcore", {}).get("runtimes", [])
    custom_models = scan_data.get("bedrock", {}).get("custom_models", [])
    flows = scan_data.get("bedrock", {}).get("flows", [])

    if not isinstance(runtimes, ScanError) and runtimes:
        return "multi-agent"
    if not isinstance(kbs, ScanError) and kbs:
        return "rag-pipeline"
    if not isinstance(custom_models, ScanError) and custom_models:
        return "fine-tuning"
    if not isinstance(flows, ScanError) and flows:
        return "inference-api"

    return "general"
