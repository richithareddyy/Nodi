"""Bounded, isolated public simulation workspaces; no owner credentials are shared."""

import secrets
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .api import create_app
from .gateway import Gateway
from .models import draft


@dataclass
class Workspace:
    directory: object
    app: object
    expires: float
    calls: int = 0
    active: int = 0


class Sandbox:
    def __init__(self, capacity=24, ttl=900, request_limit=200, clock=time.monotonic):
        self.capacity, self.ttl, self.request_limit = capacity, ttl, request_limit
        self.clock = clock
        self.workspaces = {}
        self.starts = deque()
        self.lock = threading.RLock()

    def cleanup(self):
        now = self.clock()
        for key, workspace in list(self.workspaces.items()):
            if workspace.expires <= now and not workspace.active:
                workspace.directory.cleanup()
                del self.workspaces[key]

    def start(self):
        with self.lock:
            self.cleanup()
            now = self.clock()
            while self.starts and self.starts[0] <= now - 60:
                self.starts.popleft()
            if len(self.workspaces) >= self.capacity or len(self.starts) >= 12:
                return 429, {"detail": "The demo is busy. Please try again in a few minutes."}
            self.starts.append(now)
            directory = tempfile.TemporaryDirectory(prefix="nodi-sandbox-")
            try:
                gateway = Gateway(directory.name + "/sandbox.db")
                visa = gateway.issue(draft("Buy a monitor under $300.", "procurement"))["visa_id"]
                admin, agent, key = (secrets.token_urlsafe(32) for _ in range(3))
                self.workspaces[key] = Workspace(
                    directory, create_app(gateway, admin, {agent: "procurement"}), now + self.ttl
                )
                return 201, {
                    "workspace": key,
                    "admin": admin,
                    "agent": agent,
                    "identity": "procurement",
                    "visa_id": visa,
                    "expires_in": self.ttl,
                }
            except BaseException:
                directory.cleanup()
                raise

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"].removeprefix(scope.get("root_path", ""))
        response = None
        if path == "/start" and scope["method"] == "POST":
            status, data = await run_in_threadpool(self.start)
            response = JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})
        elif path.startswith("/api/"):
            key = dict(scope.get("headers", [])).get(b"x-sandbox-id", b"").decode("latin1")
            with self.lock:
                self.cleanup()
                workspace = self.workspaces.get(key)
                if not workspace or workspace.expires <= self.clock():
                    response = JSONResponse(
                        {"detail": "Your sandbox expired. Start a fresh sandbox to continue."},
                        status_code=410,
                    )
                elif workspace.calls >= self.request_limit:
                    response = JSONResponse(
                        {"detail": "Sandbox request limit reached. Start a fresh sandbox."},
                        status_code=429,
                    )
                else:
                    workspace.calls += 1
                    workspace.active += 1
            if response is None:
                child = dict(
                    scope,
                    path=path.removeprefix("/api"),
                    root_path="",
                    raw_path=path.removeprefix("/api").encode(),
                )
                try:
                    return await workspace.app(child, receive, send)
                finally:
                    with self.lock:
                        workspace.active -= 1
        else:
            response = JSONResponse({"detail": "not found"}, status_code=404)
        await response(scope, receive, send)
