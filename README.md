# Maintenance workflow engine

[![Verification](https://github.com/ishhaaab/maintenance-workflow-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/ishhaaab/maintenance-workflow-engine/actions/workflows/ci.yml)

A Python backend that turns maintenance requests into validated plans for approval, then creates tickets and reserves parts. Ticketing and inventory services are local simulators, not live integrations.

The focus is crash recovery. When a request times out or a worker dies, the engine checks the original idempotency key before retrying so it doesn't blindly repeat an action.

## Built so far

- Optional local-LLM planning, policy validation and approvals tied to the exact plan.
- PostgreSQL workflow state and audit records, with Redis Streams for worker notifications.
- Worker leases, bounded retries and recovery of uncertain results. Unresolved actions go to `NEEDS_REVIEW`.
- Ticket and inventory simulators with durable idempotency records.
- Unit, integration and fault tests covering worker crashes, Redis loss and database outages.

## Current status

The PostgreSQL runtime is in place, but this is a health-only release while identity and human review are unfinished. Workflow submission and direct approval are retired; workflow reads and cancellation are blocked. The interactive demo and HTTP load command are unavailable.

Python 3.12, FastAPI, Pydantic 2, SQLAlchemy/Psycopg and Redis Streams. See the [runbook](docs/runbook.md) for PostgreSQL setup and startup. The old MongoDB Compose stack is historical, not a fallback.

## Development

```sh
uv sync --frozen --extra dev
uv run pytest tests/unit -q
uv run ruff check .
uv run ruff format --check .
```

See [architecture](docs/architecture.md), [consistency guarantees](docs/consistency.md) and the [verification record](docs/verification.md) for implementation details and test evidence. [Evaluation instructions](docs/evaluation.md) cover planner and benchmark reproduction. Historical MongoDB benchmarks are not PostgreSQL performance results.
