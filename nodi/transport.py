"""Shared validation for operator-configured gateway origins."""

from urllib.parse import urlsplit


def gateway_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError("gateway URL must be a plain HTTPS origin or loopback HTTP origin")
    if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1"):
        raise ValueError("plain HTTP is permitted only for literal loopback addresses")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid gateway port")
    return value.rstrip("/")
