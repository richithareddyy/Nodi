"""Independent durable loopback test service. Never processes real payments."""
import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from nodi.mock_payments import MockPaymentProvider


def serve(database, port, ready, *, crash_after_charge=False):
    token = os.environ['NODI_TEST_SERVICE_TOKEN']
    if len(token) < 24:
        raise ValueError('test service token must be at least 24 characters')
    provider = MockPaymentProvider(database)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+token):
                self.send_error(401)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('invalid body bound')
                body = json.loads(self.rfile.read(length))
                if self.path == '/identity' and body == {}:
                    result = {'identity':provider.identity}
                elif self.path in ('/charge','/lookup','/cancel') and type(body) is dict and set(body)=={'key','payload'}:
                    result = getattr(provider, self.path[1:])(body['key'], body['payload'])
                    if crash_after_charge and self.path == '/charge':
                        os._exit(73)  # Durable effect exists; no HTTP acknowledgement was sent.
                    result = {'identity':provider.identity,'result':result}
                else:
                    raise ValueError('invalid test operation')
                raw = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (ValueError, TypeError, TimeoutError):
                self.send_error(503)

        def setup(self):
            super().setup()
            self.connection.settimeout(3)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    Path(ready).write_text(json.dumps({'port':server.server_port,'identity':provider.identity}))
    server.serve_forever()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',required=True)
    p.add_argument('--ready',required=True)
    p.add_argument('--port',type=int,default=0)
    p.add_argument('--crash-after-charge',action='store_true')
    a = p.parse_args()
    serve(a.database,a.port,a.ready,crash_after_charge=a.crash_after_charge)

if __name__ == '__main__': main()
