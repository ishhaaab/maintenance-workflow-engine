# Admission latency investigation

Target: approach 38 ms p95 for durable admission at 20 concurrent clients. The original load run measured 108.126 ms on a two-CPU runner. Later controlled comparisons report four CPUs, so their paired baseline is the reference for optimization claims.

## Measurement protocol

`scripts/benchmark_admission.py` builds both revisions before timing. Each revision runs a separate pinned Compose stack on the same runner. One bootstrap workflow precedes three groups of 100 workflows at concurrency 20. The client polls every 50 ms, explicitly approves each exact plan, and waits for completion. Admission timing starts before the client sends `POST /workflows` and ends after its response arrives. It includes client scheduling, HTTP and MongoDB work.

All three trials and all per-workflow observations are retained. Pooled percentiles are calculated from 300 observations, not by averaging percentiles. The baseline runs before the candidate. This order can be affected by host drift; the trials are repeated observations, not independent random samples. Every new attempt has its own GitHub run and artifact.

## Recorded attempts

| GitHub run | Purpose | Outcome |
| --- | --- | --- |
| [40902912125](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40902912125) | First comparison | Checkout failed on an abbreviated SHA. No measurements. |
| [40946519276](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40946519276) | Async authentication versus original | 600/600 workflows, 1,200 confirmed effects, zero errors. Baseline p95 95.141 ms, candidate 97.588 ms. No measured latency improvement. |
| [40948373951](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40948373951) | Instrumented diagnosis | 100/100 workflows. Shutdown discarded the profile files. Client timings are not an optimization result. |
| [40949174274](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40949174274) | Corrected instrumented diagnosis | 100/100 workflows and complete traces. Recursive status-response encoding consumed 10.629 seconds cumulatively. |
| [40956142351](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40956142351) | Direct status response plus async authentication | 600/600 workflows, 1,200 confirmed effects, zero errors. Baseline p95 65.012 ms, candidate 59.431 ms. |

## Findings

Authentication only compares configured API keys. Making this dependency async avoids a worker-thread queue. A saturation regression timed out before that change and passed afterward. The matched load experiment above did not establish a latency reduction from this change alone.

The status handler first serializes the workflow with Pydantic, then FastAPI recursively walks the resulting JSON-compatible dictionary again. The profile recorded 16,853 status reads and 2.59 million recursive encoder calls. Returning `JSONResponse`, as admission and approval already do, removes that second walk.

The same diagnostic recorded admission store p95 66.284 ms and MongoDB-command p95 47.983 ms. These include client-side scheduling and instrumentation overhead. They do not measure MongoDB server execution alone. The first burst also spent time opening connections: store time minus command time had p95 51.443 ms for the first 20 admissions and 1.073 ms for the remaining 80.

Raw data, profiles and Compose logs are in each run's `admission-evidence` artifact. Instrumented runs are diagnosis only. Fixture-planner load timings do not measure model inference.
