"""Dedicated HTTPS GET proxy, reachable only via a private Docker volume socket."""
import http.client
import ipaddress
import json
import os
import signal
import socket
import socketserver
import ssl
import time

from ..json_input import validate_json
from .validation import MAX_CONTENT, MAX_RESULT, destination


def public_addresses(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError('DNS returned no addresses')
    for family, kind, protocol, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if (not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_reserved
                or (isinstance(ip, ipaddress.IPv6Address) and (ip.is_site_local or any((ip.ipv4_mapped, ip.sixtofour, ip.teredo))
                    or ip in ipaddress.ip_network('64:ff9b::/96')
                    or ip in ipaddress.ip_network('64:ff9b:1::/48')))):
            raise ValueError('DNS includes a nonpublic or mapped destination')
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise ValueError('unsupported address family')
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=3, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        family, kind, protocol, _, address = self.address
        raw = socket.socket(family, kind, protocol)
        try:
            raw.settimeout(3)
            raw.connect(address)  # Numeric address checked once, no second DNS lookup.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch(url, expires_at=None):
    host, path = destination(url)
    addresses = public_addresses(host)
    if expires_at is not None and time.time() >= expires_at:
        raise ValueError('authority expired during DNS resolution')
    connection = PinnedHTTPS(host, addresses[0])
    try:
        connection.request('GET', path, headers={'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:  # Includes all redirects; never follow Location.
            raise ValueError('only successful nonredirected GETs are accepted')
        if response.getheader('Content-Encoding', 'identity') != 'identity':
            raise ValueError('compressed responses are unsupported')
        data = response.read(MAX_CONTENT + 1)
        if len(data) > MAX_CONTENT:
            raise ValueError('response exceeds limit')
        return {'url': url, 'status': 200, 'content': data.decode('utf-8')}
    finally:
        connection.close()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        signal.alarm(8)
        self.connection.settimeout(3)
        try:
            raw = self.rfile.readline(4097)
            if len(raw) > 4096:
                raise ValueError('oversized proxy request')
            validate_json(raw)
            request = json.loads(raw)
            if type(request) is not dict or set(request) != {'url', 'expires_at'}:
                raise ValueError('proxy accepts only an exact URL')
            deadline = request['expires_at']
            if type(deadline) is not int or time.time() >= deadline:
                raise ValueError('expired proxy request')
            remaining = deadline - time.time()
            if remaining <= 0:
                raise ValueError('expired proxy request')
            signal.setitimer(signal.ITIMER_REAL, min(8, remaining))
            response = {'ok': True, 'result': fetch(request['url'], deadline)}
        except Exception:
            response = {'ok': False, 'error': 'GET rejected or unavailable'}
        payload = json.dumps(response).encode() + b'\n'
        if len(payload) > MAX_RESULT:
            payload = b'{"ok":false,"error":"response too large"}\n'
        self.wfile.write(payload)


class Server(socketserver.ForkingMixIn, socketserver.UnixStreamServer):
    max_children = 8


def main():
    path = '/egress/proxy.sock'
    if os.path.exists(path):
        os.unlink(path)
    with Server(path, Handler) as server:
        os.chmod(path, 0o600)
        server.serve_forever()


if __name__ == '__main__':
    main()
