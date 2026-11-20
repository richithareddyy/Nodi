import json
import os
import subprocess
import sys


def test_init_creates_private_distinct_credentials_and_refuses_overwrite(tmp_path):
    config = tmp_path / "private.json"
    command = [sys.executable, "-m", "nodi", "init", "--config", str(config)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    contents = config.read_text()
    data = json.loads(contents)
    assert data["admin_token"] not in data["agent_tokens"]
    assert len(data["admin_token"]) >= 32
    assert data["admin_token"] not in result.stdout
    if os.name == "posix":
        assert config.stat().st_mode & 0o777 == 0o600
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert config.read_text() == contents
