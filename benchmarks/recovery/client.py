"""Bounded client for the controlled loopback service, not a payment integration."""
import json
import re
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from nodi.json_input import validate_json


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class TestServiceProvider:
    __test__ = False
    path = None  # Durable store belongs to a separate service process.

    def __init__(self, port, token):
        if type(port) is not int or not 1 <= port <= 65535 or not token or len(token)<24:
            raise ValueError('invalid loopback service configuration')
        self.origin, self.token = f'http://127.0.0.1:{port}', token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        data = self._request('identity', {})
        if set(data) != {'identity'} or not isinstance(data['identity'], str) or not re.fullmatch('[a-f0-9]{32}',data['identity']):
            raise ValueError('invalid provider identity')
        self.identity = data['identity']

    def _request(self, operation, body):
        req = Request(self.origin+'/'+operation, data=json.dumps(body).encode(),
                      headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with self.opener.open(req, timeout=3) as response:
            raw = response.read(16385)
        if len(raw)>16384: raise ValueError('oversize service response')
        validate_json(raw)
        data = json.loads(raw)
        if type(data) is not dict: raise ValueError('invalid response')
        return data

    def _operation(self, operation, key, payload):
        data = self._request(operation, {'key':key,'payload':payload})
        if set(data) != {'identity','result'} or data['identity'] != self.identity:
            raise ValueError('provider identity changed')
        return data['result']

    def charge(self,key,payload): return self._operation('charge',key,payload)
    def lookup(self,key,payload): return self._operation('lookup',key,payload)
    def cancel(self,key,payload): return self._operation('cancel',key,payload)
