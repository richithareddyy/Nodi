"""Agent-only MCP stdio bridge. The HTTP gateway remains the enforcement boundary."""

import asyncio
import json
import os
import re
from typing import Any
from urllib.parse import urlencode

import httpx2
from mcp.server import MCPServer
from pydantic import Field

from .api import ActionBody, StrictModel
from .transport import gateway_origin


class StatusRequest(StrictModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")


class GatewayCallError(RuntimeError):
    """Sanitized transport failure; never invent an authorization decision."""


def execution_consistent(result):
    if type(result.get("executed")) is not bool:
        return False
    if "execution_state" not in result:
        return result["executed"] == (result.get("decision") == "ALLOW")
    state = result["execution_state"]
    return (
        type(state) is str
        and state in ("PENDING", "UNKNOWN", "COMMITTED", "FAILED")
        and result.get("decision") == "ALLOW"
        and result["executed"] == (state == "COMMITTED")
        and type(result.get("outbox_id")) is str
        and re.fullmatch(r"[a-f0-9]{32}", result["outbox_id"]) is not None
    )


class GatewayClient:
    def __init__(self, base_url: str, agent_token: str, *, transport=None):
        origin = gateway_origin(base_url)
        if (
            len(agent_token) < 32
            or not agent_token.isascii()
            or any(c.isspace() for c in agent_token)
        ):
            raise ValueError(
                "a nonempty agent credential of at least 32 ASCII characters is required"
            )
        self.endpoint = origin + "/execute"
        self.status_endpoint = origin + "/requests/status"
        self._token, self._transport = agent_token, transport

    async def execute(self, action: ActionBody) -> dict:
        # Fixed URL and credentials never come from tool arguments. No ambient proxy
        # credentials, redirects, automatic retries, or local tool fallbacks.
        payload = action.model_dump()
        try:
            encoded = json.dumps(payload, allow_nan=False).encode()
        except (ValueError, TypeError):
            raise GatewayCallError("Invalid action JSON; no request sent") from None
        if len(encoded) > 65536:
            raise GatewayCallError("Action exceeds gateway size limit; no request sent")
        try:
            async with asyncio.timeout(10):
                async with httpx2.AsyncClient(
                    transport=self._transport, trust_env=False, follow_redirects=False, timeout=5.0
                ) as client:
                    async with client.stream(
                        "POST",
                        self.endpoint,
                        content=encoded,
                        headers={
                            "Authorization": "Bearer " + self._token,
                            "Content-Type": "application/json",
                        },
                    ) as response:
                        if response.status_code in (401, 403, 413, 422):
                            raise GatewayCallError(
                                f"Gateway rejected request (HTTP {response.status_code}); "
                                f"no execution"
                            )
                        if response.status_code != 200:
                            raise GatewayCallError(
                                "Gateway result unavailable; execution outcome is unknown. "
                                "Retry the same request_id"
                            )
                        data = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=16384):
                            data.extend(chunk)
                            if len(data) > 262144:
                                raise GatewayCallError(
                                    "Gateway response too large; outcome unknown. Retry "
                                    "the same request_id"
                                )
            result = json.loads(data)
            if (
                not isinstance(result, dict)
                or result.get("decision") not in ("ALLOW", "DENY", "ASK_HUMAN")
                or type(result.get("executed")) is not bool
                or not execution_consistent(result)
                or result.get("request_id") != action.request_id
                or result.get("visa_id") != action.visa_id
            ):
                raise ValueError("invalid decision response")
            return result
        except (httpx2.HTTPError, TimeoutError, ValueError, TypeError):
            raise GatewayCallError(
                "Gateway result unavailable or invalid; outcome unknown. Retry the same request_id"
            ) from None

    async def status(self, request: StatusRequest) -> dict:
        try:
            async with asyncio.timeout(10):
                async with httpx2.AsyncClient(
                    transport=self._transport, trust_env=False, follow_redirects=False, timeout=5.0
                ) as client:
                    url = self.status_endpoint + "?" + urlencode({"request_id": request.request_id})
                    async with client.stream(
                        "GET", url, headers={"Authorization": "Bearer " + self._token}
                    ) as response:
                        if response.status_code == 404:
                            return {
                                "request_id": request.request_id,
                                "found": False,
                                "scope": "absent from this snapshot; an in-flight call may "
                                "still commit",
                            }
                        if response.status_code != 200:
                            raise GatewayCallError(
                                "Request status unavailable; do not infer whether "
                                "execution occurred"
                            )
                        data = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=16384):
                            data.extend(chunk)
                            if len(data) > 262144:
                                raise ValueError("oversized status")
            result = json.loads(data)
            if not isinstance(result, dict) or result.get("request_id") != request.request_id:
                raise ValueError("unbound status")
            state, last = result.get("state"), result.get("last_response")
            if state not in ("pending", "approved", "rejected", "invalidated", "completed"):
                raise ValueError("invalid state")
            if not isinstance(last, dict) or last.get("request_id") != request.request_id:
                raise ValueError("unbound stored response")
            decision = last.get("decision")
            if (
                decision not in ("ALLOW", "DENY", "ASK_HUMAN")
                or type(last.get("executed")) is not bool
                or not execution_consistent(last)
                or (state == "completed") != (decision != "ASK_HUMAN")
            ):
                raise ValueError("invalid stored response")
            if not isinstance(result.get("fingerprint"), str) or not re.fullmatch(
                r"[a-f0-9]{64}", result["fingerprint"]
            ):
                raise ValueError("invalid fingerprint")
            outstanding = state in ("pending", "approved")
            deadline, now = result.get("review_deadline"), result.get("server_time")
            if type(now) is not int or now < 0 or type(result.get("review_expired")) is not bool:
                raise ValueError("invalid status clock")
            if (outstanding and (type(deadline) is not int or deadline < 0)) or (
                not outstanding and deadline is not None
            ):
                raise ValueError("invalid review deadline")
            if result["review_expired"] != (outstanding and now >= deadline):
                raise ValueError("invalid expiry state")
            return {
                **result,
                "found": True,
                "scope": "stored request snapshot; not a new execution or authorization",
            }
        except (httpx2.HTTPError, TimeoutError, ValueError, TypeError):
            raise GatewayCallError(
                "Request status unavailable or invalid; do not infer whether execution occurred"
            ) from None


