import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
import pytest
import httpx
from nodi.models import draft
from nodi.gateway import Gateway

SCRIPT = Path(__file__).resolve().parents[1] / 'examples' / 'agent_client.py'
spec = importlib.util.spec_from_file_location('agent_example', SCRIPT)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)
TOKEN = 'a'*40


def test_required_request_id():
    with pytest.raises(SystemExit) as error: example.main(['search'])
    assert error.value.code == 2


@pytest.mark.parametrize('response', [{}, {'request_id':'different'}, {'request_id':'one','visa_id':'v','decision':'ALLOW','executed':False}])
def test_unbound_execution_response(response):
    client = example.AgentClient('https://gateway.example', TOKEN)
    class Stub:
        def open(self, request, timeout): return io.BytesIO(json.dumps(response).encode())
    client.opener = Stub()
    with pytest.raises(example.ClientError): client.call('search','one','v')


def test_unknown_status_is_read_only():
    client = example.AgentClient('https://gateway.example', TOKEN)
    class Stub:
        def open(self, request, timeout):
            assert request.method == 'GET' and request.data is None
            assert request.full_url == 'https://gateway.example/requests/status?request_id=one'
            raise HTTPError(request.full_url,404,'sensitive',{},None)
    client.opener = Stub()
    assert client.call('status','one')['found'] is False


def test_real_example_retry_status_and_identity(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'admin_token':'x'*40,'agent_tokens':{TOKEN:'a','b'*40:'b'},'database':Gateway(str(tmp_path/'state.db')).path}))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    server=subprocess.Popen([sys.executable,'-m','nodi','serve','--config',str(config),'--port',str(port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def command(*args, token=TOKEN):
        proc=subprocess.run([sys.executable,str(SCRIPT),'--url',origin,*args],env={**os.environ,'NODI_AGENT_TOKEN':token},capture_output=True,text=True,timeout=15)
        assert proc.returncode==0,proc.stderr
        assert token not in proc.stdout+proc.stderr
        return json.loads(proc.stdout)
    try:
        with httpx.Client(base_url=origin,trust_env=False) as client:
            for _ in range(100):
                try:
                    if client.get('/health').status_code==200: break
                except httpx.ConnectError: pass
                time.sleep(.05)
            else: pytest.fail('gateway did not start')
            headers={'Authorization':'Bearer '+'x'*40}
            visa=client.post('/visas',headers=headers,json=draft('Buy a monitor under $300.','a').dictionary()).json()['visa_id']
            first=command('search','--request-id','stable','--visa',visa)
            second=command('search','--request-id','stable','--visa',visa)
            assert first['effect_id']==second['effect_id'] and second['replayed']
            before=client.get('/control',headers=headers).json()
            result=command('status','--request-id','stable')
            assert result['state']=='completed' and result['last_response']['effect_id']==first['effect_id']
            assert command('status','--request-id','stable',token='b'*40)['found'] is False
            assert client.get('/control',headers=headers).json()==before
            assert len(before['effects'])==1
    finally:
        server.terminate()
        server.wait(timeout=10)
