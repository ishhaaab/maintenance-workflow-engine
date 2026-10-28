# Local runbook

## Requirements and secrets

Use Python 3.12 and `uv sync --frozen --extra dev`. Docker Compose v2 is required for the seven-process deployment and the two worker replicas. `.env.example` documents the environment. Replace both example API keys before starting the deployment. Keep `.env`, SQLite files, database volumes and raw local artifacts out of Git.

API-key configuration is a demo identity mechanism. The two seeded tenants have separate catalogs. Environment variables configure external URLs; no request or model output can choose a destination.

## Start, inspect, stop

```sh
docker compose config --quiet
docker compose up --build -d --wait
docker compose ps
docker compose logs --tail=50 api worker dispatcher
docker compose down
```

`down` retains MongoDB and simulator volumes. Restarting the stack does not reseed existing inventory stock or remove idempotency ledgers. `workflow-engine init` initializes indexes without requiring model credentials. API and workers also initialize indexes idempotently at startup.

The application images run as UID 10001. MongoDB and Redis images use their own upstream service identities. Published ports are localhost-only. Compose uses 28017 and 26379 for data stores to reduce conflicts with default local installations.

## Native processes

With MongoDB and Redis already running, set `WORKFLOW_MONGO_URL`, `WORKFLOW_REDIS_URL` and both external URLs, then run these independently:

```sh
uv run workflow-engine ticketing
uv run workflow-engine inventory
uv run workflow-engine api
uv run workflow-engine worker --worker-id worker-1
uv run workflow-engine worker --worker-id worker-2
uv run workflow-engine dispatcher
```

The API binds `127.0.0.1:8000` by default. Simulator defaults are 8001/8002. `--database` chooses each simulator's SQLite file. `--enable-faults` or `ENABLE_FAULTS=true` enables test-only HTTP controls; keep them off for the normal demo.

## Tests and isolation

Default `uv run pytest -q` runs offline tests and local HTTP subprocess tests. Real MongoDB/Redis and Compose cases visibly skip unless enabled.

```sh
export TEST_MONGO_URL=mongodb://127.0.0.1:28017
export TEST_REDIS_URL=redis://127.0.0.1:26379/0
uv run pytest tests/integration tests/e2e/test_workflow.py -q
```

PowerShell uses `$env:TEST_MONGO_URL='mongodb://127.0.0.1:28017'` and the corresponding Redis assignment. The suite creates and drops only its own randomly named `workflow_test_*` database. Redis cleanup deletes only each test's randomly named stream. SQLite files and child processes are owned by the individual test.

`RUN_COMPOSE=1 uv run pytest tests/e2e/test_compose.py -q` builds a random `workflow-test-*` Compose project, then removes only that project's containers and volumes. Its published ports are 8000–8002, 28017 and 26379; stop an existing demo stack first so those ports are free. The test creates a separate temporary environment file and never overwrites your `.env`.

The process-kill cases wait for a test-only marker after a durable write, kill that specific child PID, then set the isolated test lease deadline in the past. This avoids a 30-second sleep while still exercising MongoDB server-time expiry and process restart. It is not a timing-based guess about when a call committed.

## Recovery demonstrations

Use the named fault cases instead of killing an arbitrary process:

```sh
uv run pytest tests/integration/test_fault_matrix.py -k worker_death_after_effect_before_checkpoint -q
uv run pytest tests/integration/test_fault_matrix.py -k redis_data_loss_after_publish -q
```

The Redis-loss case deletes only its random test stream, never a shared Redis database. The worker-death case verifies the external ledger contains one logical effect after recovery.

## Interpreting state

- `AWAITING_APPROVAL`: fetch the plan and approve that exact hash and revision.
- `NEEDS_INPUT`: submit a clarified new request with a new admission key.
- `RETRY_WAIT`: the deadline is durable. The dispatcher will wake the workflow again.
- `RECONCILING`: do not manually issue a new action key; an earlier call may have committed.
- `NEEDS_REVIEW`: inspect confirmed partial results and the downstream operation using the stored key. This version has no manual resolve endpoint; do not modify MongoDB records to fabricate success.
- `FAILED` or `CANCELLED`: inspect retained action outcomes before deciding what to do next.

MongoDB failure rejects admission with 503. Redis failure does not invalidate a successful durable admission; readiness says degraded until the broker returns.

## Resetting disposable demo data

Only if you intentionally want to erase this project's demo workflows, inventory and idempotency ledgers:

```sh
docker compose down --volumes
```

Never reset ledgers while retaining workflows that might retry. Never use broad Docker pruning or shared database flushes.
