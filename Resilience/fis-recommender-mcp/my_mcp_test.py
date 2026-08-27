#!/usr/bin/env python3
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    mcp_url = "http://localhost:8000/mcp"

    async with streamablehttp_client(mcp_url, {}, timeout=120, terminate_on_close=False) as (
        read_stream, write_stream, _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # List tools
            tools = await session.list_tools()
            print("Available tools:")
            for tool in tools.tools:
                print(f"  - {tool.name}: {tool.description}")

            # Test recommend_fis_experiments
            print("\nTesting recommend_fis_experiments...")
            result = await session.call_tool(
                "recommend_fis_experiments",
                arguments={"finding": {"summary": "network latency spike", "type": "NETWORK_ISSUE"}},
            )
            print(f"Result:\n{result.content[0].text}")


asyncio.run(main())
