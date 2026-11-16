# Nodi

You ask an AI agent to buy a monitor within a budget. What stops it from overspending, sharing unrelated files, or giving another agent more permission than it has? Nodi checks each proposed tool action against limits you set for the task, then allows it, blocks it, or asks for your approval.

**[Try the sandbox](https://nodi-7fsc.onrender.com/)** — the free instance may take a moment to wake up. Tools are simulated. The browser demo covers monitor procurement; the backend also supports a bounded research domain. This is a prototype, not a general defense against prompt injection.

## What gets checked

A temporary “mission contract” sets the allowed tools, resources, budget, and expiry. Every action must satisfy those limits, including limits inherited from a parent agent.

- Delegated agents share the original budget, so splitting a task cannot create extra spending money.
- Approval applies to one exact action and expires after five minutes. It cannot override a hard limit.
- Prices and destinations come from the tool adapter, not claims supplied by the agent.
- Each simulated action, budget update, and audit entry is saved in one SQLite transaction. They either all succeed or all roll back.

Built with **Python, FastAPI, and SQLite**, with an optional MCP bridge and opt-in Docker read-only tools. A reservation/outbox flow demonstrates crash recovery with a durable mock payment provider; no real-money processor is connected.

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

## Domain policies

Compile a reviewed JSON policy without issuing authority:

```sh
python -m nodi policy validate examples/policies/research.json
python -m nodi policy explain examples/policies/research.json
python -m nodi evaluate --domain research
```

[Domains and policy files](docs/domains.md) explains scopes, deny patterns, and the
existing administrator issuance workflow. Research web/file tools use offline
fixtures and simulated writes; they do not access the network or host filesystem.

## Real read-only tools (local Docker)

The optional [Docker backend](docs/sandbox-execution.md) adds workspace file reads,
restricted SQLite SELECT queries, fixed diagnostic commands, and approved HTTPS
GETs through a private egress proxy. Enable it explicitly with a separate `sandbox`
mission. Existing simulations and the public website keep their current behavior.
Real filesystem writes remain disabled; the outbox milestone currently covers mock payments.

## Crash-safe mock payments

An opt-in `payments` mission reserves shared budget before queuing a charge. A
worker uses a persistent idempotency key; uncertain outcomes keep their reservation
until provider reconciliation confirms success or failure. Authorization and payment
completion are reported separately. [Setup and recovery semantics](docs/outbox.md).

```sh
python -m nodi.outbox_demo
```

The demo loses a mock success response and recovers without another charge. It uses
local temporary stores and never moves real money.

## What the tests establish

Tests cover budget races, delegation, approval replay, expiry, and rollback. The [42 evaluation cases](nodi/data/cases.json) were developed alongside the code: they are regression checks, not an independent security benchmark. Intent scores are heuristic, not probabilities.

[Generated invariant tests](docs/invariants.md) · [Detailed walkthrough and results](GUIDE.md) · [Architecture](docs/architecture.md) · [Security limits](SECURITY.md) · [Evaluation](docs/evaluation.md)

## Evaluation and external recovery

The [benchmark tools](benchmarks/README.md) include a 200-attack tool-output injection
suite, isolated guard comparisons, held-out purpose expectations and writer-lock
measurements. Live model runs require explicit opt-in and credentials; skipped runs
are not model performance results. A separate durable loopback service exercises
HTTP recovery under process death, retries and concurrent workers. This remains a
controlled test integration, not a real payment connector.

[Recorded local evidence](benchmarks/results/local/README.md) distinguishes offline
measurements from unmeasured live performance and documents label limitations.

## Offline scoped delegation

Optional [capability tokens](docs/capabilities.md) let a holder derive a narrower
child credential offline. Execution still consults the gateway ledger for shared
budgets, revocation, expiry and exact approvals. This custom chained-HMAC prototype
is disabled by default and does not replace server-side enforcement.

```sh
python -m nodi.capability_demo
```
