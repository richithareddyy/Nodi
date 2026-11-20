import asyncio

import pytest

pytest.importorskip("mcp")
import httpx2
from mcp import Client

from nodi.mcp_server import GatewayCallError, GatewayClient, StatusRequest, create_mcp

TOKEN = "a" * 40
VALID = {
    "request_id": "one",
    "state": "pending",
    "fingerprint": "a" * 64,
    "last_response": {"request_id": "one", "decision": "ASK_HUMAN", "executed": False},
    "server_time": 10,
    "review_deadline": 20,
    "review_expired": False,
}


def test_status_fixed_read_only_transport():
    def handle(request):
        assert request.method == "GET" and not request.content
        assert str(request.url) == "https://gateway.example/requests/status?request_id=one"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        return httpx2.Response(200, json=VALID)

    client = GatewayClient("https://gateway.example", TOKEN, transport=httpx2.MockTransport(handle))
    assert asyncio.run(client.status(StatusRequest(request_id="one")))["found"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "other"),
        ("fingerprint", None),
        ("state", "unknown"),
        ("review_expired", True),
        ("review_deadline", True),
        ("server_time", False),
        ("last_response", {}),
        ("last_response", {"request_id": "one", "decision": "ALLOW", "executed": True}),
    ],
)
def test_invalid_status_response(field, value):
    result = {**VALID, field: value}
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json=result))
    with pytest.raises(GatewayCallError):
        asyncio.run(
            GatewayClient("https://gateway.example", TOKEN, transport=transport).status(
                StatusRequest(request_id="one")
            )
        )


@pytest.mark.parametrize("status", [301, 307, 401, 403, 500])
def test_failure_sanitized_and_no_redirect(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx2.Response(
            status, text="sensitive details", headers={"location": "https://attacker.example"}
        )

    with pytest.raises(GatewayCallError) as error:
        asyncio.run(
            GatewayClient(
                "https://gateway.example", TOKEN, transport=httpx2.MockTransport(handle)
            ).status(StatusRequest(request_id="one"))
        )
    assert len(calls) == 1 and "sensitive" not in str(error.value) and TOKEN not in str(error.value)


def test_status_tool_rejects_identity_override():
    def handle(_):
        pytest.fail("invalid input reached network")

    async def check():
        server = create_mcp(
            GatewayClient("https://gateway.example", TOKEN, transport=httpx2.MockTransport(handle))
        )
        async with Client(server) as client:
            result = await client.call_tool(
                "get_request_status", {"request": {"request_id": "one", "agent_id": "other"}}
            )
            assert result.is_error

    asyncio.run(check())


def test_oversized_status():
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200, content=b"x" * 300000))
    with pytest.raises(GatewayCallError):
        asyncio.run(
            GatewayClient("https://gateway.example", TOKEN, transport=transport).status(
                StatusRequest(request_id="one")
            )
        )
