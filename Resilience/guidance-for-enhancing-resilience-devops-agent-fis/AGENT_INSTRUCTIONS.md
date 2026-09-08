# FIS Recommender MCP - Agent Instructions

## Available Tools

### recommend_fis_experiments
Get AWS FIS experiment recommendations based on issues or findings.

**When to use**: User asks for FIS experiments, chaos engineering recommendations, or resilience testing suggestions.

**Input**: `{"finding": {"summary": "<description of the issue>"}}`

**Supported keywords**: network, latency, database, cpu, lambda

**Example**:
- User: "Recommend FIS experiments for network latency"
- Call: `recommend_fis_experiments({"finding": {"summary": "network latency"}})`

### create_fis_template
Create an FIS experiment template in AWS account.

**When to use**: User wants to create/deploy an FIS experiment.

**Input**:
```json
{
  "recommendation": {
    "action": "aws:network:disrupt-connectivity",
    "duration": "PT5M",
    "description": "Test network resilience"
  },
  "target": {
    "resourceType": "aws:ec2:instance",
    "selectionMode": "COUNT(1)",
    "tags": {"Environment": "staging"},
    "roleArn": "arn:aws:iam::ACCOUNT:role/FISRole"
  }
}
```

**Note**: Ask user for roleArn and target details if not provided.

## Usage Flow

1. User describes a resilience concern → call `recommend_fis_experiments`
2. User wants to create experiment → call `create_fis_template` with recommendation + target
