# Security model

The gateway host, database, administrator credential, policy code and adapter metadata are trusted. An adversary may control an agent, arguments and retrieved context; it may replay calls, invent identities, inject instructions, race siblings or delegate excessive authority.

The HTTP API is the supported trust boundary. Direct Python methods are trusted application interfaces, not independently authenticated endpoints. Run agents separately, keep provider credentials and SQLite access out of their environment, and restrict egress so protected services cannot be reached directly.

## Enforced properties

Every simulated effect follows current identity, ancestry and deterministic checks. Costs and destinations come from trusted adapters. Human/semantic decisions cannot bypass hard denials. Approvals bind exact requests; retries do not duplicate simulated effects. Descendants share ancestor budgets and inherit revocation. Unknown arguments cannot execute, unknown intent requires review, and audit failure rolls back the effect.

## Limitations

The intent baselines are narrow and may over-escalate legitimate wording. Procurement retains its monitor grammar; research permits trusted fixture reads and requires review for free-form summary writes. Scores are not calibrated. Model review may be wrong or adversarially influenced and never overrides deterministic checks.

Static bearer credentials are for local development. There is no OAuth/OIDC, tenant isolation, token rotation service, user-level ownership, rate limiting or production TLS termination. One administrator has global authority. The default server binds to loopback; public deployment requires a separate security design.

An explicitly configured semantic service receives mission/action/context. Use an approved service and consider retention and leakage. HTTPS redirects are refused and response size is bounded. Timeout is per network operation, not a total deadline; slow calls can hold the database writer lock.

Audit data contains arguments/context and is not encrypted at the application layer. The gateway creates database files with mode 0600 and checks POSIX ownership, permissions, file type and link count before each connection, including existing SQLite journal/WAL sidecars. Protect host storage and backups. A privileged attacker can rewrite or truncate the local hash chain; it is not an externally anchored ledger.

There are no real payments, MCP Streamable HTTP endpoint, arbitrary shell commands/tool URLs, or general-purpose agents. An explicitly enabled Docker backend supports only bounded real reads; filesystem writes remain disabled. The separate mock-payment domain uses a reservation/outbox flow, without a real-money processor. The research domain simulates allowlisted web/file reads and summary writes using fixture data and transactional records only. The optional MCP stdio bridge uses an agent-only credential to call the HTTP gateway; it does not add an authorization bypass or an OS sandbox. Regression tests cannot prove containment for tools that do not exist.

Before connecting real services, add verified workload identity, provider credentials held only by the gateway, egress isolation, versioned adapter schemas, confirmed costs, reservations/reconciliation, authenticated human sessions, tenant boundaries, rate limits, observability and independent adversarial review.

See [audit exports and independently held checkpoints](docs/audit-exports.md) for offline prefix verification and its trust limitations.

Expiration is checked again after intent evaluation, immediately before constructing the final decision and writing a simulated effect. The host wall clock is trusted; this is not a guarantee that the SQLite commit finishes before the expiration instant. Remote review still holds the writer transaction, and this check does not impose a total evaluator timeout.

Database enforcement currently requires POSIX `O_NOFOLLOW`; native Windows is unsupported. Keep the database directory and its ancestors under trusted operator control. Checks and SQLite opens are separate operations, so this is not protection against an attacker who can replace directory entries between them. Extended ACLs, privileged users and backup permissions are outside these mode-bit checks. See [storage requirements](docs/storage.md).

HTTP request bodies must finish within five seconds of entering the body middleware. This limits slow body reads but is not a general denial-of-service defense: header handling, connection limits, rate limiting and total execution deadlines still require separate controls.

Domain registration is trusted server code. A stored purpose selects the adapter;
actions and policy files cannot import arbitrary code. Policy files compile deny
patterns to exact restrictions, retain baseline prohibitions, and never issue or
execute authority by themselves. See [domain boundaries](docs/domains.md).

## Optional real read boundary

The `sandbox_readonly` domain requires operator-configured Docker execution. The
Docker daemon, immutable worker image, egress proxy and mount roots join the trusted
computing base. Agents must have no Docker, host filesystem or private proxy-socket
access. Workers have no IP network, read-only mounts/root, no capabilities, a nonroot
user and bounded resources/deadlines. HTTPS requests go only through the private
proxy, which restricts exact documentation destinations, resolves and validates all
DNS answers, pins the connected IP, verifies TLS, and refuses redirects.

Real file/SQL reads require exact-action approval. Content remains untrusted even
when read access is authorized. A read may happen before an audit/commit failure;
retry can repeat it, and GETs can leave remote access logs. No external atomicity or
exactly-once guarantee is claimed. The shared writer lock is held during the read;
this is not a throughput or denial-of-service solution. No real writes are enabled.
See [setup, restrictions and regression checks](docs/sandbox-execution.md).

## Mock payment outbox

Authorized payments reserve shared ancestor budgets/action slots before dispatch.
The durable worker rechecks current authority before granting an external dispatch,
then calls the independent mock provider outside the ledger transaction. Provider
idempotency keys bind exact payloads. Unknown outcomes retain reservations until
lookup and, when necessary, fenced cancellation establish a terminal result.
A late response or expired worker lease cannot release or consume a hold twice.

Dispatch is the commitment point: later revocation/expiry cannot retroactively undo
an already accepted provider operation. Pending work is cancelled on edits or
revocation; unknown work is reconciled, never blindly treated as failure. The mock
provides permanent idempotency records and atomic cancellation tombstones. A real
provider must supply equivalent guarantees or retain uncertainty; no production
payment connector is implemented. Provider/gateway databases and the worker are
trusted, private operator components, not agent tools. See [recovery semantics and
validation](docs/outbox.md).

## Evaluation boundaries

The Phase 5 counterfactual modes exist only under `benchmarks/`, are not packaged
with the application, and perform inert simulated effects. Production authorization
and MCP tools remain unchanged. Synthetic labels are specification-derived and not
independently validated ground truth; skipped or scripted runs are not live model
performance evidence. The separate loopback service tests recovery assumptions,
not real payment-provider compatibility or universal exactly-once execution.

Slow/failing purpose evaluators still hold the writer transaction. Local contention
reports quantify this limitation; they do not establish production capacity or remove
the need for bounded remote evaluators. Moving checks outside the lock would require
policy-version validation and atomic budget reservation/revalidation. No such move
is made here. See [benchmark methodology](benchmarks/README.md).
