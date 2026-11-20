"""Authenticated HTTP boundary. Protected adapters are never exposed as routes."""

import asyncio
import hmac
import json
import math
import os
import sqlite3
from time import monotonic
from typing import Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .capabilities import PREFIX, CapabilityError
from .execution.docker import ExecutionError
from .gateway import Gateway
from .json_input import validate_json
from .models import Action, Contract, draft
from .queries import MAX_CURSOR, ReadModel


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MissionDraft(StrictModel):
    mission: str = Field(min_length=1, max_length=2000)
    agent_id: str = Field(min_length=1, max_length=100)


class Review(StrictModel):
    agent_id: str
    request_id: str
    approve: bool
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class CapabilityMint(StrictModel):
    key_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class ActionBody(StrictModel):
    request_id: str
    visa_id: str
    tool: str
    resource: str
    arguments: dict
    context: str = Field(default="", max_length=4000)


class BodyLimit:
    """Buffer a bounded body before parsing; also covers chunked requests."""

    def __init__(self, app, limit=65536, read_timeout=5.0):
        if (
            type(read_timeout) not in (int, float)
            or not math.isfinite(read_timeout)
            or read_timeout <= 0
        ):
            raise ValueError("body read timeout must be positive and finite")
        self.app, self.limit, self.read_timeout = app, limit, read_timeout

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        deadline = monotonic() + self.read_timeout
        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(receive(), timeout=remaining)
                if message["type"] == "http.disconnect":
                    return
                if monotonic() >= deadline:
                    raise TimeoutError
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > self.limit:
                    response = JSONResponse({"detail": "request body too large"}, status_code=413)
                    return await response(scope, receive, send)
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
        except TimeoutError:
            response = JSONResponse(
                {"detail": "request body read deadline exceeded"}, status_code=408
            )
            return await response(scope, receive, send)
        body = bytes(body)
        if body:
            content_types = [
                value.decode("latin-1").split(";", 1)[0].strip().lower()
                for key, value in scope.get("headers", [])
                if key.lower() == b"content-type"
            ]
            if len(content_types) != 1 or not (
                content_types[0] == "application/json"
                or (
                    content_types[0].startswith("application/")
                    and content_types[0].endswith("+json")
                )
            ):
                response = JSONResponse(
                    {"detail": "a single JSON Content-Type is required"}, status_code=415
                )
                return await response(scope, receive, send)
            try:
                validate_json(body)
            except (ValueError, RecursionError):
                response = JSONResponse(
                    {"detail": "invalid or ambiguous JSON request"}, status_code=400
                )
                return await response(scope, receive, send)
        sent = False

        async def bounded_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


