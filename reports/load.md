# Load benchmark status

**300/300 workflows completed with zero errors**, producing 600 confirmed action results. The completed measurement is GitHub Actions run [40700067125](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40700067125), commit `38c1078`, using the fixture planner and explicit programmatic approval.

| Client concurrency | Workflows completed | Admission p50 / p95, ms | Active duration p50 / p95, s | Queue delay p50 / p95, ms | Approval wait p50 / p95, ms |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 100/100 | 3.24 / 4.75 | 10.992 / 11.038 | 959.50 / 986.05 | 30.00 / 56.05 |
| 5 | 100/100 | 5.41 / 13.83 | 11.020 / 11.067 | 952.00 / 974.05 | 33.00 / 59.05 |
| 20 | 100/100 | 48.35 / 108.13 | 11.049 / 11.211 | 824.00 / 959.00 | 108.00 / 189.45 |

Each group had 200 confirmed effects, zero expected-review outcomes and zero errors. Active duration is client-observed completion time minus persisted approval wait; it includes queue and observation overhead. Queue delay is admission to the first planning start. The roughly eleven-second completion time reflects the specified five-second redispatch cadence, not model inference.

Environment: GitHub-hosted Linux 7.0.0-1017-azure x86_64, glibc 2.39, Python 3.12.15, two reported CPUs. The pinned Compose stack ran two worker processes. These are measured local-simulator results, not a production SLO or real-model latency claim.

Full precision values and all 300 per-workflow observations are retained in this run's `load-evidence` artifact, including the append-only JSONL journal. A compact measured summary is checked in as `load-summary.json`.

First attempt: GitHub Actions run [40523526226](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40523526226), commit `df98593`, reached the test harness's 900-second subprocess deadline. It produced no completed load summary. This failed attempt remains recorded; it is not substituted with a success.

Logs show the specified five-second redispatch interval between planning, ticket execution and reservation execution. A serial 100-workflow workload therefore requires more than 15 minutes. The benchmark timeout was corrected to 2,400 seconds without changing workflow scheduling or shortening the workload. An append-only JSONL journal now retains each completed observation even if a later timeout interrupts the run.

The corrected run above is a separately labeled execution; it does not erase the earlier timeout.
