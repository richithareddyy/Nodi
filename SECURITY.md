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

There are no real payments, MCP Streamable HTTP endpoint, shell tools, arbitrary tool URLs, real filesystem adapters or general-purpose agents. The research domain simulates allowlisted web/file reads and summary writes using fixture data and transactional records only. The optional MCP stdio bridge uses an agent-only credential to call the HTTP gateway; it does not add an authorization bypass or an OS sandbox. Regression tests cannot prove containment for tools that do not exist.

Before connecting real services, add verified workload identity, provider credentials held only by the gateway, egress isolation, versioned adapter schemas, confirmed costs, reservations/reconciliation, authenticated human sessions, tenant boundaries, rate limits, observability and independent adversarial review.

See [audit exports and independently held checkpoints](docs/audit-exports.md) for offline prefix verification and its trust limitations.

Expiration is checked again after intent evaluation, immediately before constructing the final decision and writing a simulated effect. The host wall clock is trusted; this is not a guarantee that the SQLite commit finishes before the expiration instant. Remote review still holds the writer transaction, and this check does not impose a total evaluator timeout.

Database enforcement currently requires POSIX `O_NOFOLLOW`; native Windows is unsupported. Keep the database directory and its ancestors under trusted operator control. Checks and SQLite opens are separate operations, so this is not protection against an attacker who can replace directory entries between them. Extended ACLs, privileged users and backup permissions are outside these mode-bit checks. See [storage requirements](docs/storage.md).

HTTP request bodies must finish within five seconds of entering the body middleware. This limits slow body reads but is not a general denial-of-service defense: header handling, connection limits, rate limiting and total execution deadlines still require separate controls.

Domain registration is trusted server code. A stored purpose selects the adapter;
actions and policy files cannot import arbitrary code. Policy files compile deny
patterns to exact restrictions, retain baseline prohibitions, and never issue or
execute authority by themselves. See [domain boundaries](docs/domains.md).
