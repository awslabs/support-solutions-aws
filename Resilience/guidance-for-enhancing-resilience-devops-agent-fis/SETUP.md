# FIS Recommender MCP — Detailed Setup Guide

Cross-platform setup instructions for Windows, macOS, and Linux.

## Prerequisites

```bash
pip install bedrock-agentcore-starter-toolkit
pip install -r requirements.txt
```

Verify AWS CLI is configured:

```bash
aws sts get-caller-identity
```

Note: This guide uses `python` in all commands. On macOS/Linux, use `python3` if `python` is not available. Similarly, use `pip3` instead of `pip` if needed.

---

## Option 1: MCP Server for DevOps Agent

### 1. Setup Cognito FIS

```bash
python setup_cognito_fis.py
```

The script prompts for:
- AWS Region (default: us-east-1)
- User Pool name (default: FisMcpPool)
- FIS Client name (default: FisMcpFISClient)
- Cognito domain (default: fismcp)

It creates a Cognito user pool, domain, resource server with `default-fis-resource-server/read` scope, and an FIS app client with `client_credentials` flow.

All values are saved to `.fis_config.json` and printed to the console. Copy the Discovery URL and Client ID for the next step.

### 2. Configure AgentCore

```bash
agentcore configure -e server.py --protocol MCP
```

Prompts and what to enter:
- Agent name: your choice (e.g. `FISRecommender`)
- Execution role: press Enter (auto-create)
- ECR: press Enter (auto-create)
- OAuth: `yes`
- Discovery URL: paste from step 1
- Client ID: paste from step 1
- Audience: press Enter (leave empty — Cognito FIS tokens have no `aud` claim)
- Scopes: `default-fis-resource-server/read`
- Custom claims: press Enter
- Request headers: `no`
- Memory: your choice

### 3. Deploy

```bash
agentcore deploy
```

This builds the Docker image via CodeBuild (ARM64, remote build) and deploys to AgentCore. Takes 2-5 minutes.

The Dockerfile uses `public.ecr.aws/docker/library/python:3.13-slim` to avoid Docker Hub rate limits on CodeBuild.

### 4. Verify

```bash
agentcore status
```

Check runtime logs (replace AGENT_ID with your agent ID from status output):

```bash
# Windows CMD
aws logs tail /aws/bedrock-agentcore/runtimes/AGENT_ID-DEFAULT --log-stream-name-prefix "2026/04/12/[runtime-logs]" --since 10m --region us-east-1

# macOS/Linux
aws logs tail /aws/bedrock-agentcore/runtimes/AGENT_ID-DEFAULT --log-stream-name-prefix "$(date +%Y/%m/%d)/[runtime-logs]" --since 10m --region us-east-1
```

You should see `Uvicorn running on http://0.0.0.0:8000` and `200 OK` on PingRequests.

### 5. Get Registration Values

```bash
python show_registration_values.py
```

Prints the Endpoint URL (with URL-encoded ARN), Client ID, masked Client Secret, Exchange URL, Discovery URL, and scope. Full secret is stored in `.fis_config.json` (not printed to stdout). Also saves the Agent ARN to `.fis_config.json`.

### 6. Register in DevOps Agent Console

1. Go to Capability Providers → MCP Server → Register
2. MCP Server Details:
   - Name: `FIS Recommender`
   - Endpoint URL: from step 5
3. Authorization Flow: **OAuth Client Credentials**
4. Authorization Configuration:
   - Client ID: from step 1 (FIS client)
   - Client Secret: retrieve from `.fis_config.json` (`client_secret` field)
   - Exchange URL: from step 1
   - Scope: `default-fis-resource-server/read`
5. Submit

### 7. Add to Agent Space

1. Select your Agent Space
2. Capabilities tab → MCP Servers → Add
3. Select FIS Recommender
4. Allow tools: `recommend_fis_experiments`, `create_fis_template`
5. Click Add

### 8. Test

Ask DevOps Agent:
```
Recommend FIS experiments for network latency issues
```

---

## Option 2: Lambda + EventBridge (Automatic Pipeline)

### 1. Deploy Lambda

```bash
python deploy_lambda.py
```

Prompts for Agent ARN, Cognito FIS credentials, and optional SNS Topic ARN. Values are auto-populated from `.fis_config.json` if you ran Option 1 first.

The script:
- Creates IAM role with Lambda execution + SNS publish + Secrets Manager read permissions
- Stores client secret in AWS Secrets Manager (not as plaintext env var)
- Installs dependencies for Linux Lambda runtime (cross-compiled from any OS)
- Packages and deploys the Lambda function

Lambda environment variables set:
- `AGENT_ARN` — AgentCore runtime ARN
- `COGNITO_CLIENT_ID` — Cognito app client ID
- `COGNITO_CLIENT_SECRET_ARN` — Secrets Manager ARN (secret retrieved at runtime)
- `COGNITO_TOKEN_URL` — Cognito token endpoint
- `COGNITO_SCOPE` — OAuth scope

### 2. Test Lambda

Write a test payload to a file (avoids Windows JSON escaping issues):

```bash
echo {"tool":"recommend_fis_experiments","arguments":{"finding":{"summary":"network latency"}}} > payload.json
aws lambda invoke --function-name fis-recommender-mcp-client --region us-east-1 --payload fileb://payload.json response.json
python -c "import json; print(json.dumps(json.load(open('response.json')), indent=2))"
```

### 3. Setup EventBridge + SNS

```bash
python setup_eventbridge_cross.py
```

