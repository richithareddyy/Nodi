"""Bounded REST adapters. Credentials/endpoints never come from model tool calls."""
import asyncio
import json
import os

from nodi.json_input import validate_json


class ProviderError(RuntimeError): pass


def config():
    provider = os.environ.get('NODI_BENCH_PROVIDER', 'openai')
    if provider not in ('openai','anthropic'):
        raise ValueError('NODI_BENCH_PROVIDER must be openai or anthropic')
    key = os.environ.get('NODI_BENCH_API_KEY') or os.environ.get('OPENAI_API_KEY' if provider == 'openai' else 'ANTHROPIC_API_KEY')
    model = os.environ.get('NODI_BENCH_MODEL')
    return provider, key, model


class Model:
    def __init__(self, provider, key, model, *, max_calls=100, transport=None):
        if provider not in ('openai','anthropic') or not key or not model:
            raise ValueError('provider, API key and explicit tool-capable model are required')
        self.provider, self.key, self.model = provider, key, model
        self.max_calls, self.calls, self.transport = max_calls, 0, transport

    def request(self, messages, tools):
        if self.provider == 'openai':
            return 'https://api.openai.com/v1/chat/completions', {'Authorization': 'Bearer ' + self.key}, {
                'model': self.model, 'messages': messages, 'tools': [{'type':'function','function': t} for t in tools],
                'max_completion_tokens': 1200, 'store': False}
        converted = []
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        for m in messages:
            if m['role'] == 'system': continue
            if m['role'] == 'tool':
                converted.append({'role':'user','content':[{'type':'tool_result','tool_use_id':m['tool_call_id'],'content':m['content']}]})
            elif m['role'] == 'assistant':
                blocks = [{'type':'text','text':m['content']}] if m.get('content') else []
                for call in m.get('tool_calls', []):
                    blocks.append({'type':'tool_use','id':call['id'],'name':call['function']['name'],
                                   'input':json.loads(call['function']['arguments'])})
                converted.append({'role':'assistant','content': blocks or [{'type':'text','text':'Continue.'}]})
            else: converted.append({'role':'user','content':m['content']})
        return 'https://api.anthropic.com/v1/messages', {'x-api-key':self.key,'anthropic-version':'2023-06-01'}, {
            'model':self.model,'system':system,'messages':converted,'max_tokens':1200,
            'tools':[{'name':t['name'],'description':t['description'],'input_schema':t['parameters']} for t in tools]}

    async def complete(self, messages, tools):
        import httpx2
        if self.calls >= self.max_calls: raise ProviderError('model call budget exhausted')
        self.calls += 1
        url, headers, payload = self.request(messages, tools)
        raw_request = json.dumps(payload, allow_nan=False).encode()
        if len(raw_request) > 262144: raise ProviderError('conversation exceeds request bound')
        try:
            async with asyncio.timeout(60):
                async with httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=45, transport=self.transport) as http:
                    async with http.stream('POST', url, content=raw_request, headers={**headers,'Content-Type':'application/json'}) as response:
                        if response.status_code != 200: raise ProviderError(f'model endpoint returned HTTP {response.status_code}')
                        raw = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=16384):
                            raw.extend(chunk)
                            if len(raw) > 262144: raise ProviderError('model response too large')
            validate_json(bytes(raw))
            data = json.loads(raw)
            if self.provider == 'openai':
                message = data['choices'][0]['message']
                result = {'role':'assistant','content':message.get('content') or '', 'tool_calls':message.get('tool_calls') or []}
            else:
                result = {'role':'assistant','content':'\n'.join(b['text'] for b in data['content'] if b['type']=='text'),
                          'tool_calls':[{'id':b['id'],'type':'function','function':{'name':b['name'],'arguments':json.dumps(b['input'])}}
                                        for b in data['content'] if b['type']=='tool_use']}
            if type(result['content']) is not str or type(result['tool_calls']) is not list or len(result['tool_calls']) > 4:
                raise ValueError('invalid model response')
            for call in result['tool_calls']:
                if (type(call) is not dict or type(call.get('id')) is not str or len(call['id']) > 200
                        or call.get('type') != 'function' or type(call.get('function')) is not dict
                        or type(call['function'].get('name')) is not str or type(call['function'].get('arguments')) is not str):
                    raise ValueError('invalid function call')
            return result, {'model':data.get('model', self.model),'usage':data.get('usage'), 'response_id':data.get('id'), 'raw': data}
        except (httpx2.HTTPError, TimeoutError, ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise ProviderError('model response unavailable or invalid') from None
