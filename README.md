# Nodi

You ask an AI agent to buy a monitor within a budget. What stops it from overspending, sharing unrelated files, or giving another agent more permission than it has? Nodi checks each proposed tool action against limits you set for the task, then allows it, blocks it, or asks for your approval.

**[Try the sandbox](https://nodi-7fsc.onrender.com/)** — the free instance may take a moment to wake up. Tools are simulated, and the intent check currently covers monitor procurement. This is a prototype, not a general defense against prompt injection.

## What gets checked

A temporary “mission contract” sets the allowed tools, resources, budget, and expiry. Every action must satisfy those limits, including limits inherited from a parent agent.

- Delegated agents share the original budget, so splitting a task cannot create extra spending money.
- Approval applies to one exact action and expires after five minutes. It cannot override a hard limit.
- Prices and destinations come from the tool adapter, not claims supplied by the agent.
- Each simulated action, budget update, and audit entry is saved in one SQLite transaction. They either all succeed or all roll back.

Built with **Python, FastAPI, and SQLite**, with an optional MCP bridge. Real payments would need additional handling: a database transaction cannot undo a payment made through an external API.

## Run locally

Requires Python 3.11+ on macOS or Linux.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]' -c requirements.lock
python -m pytest -q
python -m nodi demo
python -m nodi init
python -m nodi serve
```

Open http://127.0.0.1:8000. `init` creates separate administrator and agent credentials in `.nodi.local.json`; keep that file private.

## What the tests establish

Tests cover budget races, delegation, approval replay, expiry, and rollback. The [42 evaluation cases](nodi/data/cases.json) were developed alongside the code: they are regression checks, not an independent security benchmark. Intent scores are heuristic, not probabilities.

[Generated invariant tests](docs/invariants.md) · [Detailed walkthrough and results](GUIDE.md) · [Architecture](docs/architecture.md) · [Security limits](SECURITY.md) · [Evaluation](docs/evaluation.md)
