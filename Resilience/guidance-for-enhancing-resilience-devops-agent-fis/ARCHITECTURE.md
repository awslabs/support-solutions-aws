# FIS Recommender MCP - Architecture

## Overview

MCP server deployed on AWS Bedrock AgentCore that recommends and creates AWS FIS (Fault Injection Simulator) experiments based on resilience findings.

## Architecture Diagram

```
┌─────────────────┐
│  DevOps Agent   │ (Option 1 - Direct MCP)
└────────┬────────┘
         │ OAuth (Cognito)
         │ MCP Protocol
         ▼
┌─────────────────────────────────┐
│  Bedrock AgentCore Runtime      │
│  (MCP Server)                   │
│  - recommend_fis_experiments    │
│  - create_fis_template          │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────┐
│   AWS FIS API   │
└─────────────────┘


┌─────────────────┐
│  Lambda Client  │ (Option 2 - for non-MCP clients)
└────────┬────────┘
         │ Cognito Auth
         │ MCP Client SDK
         ▼
┌─────────────────────────────────┐
│  Bedrock AgentCore Runtime      │
└─────────────────────────────────┘
```

## Components

### 1. MCP Server (server.py)
- **Runtime**: Bedrock AgentCore (us-east-1)
- **Protocol**: MCP over Streamable HTTP
- **Tools**:
  - `recommend_fis_experiments`: Maps findings to FIS actions
  - `create_fis_template`: Creates FIS experiment templates via boto3

### 2. Authentication (Cognito)
- **User Pool**: `<COGNITO_USER_POOL_ID>`
- **Client ID**: `<COGNITO_CLIENT_ID>`
- **Domain**: fismcp.auth.us-east-1.amazoncognito.com
- **Flow**: OAuth 2.0 Authorization Code + PKCE

### 3. DevOps Agent (Option 1)
- **Connection**: Direct to AgentCore Gateway
- **Auth**: OAuth via Cognito
- **Protocol**: Native MCP support
- **Benefits**: No timeout limits, streaming, lower latency

### 4. Lambda Client (Option 2)
- **Function**: fis-recommender-mcp-client
- **Runtime**: Python 3.13
- **Use Case**: Non-MCP clients (API Gateway, EventBridge, etc.)
- **Limitation**: 15-minute timeout

## Data Flow

### Recommendation Flow
1. User/Agent → "Recommend FIS experiments for network latency"
2. MCP Server → Lookup mapping (network → aws:network:disrupt-connectivity)
3. Return JSON with action, duration, description

### Template Creation Flow
1. User/Agent → Provide recommendation + target details
2. MCP Server → boto3.client('fis').create_experiment_template()
3. Return template ID and ARN

## Deployment

### MCP Server
```bash
agentcore configure -e server.py --protocol MCP
agentcore launch
```

### Lambda Client
```bash
./deploy_lambda.sh
```

## Configuration

### Environment Variables (Lambda)
- `AGENT_ARN`: AgentCore runtime ARN
- `COGNITO_CLIENT_ID`: OAuth client ID
- `COGNITO_USERNAME`: Cognito user
- `COGNITO_PASSWORD`: Cognito password

### IAM Permissions
- Lambda: `cognito-idp:InitiateAuth`
- MCP Server: `fis:CreateExperimentTemplate`

## Endpoints

### AgentCore Gateway
```
https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/{ENCODED_ARN}/invocations
```

### OAuth
- **Token**: `https://fismcp.auth.us-east-1.amazoncognito.com/oauth2/token`
- **Authorize**: `https://fismcp.auth.us-east-1.amazoncognito.com/oauth2/authorize`

## Design Decisions

### Why Direct DevOps Agent Connection?
- ✅ No Lambda timeout constraints
- ✅ Native MCP protocol support
- ✅ Lower latency (one less hop)
- ✅ Streaming responses
- ✅ Lower cost

### When to Use Lambda?
- Non-MCP clients (REST APIs, EventBridge)
- Custom pre/post processing
- Rate limiting requirements
- Legacy system integration