Creates:
- SNS topic for notifications
- EventBridge rule for DevOps Agent investigation events
- IAM role with SNS publish permission
- Generated Lambda code for the EventBridge handler

Optionally subscribes your email to the SNS topic. If skipped, the script prints the command to subscribe later.

### 4. Wire EventBridge to Lambda

Run the commands printed by the script (replace ACCOUNT_ID):

```bash
aws events put-targets --rule DevOpsAgentFISRecommendations --targets "Id=1,Arn=arn:aws:lambda:us-east-1:ACCOUNT_ID:function:fis-recommender-mcp-client" --region us-east-1

aws lambda add-permission --function-name fis-recommender-mcp-client --statement-id eventbridge-invoke --action lambda:InvokeFunction --principal events.amazonaws.com --source-arn arn:aws:events:us-east-1:ACCOUNT_ID:rule/DevOpsAgentFISRecommendations --region us-east-1
```

### 5. Test the Pipeline

You cannot send events with `aws.devops-agent` source from the CLI (AWS-owned source). Temporarily add a test source:

```bash
aws events put-rule --name DevOpsAgentFISRecommendations --event-pattern "{\"source\":[\"aws.devops-agent\",\"test.fis-recommender\"],\"detail-type\":[\"DevOps Agent Investigation Complete\"]}" --region us-east-1
```

Send a test event:

```bash
aws events put-events --entries "[{\"Source\":\"test.fis-recommender\",\"DetailType\":\"DevOps Agent Investigation Complete\",\"Detail\":\"{\\\"investigationId\\\":\\\"test-123\\\",\\\"summary\\\":\\\"Network latency spike\\\",\\\"findingType\\\":\\\"NETWORK_ISSUE\\\",\\\"description\\\":\\\"High latency observed\\\"}\"}]" --region us-east-1
```

Check Lambda logs:

```bash
aws logs tail /aws/lambda/fis-recommender-mcp-client --since 5m --region us-east-1
```

Check your email for the SNS notification.

Remove the test source:

```bash
aws events put-rule --name DevOpsAgentFISRecommendations --event-pattern "{\"source\":[\"aws.devops-agent\"],\"detail-type\":[\"DevOps Agent Investigation Complete\"]}" --region us-east-1
```

---

## Option 3: Local Development

### 1. Start Server

```bash
python server.py
```

### 2. Test Locally

In a new terminal:

```bash
python my_mcp_test.py
```

### 3. Test Deployed Server

```bash
python mcp_remote_test.py
```

Prompts for Agent ARN and FIS credentials. Auto-populated from `.fis_config.json`.

You can also set environment variables to skip prompts:

```bash
# Windows CMD
set AGENT_ARN=arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:runtime/NAME-ID
set BEARER_TOKEN=your-token
python mcp_remote_test.py

# macOS/Linux
export AGENT_ARN="arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:runtime/NAME-ID"
export BEARER_TOKEN="your-token"
python mcp_remote_test.py
```

### 4. Configure MCP Client

For Kiro CLI, add to `~/.kiro/mcp-servers.json`:

```json
{
  "mcpServers": {
    "fis-recommender": {
      "command": "python",
      "args": ["server.py"],
      "env": { "AWS_REGION": "us-east-1" }
    }
  }
}
```

For Claude Desktop, config file location:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

Same JSON content as above.

---

## Troubleshooting

### Check if server is running

```bash
agentcore status
```

### Check runtime logs

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/AGENT_ID-DEFAULT --log-stream-name-prefix "YYYY/MM/DD/[runtime-logs]" --since 10m --region us-east-1
```

### Check if DevOps Agent requests reach the runtime

Run immediately after a registration attempt:

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/AGENT_ID-DEFAULT --since 2m --region us-east-1
```

### Test FIS token exchange

```bash
python -c "
import urllib.request, urllib.parse, base64, json
cid='CLIENT_ID'; cs='CLIENT_SECRET'
auth=base64.b64encode(f'{cid}:{cs}'.encode()).decode()
data=urllib.parse.urlencode({'grant_type':'client_credentials','scope':'default-fis-resource-server/read'}).encode()
req=urllib.request.Request('https://DOMAIN.auth.REGION.amazoncognito.com/oauth2/token',data=data)
req.add_header('Content-Type','application/x-www-form-urlencoded')
req.add_header('Authorization',f'Basic {auth}')
print(json.loads(urllib.request.urlopen(req).read()).get('access_token','FAILED')[:50]+'...')
"
```

### Decode a JWT token

```bash
python -c "
import base64, json
token='PASTE_TOKEN_HERE'
payload=json.loads(base64.b64decode(token.split('.')[1]+'=='))
print(json.dumps(payload, indent=2))
"
```

### Check Lambda logs

```bash
aws logs tail /aws/lambda/fis-recommender-mcp-client --since 5m --region us-east-1
```

### Check SNS subscription status

```bash
aws sns list-subscriptions-by-topic --topic-arn arn:aws:sns:us-east-1:ACCOUNT_ID:devops-agent-fis-recommendations --region us-east-1
```

### Destroy and start fresh

```bash
python cleanup.py
```

Or manually:

```bash
agentcore destroy --agent AGENT_NAME --force
del .bedrock_agentcore.yaml .fis_config.json
```

### CodeBuild Docker Hub rate limit

If `agentcore deploy` fails with `429 Too Many Requests`, the Dockerfile already uses `public.ecr.aws/docker/library/python:3.13-slim` (ECR Public Gallery) to avoid this. If you see this error, retry the deploy.
