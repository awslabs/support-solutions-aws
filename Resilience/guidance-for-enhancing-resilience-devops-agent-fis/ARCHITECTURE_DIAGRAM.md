# FIS Recommender MCP - Architecture Diagram

## Primary Flow (DevOps Agent → AgentCore)

```
┌──────────────────────────────────────────────────────────────────┐
│                         DevOps Agent                             │
│  (Native MCP Client - Recommended)                               │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             │ HTTPS + OAuth (Cognito)
                             │ MCP Protocol (Streamable HTTP)
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│              AWS Cognito User Pool (us-east-1)                   │
│  Pool: <COGNITO_USER_POOL_ID>                                       │
│  Domain: fismcp.auth.us-east-1.amazoncognito.com                 │
│  Flow: OAuth 2.0 + PKCE                                          │
└────────────────────────────┬─────────────────────────────────────┘
                             │ Bearer Token
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│         Bedrock AgentCore Runtime (us-east-1)                    │
│  ARN: arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:         │
│       runtime/fisRecommender2-FrGh02GmrK                         │
│                                                                  │
│  ┌────────────────────────────────────────────────────────┐     │
│  │              MCP Server (server.py)                    │     │
│  │                                                        │     │
│  │  Tools:                                               │     │
│  │  ┌──────────────────────────────────────────────┐     │     │
│  │  │ recommend_fis_experiments                    │     │     │
│  │  │ Input: {"finding": {"summary": "..."}}       │     │     │
│  │  │ Output: FIS action recommendations           │     │     │
│  │  └──────────────────────────────────────────────┘     │     │
│  │                                                        │     │
│  │  ┌──────────────────────────────────────────────┐     │     │
│  │  │ create_fis_template                          │     │     │
│  │  │ Input: recommendation + target details       │     │     │
│  │  │ Output: FIS template ID + ARN                │     │     │
│  │  └──────────────────────────────────────────────┘     │     │
│  └────────────────────────────────────────────────────────┘     │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             │ boto3
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                    AWS FIS Service                               │
│  CreateExperimentTemplate API                                    │
└──────────────────────────────────────────────────────────────────┘
```

## Optional Flow (Lambda → AgentCore)

```
┌──────────────────────────────────────────────────────────────────┐
│              External Trigger                                    │
│  (API Gateway / EventBridge / Manual Invoke)                     │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│         AWS Lambda (us-east-1)                                   │
│  Function: fis-recommender-mcp-client                            │
│  Runtime: Python 3.13                                            │
│  Timeout: 120s (2 min)                                           │
│                                                                  │
│  ┌────────────────────────────────────────────────────────┐     │
│  │  1. Get Cognito token (InitiateAuth)                  │     │
│  │  2. Call MCP server via streamablehttp_client         │     │
│  │  3. Return result                                     │     │
│  └────────────────────────────────────────────────────────┘     │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             │ MCP Client SDK
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│         Bedrock AgentCore Runtime                                │
│  (Same as above)                                                 │
└──────────────────────────────────────────────────────────────────┘
```

## Component Details

### DevOps Agent
- **Type**: Native MCP client
- **Protocol**: MCP over Streamable HTTP
- **Auth**: OAuth 2.0 with PKCE
- **Advantages**: No timeout, streaming, lower latency

### Lambda Client
- **Type**: MCP client wrapper
- **Use Case**: Non-MCP systems
- **Limitations**: 15-min timeout, cold starts
- **When to Use**: API Gateway, EventBridge, legacy systems

### AgentCore Runtime
- **Region**: us-east-1
- **Protocol**: MCP
- **Transport**: Streamable HTTP
- **Session**: Stateless with Mcp-Session-Id header

### Authentication
- **Provider**: AWS Cognito
- **Method**: OAuth 2.0 Authorization Code + PKCE
- **Token**: JWT Bearer token (1 hour expiry)
- **Refresh**: USER_PASSWORD_AUTH flow

## Network Flow

```
Request:
POST https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/{ARN}/invocations
Headers:
  Authorization: Bearer {token}
  Content-Type: application/json
Body:
  {
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "recommend_fis_experiments",
      "arguments": {"finding": {"summary": "network latency"}}
    }
  }

Response:
  {
    "jsonrpc": "2.0",
    "result": {
      "content": [{
        "type": "text",
        "text": "{\"recommendations\": [...]}"
      }]
    }
  }
```
