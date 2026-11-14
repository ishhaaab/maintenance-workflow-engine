# Maintenance workflow engine

[![Verification](https://github.com/ishhaaab/maintenance-workflow-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/ishhaaab/maintenance-workflow-engine/actions/workflows/ci.yml)

A Python backend for approval-gated maintenance requests. Both integrations are local simulators.

Phase 1 is intentionally incomplete. PostgreSQL is now the only runtime execution authority. Standalone admission and direct approval return 410; workflow reads and cancellation return 401 until identity and review are implemented. Legacy API keys and cookies grant no access. The interactive demo and HTTP load command are temporarily unavailable and exit nonzero before sending requests. Isolated executor tests still prepare exact-plan approvals through test-only owner fixtures, never a product switch.

The main problem this project tackles is recovery. If a worker crashes after sending a request, did the ticket get created? The engine records each attempt and looks up uncertain results by their original idempotency key before deciding whether to retry.

```text
Request → proposed plan → policy checks → approval
                                            ↓
                                     create ticket
                                            ↓
                                reserve part if available
```

The planner can use a local LLM, but it cannot create tickets or reserve stock. Policy code checks its proposal against the request and tenant catalog. Approval applies to the hash of that exact validated plan.

This is a maintenance-administration portfolio project, not an equipment control system. It does not actuate equipment, certify repairs, or make safety decisions.

## Fresh PostgreSQL deployment

The default documented deployment is `compose.postgres.yaml`, with its own PostgreSQL, Redis, two workers and durable simulator volumes. It does not extend or start the historical stack. First-time setup is explicit:

```sh
docker compose -f compose.postgres.yaml -p maintenance-workflow-engine-postgres build
docker compose -f compose.postgres.yaml -p maintenance-workflow-engine-postgres run --rm --no-deps prepare admin
docker compose -f compose.postgres.yaml -p maintenance-workflow-engine-postgres up -d --wait postgres redis
docker compose -f compose.postgres.yaml -p maintenance-workflow-engine-postgres run --rm --no-deps prepare roles
docker compose -f compose.postgres.yaml -p maintenance-workflow-engine-postgres run --rm --no-deps migrate
docker compose -f compose.postgres.yaml -p maintenance-workflow-engine-postgres up -d --wait
```

Health is at `http://127.0.0.1:18000/health/ready`. This is still a health-only release, not a working human approval demo. Preparation generates private administrator, migration-owner and runtime credentials in separate volumes. Application containers receive only the runtime URL file. Normal `up`/restart does not provision or migrate. Repeated explicit preparation validates and reuses the same allocation. Missing/partial/mismatched identity fails closed, never silently creating another database. See the [runbook](docs/runbook.md) before changing a project name, restoring volumes or recovering an interrupted preparation.

## Native runtime

Use Python 3.12 and [uv](https://docs.astral.sh/uv/). Run `uv sync --frozen --extra dev` first. A separately provisioned PostgreSQL database must already be explicitly migrated to the current Alembic head. Supply only its non-owner runtime credentials through exactly one process setting:

- `WORKFLOW_DATABASE_URL`, a secret PostgreSQL URL; or
- `WORKFLOW_DATABASE_URL_FILE`, a bounded UTF-8 file containing that one raw runtime URL, with an optional final newline. Allocation JSON or owner/admin configuration is not accepted.

Do not pass migration credentials to the API or worker. `workflow-engine init` verifies the schema only, without migrations, Redis, planner or effect clients. Planner-only tools and `--help` need no database.

```sh
uv run workflow-engine init
uv run workflow-engine api
```

Health is public at `/health/live` and `/health/ready`. The API defaults to `127.0.0.1:8000`. Use `workflow-engine` or `python -m workflow_engine.cli` on native Windows; these entrypoints select the event loop required by Psycopg, including the Uvicorn server. See the [runbook](docs/runbook.md) for worker and dispatcher settings.

`compose.yaml` and its MongoDB/simulator volumes remain the historical deployment definition. They have not been repointed or migrated. Do not run that definition with the new runtime expecting MongoDB fallback. The new stack uses distinct volumes, images, stream/group names and loopback ports. No import, live cutover, automatic rollback or ledger reset is provided.

## API

No workflow route accepts legacy key or invented cookie authentication in Phase 1. Bodies above 16 KiB are still rejected with 413 before routing.

| Route | Purpose |
| --- | --- |
| `POST /workflows` | 410. Standalone admission retired. |
| `GET /workflows/{id}` | 401. Identity/review required. |
| `POST /workflows/{id}/approve` | 410. Direct approval retired. |
| `POST /workflows/{id}/cancel` | 401. Identity/review required. |
| `GET /health/live` | Process liveness. |
| `GET /health/ready` | Connection/schema failure returns 503. Redis failure returns 200 degraded with `postgres=true, broker=false`. |

Cancellation does not undo a confirmed ticket. If stock is unavailable, the reservation is recorded as `SKIPPED`. If the engine cannot resolve an uncertain action, the workflow ends at `NEEDS_REVIEW` and retains any confirmed results.

## How recovery works

PostgreSQL stores workflow state, approvals, action attempts, scheduling information and transaction-local audit. Redis Streams notify workers that work is due. If Redis loses a notification, the dispatcher can publish it again from PostgreSQL. Generic admission accepts unapproved planning work only; generic worker and unleased saves cannot mint approval.

Workers claim time-limited leases. Revision and lease checks prevent a stale worker from overwriting newer state. Those checks do not stop a downstream service from completing an earlier request, so the ticket and inventory simulators also keep durable SQLite idempotency records.

A timeout does not mean an action failed. The runner looks up the original key, records confirmed results, and only replays an action when the downstream contract permits it. Retries and lookups have limits. Unresolved outcomes require review rather than an endless retry loop.

Duplicate prevention depends on those downstream idempotency contracts. This project does not claim end-to-end exactly-once execution. See [consistency guarantees](docs/consistency.md) for the assumptions and failure cases.

## Tests

```sh
uv run pytest tests/unit -q
uv run ruff check .
uv run ruff format --check .
```

For current integration evidence, set secret `TEST_POSTGRES_URL` and `TEST_REDIS_URL` values for isolated services, then run:

```sh
uv run --no-sync pytest tests/integration tests/e2e/test_workflow.py --ignore=tests/integration/test_fault_matrix.py -q
uv run --no-sync python scripts/benchmark_faults.py --output-dir artifacts/postgres-<unique-run-id> --seeds 0,1,2,3,4,5,6,7,8,9
```

PostgreSQL tests allocate unique databases and distinct owner/runtime roles, run explicit test migrations, exercise runtime grants, and remove only their registered resources. Redis streams, simulator files and child PIDs are test-owned. Missing service settings cause visible skips; a supplied broken service fails. The process/fault matrix now uses PostgreSQL, Redis and separate HTTP simulators, including committed-effect crashes, Redis loss, late expired workers and database outages. It preserves all 20 scenarios and validates 200 records across ten seeds. Historical MongoDB reports remain separate evidence.

`RUN_COMPOSE=1 uv run --no-sync python -m pytest -p tests.required_checks tests/e2e/test_compose.py --fail-on-skip -q` creates a new random project with dynamically assigned loopback ports. It checks preparation, migrations, grants, worker death, Redis loss and durable recreation, then removes only that project's label-verified volumes. No existing deployment must be stopped. CI requires the PostgreSQL integration, full fault matrix and fresh Compose jobs; required jobs fail on skips. Existing Ruff gates remain enforced. Remote execution of the current changes is not claimed.

## Measured results

The workflow fault/load/admission results below describe the historical MongoDB/API-key release. They are retained unchanged, not new PostgreSQL throughput claims.

- [Fault tests](reports/faults.md) matched all 200 expected outcomes. That includes 20 cases expected to need review, not 200 automatic completions.
- [Load tests](reports/load.md) completed 300 fixture-planned workflows with 600 confirmed effects and zero errors across client concurrency levels of 1, 5 and 20. These tests used simulators, not a live model.
- [Admission latency](reports/admission.md) measured pooled p95 values of 59.431 and 69.536 ms at 20 concurrent clients on separate four-CPU runners. Each run completed 300 workflows with 600 confirmed effects and zero errors. The report retains every trial, including a slower experiment that was removed.
- [Planner evaluation](reports/planner.md) used local Qwen2.5-7B-Instruct Q4_0. It matched all 80 held-out synthetic cases in the original v1 set with schema-valid responses and no unauthorized proposals. Planner request latency was 1.804 seconds at p50 and 2.304 seconds at p95.

The planner's prompt and settings were frozen before the held-out run. The cases use related synthetic templates and a small fixed catalog, with agent-authored and agent-reviewed labels. They do not establish production accuracy. The report retains all four development passes, including regressions.

A new 98-case v2 set is [frozen for a separate evaluation](reports/planner-qwen-q4_0-freeze-v2.json). No live-model result for that set has been recorded yet. The 80-case result above applies only to v1.

The [verification record](docs/verification.md) links the CI runs and records clean-clone, service and restart checks. It also lists failed attempts and local service-dependent skips. Model hashes and reproduction commands are in the [evaluation instructions](docs/evaluation.md).

## Code and design

The runtime stack is Python 3.12, FastAPI, Pydantic 2, SQLAlchemy/Psycopg PostgreSQL, Redis Streams, HTTPX and simulator-only SQLite. PyMongo remains for the historical adapter. The main modules are under [`src/workflow_engine/`](src/workflow_engine/).

- `domain.py` defines workflow records and legal state transitions.
- `policy.py` checks tenant access and request intent without trusting the model.
- `postgres_store.py` implements fenced PostgreSQL writes and atomic audit. `db.py` checks schema and owns short-session lifecycle.
- `store.py` defines shared rules, an in-memory rule fake and the historical MongoDB implementation.
- `runner.py` coordinates execution, leases, retries and uncertain outcomes.
- `gateways.py` classifies responses from external services.
- [`src/external_simulators/`](src/external_simulators/) contains the ticket and inventory services and their SQLite ledgers.

Read the [architecture](docs/architecture.md) for component responsibilities, [domain vocabulary](CONTEXT.md) for terminology, or the [original specification](docs/2026-10-18-maintenance-workflow.md) for scope and requirements.
