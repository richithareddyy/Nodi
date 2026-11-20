"""Trusted Docker launcher. No agent-controlled flags, images, mounts or commands."""

import contextlib
import json
import re
import subprocess
from pathlib import Path
from uuid import uuid4

from ..json_input import validate_json
from .validation import MAX_RESULT, validate


class ExecutionError(RuntimeError):
    """No successful read result committed; a read may already have occurred."""


def directory(value):
    if type(value) is not str or not value.startswith("/") or "," in value or "\n" in value:
        raise ValueError("sandbox mounts require canonical absolute directories")
    path = Path(value)
    if path.resolve(strict=True) != path or not path.is_dir() or path == Path("/"):
        raise ValueError("sandbox mount must be a canonical dedicated directory")
    return value


class DockerBackend:
    def __init__(self, *, image, workspace, database, egress_volume):
        if type(image) is not str or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("use a locally built immutable Docker image ID")
        if type(egress_volume) is not str or not re.fullmatch(
            r"nodi-egress-[a-z0-9-]{1,60}", egress_volume
        ):
            raise ValueError("invalid private egress volume name")
        self.image, self.workspace, self.database = image, directory(workspace), directory(database)
        self.egress_volume = egress_volume

    def command(self, name, tool):
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user=65532:65532",
            "--cpus=0.5",
            "--memory=128m",
            "--memory-swap=128m",
            "--pids-limit=16",
            "--ulimit=nofile=64:64",
            "--ulimit=core=0:0",
            "--log-driver=none",
        ]
        if tool == "filesystem.read":
            command += ["--mount", f"type=bind,src={self.workspace},dst=/workspace,readonly"]
        elif tool == "sqlite.query":
            command += ["--mount", f"type=bind,src={self.database},dst=/database,readonly"]
        elif tool == "http.get":
            command += ["--mount", f"type=volume,src={self.egress_volume},dst=/egress,readonly"]
        return [*command, "--entrypoint=python", self.image, "-B", "-m", "nodi.execution.worker"]

    def execute(self, action, expires_at):
        validate(action.tool, action.arguments)
        name = "nodi-read-" + uuid4().hex
        payload = json.dumps(
            {"tool": action.tool, "arguments": action.arguments, "expires_at": expires_at}
        ).encode()
        try:
            result = subprocess.run(
                self.command(name, action.tool),
                input=payload,
                capture_output=True,
                timeout=15,
                check=False,
            )
            if result.returncode or len(result.stdout) > MAX_RESULT:
                raise ValueError("worker unavailable or oversized result")
            validate_json(result.stdout)
            response = json.loads(result.stdout)
            if (
                type(response) is not dict
                or set(response) != {"ok", "result"}
                or response["ok"] is not True
                or type(response["result"]) is not dict
            ):
                raise ValueError("worker failed or returned invalid data")
            return {"simulation": False, "backend": "docker-readonly", **response["result"]}
        except (OSError, ValueError, TypeError, subprocess.SubprocessError, RecursionError):
            raise ExecutionError(
                "Sandbox read failed; no successful result committed. A read "
                "may have occurred; retry the same request ID."
            ) from None
        finally:
            # Killing only the CLI does not reliably terminate its container.
            # The worker also has an independent wall-clock alarm if Docker is lost.
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=3)


def from_config(value):
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"image", "workspace", "database", "egress_volume"}:
        raise ValueError("invalid sandbox configuration fields")
    return DockerBackend(**value)
