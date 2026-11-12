# Generated enforcement invariants

Phase 1 adds property-based checks around the existing SQLite simulation. It does
not change enforcement, add a real agent, or connect external tools. The test
implementation is [tests/test_invariants.py](../tests/test_invariants.py).

## What is checked

| Invariant | Test oracle and scope |
| --- | --- |
| I1 — Attenuation | Generated children retain the mission and purpose, narrow allowed sets, retain or expand forbidden sets, decrease delegation depth, and never increase budget, transaction cap, approval threshold, action cap or expiry. Each successful generated delegation is independently checked. Separate property tests try individual widenings and require rejection. |
| I2 — Budget conservation | An in-memory ledger records each new committed effect and its ancestry. After every state transition, each stored node's spend must equal the sum of these effects in its subtree and remain within its cap. Action counts must also agree. Sibling authority is shared, not reserved or multiplied. |
| I3 — Execution soundness | Immediately before recording a new effect in the model, independently check active, unexpired ancestry, depth, tool/resource permission, transaction cap, remaining budget and action count. Every effect must have exactly one matching ALLOW authorization event with the same principal, action, effect ID, cost, time and full chain; all recorded policy checks must pass. Stored effect charges are checked against the model's prices. |
| I4 — DENY finality | Hard-policy failures and forbidden uploads cannot execute. Generated histories interleave approvals, exact retries and changing remote-review decisions. A dedicated property starts with a valid approval, restricts an ancestor, then retries both the exact request and new IDs under generated semantic decisions; every attempt must be denied and cannot be approved. |
| I5 — Expiry and revocation | Revocation and clock advancement are interleaved with actions. Revoking or expiring a generated ancestor immediately probes a descendant with a fresh request. Any fresh action under an inactive or expired ancestor must be DENY. |
| I6 — Replay | A completed exact retry returns the original decision without adding an effect or usage. Changing the context with the same principal/request ID must deny without another effect. Pending and approved retries still pass through current enforcement. |
| I7 — Audit chain | `verify_audit()` must pass after every state-machine transition, throughout the targeted finality histories, and after each concurrent race. This checks local consistency, not externally anchored authenticity. |

The state machine generates trees with up to 30 nodes and at most four delegation
edges, then interleaves purchases, catalog searches, forbidden uploads, delegation,
revocations, clock changes, policy tightening, approvals/rejections and retries.
Each example has its own temporary SQLite file, logical clock and independently
maintained ancestry/usage model. Semantic responses use an in-memory transport
through the real `SemanticReviewer`; there are no external calls or API keys.

Branches sometimes retain their parent's caps and permissions. Otherwise, randomly
shrinking every field tends to generate only denials and misses spend/review paths.
The dedicated finality property guarantees that approval happens before the
restriction, including an explicit depth-four root-revocation example. The existing
unit and HTTP integration tests remain necessary for other tools, malformed input,
identity authentication, rollback and evaluator-time expiration.

## Concurrency

`test_siblings_race_one_shared_budget` runs separately with 10 and 50 workers.
A barrier releases all siblings together; each gateway execution opens its own
SQLite connection and writer transaction. Each attempts a $180 purchase against
one $900 parent cap. Exactly five effects must commit, and the parent ledger,
summed child ledgers and effect charges must all equal $900. No worker error is
ignored or converted into an acceptable denial. These are correctness checks,
not throughput or latency benchmarks.

## Semantics and limits

- Attenuation compares a child with its parent **when delegated**. Later human
  edits do not rewrite stored descendants. Subsequent executions must still obey
  every ancestor's current limits.
- Completed replays are historical responses, not new grants. A previous ALLOW
  may be returned after revocation, but it must never produce a second effect.
  I5 therefore applies to fresh execution attempts, not the label on cached history.
- Administrator edits can lower a cap below already committed spend. No system
  can undo that spend merely by editing policy. The state-machine budget edits
  stay at or above committed spend; I2's absolute cap assertion covers those
  histories. I3 additionally checks every new commit against the then-current
  cap. This suite does not claim arbitrary retroactive edits preserve an absolute
  `historical spend <= newly edited cap` relation.
- The logical clock is stationary during each operation. Existing expiration
  tests cover a clock advancing inside intent evaluation; wall-clock commit
  deadlines are not established here.
- Generation is bounded and adaptive, not exhaustive or a random sample of real
  attacks. Passing tests do not prove security or measure an attack success rate.
  Binomial/Wilson confidence intervals are **not applicable** to these correlated,
  adaptively generated examples. No population-rate estimate is reported.

## Reproduce

From the repository root, use Python 3.11+ on a supported POSIX host:

```sh
python -m pip install -e '.[test,mcp]' -c requirements.lock
HYPOTHESIS_PROFILE=nodi-ci python -m pytest tests/test_invariants.py -q --hypothesis-show-statistics
HYPOTHESIS_PROFILE=nodi-ci python -m pytest -q
python -m nodi evaluate
python -m nodi demo
```

The profile definitions live in [tests/conftest.py](../tests/conftest.py).
`nodi` is the local default (60 examples, up to 60 state-machine steps).
`nodi-ci` uses deterministic generation with 100 examples and up to 80 steps;
CI selects it explicitly. `nodi-deep` permits 300 examples and up to 150 steps:

```sh
HYPOTHESIS_PROFILE=nodi-deep python -m pytest tests/test_invariants.py -q --hypothesis-show-statistics
```

These numbers are **configured limits**, not claims about operations or attacks
covered. Read `--hypothesis-show-statistics` for actual passing, failing and invalid
example counts from a run. No frozen pass totals or latency figures are published.
Record the commit, Python version, locked dependencies and command with any result;
deterministic generation is not guaranteed identical across dependency changes.

Deadlines are disabled because filesystem scheduling is not a correctness oracle.
Local runs retain Hypothesis's ignored `.hypothesis/` failure database; the CI
profile disables that database and enables deterministic generation. Hypothesis
shrinks a failure and prints its reproducing sequence. Stop on a genuine invariant
violation, retain that minimal example, and review the enforcement fix before
continuing later phases. Do not weaken an invariant to obtain a passing run.
