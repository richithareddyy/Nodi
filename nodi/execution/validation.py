"""Closed, read-only tool vocabulary shared by gateway, worker and egress proxy."""
import re
from urllib.parse import urlsplit

URLS = {
    "docs.python.org": frozenset({"/3/", "/3/tutorial/"}),
    "www.sqlite.org": frozenset({"/index.html"}),
}
SCHEMAS = {
    "filesystem.read": ({"path"}, set()),
    "http.get": ({"url"}, set()),
    "sqlite.query": ({"query"}, set()),
    "shell.read": ({"command"}, set()),
}
COMMANDS = {"system": ("/usr/bin/uname", "-s"), "date": ("/usr/bin/date", "-u", "+%Y-%m-%d")}
RESOURCES = frozenset({"workspace:research", "database:test", "runtime:info", *('web:' + h for h in URLS)})
MAX_CONTENT = 65536
MAX_RESULT = 131072


def relative_path(value):
    # No normalization that could silently turn an escape into an allowed name.
    if (type(value) is not str or not 1 <= len(value) <= 240
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
            or any(part in ("", ".", "..") or part.startswith(".") for part in value.split("/"))):
        raise ValueError("path must be a bounded relative path without traversal or hidden components")
    return value


def destination(value):
    if type(value) is not str or len(value) > 2048 or not value.isascii() or any(ord(c) <= 32 for c in value):
        raise ValueError("invalid URL")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (parsed.scheme != "https" or host not in URLS or parsed.netloc != host
            or parsed.path not in URLS[host] or parsed.query or parsed.fragment
            or value != 'https://' + host + parsed.path):
        raise ValueError("URL must be an exact approved public documentation destination")
    return host, parsed.path


def validate(tool, arguments):
    if type(tool) is not str or tool not in SCHEMAS or type(arguments) is not dict:
        raise ValueError("unknown read-only sandbox tool")
    required, optional = SCHEMAS[tool]
    if not required <= arguments.keys() or not arguments.keys() <= required | optional:
        raise ValueError("missing or unknown sandbox arguments")
    if tool == "filesystem.read":
        relative_path(arguments['path'])
        return "workspace:research"
    if tool == "http.get":
        host, _ = destination(arguments['url'])
        return 'web:' + host
    if tool == "sqlite.query":
        query = arguments['query']
        if type(query) is not str or not query.strip() or len(query) > 2000:
            raise ValueError("invalid query")
        # SQL authorization is enforced by SQLite, not by this prefix check.
        if not re.match(r"\ASELECT\s", query, re.I):
            raise ValueError("only SELECT queries are supported")
        return "database:test"
    command = arguments['command']
    if type(command) is not str or command not in COMMANDS:
        raise ValueError("unknown fixed read-only command")
    return "runtime:info"
