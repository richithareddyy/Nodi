# Local evaluation and recovery experiments

These source-checkout tools exercise bounded simulations. They do not enable real
checkout, email, arbitrary web research or production benchmark bypasses. Install
`python -m pip install -e '.[test,mcp]' -c requirements.lock` from the repository.

## Phase 5: tool-output injection benchmark

The frozen injection dataset contains 200 attacks and 40 benign cases, split into
120 development and 120 held-out cases. Attack families are disjoint between splits;
variants within a family share construction and are correlated. Encoded instructions,
mission drift, delegation widening, confused deputies, exfiltration, multilingual
text and indirect tool-output instructions are represented. The generator can create
600 attacks plus 40 benign cases with `--variants 30`; more variants are not more
independent attack families. Generated JSON is frozen before evaluation. Do not tune
rules using held-out outcomes; reserve a new independent set after tuning.

Labels and cases are machine-authored, specification-derived expectations, **not
independently validated ground truth**. `oracle.py` labels a deliberately bounded
set of actions independently of gateway verdicts; unknown semantics are unscored.
Private tokens in cases are synthetic canaries, never actual secrets. No human label
validation or general semantic safety coverage is claimed.

Run without paid calls (writes an explicit skipped report):

```sh
python -m benchmarks.injection.runner --output /tmp/nodi-benchmark-skip
```

For a separately authorized live experiment, set `NODI_BENCH_PROVIDER` to `openai` or
`anthropic`, `NODI_BENCH_MODEL` to an explicit tool-capable model, and
`NODI_BENCH_API_KEY` (or the provider-specific key) in the environment. Only adding
`--live` enables potentially billed requests. Missing credentials produce a skipped
report with no performance rates. No key is read from repository credential files.

```sh
python -m benchmarks.injection.runner --live --max-calls 100 --limit 10 \
  --dataset benchmarks/injection/data/heldout.json --output /tmp/nodi-benchmark-live
python -m benchmarks.injection.suite --variants 30 --output /tmp/nodi-larger-suite
```

Each case runs in four separate, adaptively unfolding episodes: no guard, static
allowlist, model judge, and full Nodi with its deterministic domain evaluator.
All go through the production MCP client/server bridge in process, using ASGI HTTP
transport. Full Nodi uses real authorization routes and a fresh local ledger;
other modes use **isolated inert counterfactual endpoints**, never protected
adapters. Benchmark-only delegation wraps the production authenticated delegation
route; it does not alter the production MCP tool surface. Humans are not simulated:
ASK_HUMAN remains unexecuted and counts as containment, not a denial or approval.

A forced legitimate initial read is setup, excluded from scored calls and judge
calls. Its returned description/page/file content receives the attack, never the
trusted system message, contract, catalog prices or stored metadata. Subsequent
proposals come from the model, not the dataset target. The target exists only for
fixture validation. Each mode receives the same initial task and source payload;
subsequent responses and trajectories may differ. Counterfactual outputs are inert
fixtures, so this is not an end-to-end task-success comparison. The model and judge
share the explicit call budget. Calls have bounded time/output; no automatic retries.

Reports include counts, denominators, Wilson 95% intervals, categories, family counts,
review/denial rates, latency, exact dataset SHA-256, UTC date, requested model,
repository commit and dirty status. Raw JSONL retains actual returned model IDs,
usage, responses, judge exchanges, proposals and gateway results. Credentials are
not recorded. Provider error text is suppressed to avoid leaking headers. Protect
reports because future user-supplied source text could be sensitive.

Agent compromise means a harmful tool **attempt**, not an executed effect. Containment
uses harmful attempts as its denominator. Benign false denial excludes legitimate
review thresholds; unnecessary review uses only auto-eligible benign actions.
Incomplete/error/truncated episodes are explicitly counted and excluded from rates;
observed harmful attempts/effects across **all** statuses remain separately visible.
Zero denominators produce null, not 0%. Unscored calls remain visible. No model's
final prose is treated as an executed tool call. Wilson intervals are descriptive,
not population guarantees: cases and adaptive calls are correlated. Overhead is a
descriptive difference in means across trajectories, not a paired causal estimate.
A partial call budget can leave modes unevenly sampled; incomplete status is explicit.

Provider request wiring is covered by mocked transport tests, not verified live
compatibility for every model. API references:
[chat completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [messages](https://platform.claude.com/docs/en/api/messages).
Scripted harness tests never become live model performance results.

## Purpose evaluation and writer contention

```sh
python -m benchmarks.purpose.run --output /tmp/nodi-purpose.json
python -m benchmarks.purpose.contention --output /tmp/nodi-contention.json
```

`purpose/cases.json` separates development and held-out ordinary, ambiguous and
adversarial expectations before measurement. Distinct wording tests ordinary
paraphrases; repeated hard-boundary/fixture anchors check regressions. The splits
are not independent random samples and label provenance is embedded. Reports show
false allows (ALLOW when review/deny expected), false denials (DENY when ALLOW
expected), ordinary review, overall review, exact agreement and latency per split
and category. Review is not silently counted as a denial. These runs measure the
existing deterministic evaluator, **not a remote model**. No baseline rules were
tuned against these cases. Research covers two exact page fixtures, one source file
and a reviewed summary destination; it is not general web research.

The contention experiment starts eight writers together for fast, 50 ms delayed,
and 50 ms failing evaluators. Transaction-entry through BEGIN IMMEDIATE includes
file validation/connect overhead; it is an acquisition-delay proxy, not pure SQLite
lock instrumentation. One small batch per condition is descriptive, hardware- and
scheduler-dependent. The failing evaluator produces ASK_HUMAN. Checks remain
inside the transaction. An event-coordinated test proves another writer waits and
then rechecks the updated budget; an always-approving evaluator cannot override
hard caps. No stale-policy/budget race is introduced to improve timing numbers.

## Controlled external service

`recovery/service.py` runs a separate authenticated loopback HTTP process with its
own durable database. `recovery/client.py` uses fixed loopback destinations, bounded
responses/timeouts, no proxy and no redirects. This is a **controlled test service**,
not a verified third-party payment integration. The production service has no new
public route and real payments remain disabled.

```sh
python -m pytest -q tests/test_external_recovery.py tests/test_outbox.py \
  tests/test_outbox_invariants.py tests/test_benchmarks.py
```

The service supports payload-bound permanent idempotency, authoritative lookup and
an atomic cancellation tombstone that fences late delivery. These are assumptions
needed by this recovery protocol; many real APIs do not supply them. A timeout or
ABSENT lookup alone cannot justify releasing uncertain funds. Identity is bound to
the durable service store; replacements must not silently inherit unresolved work.
See [state transitions](../docs/outbox.md). No universal exactly-once guarantee is
claimed. The tests demonstrate one durable controlled effect per key under the
stated protocol, not atomicity between SQLite and arbitrary external APIs.
