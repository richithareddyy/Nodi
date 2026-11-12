# Domains and declarative mission policies

Nodi selects a trusted domain from the **stored contract's purpose**, never from
an action argument, agent-provided module path or URL. The same gateway transaction
still performs preparation, full-ancestry policy checks, intent review, simulation,
usage accounting and audit. Approval cannot override a hard denial.

## Domain interface and compatibility

[Domain](../nodi/domains/base.py) declares a name, canonical purpose, closed tool
argument shapes, trusted metadata, tools/resources, `prepare(action) -> Prepared`,
an intent evaluator, a default contract, an evaluation dataset and a setup action.
Preparation validates value types, sizes and destinations as well as required and
optional argument keys. It computes trusted cost and impact but performs **no I/O
or external effect**. The gateway alone writes simulated effects.

The server-owned [registry](../nodi/domains/__init__.py) explicitly registers two
implementations. To add a domain, implement that interface, register its reviewed
name/purpose, and provide adapter, intent, policy and evaluation tests. There is no
runtime loading of arbitrary plugins from agent requests or policy files. Python
plugin code is trusted operator code, not a security sandbox.

| Domain name | Stored purpose | Implementation |
| --- | --- | --- |
| `procurement` | `monitor_procurement` | [Procurement plugin](../nodi/domains/procurement/domain.py) |
| `research` | `research_assistant` | [Research plugin](../nodi/domains/research/domain.py) |

Existing procurement contracts retain their original shape, tool IDs, defaults
and purpose; no ledger migration is needed. `nodi.tools` and `nodi.intent` retain
compatibility imports, while the catalog, adapter, bounded drafting grammar and
procurement evaluator live in the procurement package. `Contract` retains legacy
procurement defaults for callers that construct it directly. A research contract
must explicitly supply research scopes, or use the compiler/domain factory.

Delegation and modification cannot change a contract's purpose. Every contract
validates scopes against its selected domain. A procurement visa cannot run a
research tool, and a child cannot switch domains. The optional semantic reviewer
uses the selected domain's baseline and can only tighten it.

## Policy files

Phase 2 supports **JSON**, not YAML. This keeps parsing strict and adds no runtime
dependency. Duplicate keys, unknown top-level or nested fields, nonfinite numbers,
invalid types, unknown tools/resources and files larger than 64 KiB are rejected.
Booleans are not numbers. Dollar values are parsed as decimals and must represent
exact whole cents; fractional cents are rejected rather than rounded.

```sh
python -m nodi policy validate examples/policies/procurement.json
python -m nodi policy explain examples/policies/procurement.json
python -m nodi policy explain examples/policies/research.json --agent researcher
```

`validate` exits zero for a valid policy and two for an invalid file. `explain`
returns a `contract` object plus effective tools, blocked tools, argument shapes,
alias resolutions and unmatched deny patterns. Neither command issues authority,
creates a ledger, or executes a tool. `expires_at` is computed at compilation time;
recompile an old preview before reviewing and issuing it.

Required fields:

| Field | Meaning |
| --- | --- |
| `mission` | Nonblank mission description, subject to the contract's length bound |
| `domain` | Registered public name, `procurement` or `research` |
| `allow` | Exact domain tool IDs or documented aliases; no allow globs |
| `deny` | Case-sensitive shell-style tool globs (`*`, `?`, character classes) |
| `budget` | `max_usd` and `max_transaction_usd`, nonnegative exact-cent amounts, each at most one billion USD |
| `delegation` | `max_depth`, integer within the existing contract's depth limit |
| `approval` | `purchase_above_usd`, compiled to the existing cost threshold; intent may require approval even for a zero-cost write |
| `expires_in` | Positive integer plus `s`, `m`, `h` or `d`, at most seven days |

Optional fields are `agent_id`, `max_actions`, and `resources` with exact-ID
`allow` and `deny` lists. Identity defaults to the domain name; `--agent` overrides
it. Omitted resources and action counts use the domain's reviewed default contract,
and `explain` reports when resources were defaulted. For least authority, supply
explicit resource scopes. Threshold must not exceed transaction cap, which must
not exceed total budget.

