"""Environment-only entry point for a single-instance hosted demonstration."""

import os
from pathlib import Path

from .api import create_app
from .gateway import Gateway


def create_hosted_app():
    admin = os.environ.get("NODI_ADMIN_TOKEN", "")
    agent = os.environ.get("NODI_AGENT_TOKEN", "")
    database = os.environ.get("NODI_DB", "")
    if len(admin) < 32 or len(agent) < 32 or admin == agent:
        raise RuntimeError("Set distinct administrator and agent secrets of at least 32 characters")
    path = Path(database)
    if not database or not path.is_absolute() or not path.parent.is_dir():
        raise RuntimeError("NODI_DB must be an absolute path inside an existing directory")
    app = create_app(Gateway(str(path)), admin, {agent: "procurement"})
    app.state.hosted_demo = True
    from .sandbox import Sandbox

    app.mount("/sandbox", Sandbox())
    return app
