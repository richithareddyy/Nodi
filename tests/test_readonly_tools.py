"""Read confinement and SQL/command boundaries; no Docker required."""

import os
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from nodi.execution import egress, worker
from nodi.execution.validation import destination, validate


@pytest.fixture
def files(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "source.txt").write_text("A bounded source.")
    db = sqlite3.connect(tmp_path / "test.db")
    db.executescript(
        "CREATE TABLE articles(title TEXT); INSERT INTO articles "
        "VALUES ('SQLite'); CREATE TABLE secrets(value TEXT);"
    )
    db.close()
    return tmp_path


def test_real_read_and_database_snapshot(files):
    assert (
        worker.execute("filesystem.read", {"path": "notes/source.txt"}, workspace=files)["content"]
        == "A bounded source."
    )
    before = (files / "test.db").read_bytes()
    assert worker.execute("sqlite.query", {"query": "SELECT title FROM articles"}, database=files)[
        "rows"
    ] == [("SQLite",)]
    assert worker.execute(
        "sqlite.query", {"query": "SELECT count(*) FROM articles"}, database=files
    )["rows"] == [(1,)]
    assert (files / "test.db").read_bytes() == before


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/etc/passwd",
        "notes/../../secret",
        "notes//source.txt",
        "notes/./source.txt",
        "notes\\source.txt",
        ".env",
        "%2e%2e/secret",
        "",
        4,
    ],
)
def test_path_rejections(files, path):
    with pytest.raises((ValueError, OSError)):
        worker.read_file(files, path)


def test_symlinks_hardlinks_fifo_and_size(files, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (files / "link").symlink_to(outside)
    (files / "directory").symlink_to(files / "notes", target_is_directory=True)
    os.link(outside, files / "hard")
    os.mkfifo(files / "pipe")
    (files / "large").write_bytes(b"x" * 65537)
    for name in ("link", "directory/source.txt", "hard", "pipe", "large"):
        with pytest.raises((ValueError, OSError)):
            worker.read_file(files, name)


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM articles",
        "SELECT * FROM articles; DELETE FROM articles",
        "ATTACH '/tmp/a' AS a",
        "PRAGMA journal_mode=WAL",
        "SELECT * FROM secrets",
        "SELECT * FROM sqlite_master",
        "SELECT load_extension('/tmp/x')",
        "SELECT randomblob(1000000000)",
        "SELECT * FROM pragma_database_list",
        "SELECT writefile('/tmp/pwn','x')",
    ],
)
def test_sql_is_read_only_closed_and_single_statement(files, query):
    before = (files / "test.db").read_bytes()
    with pytest.raises((ValueError, sqlite3.Error)):
        worker.execute("sqlite.query", {"query": query}, database=files)
    assert (files / "test.db").read_bytes() == before


def test_sql_row_and_instruction_bounds(files):
    with sqlite3.connect(files / "test.db") as db:
        db.executemany("INSERT INTO articles VALUES (?)", [(str(n),) for n in range(110)])
    with pytest.raises(ValueError, match="row limit"):
        worker.query_database(files, "SELECT * FROM articles")
    with pytest.raises(sqlite3.OperationalError):
        worker.query_database(
            files, "SELECT count(*) FROM articles a, articles b, articles c, articles d"
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.python.org/3/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://docs.python.org.evil.test/3/",
        "https://user@docs.python.org/3/",
        "https://docs.python.org:443/3/",
        "https://docs.python.org/3/?secret=x",
        "https://docs.python.org/3/#x",
        "https://docs.python.org/3/../secret",
        "https://docs.python.org/3/\r\nX: y",
        "https://DOCS.PYTHON.ORG/3/",
    ],
)
def test_closed_web_destinations(url):
    with pytest.raises(ValueError):
        destination(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "192.168.1.1",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:8.8.8.8",
        "fec0::1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::1",
    ],
)
def test_dns_rejects_nonpublic_even_in_mixed_answers(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (family, socket.SOCK_STREAM, 6, "", (address, 443)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers)
    with pytest.raises(ValueError):
        egress.public_addresses("docs.python.org")


def test_get_pins_validated_ip_refuses_redirect_and_bounds_response(monkeypatch):
    address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
    monkeypatch.setattr(egress, "public_addresses", lambda host: [address])
    responses = [302, 200, 200]
    sizes = [65537, 12]
    calls = []

    class Connection:
        def __init__(self, host, chosen):
            assert chosen == address and host == "docs.python.org"

        def request(self, *args, **kwargs):
            calls.append(args)

        def getresponse(self):
            return SimpleNamespace(
                status=responses.pop(0),
                getheader=lambda *args: "identity",
                read=lambda n: b"x" * sizes.pop(0),
            )

        def close(self):
            pass

    monkeypatch.setattr(egress, "PinnedHTTPS", Connection)
    for _ in range(2):
        with pytest.raises(ValueError):
            egress.fetch("https://docs.python.org/3/")
    assert egress.fetch("https://docs.python.org/3/")["content"] == "x" * 12
    assert calls == [("GET", "/3/")] * 3


def test_fixed_commands_no_shell_or_user_arguments(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["shell"] is False and kwargs["timeout"] == 2
        return SimpleNamespace(stdout=b"Linux\n")

    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.execute("shell.read", {"command": "system"})["stdout"] == "Linux\n"
    for arguments in (
        {"command": "system; cat /etc/passwd"},
        {"command": "system", "args": ["-a"]},
        {"command": ["sh"]},
    ):
        with pytest.raises(ValueError):
            worker.execute("shell.read", arguments)
    assert calls == [("/usr/bin/uname", "-s")]


def test_write_tools_are_not_registered():
    for tool in ("filesystem.write", "shell.exec", "http.post"):
        with pytest.raises(ValueError):
            validate(tool, {})


def test_pinned_connection_uses_numeric_address_and_original_tls_name(monkeypatch):
    events = []

    class Socket:
        def settimeout(self, seconds):
            pass

        def connect(self, address):
            events.append(("connect", address))

        def close(self):
            pass

    class Context:
        def wrap_socket(self, sock, server_hostname):
            events.append(("tls", server_hostname))
            return sock

    monkeypatch.setattr(egress.socket, "socket", lambda *args: Socket())
    connection = egress.PinnedHTTPS(
        "docs.python.org", (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
    )
    connection._context = Context()
    connection.connect()
    assert events == [("connect", ("8.8.8.8", 443)), ("tls", "docs.python.org")]


def test_expired_dns_result_never_connects(monkeypatch):
    monkeypatch.setattr(egress, "public_addresses", lambda host: [])
    with pytest.raises(ValueError, match="expired"):
        egress.fetch("https://docs.python.org/3/", expires_at=1)
