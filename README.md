# Maintenance workflow engine

[![Verification](https://github.com/ishhaaab/maintenance-workflow-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/ishhaaab/maintenance-workflow-engine/actions/workflows/ci.yml)

A Python backend for approval-gated maintenance requests. Submit a request, review the plan, and approve it before the worker creates a ticket or reserves a part. Both integrations are local simulators.

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

## Run locally

You'll need Python 3.12, [uv](https://docs.astral.sh/uv/), and Docker with Compose v2.

```sh
git clone https://github.com/ishhaaab/maintenance-workflow-engine.git
cd maintenance-workflow-engine
uv sync --frozen --extra dev
cp .env.example .env
# Replace both example API keys in .env with different randomly generated keys.
docker compose config --quiet
docker compose up --build -d --wait
```

In PowerShell, use `Copy-Item .env.example .env` to copy the file.

The default configuration uses a fixture planner. You can run the demo without downloading a model or paying for an API. To use a local model instead, see the [evaluation instructions](docs/evaluation.md).

All exposed ports bind to `127.0.0.1`.

- API documentation: <http://127.0.0.1:8000/docs>
- MongoDB: `mongodb://127.0.0.1:28017`
- Redis: `redis://127.0.0.1:26379/0`
- Ticket and inventory simulators: ports 8001 and 8002

### Try a request

Set the `WORKFLOW_DEMO_API_KEY` environment variable to the key mapped to `tenant-a` in your `.env`, then run:

```sh
uv run python scripts/demo.py
```

The script submits this request:

> Create an inspection ticket for pump P-17 and reserve one seal kit if available.

It prints the validated plan and its hash, then stops at `AWAITING_APPROVAL`. No ticket or reservation is created.

To submit a new demo request and approve its plan:

```sh
uv run python scripts/demo.py --approve
```

The API supports a [limited request grammar](docs/request-format.md), not arbitrary maintenance instructions. Unsupported or ambiguous requests end at `NEEDS_INPUT`. To clarify a request, submit a new one.

## API

Workflow routes require `X-API-Key`. The configured key determines the tenant, not the request body or model output.

| Route | Purpose |
| --- | --- |
| `POST /workflows` | Save `{ "text": "..." }`. Requires `Idempotency-Key`. Returns 202 for a new request, 200 for a replay, or 409 if the same key has a different payload. |
| `GET /workflows/{id}` | Tenant-scoped state, revision, plan, approvals and action results. Foreign IDs return 404. |
| `POST /workflows/{id}/approve` | Approve with `{ "expected_revision": 2, "plan_hash": "..." }`. Repeating approval for the same approved hash returns 200. |
| `POST /workflows/{id}/cancel` | `{ "expected_revision": 2 }`. Active leases and uncertain/in-flight actions return 409. |
| `GET /health/live` | Process liveness. |
| `GET /health/ready` | Mongo failure returns 503; Redis failure returns 200 with explicit degraded status. |

Cancellation does not undo a confirmed ticket. If stock is unavailable, the reservation is recorded as `SKIPPED`. If the engine cannot resolve an uncertain action, the workflow ends at `NEEDS_REVIEW` and retains any confirmed results.

## How recovery works

MongoDB stores workflow state, approvals, action attempts and scheduling information. Redis Streams notify workers that work is due. If Redis loses a notification, the dispatcher can publish it again from MongoDB.

Workers claim time-limited leases. Revision and lease checks prevent a stale worker from overwriting newer state. Those checks do not stop a downstream service from completing an earlier request, so the ticket and inventory simulators also keep durable SQLite idempotency records.

A timeout does not mean an action failed. The runner looks up the original key, records confirmed results, and only replays an action when the downstream contract permits it. Retries and lookups have limits. Unresolved outcomes require review rather than an endless retry loop.

Duplicate prevention depends on those downstream idempotency contracts. This project does not claim end-to-end exactly-once execution. See [consistency guarantees](docs/consistency.md) for the assumptions and failure cases.

## Tests

```sh
uv run pytest tests/unit -q
uv run ruff check .
uv run ruff format --check .
```

For integration tests, set `TEST_MONGO_URL` and `TEST_REDIS_URL` to isolated MongoDB and Redis services, then run:

```sh
uv run pytest tests/integration tests/e2e/test_workflow.py -q
```

Tests create unique `workflow_test_*` databases, Redis streams and temporary SQLite files. Service-dependent tests skip when their URLs are unset and fail if a supplied service is broken.

The Compose test requires `RUN_COMPOSE=1` and starts a randomly named disposable project. Read the [runbook](docs/runbook.md) before running it alongside an existing deployment. GitHub Actions runs the Linux integration and Compose checks.

## Measured results

- [Fault tests](reports/faults.md) matched all 200 expected outcomes. That includes 20 cases expected to need review, not 200 automatic completions.
- [Load tests](reports/load.md) completed 300 fixture-planned workflows with 600 confirmed effects and zero errors across client concurrency levels of 1, 5 and 20. These tests used simulators, not a live model.
- [Planner evaluation](reports/planner.md) used local Qwen2.5-7B-Instruct Q4_0. It matched all 80 held-out synthetic cases with schema-valid responses and no unauthorized proposals. Client latency was 1.804 seconds at p50 and 2.304 seconds at p95.

The planner's prompt and settings were frozen before the held-out run. The cases use related synthetic templates and a small fixed catalog, with agent-authored and agent-reviewed labels. They do not establish production accuracy. The report retains all four development passes, including regressions.

The [verification record](docs/verification.md) links the CI runs and records clean-clone, service and restart checks. It also lists failed attempts and local service-dependent skips. Model hashes and reproduction commands are in the [evaluation instructions](docs/evaluation.md).

## Code and design

The stack is Python 3.12, FastAPI, Pydantic 2, asynchronous PyMongo, Redis Streams, HTTPX and SQLite. Dependencies and container images are pinned. The main modules are under [`src/workflow_engine/`](src/workflow_engine/).

- `domain.py` defines workflow records and legal state transitions.
- `policy.py` checks tenant access and request intent without trusting the model.
- `store.py` implements conditional MongoDB writes and an in-memory test implementation.
- `runner.py` coordinates execution, leases, retries and uncertain outcomes.
- `gateways.py` classifies responses from external services.
- [`src/external_simulators/`](src/external_simulators/) contains the ticket and inventory services and their SQLite ledgers.

Read the [architecture](docs/architecture.md) for component responsibilities, [domain vocabulary](CONTEXT.md) for terminology, or the [original specification](docs/2026-10-18-maintenance-workflow.md) for scope and requirements.
