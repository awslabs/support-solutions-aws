# ============================================================
# FILE_NAME: mcp_remote_test.py
# AUTHOR: vsharmro
# DATE: 2026-04-12
# VERSION: 1.0
# PURPOSE: Syncs aio.run, prompts user for input config store., tests run. uses HTTP client, OAuth.
# DEPENDENCIES: asyncio, base64, config_store, json, mcp, os, urllib
# EXPORTS: get_fis_token, run_test, main
# ISSUE : None
# ============================================================

import asyncio
import base64
import json
import os
import urllib.request
import urllib.parse
import config_store
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


# ============================================================
# NAME: get_fis_token
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: client_id, client_secret, token_url, scope
# PURPOSE: Parses source code, using OAuth.
# CALLED BY: main
# ISSUE : None
# ============================================================
def get_fis_token(client_id, client_secret, token_url, scope):
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
# NAME: run_test
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PARAMETERS: mcp_url, headers
# PURPOSE: Iterates over items, outputs to stdout.
# CALLED BY: main
# ISSUE : None
# ============================================================
async def run_test(mcp_url, headers):
    print(f"\nConnecting to: {mcp_url}\n")
    async with streamablehttp_client(mcp_url, headers, timeout=120, terminate_on_close=False) as (
        read_stream, write_stream, _,
    ):

        async with ClientSession(read_stream, write_stream) as session:

            await session.initialize()
            tools = await session.list_tools()
            print("Available tools:")
            for tool in tools.tools:
                print(f"  - {tool.name}: {tool.description}")
            print("\nTesting recommend_fis_experiments...")

            result = await session.call_tool(
                "recommend_fis_experiments",
                arguments={"finding": {"summary": "network latency spike", "type": "NETWORK_ISSUE"}},
            )
            print(f"Result: {result.content[0].text}")


# ============================================================
# NAME: main
# TYPE: function
# AUTHOR: vsharmro
# DATE: 2026-04-12
# PURPOSE: Retrieves environment/system info, with error handling, outputs to stdout, using Bedrock, MCP server, OAuth.
# CALLED BY: <module>
# ISSUE : None
# ============================================================
def main():
    agent_arn = os.getenv("AGENT_ARN")
    bearer_token = os.getenv("BEARER_TOKEN")

    if agent_arn and bearer_token:
        print("Using AGENT_ARN and BEARER_TOKEN from environment.")
    else:
        print("=" * 60)
        print("FIS MCP Server — Remote Test")
        print("=" * 60)
        print()
        agent_arn = config_store.prompt("Agent ARN", "agent_arn")
        client_id = config_store.prompt("FIS Client ID", "client_id")
        client_secret = config_store.prompt("FIS Client Secret", "client_secret")
        token_url = config_store.prompt("Token URL", "exchange_url")
        scope = config_store.prompt("Scope", "fis_scope", "default-fis-resource-server/read")

        if not all([agent_arn, client_id, client_secret, token_url]):
            print("❌ All values are required.")
            return

        print("\n🔑 Fetching FIS token..."
)
        try:
            bearer_token = get_fis_token(client_id, client_secret, token_url, scope)
            print("✅ Token obtained")
        except Exception as e:
            print(f"❌ Token exchange failed: {e}")
            return


    region = agent_arn.split(":")[3]
    encoded_arn = agent_arn.replace(":", "%3A").replace("/", "%2F")
    mcp_url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    headers = {"authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"}
    asyncio.run(run_test(mcp_url, headers))

if __name__ == "__main__":
    main()

