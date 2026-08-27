# ============================================================
# FILE_NAME: lambda_function.py
# AUTHOR: vsharmro
# DATE: 2026-04-11
# VERSION: 1.0
# PURPOSE: Provides public functions:get_bearer_token,
#   call_mcp_tool, lambda_handler. Uses async/await for
#   asynchronous operations.
# DEPENDENCIES: asyncio, base64, json, mcp, os, urllib
# EXPORTS: get_bearer_token, call_mcp_tool, lambda_handler
# ISSUE :
#   - No module-level docstring.
# ============================================================
import asyncio
import json
import os
import base64
import urllib.request
import urllib.parse
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


# ============================================================
# NAME: get_bearer_token
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11
# PURPOSE: Parses source code.
# CALLED BY: call_mcp_tool
# ISSUE : None
# ============================================================
def get_bearer_token():
    client_id = os.environ["COGNITO_CLIENT_ID"]
    token_url = os.environ["COGNITO_TOKEN_URL"]
    scope = os.environ.get("COGNITO_SCOPE", "default-fis-resource-server/read")

    import re
    if not re.match(r"^https://[\w-]+\.auth\.[\w-]+\.amazoncognito\.com/oauth2/token$", token_url):
        raise ValueError(f"Invalid token URL: must be a valid Cognito endpoint, got: {token_url}")

    # Retrieve client secret from Secrets Manager
    import boto3
    secret_arn = os.environ.get("COGNITO_CLIENT_SECRET_ARN")
    if secret_arn:
        sm = boto3.client("secretsmanager")
        client_secret = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
    else:
        # Fallback for local testing
        client_secret = os.environ["COGNITO_CLIENT_SECRET"]

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": scope,
    }).encode()

    req = urllib.request.Request(token_url, data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", f"Basic {auth}")

    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())["access_token"]


# ============================================================
# NAME: call_mcp_tool
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11
# PARAMETERS: tool_name, arguments
# PURPOSE: Retrieves environment/system info.
# CALLED BY: lambda_handler
# ISSUE : None
# ============================================================
async def call_mcp_tool(tool_name, arguments):
    agent_arn = os.environ["AGENT_ARN"]
    bearer_token = get_bearer_token()

    region = agent_arn.split(":")[3]
    encoded_arn = agent_arn.replace(":", "%3A").replace("/", "%2F")
    mcp_url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    headers = {"authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}

    async with streamablehttp_client(mcp_url, headers, timeout=120, terminate_on_close=False) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return result.content[0].text


# ============================================================
# NAME: lambda_handler
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-11
# PARAMETERS: event, context
# PURPOSE: Processes JSON data, builds a collection from
#   results.
# ISSUE : None
# ============================================================
def lambda_handler(event, context):
    detail = event.get("detail", {})
    if detail:
        tool_name = "recommend_fis_experiments"
        arguments = {
            "finding": {
                "id": detail.get("investigationId", "unknown"),
                "summary": detail.get("summary", ""),
                "type": detail.get("findingType", ""),
                "description": detail.get("description", ""),
            }
        }
    else:
        ALLOWED_TOOLS = {"recommend_fis_experiments"}
        tool_name = event.get("tool", "recommend_fis_experiments")
        if tool_name not in ALLOWED_TOOLS:
            return {
                "statusCode": 403,
                "body": json.dumps({"error": f"Tool not allowed: {tool_name}", "allowed": sorted(ALLOWED_TOOLS)}),
            }
        arguments = event.get("arguments", {})

    result = asyncio.run(call_mcp_tool(tool_name, arguments))

    sns_topic = os.environ.get("SNS_TOPIC_ARN")
    if sns_topic:
        import boto3
        sns = boto3.client("sns")
        finding_id = arguments.get("finding", {}).get("id", "unknown")
        sns.publish(
            TopicArn=sns_topic,
            Subject=f"FIS Recommendations for {finding_id}",
            Message=result,
        )

    return {
        "statusCode": 200,
        "body": json.dumps({"result": result}),
    }
