# Recorded local evidence

These files record an actual local run of the existing deterministic evaluator and
controlled contention harness. The source was uncommitted during measurement;
reports preserve the base commit, dirty flag and per-source SHA-256 hashes. Results
are not live model benchmarks, independent human labels or production guarantees.

- [Purpose report](purpose.json): 12 development and 14 held-out cases. Held-out
  false allows: 0/8 cases expected to require denial or review. False denials: 0/6
  ordinary cases. Ordinary reviews: 2/6. Total reviews: 5/14. Exact agreement:
  12/14. The two ordinary paraphrases sent for review are retained, not tuned away.
- [Contention report](contention.json): eight simultaneous writers per condition,
  with a configured 50 ms delay for slow/failing evaluators. The failure condition
  returned ASK_HUMAN for all eight; audit checks passed for every condition.
- [Injection report](injection/report.json): **skipped**, live execution not enabled.
  No model API calls, attack-success rates or model latency measurements exist in
  this report. Scripted unit tests validate plumbing only.

| Evaluator condition | Acquisition p95 | End-to-end p95 |
| --- | --- | --- |
| fast | 76.0 ms | 76.8 ms |
| slow | 604.4 ms | 665.2 ms |
| failing | 609.9 ms | 669.2 ms |

Acquisition time includes connection/file checks and SQLite lock waiting. These are
single small local batches, not capacity estimates. Purpose labels are
machine-authored specification expectations, not independently validated ground
truth; intervals in the JSON do not remove selection bias or correlated cases.
Research remains bounded to exact offline fixtures. See the
[methodology](../../README.md) and [recovery semantics](../../../docs/outbox.md).
