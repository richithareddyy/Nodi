"""Human operator CLI. Calls only authenticated control APIs; never executes tools."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .models import Contract
from .transport import gateway_origin


class OperatorError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ControlClient:
    def __init__(self, origin: str, token: str):
        self.origin = gateway_origin(origin)
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("invalid administrator credential")
        self._token = token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method: str, path: str, body=None):
        payload = None if body is None else json.dumps(body, allow_nan=False).encode()
        if payload is not None and len(payload) > 65536:
            raise OperatorError("Request too large; nothing sent")
        req = Request(
            self.origin + path,
            data=payload,
            method=method,
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=5) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise OperatorError("Response too large; request a smaller page")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("unexpected response")
            return result
        except HTTPError as exc:
            raise OperatorError(
                f"Gateway returned HTTP {exc.code}; inspect current state before retrying a "
                f"mutation"
            ) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise OperatorError(
                (
                    "Gateway response unavailable or invalid; outcome unknown. "
                    "Inspect state before retrying a mutation"
                )
            ) from None


def page_arguments(command):
    command.add_argument("--limit", type=int, default=25)
    command.add_argument("--after", type=int, default=0)
    command.add_argument("--through", type=int)
    command.add_argument("--agent")


def parser():
    root = argparse.ArgumentParser(prog="python -m nodi.operator")
    root.add_argument("--url", default="http://127.0.0.1:8000", help="Trusted gateway origin")
    root.add_argument(
        "--config", help="Private JSON containing admin_token; otherwise use NODI_ADMIN_TOKEN"
    )
    commands = root.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft")
    draft.add_argument("--mission", required=True)
    draft.add_argument("--agent", required=True)
    draft.add_argument("--output", required=True, help="New contract file; never overwrites")
    for name in ("issue", "modify"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--contract", required=True)
        if name == "modify":
            cmd.add_argument("--visa", required=True)
    for name in ("visas", "approvals", "delegations"):
        listing = commands.add_parser(name)
        page_arguments(listing)
        if name == "approvals":
            listing.add_argument("--include-expired", action="store_true")
    inspect_visa = commands.add_parser("inspect-visa")
    inspect_visa.add_argument("--visa", required=True)
    commands.add_parser("integrity")
    checkpoint_cmd = commands.add_parser("checkpoint")
    checkpoint_cmd.add_argument("--output", required=True)
    export_cmd = commands.add_parser("export-audit")
    export_cmd.add_argument("--checkpoint", required=True)
    export_cmd.add_argument("--output", required=True)
    for name in ("inspect", "approve", "reject"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--agent", required=True)
        cmd.add_argument("--request-id", required=True)
        if name != "inspect":
            cmd.add_argument("--fingerprint", required=True, help="Exact hash shown by inspect")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--visa", required=True)
    audit = commands.add_parser("audit")
    page_arguments(audit)
    audit.add_argument("--visa")
    audit.add_argument("--decision", choices=["ALLOW", "DENY", "ASK_HUMAN"])
    audit.add_argument("--blocked", action="store_true")
    return root


def run(args, client: ControlClient):
    command = args.command
    if command in ("checkpoint", "export-audit"):
        from .audit_export import export_audit, save_checkpoint

        if command == "checkpoint":
            return save_checkpoint(client, args.output)
        return export_audit(client, args.checkpoint, args.output)
    if command == "draft":
        destination = Path(args.output)
        if destination.exists():
            raise OperatorError("Draft output already exists; choose a new path")
        result = client.request(
            "POST", "/drafts", {"mission": args.mission, "agent_id": args.agent}
        )
        contract = Contract(**result["contract"]).dictionary()
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(contract, file, indent=2)
            file.write("\n")
        return {
            "contract_file": str(destination),
            "next_step": "Inspect/edit the contract, then run issue --contract with that path",
        }
    if command in ("issue", "modify"):
        path = Path(args.contract)
        if path.stat().st_size > 65536:
            raise OperatorError("Contract file too large")
        contract = Contract(**json.loads(path.read_text())).dictionary()
        return client.request(
            "POST" if command == "issue" else "PUT",
            "/visas" if command == "issue" else "/visas/" + quote(args.visa, safe=""),
            contract,
        )
    if command == "inspect-visa":
        return client.request("GET", "/visas/inspect?" + urlencode({"visa_id": args.visa}))
    if command == "revoke":
        return client.request("POST", "/visas/" + quote(args.visa, safe="") + "/revoke")
    if command == "integrity":
        result = client.request("GET", "/audit/integrity")
        if result.get("valid") is not True:
            raise OperatorError("Audit integrity verification failed")
        return result
    if command in ("audit", "visas", "approvals", "delegations"):
        if (
            not 1 <= args.limit <= 100
            or not 0 <= args.after <= 2**63 - 1
            or (args.through is not None and not 0 <= args.through <= 2**63 - 1)
        ):
            raise OperatorError("Page limit must be 1..100 and cursors nonnegative 64-bit integers")
        params = {"limit": args.limit, "after": args.after}
        for key in ("through", "agent", "visa", "decision"):
            if getattr(args, key, None) is not None:
                params[key] = getattr(args, key)
        if getattr(args, "blocked", False):
            params["blocked"] = "true"
        if getattr(args, "include_expired", False):
            params["include_expired"] = "true"
        path = {
            "audit": "/audit/events",
            "visas": "/visas",
            "delegations": "/visas",
            "approvals": "/approvals",
        }[command]
        result = client.request("GET", path + "?" + urlencode(params))
        items = result.pop("items")
        if command == "delegations":
            items = [
                {
                    "cursor": v["cursor"],
                    "visa_id": v["id"],
                    "parent": v["parent"],
                    "agent": v["contract"]["agent_id"],
                    "status": v["status"],
                    "spent_cents": v["spent"],
                }
                for v in items
            ]
        return {command: items, **result}
    if command in ("approve", "reject") and not re.fullmatch(r"[a-f0-9]{64}", args.fingerprint):
        raise OperatorError("Fingerprint must be the 64-character lowercase hash from inspect")
    if command in ("inspect", "approve", "reject"):
        review = client.request(
            "GET",
            "/approvals/inspect?" + urlencode({"agent": args.agent, "request_id": args.request_id}),
        )
        if command == "inspect":
            return review
        if (
            review["fingerprint"] != args.fingerprint
            or review["approval_status"] != "pending"
            or review["expired"]
        ):
            raise OperatorError(
                "Review is stale, expired, not pending, or fingerprint mismatches; inspect again"
            )
        result = client.request(
            "POST",
            "/approvals",
            {
                "agent_id": args.agent,
                "request_id": args.request_id,
                "approve": command == "approve",
                "expected_fingerprint": args.fingerprint,
            },
        )
        return {
            "review": review,
            "result": result,
            "execution": "No tool executed by this command; the agent must retry its exact action",
        }
    raise OperatorError("Unsupported operator command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        token = (
            json.loads(Path(args.config).read_text())["admin_token"]
            if args.config
            else os.environ["NODI_ADMIN_TOKEN"]
        )
        result = run(args, ControlClient(args.url, token))
        # Escaping control characters keeps untrusted agent text inert in terminals.
        print(json.dumps(result, indent=2, ensure_ascii=True))
    except OperatorError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, TypeError, KeyError, OSError):
        print(
            (
                "Operator command failed. Check credentials, gateway state, "
                "file paths and review fingerprint; no automatic retry was "
                "attempted."
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
