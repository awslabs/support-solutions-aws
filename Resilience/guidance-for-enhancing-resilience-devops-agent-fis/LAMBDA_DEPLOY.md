# Deploy Lambda to Call AgentCore MCP

## Quick Deploy

```bash
python deploy_lambda.py
```

The script prompts for values (auto-populated from `.fis_config.json` if you ran `setup_cognito_fis.py` first).

## What It Does

1. Creates IAM role `fis-mcp-lambda-role` with:
   - Lambda execution permissions
   - SNS publish access
   - Secrets Manager read access (scoped to `fis-mcp/*`)
2. Stores client secret in AWS Secrets Manager (`fis-mcp/fis-recommender-mcp-client/cognito-client-secret`)
3. Packages Lambda with MCP dependencies (cross-compiled for Linux)
4. Creates/updates Lambda function with environment variables

## Test

```bash
echo '{"tool":"recommend_fis_experiments","arguments":{"finding":{"summary":"network latency"}}}' > payload.json
aws lambda invoke --function-name fis-recommender-mcp-client --region us-east-1 --payload fileb://payload.json response.json
python -c "import json; print(json.dumps(json.load(open('response.json')), indent=2))"
```

## Environment Variables

Set in Lambda (by `deploy_lambda.py`):
- `AGENT_ARN`: AgentCore runtime ARN
- `COGNITO_CLIENT_ID`: Cognito app client ID
- `COGNITO_CLIENT_SECRET_ARN`: Secrets Manager ARN (secret retrieved at runtime, not stored as plaintext)
- `COGNITO_TOKEN_URL`: Cognito token endpoint
- `COGNITO_SCOPE`: OAuth scope (default: `default-fis-resource-server/read`)
- `SNS_TOPIC_ARN`: (optional) SNS topic for notifications

## Security

- Client secret is stored in AWS Secrets Manager, not as a plaintext Lambda env var
- Lambda retrieves the secret at runtime via `secretsmanager:GetSecretValue`
- Only `recommend_fis_experiments` tool is allowed (allowlist in Lambda handler)
- Token URL is validated against Cognito endpoint pattern before sending credentials

## Cleanup

```bash
python cleanup.py
```
