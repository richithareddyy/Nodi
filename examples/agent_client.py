"""Explicit-ID catalog search and read-only recovery using agent credentials."""

import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from nodi.transport import gateway_origin


class ClientError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AgentClient:
    def __init__(self, origin, token):
        self.origin = gateway_origin(origin)
        if (
            not isinstance(token, str)
            or len(token) < 32
            or not token.isascii()
            or any(c.isspace() for c in token)
        ):
            raise ValueError("invalid agent credential")
        self.token = token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, command, request_id, visa=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise ValueError("invalid request ID")
        if command == "search":
            if not isinstance(visa, str) or not visa.strip() or len(visa) > 150:
                raise ValueError("search requires a visa")
            payload = {
                "request_id": request_id,
                "visa_id": visa,
                "tool": "search_products",
                "resource": "catalog:monitors",
                "arguments": {"query": "monitor"},
            }
            path, method, data = "/execute", "POST", json.dumps(payload).encode()
        elif command == "status":
            path, method, data = (
                "/requests/status?" + urlencode({"request_id": request_id}),
                "GET",
                None,
            )
        else:
            raise ValueError("unknown command")
        request = Request(
            self.origin + path,
            data=data,
            method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError("oversized response")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("request_id") != request_id:
                raise ValueError("unbound response")
            if command == "search":
                if (
                    result.get("visa_id") != visa
                    or result.get("decision") not in ("ALLOW", "DENY", "ASK_HUMAN")
                    or type(result.get("executed")) is not bool
                    or result["executed"] != (result["decision"] == "ALLOW")
                ):
                    raise ValueError("invalid execution response")
            else:
                if result.get("state") not in (
                    "pending",
                    "approved",
                    "rejected",
                    "invalidated",
                    "completed",
                ):
                    raise ValueError("invalid status response")
                last = result.get("last_response")
                if not isinstance(last, dict) or last.get("request_id") != request_id:
                    raise ValueError("unbound stored response")
            return result
        except HTTPError as exc:
            if command == "status" and exc.code == 404:
                return {
                    "request_id": request_id,
                    "found": False,
                    "scope": "absent from this snapshot; an in-flight call may still commit",
                }
            raise ClientError(
                f"Gateway returned HTTP {exc.code}. Preserve the original request ID and "
                f"exact action; no automatic retry was attempted."
            ) from None
        except (URLError, OSError, ValueError, TypeError):
            raise ClientError(
                (
                    "Gateway result unavailable or invalid. Outcome is uncertain; "
                    "preserve the original request ID and exact action. No "
                    "automatic retry was attempted."
                )
            ) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("search", "status"):
        command = commands.add_parser(name)
        command.add_argument("--request-id", required=True)
        if name == "search":
            command.add_argument("--visa", default=os.environ.get("NODI_VISA_ID"))
    args = parser.parse_args(argv)
    try:
        client = AgentClient(args.url, os.environ["NODI_AGENT_TOKEN"])
        result = client.call(args.command, args.request_id, getattr(args, "visa", None))
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
    except (ValueError, KeyError):
        print(
            (
                "Check the agent credential, gateway origin, request ID and "
                "visa. No automatic retry was attempted."
            ),
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
