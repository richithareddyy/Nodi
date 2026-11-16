# Local concurrency measurements

Measured ledger operations; no real purchases or model calls. Source/version/hardware details and raw observations are in the accompanying JSON.

| Backend | Workers | Workload | Trials / operations | Errors | Mean ops/s [descriptive 95% interval] | p50 / p95 / p99 ms |
| --- | ---: | --- | ---: | ---: | --- | --- |
| sqlite | 10 | shared_budget | 3 / 30 | 0 | 69.4 [63.1, 81.3] | 25.5 / 155.4 / 158.0 |
| sqlite | 10 | approval_race | 3 / 30 | 0 | 92.2 [66.8, 109.2] | 24.5 / 120.6 / 149.0 |
| sqlite | 10 | duplicates | 3 / 30 | 0 | 165.4 [103.8, 234.4] | 22.6 / 64.4 / 95.3 |
| sqlite | 50 | shared_budget | 3 / 150 | 0 | 57.3 [42.0, 73.5] | 122.1 / 878.3 / 1083.2 |
| sqlite | 50 | approval_race | 3 / 150 | 0 | 75.5 [64.3, 87.7] | 97.3 / 564.7 / 669.3 |
| sqlite | 50 | duplicates | 3 / 150 | 0 | 98.7 [56.1, 134.0] | 70.6 / 676.3 / 880.7 |
| sqlite | 100 | shared_budget | 3 / 300 | 0 | 73.9 [66.6, 83.8] | 153.8 / 1187.3 / 1391.7 |
| sqlite | 100 | approval_race | 3 / 300 | 0 | 103.7 [82.6, 127.3] | 121.6 / 874.4 / 1094.7 |
| sqlite | 100 | duplicates | 3 / 300 | 0 | 91.4 [83.3, 100.1] | 127.9 / 890.3 / 1093.9 |
| postgres | 10 | shared_budget | 3 / 30 | 0 | 135.3 [123.1, 157.5] | 43.4 / 78.3 / 78.9 |
| postgres | 10 | approval_race | 3 / 30 | 0 | 214.0 [176.1, 238.4] | 34.0 / 53.2 / 56.1 |
| postgres | 10 | duplicates | 3 / 30 | 0 | 299.7 [280.3, 324.2] | 19.9 / 33.1 / 34.4 |
| postgres | 50 | shared_budget | 3 / 150 | 0 | 158.4 [150.5, 162.6] | 162.1 / 298.9 / 319.3 |
| postgres | 50 | approval_race | 3 / 150 | 0 | 256.8 [252.1, 260.9] | 128.8 / 185.1 / 191.9 |
| postgres | 50 | duplicates | 3 / 150 | 0 | 331.9 [319.5, 338.5] | 80.7 / 140.1 / 149.7 |
| postgres | 100 | shared_budget | 3 / 300 | 0 | 170.6 [167.4, 176.5] | 313.0 / 550.0 / 576.1 |
| postgres | 100 | approval_race | 3 / 300 | 0 | 320.2 [304.4, 339.4] | 203.3 / 296.5 / 308.5 |
| postgres | 100 | duplicates | 3 / 300 | 0 | 380.4 [350.9, 411.5] | 147.9 / 244.0 / 270.4 |

Intervals resample entire trials (2,000 deterministic bootstrap draws). With only a few local trials they are descriptive, not reliable population or production bounds. Latency uses all recorded operations, including errors. No percentile confidence intervals are claimed.

SQLite runs on the client filesystem; PostgreSQL may run in Docker or on another host. Consult server_environment in the JSON: these are deployment-path comparisons, not isolated database-engine rankings. PostgreSQL uses a bounded pool and retains a global write/audit-order lock. Setup and teardown are excluded; connection checkout, approvals and database work are included. Throughput counts completed gateway operations including denials and replays, not new purchases. HTTP/MCP transport is excluded. These synchronized bursts do not establish sustained throughput, horizontal scalability or third-party effect reliability.
