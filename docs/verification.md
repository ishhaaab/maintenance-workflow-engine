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

## Outstanding specification gate

No real-model inference has been run. The OpenAI-compatible adapter and offline failure tests are implemented, but measured held-out model quality still requires approved local-model resources or provider budget. Fixture accuracy does not satisfy that gate.

The original checklist retains unchecked historical pre-implementation service RED steps that could not run on this workstation. These are process-history limitations, not fabricated verification results. The build record distinguishes them from the outstanding live-model measurement.
