import os
import json
import re
from mcp.server.fastmcp import FastMCP

# Default to 127.0.0.1 for local development security.
# AgentCore sets AWS_EXECUTION_ENV at runtime; detect it to bind 0.0.0.0 for proxy access.
if os.environ.get("AWS_EXECUTION_ENV") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    SERVER_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
else:
    SERVER_HOST = os.environ.get("MCP_HOST", "127.0.0.1")

# AgentCore direct_code_deploy routes traffic through an internal proxy that sets
# a non-standard Host header (e.g., cell01.us-west-2.prod.arp.kepler-analytics.aws.dev).
# Uvicorn rejects these by default. This setting is safe because the runtime is not
# directly internet-facing — all traffic passes through AgentCore's authenticated gateway.
os.environ.setdefault("UVICORN_FORWARDED_ALLOW_IPS", "*")

mcp = FastMCP(host=SERVER_HOST, stateless_http=True)

MAX_INPUT_SIZE = 10240  # 10KB
MAX_DURATION_MINUTES = 60
ROLE_ARN_PATTERN = re.compile(r"^arn:aws:iam::\d{12}:role/[\w+=,.@-]+$")
STOP_CONDITION_ARN_PATTERN = re.compile(r"^arn:aws:cloudwatch:[\w-]+:\d{12}:alarm:[\w+=,.@/-]+$")

ALLOWED_ACTIONS = {
    "aws:network:disrupt-connectivity",
    "aws:rds:reboot-db-instances",
    "aws:rds:failover-db-cluster",
    "aws:ec2:stop-instances",
    "aws:ec2:reboot-instances",
    "aws:ec2:send-spot-instance-interruptions",
    "aws:ecs:stop-task",
    "aws:eks:pod-delete",
    "aws:lambda:invocation-error",
    "aws:lambda:invocation-add-delay",
    "aws:ebs:pause-volume-io",
    "aws:ssm:send-command",
}

ALLOWED_SELECTION_MODES = re.compile(r"^COUNT\(\d+\)$|^PERCENT\(\d+\)$")

FINDING_MAPPINGS = {
    "network": {"action": "aws:network:disrupt-connectivity", "duration": "PT5M", "description": "Test network resilience"},
    "latency": {"action": "aws:network:disrupt-connectivity", "duration": "PT10M", "description": "Inject network latency"},
    "database": {"action": "aws:rds:reboot-db-instances", "duration": "PT2M", "description": "Test database failover"},
    "cpu": {"action": "aws:ec2:stop-instances", "duration": "PT3M", "description": "Test auto-scaling"},
    "lambda": {"action": "aws:lambda:invocation-error", "duration": "PT5M", "description": "Test Lambda error handling"},
}


def _validate_duration(duration: str) -> bool:
    match = re.match(r"^PT(\d+)M$", duration)
    if not match:
        return False
    return int(match.group(1)) <= MAX_DURATION_MINUTES


@mcp.tool()
def recommend_fis_experiments(finding: dict) -> str:
    """Recommend FIS experiments based on findings"""
    try:
        raw = json.dumps(finding)
        if len(raw) > MAX_INPUT_SIZE:
            return json.dumps({"error": "Input too large", "max_bytes": MAX_INPUT_SIZE})

        if not isinstance(finding.get("summary", ""), str):
            return json.dumps({"error": "finding.summary must be a string"})

        text = raw.lower()
        recs = [
            {"action": v["action"], "duration": v["duration"], "description": v["description"]}
            for k, v in FINDING_MAPPINGS.items()
            if k in text
        ]
        return json.dumps({"recommendations": recs, "count": len(recs)}, indent=2)
    except (TypeError, ValueError) as e:
        return json.dumps({"error": f"Invalid input: {str(e)}"})


@mcp.tool()
def create_fis_template(recommendation: dict, target: dict) -> str:
    """Create FIS experiment template in AWS account"""
    import boto3

    try:
        action = recommendation.get("action", "")
        if action not in ALLOWED_ACTIONS:
            return json.dumps({"error": f"Action not allowed: {action}", "allowed": sorted(ALLOWED_ACTIONS)})

        duration = recommendation.get("duration", "")
        if not _validate_duration(duration):
            return json.dumps({"error": f"Invalid duration: {duration}. Use PTxM format, max {MAX_DURATION_MINUTES} minutes"})

        description = recommendation.get("description", "")
        if not description or len(description) > 500:
            return json.dumps({"error": "description is required and must be <= 500 chars"})

        role_arn = target.get("roleArn", "")
        if not ROLE_ARN_PATTERN.match(role_arn):
            return json.dumps({"error": "Invalid roleArn format. Expected arn:aws:iam::<account>:role/<name>"})

        # Ensure roleArn and stopConditionArn belong to the same account
        stop_arn_raw = target.get("stopConditionArn", "")
        if stop_arn_raw and stop_arn_raw.count(":") >= 5:
            role_account = role_arn.split(":")[4]
            stop_account = stop_arn_raw.split(":")[4]
            if role_account != stop_account:
                return json.dumps({"error": "roleArn and stopConditionArn must belong to the same AWS account"})

        selection_mode = target.get("selectionMode", "COUNT(1)")
        if not ALLOWED_SELECTION_MODES.match(selection_mode):
            return json.dumps({"error": f"Invalid selectionMode: {selection_mode}. Use COUNT(n) or PERCENT(n)"})

        tags = target.get("tags", {})
        if not tags:
            return json.dumps({"error": "tags are required to scope the target. Empty tags would match all resources"})

        stop_condition_arn = target.get("stopConditionArn", "")
        if not stop_condition_arn:
            return json.dumps({"error": "stopConditionArn is required. Provide a CloudWatch alarm ARN as a safety guardrail"})
        if not STOP_CONDITION_ARN_PATTERN.match(stop_condition_arn):
            return json.dumps({"error": "Invalid stopConditionArn format. Expected arn:aws:cloudwatch:<region>:<account>:alarm:<name>"})

        template = {
            "description": description[:500],
            "actions": {
                "action1": {
                    "actionId": action,
                    "parameters": {"duration": duration},
                    "targets": {"Instances": "target1"},
                }
            },
            "targets": {
                "target1": {
                    "resourceType": target.get("resourceType", "aws:ec2:instance"),
                    "selectionMode": selection_mode,
                    "resourceTags": tags,
                }
            },
            "stopConditions": [{"source": "aws:cloudwatch:alarm", "value": stop_condition_arn}],
            "roleArn": role_arn,
        }
        fis = boto3.client("fis")
        resp = fis.create_experiment_template(**template)
        return json.dumps(
            {"templateId": resp["experimentTemplate"]["id"], "arn": resp["experimentTemplate"]["arn"]},
            indent=2,
        )
    except (KeyError, TypeError, ValueError) as e:
        return json.dumps({"error": f"Invalid input: {str(e)}"})
    except Exception:
        return json.dumps({"error": "Failed to create FIS template. Check IAM permissions and input parameters."})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
