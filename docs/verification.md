# Verification record

## Local checkout and clean clone

Verified on Windows 11, CPython 3.12.16, uv 0.13.6. Code revision `581a740`.

| Command | Observed result |
| --- | --- |
| `uv sync --frozen --extra dev` | Successful install from the exact lockfile |
| `uv run pytest -q` | 87 passed, 26 explicit service/Compose skips |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 64 files already formatted in the final check |
| Clean temporary clone: `uv sync --frozen --extra dev` | Successful independent environment creation |
| Clean temporary clone: `uv run workflow-engine --help` | Exit 0, no service/model credentials required |
| Clean temporary clone: `uv run pytest tests/unit -q` | 53 passed |
| `uv run python scripts/evaluate_planner.py --output artifacts/fixture-smoke-581a740.json` | 80 fixture cases processed; zero inference responses; real-model gate correctly false |

The 26 local skips are 20 real-service fault scenarios, five other MongoDB/Redis cases, and the opt-in Compose test. They are not counted as passing tests. Real service verification occurs in the CI runs below because Docker is not installed on the workstation.

## Remote runs

- [40521208198](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40521208198): initial unit, real MongoDB/Redis integration and isolated Compose/restart jobs passed.
- [40523526226](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40523526226): first full 200-case fault run passed. The separate load job exceeded its initial 900-second harness deadline and failed. Review later found that one Redis-outage scenario covered replay rather than fresh admission; the corrected case ran separately.
- [40700067125](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40700067125): complete 300-workflow fixture load run passed, with 600 confirmed effects and zero errors. See the [measured load report](../reports/load.md).
- [40750863369](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40750863369): corrected-matrix attempt stopped before measurement at test collection. The `pytest` console entry point lacked the root import path used by `python -m pytest`. Reproduced locally and corrected in `581a740`; no fault runs from this failed attempt are counted.
- [40751664059](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40751664059), commit `581a740`: 53 unit tests, 39 real-service integration tests and one isolated Compose/restart test passed. This includes the real-MongoDB approval/cancellation race and simulator-process restart checks.
- [40751719978](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40751719978), commit `581a740`: 53 unit tests, 39 integration tests and all 200 corrected fault cases passed. The matrix took 505.42 seconds. Compose was skipped in this fault-only dispatch and verified by the push run above.

The Linux commands were `uv sync --frozen --extra dev`, `uv run pytest tests/unit -q`, `uv run pytest tests/integration tests/e2e/test_workflow.py --ignore=tests/integration/test_fault_matrix.py -q`, `uv run pytest tests/e2e/test_compose.py -m compose -q` with `RUN_COMPOSE=1`, and `uv run python scripts/benchmark_faults.py --output-dir artifacts/faults`. CI supplied isolated MongoDB/Redis service URLs. The benchmark environment was Python 3.12.15 on Linux 7.0.0-1017-azure x86_64 with two reported CPUs.

The final corrected matrix is tracked in the [fault report](../reports/faults.md). Downloaded per-case JSON records and JUnit counts were revalidated locally. All 300 downloaded load observations also reproduce the committed load summary exactly. Earlier unsuccessful runs remain linked rather than being replaced by later successful evidence.

## Completed live-model gate

After the user approved local inference and supplied Qwen2.5-7B-Instruct Q4_0, both model shards and the pinned CUDA runtime archives passed SHA256 verification. Four 40-case development passes refined the prompt. Commit `1016330` froze the final prompt, corpus, model and server settings before the single 80-case held-out pass.

The held-out pass made 80 genuine inference calls and matched all 80 expected outcomes with schema-valid responses, zero unauthorized proposals, zero provider errors and zero truncations. Latency p50/p95 was 1.804/2.304 seconds on an RTX 3060 Laptop GPU. Every raw response was separately reparsed and checked against its request, tenant catalog, expected label and policy; all frozen input hashes matched after the run. See the [planner report](../reports/planner.md) for full token counts, development regressions, reproduction commands and synthetic-corpus limitations.

The complete local suite passed after the final prompt change: 87 passed, 26 service-dependent skips. The evaluation server was stopped afterward. The original fixture smoke test above remains fixture evidence only.

The original checklist retains unchecked historical pre-implementation service RED steps that could not run on this workstation. These are process-history limitations, not outstanding implementation or measurement gates.

## Admission optimization verification

The retained performance build is `b3b48e9`, with async authentication and direct status JSON responses. A later native-networking experiment was removed after a slower benchmark. Before merging with main, `git diff --exit-code b3b48e9 -- src pyproject.toml uv.lock fixtures prompts compose.yaml Dockerfile` verified that the performance branch's product code, dependencies, fixtures and deployment matched that build exactly. Main's later v2 evaluation changes are separate from those measurements.

After removing the experiment, `uv sync --frozen --extra dev` succeeded, Ruff check and formatting passed, and the full local suite passed 88 tests with 26 explicit service-dependent skips. The thread-pool saturation regression had failed with a timeout before the authentication change. A separate response-equivalence check preserved the complete final JSON body and content type, including plan, action results, history, nulls and timestamps.

Real Compose load comparisons [40956142351](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40956142351) and [40957512974](https://github.com/ishhaaab/maintenance-workflow-engine/actions/runs/40957512974) both exercised the retained build for 300 workflows, each with 600 confirmed action results and zero errors. Its observed pooled admission p95 values were 59.431 and 69.536 ms. All 1,800 observations across three comparisons were independently checked against JSONL journals, completion/effect counts, unique workflow IDs and recomputed p50/p95 values. The full records and artifact hashes are in [admission-results.json](../reports/admission-results.json).

The user accepted the measured latency range. The original 38 ms target remains unmet. No further tuning is deferred as a required implementation task.
