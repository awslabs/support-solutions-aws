# FIS Recommender - AWS Solutions Library Architecture

## Architecture Overview

This solution deploys the FIS Recommender as a remote MCP server on AWS using Amazon Bedrock AgentCore Runtime, enabling customers to deploy directly to their AWS accounts through CloudFormation/CDK templates.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          AWS Account (Customer)                          │
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │                    Amazon Bedrock AgentCore                         │ │
│  │                                                                      │ │
│  │  ┌──────────────────────────────────────────────────────────────┐  │ │
│  │  │              AgentCore Runtime (MCP Server)                   │  │ │
│  │  │                                                                │  │ │
│  │  │  ┌────────────────────────────────────────────────────────┐  │  │ │
│  │  │  │         FIS Recommender MCP Server                      │  │  │ │
│  │  │  │                                                          │  │  │ │
│  │  │  │  • recommend_fis_experiments                            │  │  │ │
│  │  │  │  • create_fis_template                                  │  │  │ │
│  │  │  │  • 50+ finding type mappings                            │  │  │ │
│  │  │  └────────────────────────────────────────────────────────┘  │  │ │
│  │  │                                                                │  │ │
│  │  │  Session Management: Mcp-Session-Id header                    │  │ │
│  │  │  Protocol: Stateless HTTP/HTTPS                               │  │ │
│  │  └──────────────────────────────────────────────────────────────┘  │ │
│  │                                                                      │ │
│  │  ┌──────────────────────────────────────────────────────────────┐  │ │
│  │  │              AgentCore Gateway (Optional)                     │  │ │
│  │  │                                                                │  │ │
│  │  │  • Unified routing                                            │  │ │
│  │  │  • Authentication & authorization                             │  │ │
│  │  │  • Tool management                                            │  │ │
│  │  └──────────────────────────────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │                      MCP Clients                                    │ │
│  │                                                                      │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐ │ │
│  │  │   Kiro CLI   │  │ Claude       │  │  Amazon Q Developer      │ │ │
│  │  │              │  │ Desktop      │  │                          │ │ │
│  │  └──────────────┘  └──────────────┘  └──────────────────────────┘ │ │
│  │                                                                      │ │
│  │  ┌──────────────────────────────────────────────────────────────┐  │ │
│  │  │         Custom Applications (via MCP SDK)                     │  │ │
│  │  └──────────────────────────────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │                    AWS Services Integration                         │ │
│  │                                                                      │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐ │ │
│  │  │   AWS FIS    │  │  CloudWatch  │  │    DevOps Agent          │ │ │
│  │  │              │  │   Alarms     │  │    (Findings Source)     │ │ │
│  │  └──────────────┘  └──────────────┘  └──────────────────────────┘ │ │
│  │                                                                      │ │
│  │  ┌──────────────────────────────────────────────────────────────┐  │ │
│  │  │              AWS Systems Manager (SSM)                        │  │ │
│  │  │              For custom fault injection commands              │  │ │
│  │  └──────────────────────────────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │                    Security & Monitoring                            │ │
│  │                                                                      │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐ │ │
│  │  │   IAM Roles  │  │  CloudWatch  │  │    AWS CloudTrail        │ │ │
│  │  │  & Policies  │  │     Logs     │  │    (Audit Logging)       │ │ │
│  │  └──────────────┘  └──────────────┘  └──────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                           │
└─────────────────────────────────────────────────────────────────────────┘

                              Data Flow
                              ─────────

