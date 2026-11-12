# Evaluation methodology

Run `python -m nodi evaluate --output evaluation/results.json`. The command exits nonzero on any decision-label mismatch or dangerous false allow. Each case uses a new temporary database and fixed logical clock; latency uses the host's performance counter. Setup/cleanup are excluded.

The 42 hand-authored cases contain 9 expected allows, 4 expected human reviews and 29 expected denials. All 33 non-ALLOW cases are labeled dangerous or review-required, including ambiguous actions. An automatic allow for any of them counts as a false allow.

| Metric | Definition |
| --- | --- |
| Dangerous false allow | Actual ALLOW / dangerous or review-required cases |
| Exact decision accuracy | Matching three-way decisions / all cases |
| False deny | Actual DENY / expected ALLOW cases |
| Unnecessary review | Actual ASK_HUMAN / expected ALLOW cases |
| Mission-drift detection | Actual DENY / mission-drift cases |
| Injection containment | Actual non-ALLOW / injection cases |
| Delegation prevention | Actual DENY / escalation cases |
| Gateway latency | Complete execution/delegation including local commit and audit |
| Policy latency | Instrumented ancestry policy loop, excluding parsing, intent, commit and delegation checks |

The reference run uses the offline baseline. Requests rejected before the policy loop contribute zero policy time. The p95 is a lower discrete percentile on a small sample without warm-up or load testing. These timings are not production performance estimates.

The fixtures were developed alongside the implementation. They are not independently labeled, representative or held out. Zero observed false allows is not an estimate of an attacker's success probability. Injection tests exercise attempted effects, not a live compromised model. Unit/integration tests separately cover races, replay, persistence and audit failure.

Broader evaluation needs independently authored held-out missions, paraphrases, multilingual actions, multi-step traces, new adapters, realistic human reviews and calibrated model evaluation. Report category denominators and uncertainty before drawing population-level conclusions.

The default dataset is packaged at `nodi/data/cases.json` and loaded through package resources. Evaluation no longer depends on the current working directory. Use `python -m nodi evaluate --dataset /absolute/path/to/custom.json` for an explicit custom dataset.

## Custom dataset validation

The complete dataset is validated before any gateway or simulation state is created. Input must be finite, unambiguous UTF-8 JSON, no larger than 4 MiB, with 1–1,000 case objects and unique IDs matching `[A-Za-z0-9_-]{1,100}`. Unknown fields, duplicate JSON keys and unsupported preconditions are rejected.

Every case requires `id`, a nonblank `category` of at most 100 characters, `expected` (ALLOW, DENY or ASK_HUMAN), and a Boolean `dangerous`. A dangerous case cannot also expect ALLOW. Category names may be custom; named category metrics only count their exact documented categories. Labels are supplied by the dataset author; validation does not establish label correctness.

Action cases use `kind: "action"` (or omit kind) and require `tool`, `resource` and `arguments`. Optional `context` and `principal` follow the action/identity size rules. Tool arguments may deliberately violate adapter policy to exercise denial paths. Delegation cases use `kind: "delegation"` and a `changes` object that must construct a valid contract; authority widening can still be tested. They do not accept action-only fields.

Supported preconditions are `expired`, `revoked`, `budget_spent` and `action_limit`. Case ID `setup` is reserved when using the last two because their precondition creates that request. Invalid datasets make the CLI exit 2 without writing or replacing a report. Decision mismatches or dangerous false allows exit 1; a successful evaluation exits 0. Missing metric denominators remain `null`; delegation-only datasets also report `null` for policy-loop latency. A dataset with no dangerous cases cannot establish safety on dangerous actions, even when its command exits successfully.

## Report provenance and denominators

New reports include `report_schema_version: 1` and `dataset_sha256`, the SHA-256 of the exact input bytes read for validation and evaluation. Renaming an unchanged dataset preserves its digest; whitespace-only changes alter it. The digest identifies input content, not correctness, authorship or a trusted publication date. It is not a signature and does not identify the implementation revision; retain the source commit and dependency lock alongside reports.

`metric_sample_counts` names the sample count for each rate and timing statistic, while `category_counts` records the exact category distribution. A zero denominator corresponds to a `null` rate, not a measured zero failure rate. Policy-loop timing counts action cases only; delegation-only datasets have zero policy samples and null policy timing. Existing top-level metrics remain available. Older archived reports without these new fields remain historical records rather than being retrospectively assigned provenance.

## Domain datasets

`python -m nodi evaluate` continues to run the unchanged procurement dataset.
`python -m nodi evaluate --domain research` selects the separately packaged research
fixtures and their default contract. `--dataset` can supply a custom fixture file
for the selected domain; validation still happens before gateway creation. The
procurement-only `budget_spent` precondition is rejected for research. Reports add
an explicit `domain` field and use the selected baseline's name. Existing metrics
and their regression-only interpretation remain unchanged. Both domains are
checked by CI and by the installed-wheel script outside the source checkout.
