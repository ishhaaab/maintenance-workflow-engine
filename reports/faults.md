# Fault benchmark status

The corrected process-backed matrix passed **200/200 expected outcomes** in GitHub Actions run [40751719978](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40751719978), commit `581a740`. Pytest completed all 200 cases in 505.42 seconds. The same run passed 53 unit tests and 39 integration tests.

| Outcome | Runs |
| --- | ---: |
| Automatic completion, including conditional stock skip | 150 |
| Expected `NEEDS_REVIEW` | 20 |
| Expected permanent failure | 10 |
| Expected clarification for cross-tenant proposal | 10 |
| Expected cancellation after confirmed ticket | 10 |
| Missing or mismatched runs | 0 |

All 20 scenario IDs ran with seeds 0–9. The validator checked exact terminal state and logical ticket/reservation effect counts for each run. Expected review is not counted as successful automatic completion.

The corrected `redis_unavailable_at_admission` scenario creates a new workflow through an application whose Redis connection is unavailable. It requires HTTP 202, checks degraded readiness and durable MongoDB state, then verifies redispatch through a recovered broker. All ten seeds passed. The earlier version only tested an existing-key replay and did not establish fresh-admission recovery.

Environment: Python 3.12.15, Linux 7.0.0-1017-azure x86_64, glibc 2.39, two reported CPUs. Real MongoDB 7.0.16 and Redis 7.2.7 ran as service containers. Each scenario used separate HTTP simulator subprocesses and SQLite files. Worker and dispatcher crash scenarios used abrupt OS process termination at an observed test checkpoint.

The push run for the same commit, [40751664059](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40751664059), also passed the isolated Compose workflow/restart test. Compose was intentionally skipped in the dedicated fault run.

The corrected run's `fault-evidence` artifact includes per-case histories, logical effect counts, JUnit results and the report validator's result. All downloaded records were revalidated against the scenario definitions. Its compact summary is checked in as `faults-corrected-summary.json`. GitHub artifact retention is finite; download the full artifact if you need to retain every diagnostic record.

## Earlier runs

- [40521208198](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40521208198) passed the earlier MongoDB/Redis integration suite and isolated Compose workflow/restart check. It did not include the full matrix.
- [40523526226](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40523526226), commit `df98593`, passed the original 200-case matrix. Its replay-only Redis-admission case was corrected after review. The original measured summary remains in `faults-summary.json`. The separate load job timed out, as recorded in the [load report](load.md).
- [40750863369](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40750863369) failed during test collection before the corrected matrix started. The console pytest entry point could not import the demo script. Commit `581a740` fixes the repository import path; no fault cases from the failed attempt count toward the corrected result.
