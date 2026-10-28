# Fault benchmark status

The complete process-backed matrix passed **200/200 expected outcomes** in GitHub Actions run [40523526226](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40523526226), commit `df98593`. The integration job passed; the separate load job in that run timed out, as recorded in the load report.

| Outcome | Runs |
| --- | ---: |
| Automatic completion, including conditional stock skip | 150 |
| Expected `NEEDS_REVIEW` | 20 |
| Expected permanent failure | 10 |
| Expected clarification for cross-tenant proposal | 10 |
| Expected cancellation after confirmed ticket | 10 |
| Missing or mismatched runs | 0 |

All 20 scenario IDs ran with seeds 0–9. The validator checked exact terminal state and logical ticket/reservation effect counts for each run. Expected review is not counted as successful automatic completion.

Environment: Python 3.12.15, Linux 7.0.0-1017-azure x86_64, glibc 2.39, two reported CPUs. Real MongoDB 7.0.16 and Redis 7.2.7 ran as service containers. Each scenario used separate HTTP simulator subprocesses and SQLite files. Worker and dispatcher crash scenarios used abrupt OS process termination at an observed test checkpoint.

GitHub Actions run [40521208198](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40521208198) passed the earlier MongoDB/Redis integration suite and isolated Compose workflow/restart check. That run did not include the full matrix.

The run's `fault-evidence` artifact includes per-case histories, logical effect counts, JUnit results and the report validator's result. A compact verified summary is checked in as `faults-summary.json`. GitHub artifact retention is finite; download the full artifact if you need to retain every diagnostic record.
