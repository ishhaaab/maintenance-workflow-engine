# Load benchmark status

The fixture-only load driver is implemented. A complete measured run of 100 workflows at concurrency 1, 5 and 20 is pending.

First attempt: GitHub Actions run [40523526226](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40523526226), commit `df98593`, reached the test harness's 900-second subprocess deadline. It produced no completed load summary. This failed attempt remains recorded; it is not substituted with a success.

Logs show the specified five-second redispatch interval between planning, ticket execution and reservation execution. A serial 100-workflow workload therefore requires more than 15 minutes. The benchmark timeout was corrected to 2,400 seconds without changing workflow scheduling or shortening the workload. An append-only JSONL journal now retains each completed observation even if a later timeout interrupts the run.

No latency or throughput numbers are claimed before the new run finishes. The `load-evidence` artifact will include the environment, per-workflow records, completion/errors, confirmed effects and separate timing distributions.
