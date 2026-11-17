# Local runbook

## Current review application

Use [the approval review runbook](review-runbook.md) for the current application, clean setup, seed accounts, role matrix, reminders, exports and migration instructions. [The walkthrough](review-walkthrough.md) gives the complete browser sequence; [acceptance](review-acceptance.md) records verification.

## Retained Phase 1 foundation instructions

The section below records the earlier fail-closed foundation before identity/review existed. Its health-only limitations are historical, not the current product boundary. Credential provisioning, volume preservation and Windows-loop guidance still apply. Do not use its statements about unavailable UI/reminders/exports as current release status.

This release deliberately exposes health only. Admission/direct approval return 410. Protected reads/cancellation return 401 regardless of legacy keys or cookies. Interactive demo and load commands exit nonzero before making requests. No approval override is available.

Use Python 3.12 and `uv sync --frozen --extra dev`. Provision roles and apply Alembic migrations explicitly through the separate operator tools before starting the application. Supply exactly one `WORKFLOW_DATABASE_URL` or `WORKFLOW_DATABASE_URL_FILE` to the API/worker. The file is at most 16 KiB of UTF-8 containing one raw runtime PostgreSQL URL with an optional final newline. Empty, unreadable, oversized, multiline and allocation-JSON files fail with a static configuration error. Never give the application migration/owner/admin credentials or a full allocation file.

`uv run workflow-engine init` only verifies the required schema head and closes the database engine. It does not migrate, create roles, read a catalog, contact Redis, or construct planner/effect clients. Planner-only tools and CLI help do not require database settings.

With a separately configured Redis service and effect URLs, native commands are:

```sh
uv run workflow-engine api
uv run workflow-engine worker --worker-id worker-1
uv run workflow-engine dispatcher
```

`WORKFLOW_REDIS_URL` selects Redis. Both API and executor resources use `WORKFLOW_REDIS_STREAM=workflow-pg-wakeups-v1` and `WORKFLOW_REDIS_GROUP=workflow-pg-workers-v1` by default, distinct from the historical stack. Do not point these at the old deployment. Preserve fresh simulator ledgers for any later PostgreSQL demo.

On Windows use `workflow-engine` or `python -m workflow_engine.cli`, not an unconfigured Uvicorn invocation. Both the async process runner and API server use a Psycopg-compatible selector loop. Linux uses its normal asyncio loop.

`/health/live` stays public. `/health/ready` requires a working PostgreSQL connection at the expected migration head. A false store ping or schema/connection error returns 503 with `postgres=false`. Redis loss returns 200 degraded with `postgres=true, broker=false`. No diagnostics or connection values are returned. API startup keeps these health endpoints available on schema failure; executor startup fails closed. Lifecycle contexts close the engine, Redis and constructed HTTP clients on exit or initialization failure.

For tests, supply isolated `TEST_POSTGRES_URL` and `TEST_REDIS_URL` secrets. The test bootstrap alone receives administrative credentials. Actual store/runtime operations use the generated non-owner role. Owner-side preparation exists only in `tests/`, checks exact allocation ownership, and is not a production workflow command. Runtime children receive only explicit runtime configuration; simulator children receive no database credentials. Run the integration command and full ten-seed fault benchmark listed in [verification](verification.md).

## Fresh Compose setup and lifecycle

Always select `-f compose.postgres.yaml` and a consistent explicit `-p` project name. The README lists the six first-time commands: build, prepare admin, start PostgreSQL/Redis, prepare roles, migrate, start application services. The default new project is `maintenance-workflow-engine-postgres`. The first admin job requires an empty new PostgreSQL data volume; it generates the password privately. Never copy the historical `.env` into service environments. Optional non-secret port/image overrides can live in a new dedicated `--env-file` used consistently by every command.

Default loopback ports are API 18000, simulators 18001/18002, PostgreSQL 15432 and Redis 16379. `WORKFLOW_PG_API_PORT`, `WORKFLOW_PG_TICKET_PORT`, `WORKFLOW_PG_INVENTORY_PORT`, `WORKFLOW_PG_DB_PORT` and `WORKFLOW_PG_REDIS_PORT` override them. Tests use port 0 and discover actual bindings through `compose port`; a random project alone does not isolate fixed ports.