The procurement policy supports `web.search` as an alias for `search_products`
and `product.lookup` for `compare_products`. The latter takes the original
`product_ids` list; it does not add a general product or laptop catalog. Execution
uses the canonical IDs shown in `explain`. The procurement baseline still supports
monitors only, regardless of a policy's free-form mission name.

### Deny always wins

Deny patterns match canonical tool IDs **and** their aliases, expand to concrete
forbidden operations, and remove those operations from the allowed set. Domain
baseline prohibitions are retained. Resource denies likewise remove matching
exact IDs from allowed resources. The resulting ordinary `Contract` goes through
unchanged `policy.checks()` and `policy.attenuation()` enforcement.

A pattern such as `shell.*` currently matches no registered tool. It is accepted
and explicitly listed in `unmatched_deny_patterns`; unknown tools cannot execute
anyway. Pattern text is not stored as future dynamic authority: the compiled
contract contains exact sets. Adding a tool later does not add it to an existing
visa's allowlist. Recompile and review policies after changing the domain registry.
Children must retain the compiled forbidden set and cannot widen the allowed set.

### Review and issue through existing controls

After reviewing the `explain` output, extract only its `contract` object into a
contract JSON file. The existing administrator workflow accepts it:

```sh
python -m nodi.operator --config .nodi.local.json issue --contract reviewed-contract.json
```

The agent identity must correspond to a server-configured agent credential. A
policy file does not create an identity or grant administrator access. Subsequent
actions still go through `/execute` (or the existing MCP bridge); no direct domain
execution route was added. The public browser sandbox remains the procurement
demo; Phase 2 does not add a research UI or public account provisioning.

## Research simulation

The research domain uses a logical workspace and fixed, trusted fixtures:

| Tool | Arguments | Restrictions |
| --- | --- | --- |
| `web.fetch` | `url` | Exact fixture URLs for the Python tutorial and SQLite documentation; resource `web:docs.python.org` |
| `filesystem.read` | `path` | Only `notes/source.txt`; resource `workspace:notes/source.txt` |
| `filesystem.write` | `path`, `content` | Only `summaries/summary.txt`, bounded nonblank content, exact-content human review required |

The exact URLs and fixture contents are in the plugin's trusted metadata. URL
credentials, extra query strings, unknown destinations, absolute paths, traversal,
backslashes, mismatched resource claims and extra argument keys are rejected.
The text returned is a small **offline fixture**, not current content fetched from
the named website. A summary write records the proposed text in the transactional
SQLite effect log; it does not write a host file, and later reads do not pretend to
read that effect as a mutable filesystem. Its response is marked `simulation`.

This is a second enforcement domain, not real filesystem confinement or SSRF
protection. No DNS resolution, HTTP fetch, host file read/write or shell execution
occurs. Those belong to Phase 3, with its separate isolation requirements. The
research intent baseline is deliberately bounded: trusted fixture reads may run;
free-form summary text requires human review. It is not a general mission-relevance
classifier or protection against malicious content from arbitrary sources.

## Validation and scope

```sh
HYPOTHESIS_PROFILE=nodi-ci python -m pytest -q
python -m nodi evaluate
python -m nodi evaluate --domain research
python -m nodi demo
python -m pip wheel . --no-deps --wheel-dir dist
python scripts/check_wheel.py dist/nodi-0.1.0-py3-none-any.whl
```

The original procurement dataset is unchanged. The separately packaged
[research cases](../nodi/data/research_cases.json) cover allowed reads, reviewed
writes, path/URL boundaries, identity, expiry, revocation, action limits and
delegation. Reports include domain, dataset hash, case counts and category
counts. These hand-authored regression fixtures are not independently sampled
attacks; their observed rates do not establish population security or meaningful
confidence bounds on attacker success. No production performance or security
rate is claimed. See [evaluation methodology](evaluation.md) and
[generated invariants](invariants.md).
