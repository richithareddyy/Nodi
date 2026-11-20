"""Bounded first-party chained-HMAC capabilities; ledger enforcement is mandatory.

This versioned format uses the macaroon attenuation construction, not the macaroon
or Biscuit wire format. It has no third-party caveats or public-key verification.
"""
import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from uuid import uuid4

from .json_input import validate_json
from .models import Contract
from .policy import attenuation

PREFIX = 'nodi1.'
MAX_TOKEN = 32768
DOMAIN = b'nodi.capability.v1\x00'
FIELDS = set(Contract.__dataclass_fields__)


class CapabilityError(ValueError):
    """Invalid credential or stale ledger binding; messages never include tokens."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def contract(value):
    if type(value) is not dict or set(value) != FIELDS:
        raise CapabilityError('capability requires a complete contract')
    return Contract(**value)


def pack(data):
    token = PREFIX + base64.urlsafe_b64encode(encoded(data)).decode().rstrip('=')
    if len(token) > MAX_TOKEN:
        raise CapabilityError('capability exceeds size bound')
    return token


def unpack(token):
    try:
        if type(token) is not str or not token.startswith(PREFIX) or not len(PREFIX) < len(token) <= MAX_TOKEN:
            raise ValueError()
        text = token[len(PREFIX):]
        if not re.fullmatch('[A-Za-z0-9_-]+', text): raise ValueError()
        raw = base64.b64decode(text + '=' * (-len(text) % 4), altchars=b'-_', validate=True)
        validate_json(raw, max_depth=12)
        data = json.loads(raw)
        if type(data) is not dict or set(data) != {'root','caveats','signature'} or pack(data) != token:
            raise ValueError()
        root, caveats = data['root'], data['caveats']
        if type(root) is not dict or set(root) != {'id','key_id','visa_id','epoch','contract'}:
            raise ValueError()
        if not isinstance(root['id'], str) or not re.fullmatch('[a-f0-9]{32}', root['id']): raise ValueError()
        if not isinstance(root['key_id'], str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', root['key_id']): raise ValueError()
        if not isinstance(root['visa_id'], str) or not re.fullmatch('[a-f0-9]{32}', root['visa_id']): raise ValueError()
        if type(root['epoch']) is not int or not 0 <= root['epoch'] < 2**63: raise ValueError()
        parent = contract(root['contract'])
        if type(caveats) is not list or len(caveats) > 8: raise ValueError()
        for item in caveats:
            if type(item) is not dict or set(item) != {'id','contract'}: raise ValueError()
            if not isinstance(item['id'], str) or not re.fullmatch('[a-f0-9]{32}', item['id']): raise ValueError()
            child = contract(item['contract'])
            attenuation(parent, child)
            parent = child
        if not isinstance(data['signature'], str) or not re.fullmatch('[a-f0-9]{64}', data['signature']): raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, RecursionError):
        raise CapabilityError('invalid capability encoding or attenuation') from None


def chain_mac(key, part, value):
    return hmac.digest(key, DOMAIN + part + b'\x00' + encoded(value), 'sha256')


def node_ids(data):
    """Public stable scope IDs. No bearer signature is persisted or logged."""
    result = [data['root']['visa_id']]
    for index in range(len(data['caveats'])):
        material = {'root':data['root'], 'caveats':data['caveats'][:index+1]}
        result.append('cap_' + hashlib.sha256(DOMAIN + encoded(material)).hexdigest())
    return result


def describe(token):
    """Decode claims for offline use, WITHOUT asserting signature validity."""
    data = unpack(token)
    return {'visa_id':node_ids(data)[-1], 'root_id':data['root']['id'],
            'contract':(data['caveats'][-1] if data['caveats'] else data['root'])['contract'],
            'signature_verified':False}


def attenuate(token, child):
    """Append a narrower contract offline; holders cannot verify the root MAC."""
    data = unpack(token)
    parent = contract((data['caveats'][-1] if data['caveats'] else data['root'])['contract'])
    attenuation(parent, child)
    caveat = {'id':uuid4().hex, 'contract':child.dictionary()}
    data['signature'] = chain_mac(bytes.fromhex(data['signature']), b'caveat', caveat).hex()
    data['caveats'].append(caveat)
    result = pack(data)
    unpack(result)
    return result


@dataclass(frozen=True)
class Verified:
    data: dict

    @property
    def visa_id(self): return node_ids(self.data)[-1]

    @property
    def principal(self):
        return (self.data['caveats'][-1] if self.data['caveats'] else self.data['root'])['contract']['agent_id']


class Keys:
    def __init__(self, keys):
        if type(keys) is not dict or not 1 <= len(keys) <= 16:
            raise ValueError('capabilities require 1..16 configured keys')
        for name, key in keys.items():
            if (type(name) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,64}', name)
                    or type(key) is not bytes or len(key) != 32):
                raise ValueError('capability key IDs must be bounded and keys exactly 32 bytes')
        self.keys = dict(keys)

    def mint(self, key_id, visa_id, epoch, scope):
        if key_id not in self.keys: raise CapabilityError('unknown capability key')
        root = {'id':uuid4().hex, 'key_id':key_id, 'visa_id':visa_id, 'epoch':epoch, 'contract':scope.dictionary()}
        result = pack({'root':root, 'caveats':[], 'signature':chain_mac(self.keys[key_id], b'root', root).hex()})
        unpack(result)
        return result

    def verify(self, token):
        data = unpack(token)
        root = data['root']
        if root['key_id'] not in self.keys: raise CapabilityError('unknown capability key')
        signature = chain_mac(self.keys[root['key_id']], b'root', root)
        for caveat in data['caveats']:
            signature = chain_mac(signature, b'caveat', caveat)
        if not hmac.compare_digest(signature.hex(), data['signature']):
            raise CapabilityError('invalid capability signature')
        return Verified(data)


def from_config(value):
    if value is None: return None
    if type(value) is not dict: raise ValueError('capability_keys must map key IDs to 64 hex characters')
    if any(type(v) is not str or not re.fullmatch('[a-f0-9]{64}', v) for v in value.values()):
        raise ValueError('capability keys must be 64 lowercase hex characters')
    return Keys({k:bytes.fromhex(v) for k,v in value.items()})


def main():
    """Offline CLI reads credentials from files, never command-line arguments."""
    import argparse
    import os
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['attenuate','describe'])
    parser.add_argument('--input', type=Path, required=True, help='File containing only the parent token')
    parser.add_argument('--contract', type=Path, help='Complete narrower child contract JSON')
    parser.add_argument('--output', type=Path, help='New owner-only child token file')
    args = parser.parse_args()
    with args.input.open('rb') as stream: raw = stream.read(MAX_TOKEN+2)
    if len(raw) > MAX_TOKEN+1: parser.error('input exceeds token bound')
    token = raw.decode('ascii').strip()
    if args.command == 'describe':
        print(json.dumps(describe(token), indent=2))
        return
    if not args.contract or not args.output: parser.error('attenuate requires --contract and --output')
    with args.contract.open('rb') as stream: raw = stream.read(16385)
    if len(raw)>16384: parser.error('contract exceeds size bound')
    validate_json(raw)
    child = attenuate(token, contract(json.loads(raw)))
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd,'w') as stream: stream.write(child+'\n')
    print(json.dumps({'visa_id':describe(child)['visa_id'],'output':str(args.output),'signature_verified':False}))


if __name__ == '__main__': main()