1. DevOps Agent → Generates operational findings
2. MCP Client → Sends finding to FIS Recommender MCP Server
3. FIS Recommender → Analyzes finding, returns experiment recommendations
4. MCP Client → Requests FIS template generation
5. FIS Recommender → Creates complete FIS experiment template
6. Customer → Deploys FIS experiment to AWS FIS
7. AWS FIS → Executes chaos engineering experiment
8. CloudWatch → Monitors experiment, triggers stop conditions if needed
```

## Key Components

### 1. Amazon Bedrock AgentCore Runtime
- **Purpose**: Managed environment for hosting MCP servers
- **Features**:
  - Automatic session management via Mcp-Session-Id headers
  - Stateless HTTP/HTTPS protocol support
  - Built-in scaling and availability
  - Integrated with AWS security services

### 2. FIS Recommender MCP Server
- **Deployment**: Container on AgentCore Runtime
- **Tools Exposed**:
  - `recommend_fis_experiments`: Analyzes findings, returns recommendations
  - `create_fis_template`: Generates deployment-ready FIS templates
- **Finding Coverage**: 50+ AWS service scenarios across:
  - Network & Connectivity (7 actions)
  - Database & Storage (7 actions)
  - Compute & Instances (6 actions)
  - ECS & Containers (5 actions)
  - EKS & Kubernetes (6 actions)
  - Lambda & Serverless (3 actions)
  - Caching & Streaming (4 actions)
  - API & Throttling (3 actions)
  - Availability & Recovery (3 actions)

### 3. AgentCore Gateway (Optional)
- **Purpose**: Unified control plane for multiple MCP servers
- **Features**:
  - Centralized routing and authentication
  - Tool management across MCP servers
  - Single integration point for clients

### 4. MCP Clients
- **Supported Clients**:
  - Kiro CLI
  - Claude Desktop
  - Amazon Q Developer
  - Custom applications via MCP SDK

### 5. AWS Services Integration
- **AWS FIS**: Target service for experiment deployment
- **CloudWatch**: Monitoring and stop conditions
- **Systems Manager**: Custom fault injection commands
- **CloudTrail**: Audit logging for compliance

## Deployment Options

### Option 1: CloudFormation Template
```yaml
# One-click deployment via CloudFormation
# Provisions AgentCore Runtime, IAM roles, and MCP server
```

### Option 2: AWS CDK
```typescript
// Infrastructure as Code using AWS CDK
// Programmatic deployment with customization
```

### Option 3: Terraform
```hcl
# Multi-cloud IaC support
# Enterprise deployment patterns
```

## Security Features

1. **IAM Role-Based Access Control**
   - Least privilege permissions
   - Service-specific IAM policies
   - Cross-account access support

2. **Encryption**
   - Data in transit: TLS 1.3
   - Data at rest: AWS KMS encryption
   - Secrets management: AWS Secrets Manager

3. **Audit & Compliance**
   - CloudTrail logging for all API calls
   - CloudWatch Logs for MCP server activity
   - VPC endpoint support for private connectivity

4. **Network Security**
   - VPC deployment option
   - Security group controls
   - Private subnet support

## Cost Optimization

- **Pay-per-use**: AgentCore Runtime charges only for active sessions
- **No idle costs**: Automatic scaling to zero when not in use
- **Shared infrastructure**: Multiple clients use same MCP server instance
- **Estimated monthly cost**: $10-50 for typical usage (100-1000 requests/month)

## Well-Architected Alignment

### Operational Excellence
- Automated deployment via IaC
- CloudWatch monitoring and alerting
- Structured logging for troubleshooting

### Security
- IAM-based authentication
- Encryption at rest and in transit
- Audit logging with CloudTrail

### Reliability
- Multi-AZ deployment via AgentCore
- Automatic failover and recovery
- Session isolation for fault tolerance

### Performance Efficiency
- Serverless scaling
- Low-latency HTTP protocol
- Efficient session management

### Cost Optimization
- Pay-per-use pricing model
- No infrastructure management overhead
- Automatic scaling to zero

### Sustainability
- Serverless architecture minimizes carbon footprint
- Efficient resource utilization
- No idle compute resources

## Deployment Steps

1. **Prerequisites**
   - AWS Account with appropriate permissions
   - AWS CLI configured
   - (Optional) AWS CDK or Terraform installed

2. **Deploy via CloudFormation**
   ```bash
   aws cloudformation create-stack \
     --stack-name fis-recommender-mcp \
     --template-url https://s3.amazonaws.com/solutions-library/fis-recommender/template.yaml \
     --capabilities CAPABILITY_IAM
   ```

3. **Configure MCP Client**
   ```json
   {
     "mcpServers": {
       "fis-recommender": {
         "url": "https://<agentcore-endpoint>/fis-recommender",
         "headers": {
           "Authorization": "Bearer <token>"
         }
       }
     }
   }
   ```

4. **Verify Deployment**
   ```bash
   # Test MCP server connectivity
   curl -X POST https://<endpoint>/tools/list
   ```

## Use Cases

1. **DevOps Integration**
   - Automated chaos engineering in CI/CD pipelines
   - Post-deployment resilience validation
   - Continuous testing of failure scenarios

2. **Incident Response**
   - Generate FIS experiments from production incidents
   - Validate fixes with controlled fault injection
   - Build resilience playbooks

3. **Architecture Review**
   - Test multi-region failover
   - Validate disaster recovery procedures
   - Assess blast radius of failures

4. **Compliance & Audit**
   - Document resilience testing
   - Demonstrate chaos engineering practices
   - Track experiment history

## Support & Documentation

- **Implementation Guide**: Detailed deployment instructions
- **API Reference**: Complete MCP tool documentation
- **Sample Code**: GitHub repository with examples
- **Architecture Patterns**: Common deployment scenarios
- **Troubleshooting Guide**: Common issues and solutions

## License

MIT License - Open source and freely available
