# Test Lambda - Create FIS Template

## 1. Get Recommendations
```bash
aws lambda invoke --function-name fis-recommender-mcp-client --region us-east-1 \
  --payload '{"tool":"recommend_fis_experiments","arguments":{"finding":{"summary":"network latency"}}}' \
  response.json && cat response.json
```

## 2. Create FIS Template
```bash
aws lambda invoke --function-name fis-recommender-mcp-client --region us-east-1 \
  --payload '{
    "tool": "create_fis_template",
    "arguments": {
      "recommendation": {
        "action": "aws:network:disrupt-connectivity",
        "duration": "PT5M",
        "description": "Test network resilience"
      },
      "target": {
        "resourceType": "aws:ec2:instance",
        "selectionMode": "COUNT(1)",
        "tags": {"Environment": "staging"},
        "roleArn": "arn:aws:iam::<ACCOUNT_ID>:role/FISExecutionRole"
      }
    }
  }' \
  response.json && cat response.json
```

## Note
Replace `roleArn` with your FIS execution role ARN.
