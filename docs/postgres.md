# PostgreSQL ledger backend

SQLite remains the default. PostgreSQL is an optional ledger backend; protected
adapters, deterministic policy, semantic tightening, exact approvals, capability
bindings and outbox accounting retain the same gateway path. This does not migrate
existing SQLite data or connect a real payment provider.

## Local setup

```sh
python -m pip install -e '.[test,mcp,postgres]' -c requirements.lock
docker compose -f docker/compose.postgres.yml up -d --wait
```

Set the existing private configuration's `database` field to
`postgresql://postgres:local-test-only@127.0.0.1:55432/nodi`, then run
`python -m nodi serve`. These credentials belong only to the dedicated loopback test
container. The environment API entry point accepts the same URL through `NODI_DB`.
The hosted demo intentionally retains its SQLite-only configuration.

The default schema is `nodi`. An optional `nodi_schema=nodi_<lowercase alphanumeric>`
query parameter selects an isolated schema; it is removed before passing the DSN to
the driver. Schema names are strictly validated, not accepted as arbitrary SQL.
The operator must supply a trusted database role with schema/table creation rights.
For remote use, configure PostgreSQL TLS verification and least-privilege roles;
SQLite's owner/mode-bit file checks do not apply to a remote database. No public
connection string or production credentials are supplied by the project.

Stop the test service with `docker compose -f docker/compose.postgres.yml down`.
Adding `--volumes` deletes that local test database. Production migration, backups,
HA, connection credential rotation and disaster recovery are separate operational
work; changing the URL does not copy an existing ledger.

## Transaction strategy and audit ordering

Writers use READ COMMITTED, acquire `ledger_mutex` with SELECT FOR UPDATE **before
reading authorization state**, and lock each ancestor visa row with FOR UPDATE.
All normal writers—including issuance, lifecycle edits, approvals, capability
materialization, outbox claims and settlement—use this same transaction boundary.
The global mutex excludes concurrent tree changes, request insertions and budget
updates while the ancestor locks protect the rows consulted by policy.

This deliberately preserves a single ordered audit stream. Locking only currently
known ancestors would not serialize new siblings, exact-request races and the
shared hash-chain tail. Removing the mutex requires a redesigned audit order and
proven tree/request locking strategy; this backend does not claim independent
missions execute their write transactions concurrently. There are no optimistic
transaction retries around protected effects. A failed or uncertain commit must be
resolved with the existing request ID/outbox reconciliation rules.

PostgreSQL READ COMMITTED gives subsequent statements a current committed view after
waiting for the mutex. Starting a repeatable snapshot before waiting could retain
stale policy; that is not the writer design here. Read-only views use REPEATABLE READ
READ ONLY snapshots without acquiring the write mutex. They cannot mutate state.
See PostgreSQL's [explicit-locking guidance](https://www.postgresql.org/docs/18/explicit-locking.html)
and [application consistency rules](https://www.postgresql.org/docs/18/applevel-consistency.html).

Audit sequence numbers allocate under the mutex inside the transaction. Unlike
ordinary PostgreSQL sequences, aborted audit inserts leave no gaps, preserving the
existing offline checkpoint verifier. Visa/request pagination cursors use monotonically
allocated PostgreSQL sequences; gaps are allowed in those cursors. Scope IDs and
request fingerprints retain their existing meanings.

## Adapter and resource bounds

`database.py` owns SQLite transactions and backend selection. `postgres.py` adapts
only Nodi's static SQL subset: parameter markers, exact JSON-field extraction,
conflict-ignore inserts and schema declarations. Values remain parameterized. This
is not a general SQL transpiler and never receives agent SQL: sandbox database
queries still run inside the separate read-only SQLite worker.

The optional Psycopg pool has at most 64 connections and 256 waiting requests per
configured database/process, a 15-second checkout/lock timeout and a 30-second
statement timeout. At most eight configured pools are retained per process; idle
connections shrink and pools close on normal process exit. Pool acquisition and
reconnection do not retry application transactions or external effects. Connection
pooling follows the driver's [transaction context contract](https://www.psycopg.org/psycopg3/docs/advanced/pool.html).
Slow evaluators still hold the writer lock. PostgreSQL does not remove this
availability/throughput limitation. A single statement timeout is not a complete
HTTP or evaluator execution deadline.

Storage failures use a sanitized error contract and HTTP 503; responses do not
expose DSNs, passwords or query parameters. A 503 does not prove that a commit or an
external effect did not happen. Standard request replay and UNKNOWN recovery still
apply. The mock payment provider deliberately retains its independent SQLite store;
its durable state must not share the authorization transaction.

## Tests and benchmarks

```sh
HYPOTHESIS_PROFILE=nodi-ci python -m pytest -q
NODI_TEST_POSTGRES_DSN=postgresql://postgres:local-test-only@127.0.0.1:55432/nodi \
  HYPOTHESIS_PROFILE=nodi-ci python -m pytest -q
NODI_BENCH_POSTGRES_DSN=postgresql://postgres:local-test-only@127.0.0.1:55432/nodi \
  python -m benchmarks.concurrency --output /tmp/nodi-concurrency.json
```

Use a **dedicated test database**. The test-only fixture maps portable in-process
gateway paths to isolated PostgreSQL schemas and drops those schemas afterward.
Restart/crash workers receive the explicit PostgreSQL URL in `gateway.path`.
The entire suite runs in both CI matrices, including all three state machines.
POSIX/SQLite file-security tests, the explicitly SQLite hosted-demo tests, SQLite tool fixtures, independent mock-provider
stores remain SQLite; CLI configuration file permission tests also remain filesystem tests. Real HTTP/operator/MCP subprocess tests receive the selected ledger URL;
they are not misrepresented as PostgreSQL file behavior. No PostgreSQL test switch
is interpreted by production code. Optional public-network/Docker tests retain
their existing explicit opt-ins; the separate Docker CI job exercises real containers, and the PostgreSQL jobs repeat the real-container checks against their server-backed ledger.

The concurrency runner uses 10/50/100 barrier-synchronized workers for shared-budget
races, exact-approval races and duplicate requests. It records p50/p95/p99 latency,
throughput, errors, raw samples, effect/budget/audit assertions, source hashes,
Python/driver/server versions and host/Docker resource information. Setup and teardown
are excluded; pool checkout, database work and approval calls are included. These
are direct Python gateway operations, excluding HTTP/MCP transport. Completed-operation throughput includes policy denials and replays, not just new effects; approval-race operations include both approval and execution calls. These are small local bursts, not sustained production load or evidence of horizontal
write scalability. Run it without competing tests for interpretable local results.
Set `NODI_BENCH_SERVER_NOTE` to record the actual server location. Local Docker resource information describes the client host and does not establish remote server hardware. SQLite on the host versus PostgreSQL in Docker is a deployment-path comparison, not an isolated engine ranking.
No real effects, emails or paid model calls occur.