def create_app(gateway: Gateway, admin_token: str, agent_tokens: dict[str, str]) -> FastAPI:
    """Tokens map to identities on the server, never from request agent_id fields."""
    if (
        len(admin_token) < 32
        or not agent_tokens
        or any(
            type(k) is not str or len(k) < 32 or type(v) is not str or not v
            for k, v in agent_tokens.items()
        )
        or admin_token in agent_tokens
    ):
        raise ValueError("distinct admin/agent credentials of at least 32 characters required")
    app = FastAPI(
        title="Nodi",
        version="0.1.0",
        description="Mission-scoped authorization. Simulation by default, with optional Docker read-only execution.",
    )
    from .dashboard import install_dashboard

    install_dashboard(app)
    app.add_middleware(BodyLimit)
    reads = ReadModel(gateway)
    bearer = HTTPBearer(auto_error=False)

    def token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(401, "bearer credential required")
        return credentials.credentials

    def admin(value: str = Depends(token)):
        if not hmac.compare_digest(value.encode(), admin_token.encode()):
            if any(hmac.compare_digest(value.encode(), secret.encode()) for secret in agent_tokens):
                raise HTTPException(403, "human administrator credential required")
            raise HTTPException(401, "invalid credential")

    def agent(value: str = Depends(token)) -> str:
        for secret, identity in agent_tokens.items():
            if hmac.compare_digest(value.encode(), secret.encode()):
                return identity
        if hmac.compare_digest(value.encode(), admin_token.encode()):
            raise HTTPException(403, "agent credential required")
        raise HTTPException(401, "invalid credential")

    @app.exception_handler(ValueError)
    @app.exception_handler(TypeError)
    async def invalid(_request: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(CapabilityError)
    async def invalid_capability(_request: Request, _exc: Exception):
        return JSONResponse(
            {"detail": "invalid, inactive or out-of-scope capability"},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(sqlite3.Error)
    async def unavailable(_request: Request, _exc: Exception):
        return JSONResponse(
            {"detail": "authorization store unavailable; execution not confirmed"}, status_code=503
        )

    @app.exception_handler(ExecutionError)
    async def read_failed(_request: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "mode": "simulation"
            + ("+sandbox-readonly" if gateway.backend else "")
            + ("+mock-payments" if gateway.payment_provider else ""),
        }

    @app.post("/drafts", dependencies=[Depends(admin)])
    def draft_contract(body: MissionDraft):
        return {
            "requires_human_review": True,
            "contract": draft(body.mission, body.agent_id).dictionary(),
        }

    @app.post("/visas", dependencies=[Depends(admin)])
    def issue(body: dict = Body(...)):
        return gateway.issue(Contract(**body))

    @app.put("/visas/{visa_id}", dependencies=[Depends(admin)])
    def modify(visa_id: str, body: dict = Body(...)):
        gateway.modify(visa_id, Contract(**body))
        return {"updated": True}

    @app.post("/visas/{visa_id}/revoke", dependencies=[Depends(admin)])
    def revoke(visa_id: str):
        gateway.revoke(visa_id)
        return {"revoked": True}

    @app.post("/visas/{visa_id}/delegate")
    def delegate(visa_id: str, body: dict = Body(...), principal: str = Depends(agent)):
        return gateway.delegate(principal, visa_id, Contract(**body))

    @app.post("/visas/{visa_id}/capability", dependencies=[Depends(admin)])
    def mint_capability(visa_id: str, body: CapabilityMint):
        return JSONResponse(
            gateway.issue_capability(visa_id, body.key_id), headers={"Cache-Control": "no-store"}
        )

    @app.post("/execute")
    def execute(body: ActionBody, credential: str = Depends(token)):
        action = Action(**body.model_dump())
        if credential.startswith(PREFIX):
            return gateway.execute_capability(credential, action)
        return gateway.execute(agent(credential), action)

    @app.get("/requests/status")
    def request_status(
        request_id: str = Query(..., pattern=r"^[A-Za-z0-9_-]{1,100}$"),
        credential: str = Depends(token),
    ):
        try:
            if credential.startswith(PREFIX):
                if gateway.capabilities is None:
                    raise CapabilityError("capabilities disabled")
                verified = gateway.capabilities.verify(credential)
                result = reads.request_status(verified.principal, request_id, capability=verified)
            else:
                result = reads.request_status(agent(credential), request_id)
        except LookupError:
            return JSONResponse(
                {"detail": "request not found"},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/approvals", dependencies=[Depends(admin)])
    def approval(body: Review):
        return gateway.approve(
            body.agent_id, body.request_id, body.approve, body.expected_fingerprint
        )

    @app.get("/audit/checkpoint", dependencies=[Depends(admin)])
    def audit_checkpoint():
        return reads.checkpoint()

    @app.get("/audit/events", dependencies=[Depends(admin)])
    def audit_events(
        after: int = Query(0, ge=0, le=MAX_CURSOR),
        limit: int = Query(25, ge=1, le=100),
        through: int | None = Query(None, ge=0, le=MAX_CURSOR),
        agent: str | None = Query(None, max_length=100),
        visa: str | None = Query(None, max_length=150),
        decision: Literal["ALLOW", "DENY", "ASK_HUMAN"] | None = None,
        blocked: bool = False,
    ):
        return reads.audit(after, limit, through, agent, visa, decision, blocked)

    @app.get("/visas/inspect", dependencies=[Depends(admin)])
    def inspect_visa(visa_id: str = Query(..., min_length=1, max_length=150)):
        try:
            return reads.inspect_visa(visa_id)
        except LookupError:
            raise HTTPException(404, "visa not found") from None

    @app.get("/visas", dependencies=[Depends(admin)])
    def list_visas(
        after: int = Query(0, ge=0, le=MAX_CURSOR),
        limit: int = Query(25, ge=1, le=100),
        through: int | None = Query(None, ge=0, le=MAX_CURSOR),
        agent: str | None = Query(None, max_length=100),
    ):
        return reads.visas(after, limit, through, agent)

    @app.get("/approvals", dependencies=[Depends(admin)])
    def list_approvals(
        after: int = Query(0, ge=0, le=MAX_CURSOR),
        limit: int = Query(25, ge=1, le=100),
        through: int | None = Query(None, ge=0, le=MAX_CURSOR),
        agent: str | None = Query(None, max_length=100),
        include_expired: bool = False,
    ):
        return reads.approvals(after, limit, through, agent, include_expired)

    @app.get("/approvals/inspect", dependencies=[Depends(admin)])
    def inspect_approval(
        agent: str = Query(..., min_length=1, max_length=100),
        request_id: str = Query(..., min_length=1, max_length=100),
    ):
        try:
            return reads.inspect(agent, request_id)
        except LookupError:
            raise HTTPException(404, "outstanding approval not found") from None

    @app.get("/control", dependencies=[Depends(admin)])
    def control():
        return gateway.snapshot()

    @app.get("/audit/integrity", dependencies=[Depends(admin)])
    def integrity():
        return {
            "valid": gateway.verify_audit(),
            "scope": "local hash chain; not an external tamper-proof log",
        }

    return app


def from_environment() -> FastAPI:
    try:
        admin_token = os.environ["NODI_ADMIN_TOKEN"]
        agents = json.loads(os.environ["NODI_AGENT_TOKENS"])
        if not isinstance(agents, dict):
            raise ValueError("agent credentials must be a JSON object")
    except (KeyError, ValueError) as exc:
        raise RuntimeError("Set NODI_ADMIN_TOKEN and NODI_AGENT_TOKENS before startup") from exc
    from .execution.docker import from_config

    backend = from_config(
        json.loads(os.environ["NODI_SANDBOX"]) if "NODI_SANDBOX" in os.environ else None
    )
    from .mock_payments import from_config as payment_config

    provider = payment_config(
        json.loads(os.environ["NODI_MOCK_PAYMENTS"]) if "NODI_MOCK_PAYMENTS" in os.environ else None
    )
    from .capabilities import from_config as capability_config

    keys = capability_config(
        json.loads(os.environ["NODI_CAPABILITY_KEYS"])
        if "NODI_CAPABILITY_KEYS" in os.environ
        else None
    )
    return create_app(
        Gateway(
            os.environ.get("NODI_DB", "nodi.db"),
            backend=backend,
            payment_provider=provider,
            capability_keys=keys,
        ),
        admin_token,
        agents,
    )
