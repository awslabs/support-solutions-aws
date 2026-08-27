# FIS Recommender MCP - Agent Runbook

## Prerequisites
- MCP server deployed to Bedrock AgentCore
- Cognito OAuth configured
- Agent registered in DevOps Agent Console

## Agent Configuration

### MCP Server Details
- **Name**: FIS Recommender
- **Endpoint**: `https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A<ACCOUNT_ID>%3Aruntime%2FfisRecommender2-FrGh02GmrK/invocations`
- **Protocol**: MCP over Streamable HTTP

### OAuth Configuration
- **Client ID**: `<COGNITO_CLIENT_ID>`
- **Exchange URL**: `https://fismcp.auth.us-east-1.amazoncognito.com/oauth2/token`
- **Authorization URL**: `https://fismcp.auth.us-east-1.amazoncognito.com/oauth2/authorize`
- **Scopes**: `openid`
- **PKCE**: Enabled
- **Callback URL**: `https://api.prod.cp.aidevops.us-east-1.api.aws/v1/register/mcpserver/callback`

## Available Tools

### 1. recommend_fis_experiments
**Purpose**: Get FIS experiment recommendations based on findings

**Input**:
```json
{
  "finding": {
    "summary": "network latency issues in production"
  }
}
```

**Output**:
```json
{
  "recommendations": [
    {
      "action": "aws:network:disrupt-connectivity",
      "duration": "PT10M",
      "description": "Inject network latency"
    }
  ]
}
```

**Supported Keywords**: network, latency, database, cpu, lambda

### 2. create_fis_template
**Purpose**: Create FIS experiment template in AWS

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

## Agent Usage Examples

### Example 1: Get Recommendations
**User**: "Recommend FIS experiments for network latency issues"

**Agent Action**:
```
Tool: recommend_fis_experiments
Input: {"finding": {"summary": "network latency"}}
```

### Example 2: Create Template
**User**: "Create FIS template to test database failover on staging RDS instances"

**Agent Actions**:
1. Call `recommend_fis_experiments` with `{"finding": {"summary": "database"}}`
2. Call `create_fis_template` with recommendation + target details

## Testing

### Manual Test
```bash
export AGENT_ARN="arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/fisRecommender2-FrGh02GmrK"
export BEARER_TOKEN="<from-cognito>"
python3 mcp_remote_test.py
```

### Expected Response
```json
{
  "tools": [
    {"name": "recommend_fis_experiments", ...},
    {"name": "create_fis_template", ...}
  ]
}
```

## Troubleshooting

### Error: "Session terminated"
- Check bearer token is valid (expires in 1 hour)
- Verify OAuth configuration matches Cognito settings
- Ensure callback URL is registered in Cognito

### Error: "BadRequest"
- Verify endpoint URL format is correct
- Check OAuth is enabled on the runtime
- Confirm region matches (us-east-1)

### Refresh Token
```bash
aws cognito-idp initiate-auth \
  --client-id <COGNITO_CLIENT_ID> \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=<COGNITO_USERNAME>,PASSWORD=<COGNITO_PASSWORD> \
  --region us-east-1 | jq -r '.AuthenticationResult.AccessToken'
```
