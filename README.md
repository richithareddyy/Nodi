# Nodi

You ask an AI agent to buy a monitor within a budget. What stops it from overspending, sharing unrelated files, or giving another agent more permission than it has? Nodi checks each proposed tool action against limits you set for the task, then allows it, blocks it, or asks for your approval.

**[Try the sandbox](https://nodi-7fsc.onrender.com/)** — the free instance may take a moment to wake up. Tools in this demo are simulated, and hosted data is temporary and can reset on restart or redeployment. The browser demo covers monitor procurement; the backend also supports a bounded research simulation using fixed page and file fixtures. This is a prototype, not a general defense against prompt injection.

## What gets checked

A temporary “mission contract” sets the allowed tools, resources, budget, and expiry. Every action must satisfy those limits, including limits inherited from a parent agent.

- Delegated agents share the original budget, so splitting a task cannot create extra spending money.
- Approval applies to one exact action. Its deadline is at most five minutes from the first request, capped by the earliest contract expiry in the delegation chain. Approval does not restart that clock or override a hard limit; the agent must retry the identical request to execute it.
- Prices and destinations come from the tool adapter, not claims supplied by the agent.
- Each simulated effect, budget update, and audit entry is saved in one ledger transaction (SQLite by default, optionally PostgreSQL). They either all succeed or all roll back. This does not make external API effects atomic with the ledger.

Built with **Python, FastAPI, and SQLite**, with optional PostgreSQL storage, an [MCP bridge](docs/mcp.md), and opt-in Docker read-only tools. A reservation/outbox flow demonstrates crash recovery with a durable mock payment provider; no real-money processor is connected.

The default intent evaluator is a deterministic, domain-specific heuristic. An optional
[HTTPS semantic reviewer](docs/semantic.md) can tighten its verdict, but cannot relax
the baseline or bypass hard policy. Evaluator failures require human review.

## Run locally

Requires Python 3.11+ on macOS or Linux. Run these commands from the repository root.

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
restricted SQLite SELECT queries, fixed diagnostic commands, and HTTPS GETs to
exact allowlisted documentation URLs through a private egress proxy. Enable it explicitly with a separate `sandbox`
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

## Development checks and CI

Install the optional test dependencies and pinned linter for the full local suite:

```sh
python -m pip install -e '.[test,mcp,postgres,lint]' -c requirements.lock
python -m ruff check .
python -m pytest -q -rs
```

Ruff targets Python 3.11, uses a 100-character line limit, and checks `E`, `F`, `I`,
`B`, `UP`, and `SIM`. The minimal `.[test]` install above can additionally skip
MCP- and PostgreSQL-driver-dependent tests.

Without integration setup, the full local command skips five Docker tests and one
PostgreSQL-only check. Docker tests need `NODI_TEST_SANDBOX_IMAGE`; the public HTTPS
smoke test also needs `NODI_TEST_PUBLIC_HTTP=1`. The PostgreSQL check needs
`NODI_TEST_POSTGRES_DSN` pointing to a dedicated test database. Follow the
[Docker setup](docs/sandbox-execution.md#reproducible-checks) and
[PostgreSQL setup](docs/postgres.md#tests-and-benchmarks) before enabling them.

[CI](.github/workflows/tests.yml) runs SQLite and PostgreSQL suites on Python 3.11
and 3.14, Ruff in both SQLite jobs, and real Docker checks including the public
HTTPS smoke test. Each CI coverage profile permits only the named skips assigned
to another job; missing required setup, unexpected skips, and skipped modules fail
the job. Local opt-in behavior is unchanged. The public HTTPS check depends on the
availability of its external documentation site.

## Evaluation and external recovery

The [benchmark tools](benchmarks/README.md) include a 200-attack tool-output injection
suite plus 40 benign cases, isolated guard comparisons, held-out purpose
expectations and writer-lock measurements. These benchmark cases and labels are
machine-authored specification expectations, not independently validated ground truth. Live model runs require explicit opt-in and credentials; skipped runs
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

## Optional PostgreSQL ledger

SQLite remains the default. [PostgreSQL setup and locking design](docs/postgres.md)
adds a server-backed ledger with the same policy, capability, approval and outbox
checks. A global audit-order lock still serializes writers; this is not a claim of
horizontal write scalability. Both backend test matrices run the enforcement and
invariant suites. [Concurrency measurements](benchmarks/concurrency.py) exercise
shared-budget, approval and duplicate-request races at 10/50/100 workers.
[Recorded local results and uncertainty](benchmarks/results/concurrency/README.md)
include raw samples and deployment/hardware context; they are not production
capacity estimates.

## License

[MIT](LICENSE) — Copyright (c) 2026 Richitha Rekula.