The new project owns six named volumes: `pg-data-v1`, `pg-admin-v1`, `pg-owner-v1`, `pg-runtime-v1`, `pg-ticket-data-v1` and `pg-inventory-data-v1`. Compose scopes their names to the project. Keep this complete recovery set and the same project name. Changing the project name creates a different deployment, not a renamed database. `down` without `--volumes` retains state. Do not remove volumes to fix a configuration error or reuse historical simulator files.

Preparation is an explicit Linux-root, short-lived job. Administrator/canonical records are root-only; the PostgreSQL password file is readable by its verified GID 999. Owner/runtime readers are UID/GID 10001 and their URL files are mode 0400. Each application container has only the runtime volume mounted read-only. Migration has only the owner volume. No application container receives privileged settings, ancestor secret directories, PostgreSQL data/socket or a Docker socket. These are private Docker volumes, not encrypted secrets; Docker/host administrators remain trusted.

For ordinary restart, use the same prefix with `up -d --wait` or `restart worker-a worker-b dispatcher api`. No job is automatically run. For an explicit schema change, stop application processes first, run `migrate`, then restart them. Repeated `prepare roles` and migration reuse the same complete allocation. They never rotate passwords, repair arbitrary existing roles, or create another application database after losing a split file. A missing split file can be recreated from a valid canonical record; a mismatched file fails rather than being overwritten.

Preparation serializes concurrent attempts with an exclusive file lock and reserves a durable started marker before DDL. Stale locks, incomplete markers, corrupt records or invalid target credentials require operator inspection. Do not delete a marker or rename credential files as a normal recovery step. An abrupt crash after DDL but before canonical persistence may leave an allocation needing explicit recovery. A normal pre-canonical publication failure attempts to compensate only the exact fresh allocation it just created; failed compensation remains an error with retained state. No prefix-based cleanup or automatic deletion of a committed database is available. Keep credential files out of logs, command arguments, build context, screenshots and uploaded artifacts.

PostgreSQL logs suppress statement logging, error statements, parameter values and row DETAIL/CONTEXT before bootstrap. Tests verify representative password-statement and failing-row sentinels. This does not mean arbitrary PostgreSQL primary error messages can never contain data. Do not deliberately submit secrets as SQL literals or print driver exceptions.

## Native role provisioning

Use a new disposable/fresh PostgreSQL target, never a user's existing workflow database. `uv run --no-sync python scripts/init_postgres_roles.py --output <new-private-allocation-file>` takes the administrative URL through a hidden prompt. Alternatively `--config <private-json-file>` reads one `admin_url`; no password is accepted in argv. Protect these files with local user-only permissions/ACLs and keep them outside the repository. The allocator accepts no existing role/database name and refuses an existing output file. If it fails or leaves partial output, inspect the owned allocation before trying another output path; it is not an existing-database repair/rotation command.

In a separate operator process, load only `owner_url` from that private record into `WORKFLOW_MIGRATION_URL`, then run `uv run --no-sync alembic upgrade head` and `uv run --no-sync alembic check`. Clear that variable when finished. Copy only `runtime_url` into a new private UTF-8 raw URL file, without a BOM or JSON wrapper, and give its path to application processes as `WORKFLOW_DATABASE_URL_FILE`. Never give API/workers the combined allocation file or inherit the operator process environment. On PowerShell, use process-scoped `$env:` variables, not command-line password values; Python `Path.write_text(value, encoding='utf-8')` writes the raw URL without a BOM. Native `workflow-engine init` verifies schema only and performs no migration.

## Required isolated verification

The PostgreSQL integration and full fault commands are documented in verification. `RUN_COMPOSE=1` enables `uv run --no-sync python -m pytest -p tests.required_checks tests/e2e/test_compose.py --fail-on-skip -q`. It selects both the new deployment file and a test-only override, creates a random project with dynamic loopback ports, and uses an explicit test-tools image to prepare exact-plan fixtures in that deployed database. The production image has no fixture command or test sources. Do not select that override for the normal deployment. Final cleanup verifies project labels and removes only the test's recorded resources, including volumes referenced by inactive preparation profiles. CI uploads only selected safe test logs/results, never credential volumes.

The current release still cannot accept human approval, send reminders, export reviews or display a review UI. Execution success in these tests is not evidence those later phases exist. Historical MongoDB/load/model reports below remain unchanged.

## Historical MongoDB deployment notes

Everything below describes the prior release, including its old keys, commands, reset procedure and measurements. It is retained for history, not instructions for the current runtime. `compose.yaml` and its volumes are unchanged. Do not start, stop, reset or repoint that stack as part of this Phase 1 work.

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
