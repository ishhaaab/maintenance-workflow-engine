# Admission latency investigation

Target: approach 38 ms p95 for durable admission at 20 concurrent clients. The original load run measured 108.126 ms on a two-CPU runner. Later controlled comparisons report four CPUs, so their paired baseline is the reference for optimization claims.

**Retained result:** pooled admission p95 was 59.431 ms in the first comparison and 69.536 ms in a later run of the same code. The user accepted this range. The native-networking experiment was removed after its slower result; the retained product code and dependencies match `b3b48e9`. The 38 ms target was not reached.

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
| [40957512974](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40957512974) | Native networking versus retained serialization build | 600/600 workflows, 1,200 confirmed effects, zero errors. Retained build p95 69.536 ms, native candidate 101.062 ms. Native dependencies removed. |

## All uninstrumented trials

Each row contains 300 workflows across three 100-workflow trials. Every row completed all workflows with 600 confirmed action results and zero errors. Bootstrap workflows are additional and excluded from these counts.

| Run / build | Trial 1 p95, ms | Trial 2 p95, ms | Trial 3 p95, ms | Pooled p50 / p95, ms |
| --- | ---: | ---: | ---: | ---: |
| 40946519276 / original `ec611a7` | 83.085 | 93.687 | 122.825 | 40.838 / 95.141 |
| 40946519276 / async auth `f79d8ff` | 101.640 | 91.427 | 88.336 | 47.179 / 97.588 |
| 40956142351 / original `ec611a7` | 66.997 | 60.232 | 58.645 | 27.066 / 65.012 |
| 40956142351 / retained `b3b48e9` | 65.736 | 51.536 | 61.026 | 23.159 / 59.431 |
| 40957512974 / retained `b3b48e9` | 68.039 | 66.151 | 69.539 | 30.154 / 69.536 |
| 40957512974 / native `df6ecbe` | 111.301 | 89.197 | 68.217 | 39.764 / 101.062 |

The first matched comparison of the retained build was 8.58% lower than its same-host baseline. Baseline-first ordering and host variability limit causal inference; the later 69.536 ms result also belongs to the retained implementation. These are observed fixture-load results, not a guaranteed production SLO. Exact summaries and all 1,800 observations are checked in as [admission-results.json](admission-results.json), independently checked against the raw JSONL journals and recomputed percentiles.

## Findings

Authentication only compares configured API keys. Making this dependency async avoids a worker-thread queue. A saturation regression timed out before that change and passed afterward. The matched load experiment above did not establish a latency reduction from this change alone.

The status handler first serializes the workflow with Pydantic, then FastAPI recursively walks the resulting JSON-compatible dictionary again. The profile recorded 16,853 status reads and 2.59 million recursive encoder calls. Returning `JSONResponse`, as admission and approval already do, removes that second walk.

The same diagnostic recorded admission store p95 66.284 ms and MongoDB-command p95 47.983 ms. These include client-side scheduling and instrumentation overhead. They do not measure MongoDB server execution alone. The first burst also spent time opening connections: store time minus command time had p95 51.443 ms for the first 20 admissions and 1.073 ms for the remaining 80.

Raw data, profiles and Compose logs are in each run's `admission-evidence` artifact. Instrumented runs are diagnosis only. Fixture-planner load timings do not measure model inference.

## Reproduction

Run the **Admission latency comparison** GitHub workflow with full 40-character baseline/candidate commit IDs, `repetitions=3`, and `profile_api=false`. For the retained comparison, use `ec611a716bf6684117d976e0f78449cc7d8b9372` and `b3b48e9758c47146b895fe947d5bf2654d10b61f`. Each dispatch creates a new artifact; do not overwrite prior results.

For an already checked-out pair of revisions on a Linux Docker host, install the candidate's pinned development dependencies and run from its root:

```sh
uv run python scripts/benchmark_admission.py --baseline /path/to/baseline --candidate . --repetitions 3 --output-dir artifacts/admission-new-run
```

The driver creates and removes disposable Compose projects and volumes. It needs the Compose host ports to be free. `--profile-api true` runs only the candidate with instrumentation and must not be used for a performance claim.