def create_mcp(client: GatewayClient) -> MCPServer:
    server = MCPServer(
        "Nodi",
        instructions=(
            "Submit protected actions through execute_action. DENY and ASK_HUMAN do not execute. "
            "For outbox payments, ALLOW is authorization only; inspect execution_state and "
            "executed for confirmation. "
            "Only a human administrator can approve via the separate HTTP control API. "
            "On transport failure, outcome is unknown; inspect with get_request_status or "
            "retry the identical request_id and action. "
            "Never assume a failed response means the effect did not occur."
        ),
    )

    @server.tool(structured_output=True)
    async def execute_action(action: ActionBody) -> dict[str, Any]:
        """Authorize and execute one exact action through the Nodi gateway.

        Prices, destinations, identity, ancestry and approvals are checked by the
        gateway. Keep the same request_id for retries; new actions need new IDs.
        """
        return await client.execute(action)

    @server.tool(structured_output=True)
    async def get_request_status(request: StatusRequest) -> dict[str, Any]:
        """Read this agent's stored request state without executing an action.

        Missing status does not rule out an in-flight commit. Completed results
        are historical; approved status still requires an exact execution retry.
        """
        return await client.status(request)

    return server


def main():
    try:
        token = os.environ["NODI_AGENT_TOKEN"]
        client = GatewayClient(os.environ.get("NODI_GATEWAY_URL", "http://127.0.0.1:8000"), token)
    except (KeyError, ValueError):
        raise SystemExit("Configure NODI_AGENT_TOKEN and a valid NODI_GATEWAY_URL") from None
    # stdout is reserved for MCP protocol messages.
    create_mcp(client).run(transport="stdio")


if __name__ == "__main__":
    main()
