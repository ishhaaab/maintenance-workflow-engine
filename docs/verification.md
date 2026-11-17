# Verification record

## Current review release

See [review acceptance](review-acceptance.md) for the new named-review, SMTP, export, seed and browser checks. [The runbook](review-runbook.md) and [walkthrough](review-walkthrough.md) describe the current application. Evidence below retains its original Phase 1 and historical MongoDB scope and is not being relabeled as current human-review acceptance.

Final local core-release acceptance on 2026-11-17: 612 regression tests passed with zero skips, including two fresh-Compose checks, plus all 200 ten-seed PostgreSQL/Redis/HTTP fault observations. Independent journal and tested-postimage comparisons passed. Five review findings were reproduced and fixed under TDD, then verified in that complete suite. The failed native Windows run is retained separately; passing database-heavy acceptance used isolated Linux private networking. Remote Actions, production deliverability/cutover and throughput remain unverified.

## Retained Phase 1 verification boundary

The current runtime uses PostgreSQL only. `tests/integration/test_runtime.py` checks non-owner runtime credentials, real Redis dispatch, HTTP simulators, separate heartbeat/tick sessions, prepared-fixture recovery, native Windows CLI/server loops and schema-only init. `test_api.py` checks 410/401 denial and safe readiness. Admission/save guards run in the shared memory/PostgreSQL store contract. Test resources are invocation-owned; core PostgreSQL checks must run with a supplied isolated service, not be replaced by skips.

The process/fault tests now use owned PostgreSQL runtime roles, random Redis streams and separate HTTP simulators. `test_postgres_recovery.py` covers a database connection outage after the effect commits, abandoned pending-message reclaim with the production 60-second threshold, a late expired worker and cancellation during unresolved replay. The 20-case matrix retains its original IDs, expected states and logical effect counts. New evidence is labeled `PostgreSQL+Redis+HTTP subprocesses`; creation/approval setup is explicitly internal and test-only, with separate public 410/401 assertions. The fault benchmark rejects wrong backends, missing/duplicate results, changed original action keys and incorrect logical effect identities. Historical report parsing remains available without treating those reports as new acceptance.

Run the required real-service checks with `TEST_POSTGRES_URL` and `TEST_REDIS_URL` supplied. An absent service may cause an explicit skip outside acceptance; a configured but broken service fails. `uv run --no-sync pytest tests/integration tests/e2e/test_workflow.py --ignore=tests/integration/test_fault_matrix.py -q` runs the integration checks. `uv run --no-sync python scripts/benchmark_faults.py --output-dir artifacts/postgres-<unique-run-id> --seeds 0,1,2,3,4,5,6,7,8,9` requires exactly 200 validated results. Each result directory is new; failed attempts are retained, not retried into the same evidence.

The integration CI job provisions isolated PostgreSQL/Redis and requires the full fault matrix. The separate required Compose job uses `compose.postgres.yaml` plus an explicit test-only override. Its application images contain no test approval command or privileged credential mounts. Required checks reject skips, including when terminal reporting is disabled. Remote execution of these changes has not been observed. No new throughput claim is made.

## New PostgreSQL local evidence

Observed on Windows11, CPython3.12.16 and uv0.13.6, using real PostgreSQL17.12/Redis7.2.7 and Linux Docker containers. These are uncommitted Phase1 foundation checks, not human-review-product or throughput results.

- Required integration/e2e command before deployment work:312 passed, zero skips.
- Full20-by-10 fault benchmark:200 passed in901.44s. Independent standard-library inspection confirmed every original key, expected state, ticket-before-reservation ordering,170 ticket/140 reservation logical effects, complete case/seed coverage and200 exact-allocation cleanup records. Artifacts: `artifacts/postgres-phase1-20261114-task5-acceptance-c6920a41/`. Summary SHA256: `9b4e050d6e614ec4c445a79f7b1c04e69f93345572b5bdbdb6edb4bb2b2397cb`.
- Pre-deployment full suite:521 passed, one opt-in Compose skip. The skipped check was not counted as PostgreSQL acceptance.
- Fresh enhanced Compose check:1 passed in201.43s, no skips. It covered simultaneous preparation and same allocation, explicit repeated migrations, real credential modes/mount separation, runtime grants, file-loss/mismatch failures, two workers, denied legacy HTTP commands, committed-effect worker death, original-key recovery, Redis loss, recreation and retained-volume down/up. Safe artifacts: `artifacts/postgres-phase1-compose-20261114-final/workflow-pg-test-ccce6b968b6f/`.
- Preparation/strict-skip checks:21 passed in5.95s. This includes real SQL identity verification and missing/mismatched administrator password files, not only mocked database boundaries.
- Final complete suite with real services, `RUN_COMPOSE=1` and `--fail-on-skip`:543 passed in683.67s, zero skips. It verified283 exact fixture databases/566 roles removed, plus separate exact preparation cleanup and label-verified removal of the fresh Compose project's six volumes. Full-suite safe Compose artifacts: `artifacts/postgres-phase1-compose-20261114-full-suite/workflow-pg-test-a5b59e214db7/`.
- Final offline units:209 passed in7.05s. Ruff lint/format and whitespace checks pass,93 Python files. A new explicit owned-database Alembic upgrade/check passed; runtime credentials verified the current head. All42 source/dependency/fixture/migration inputs used by the successful200-run matrix remain byte-identical after deployment work, so that completed measurement was not needlessly repeated.

Development failures were retained and fixed rather than counted as acceptance: retired API-key fixture setup, the worker resume/Windows loop port, expiring a nonexistent dispatcher lease, a server-wide catalog assertion racing another owned test allocation, inactive Compose profiles/cleanup, JSON-lines parsing and Windows output encoding. Preparation regressions also caught terminal-independent skip enforcement, the Linux privilege guard and incomplete administrator split validation. No failed fault measurement was retried into the successful artifact directory. Independent review approved Phase 1 without Critical/Important findings; the controller completed the unfinished postimage check without a source change. See the [handoff](superpowers/reviews/2026-11-14-postgresql-foundation-handoff.md) for deferred minors and review boundaries. Everything below remains historical and has not been relabeled.

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
