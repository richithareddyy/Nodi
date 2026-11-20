"""Run an agent-only MCP client against an already-issued visa."""
import argparse
import asyncio
import os
import sys

from mcp import Client, StdioServerParameters


async def run(request_id: str, purchase: bool):
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "nodi.mcp_server"],
        env={"NODI_AGENT_TOKEN": os.environ["NODI_AGENT_TOKEN"],
             "NODI_GATEWAY_URL": os.environ.get("NODI_GATEWAY_URL", "http://127.0.0.1:8000")},
    )
    action = {"request_id": request_id, "visa_id": os.environ["NODI_VISA_ID"],
              "tool": "purchase" if purchase else "search_products",
              "resource": "vendor:approved" if purchase else "catalog:monitors",
              "arguments": {"product_id": "monitor-pro", "quantity": 1} if purchase else {"query": "monitor"}}
    async with Client(server) as client:
        result = await client.call_tool("execute_action", {"action": action})
        print(result.model_dump_json(indent=2))
        if result.is_error:
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-id", required=True, help="Reuse this exact ID and action when retrying")
    parser.add_argument("--purchase", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.request_id, args.purchase))
