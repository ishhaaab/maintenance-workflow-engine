# Maintenance workflow engine

[![Verification](https://github.com/ishhaaab/maintenance-workflow-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/ishhaaab/maintenance-workflow-engine/actions/workflows/ci.yml)

An asynchronous Python backend that turns a maintenance request into a validated plan, waits for approval, then creates a simulated ticket and optionally reserves a simulated part. It recovers work after worker crashes and Redis message loss without blindly repeating uncertain external mutations.

```text
Request → LLM proposal → deterministic policy → exact-plan approval
                                                    ↓
MongoDB workflow + due intent → Redis wakeup → leased runner
                                                    ↓
                                  ticket → conditional reservation
                                                    ↓
                                  uncertain outcome → key lookup
```

The model cannot invoke mutation tools. MongoDB is the authority; Redis carries replaceable notifications. External effects rely on the simulators' durable idempotency contracts. This project makes no end-to-end exactly-once claim.

## Run locally

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), and Docker with Compose v2. GitHub Actions runs the Linux integration and container checks independently of your workstation.

```sh
git clone https://github.com/ishhaaab/maintenance-workflow-engine.git
cd maintenance-workflow-engine
uv sync --frozen --extra dev
cp .env.example .env
# Replace both example API keys in .env with different randomly generated keys.
docker compose config --quiet
docker compose up --build -d --wait
```

PowerShell uses `Copy-Item .env.example .env` instead of `cp`. Container ports bind only to `127.0.0.1`. Startup initializes indexes idempotently; catalog data is a versioned read-only fixture. No paid key or model download is needed in fixture mode.

- API and OpenAPI documentation: <http://127.0.0.1:8000/docs>
- MongoDB: `mongodb://127.0.0.1:28017`
- Redis: `redis://127.0.0.1:26379/0`
- Ticket and inventory simulators: ports 8001 and 8002

### Two-minute demo

Set `WORKFLOW_DEMO_API_KEY` to your tenant-A key, then run:

```sh
uv run python scripts/demo.py
```

This prints the validated plan and its hash, with zero external effects. To authorize that synthetic demonstration explicitly:

```sh
uv run python scripts/demo.py --approve
```

The request is:

> Create an inspection ticket for pump P-17 and reserve one seal kit if available.

The API accepts a bounded [request grammar](docs/request-format.md). Unsupported or ambiguous requests become `NEEDS_INPUT`. Clarifications are new requests in this version.

## HTTP contracts

Authenticated routes require `X-API-Key`. Tenant identity comes from configuration, never the request body or model.

| Route | Purpose |
| --- | --- |
| `POST /workflows` | Admit `{ "text": "..." }` durably; requires `Idempotency-Key`. New request 202, replay 200, changed payload 409. |
| `GET /workflows/{id}` | Tenant-scoped state, revision, plan, approvals and action results. Foreign IDs return 404. |
| `POST /workflows/{id}/approve` | `{ "expected_revision": 2, "plan_hash": "..." }`. Exact approved-hash replay returns 200. |
| `POST /workflows/{id}/cancel` | `{ "expected_revision": 2 }`. Active leases and uncertain/in-flight actions return 409. |
| `GET /health/live` | Process liveness. |
| `GET /health/ready` | Mongo failure returns 503; Redis failure returns 200 with explicit degraded status. |

Cancellation does not undo a confirmed ticket. Stock shortage is a stored `SKIPPED` reservation. Unresolved effects end at `NEEDS_REVIEW`; confirmed partial outcomes remain visible.

## Verify it

```sh
uv run pytest tests/unit -q
uv run ruff check .
uv run ruff format --check .
```

For real MongoDB/Redis tests, set `TEST_MONGO_URL` and `TEST_REDIS_URL` to isolated test services, then run:

```sh
uv run pytest tests/integration tests/e2e/test_workflow.py -q
```

Tests allocate unique `workflow_test_*` databases, Redis streams and temporary SQLite files. Missing external services are explicit skips unless their test URLs are supplied; supplied but broken services fail. The Compose test is opt-in with `RUN_COMPOSE=1` and starts a randomly named disposable project. See the [runbook](docs/runbook.md) before running it alongside an existing deployment.

## Evidence and completion gates

- [Fault report](reports/faults.md): all 200 corrected fault cases matched their expected outcomes, including 20 expected review cases.
- [Load report](reports/load.md): 300/300 fixture-planned workflows completed with 600 confirmed effects and zero errors at client concurrency 1, 5 and 20.
- [Planner report](reports/planner.md): local Qwen2.5-7B-Instruct Q4_0 matched 80/80 held-out synthetic cases, with 100% schema validity, zero unauthorized proposals and 1.804 / 2.304-second p50 / p95 latency.
- [Verification record](docs/verification.md): clean-clone installation, 87 locally passing tests, real MongoDB/Redis checks, Compose/restart verification and linked CI artifacts.

The real-model measurement gate is complete. The prompt, model and settings were frozen before one held-out pass; all four development passes are retained. These related synthetic templates establish bounded-corpus performance, not production accuracy. See [evaluation instructions](docs/evaluation.md) for model hashes and reproduction commands. Fixture reliability/load results remain separate from live-model quality.

## Design

Python 3.12, FastAPI, Pydantic 2, asynchronous PyMongo, Redis Streams, HTTPX and SQLite. Dependencies and container images are pinned.

- `domain.py` owns legal transitions and typed records.
- `policy.py` checks tenant access and request intent independently of the model.
- `store.py` implements conditional MongoDB writes and an in-memory contract fake.
- `runner.py` owns orchestration, durable attempts, leases, retries and reconciliation.
- `gateways.py` classifies external responses; uncertain responses never imply no effect.
- `external_simulators/` provides transactional effect/idempotency ledgers.

Read [architecture](docs/architecture.md), [consistency guarantees](docs/consistency.md), [domain vocabulary](CONTEXT.md), and the [source specification](docs/2026-10-18-maintenance-workflow.md).

This is a local maintenance-administration portfolio project. The integrations are simulators. It does not actuate equipment, certify repairs, or make safety decisions.

## Sync local changes

```sh
git add <changed-files>
git commit -m "Describe the change"
git push
```

The `origin` remote is the private GitHub repository. To download later remote changes, use `git pull --ff-only` with a clean working tree.
