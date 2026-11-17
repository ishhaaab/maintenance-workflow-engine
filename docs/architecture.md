# Architecture

## Responsibilities

The runner has one public operation, `tick(workflow_id, worker_id)`. It claims eligible work, performs one phase with at most one external mutation, saves the result under its lease, then releases that lease. API handlers do not duplicate the state machine. The broker does not decide which action to execute.

The domain returns new validated snapshots without I/O. The store checks revisions, current tokens and immutable fields, then updates selected fields. This keeps heartbeat and enqueue changes from being overwritten by a worker's older snapshot. Generic admission can create only unapproved planning work. General saves cannot introduce approval, including leased worker saves. Only the final assigned decision's transaction-owned execution bridge creates production authorization. Isolated owner test fixtures are not an API/CLI command.

PostgreSQL is the only default runtime store. Each operation opens a short independent session; tick and heartbeat never share one. A clock-injected memory fake proves domain rules, not PostgreSQL concurrency. The MongoDB adapter remains historical. The planner has fixture and OpenAI-compatible model adapters, and gateways are injected into the runner.

FastAPI serves same-origin Jinja pages and JSON command adapters. PostgreSQL-backed opaque sessions resolve a live tenant membership. Authenticated review commands own cycle/item/version/decision transactions; the UI never authorizes a role. Standalone admission/direct approval still return 410, with no API-key fallback or approval fixture setting. Protected reads enforce ownership/assignment, and safe cancellation is ADMIN-only. Runtime construction accepts only a runtime PostgreSQL URL or bounded raw-URL file. Schema initialization verifies the migration head without DDL. CLI init does not construct executor clients.

## State ownership

`compose.postgres.yaml` separates ten long-running services from explicit preparation/migration jobs. A private administrator volume holds the started journals and canonical allocation; only preparation and PostgreSQL mount it. The migration job mounts only the owner credential volume. API, dispatcher, both workers and the notification worker mount only the runtime credential volume, read-only. Mailpit captures local email. Neither application startup nor ordinary Compose `up` runs DDL. The production image contains no tests or fixture approval command; the executor's privileged test setup exists only in an explicitly selected test image/override.

Preparation persists an exclusive started marker before fresh allocation, then fsyncs a canonical record before atomically publishing separate owner/runtime files. Repeated explicit preparation verifies the recorded database, role properties, credential identities and runtime binding. A missing consumer file can be republished from the same canonical identity; a mismatched file is not overwritten. An interrupted pre-canonical bootstrap remains a visible recovery condition. No prefix scan, existing-role repair, password rotation or deletion of a committed deployment is part of preparation.

```mermaid
flowchart TD
    Client -->|same-origin forms and opaque session| API[FastAPI / Jinja]
    API --> Commands[Authorized review commands]
    Commands -->|versions, decisions, audit and due intent atomically| PostgreSQL[(PostgreSQL)]
    API -->|consistent administrator reports| PostgreSQL
    Notifications -->|leased current-step reminder jobs| PostgreSQL
    Notifications -->|SMTP outside SQL transaction| Mailpit
    Dispatcher -->|find due unleased executions| PostgreSQL
    Dispatcher -->|workflow ID and sequence only| Redis[(Redis Streams)]
    Redis --> Worker
    Worker --> Runner
    Runner -->|revision and lease-fenced writes plus audit| PostgreSQL
    Runner -->|read-only proposal| Planner
    Planner --> Policy[Deterministic tenant policy]
    Runner -->|approved stable-key action| Ticket[Ticket simulator]
    Runner -->|after confirmed ticket| Inventory[Inventory simulator]
    Ticket --> TicketDB[(Ticket SQLite ledger)]
    Inventory --> InventoryDB[(Inventory SQLite ledger and stock)]
```

Each execution contains its request, plan, approval, action identities, bounded attempt counts, due intent, lease and at most 256 history entries. The full serialized snapshot budget is 256 KiB. Column-backed fields and one JSONB payload form one authoritative execution record. Semantic changes append audit within the same transaction; audit is not trimmed with operational history. There is no cross-store transaction.

## Concurrency

- PostgreSQL server time controls leases and retry eligibility.
- Semantic changes increment revision. Lease renewal and dispatch markers do not.
- Lease duration is 30 seconds; heartbeats run every 5 seconds.
- The dispatcher scans once a second and republishes each eligible workflow no more than once per 5 seconds.
- Redis abandoned pending entries are reclaimed after 60 seconds. PostgreSQL redispatch independently covers Redis loss. Fresh default names are `workflow-pg-wakeups-v1` and `workflow-pg-workers-v1`.
- Internal saves reject conflicting revisions and live foreign leases. Public review decisions fence item/progress revisions and exact version/plan hash. Relevant principals lock in stable order before cycle, item, progress and run, so last-item decisions cannot miss completion.

An expired worker may still be executing at a downstream system. Its token prevents stale local writes. Only the downstream same-key contract prevents duplicate external effects. These guarantees solve different problems.

## Model restrictions

The model receives request text and one authorized catalog snapshot. It returns either a typed plan or clarification questions. The policy independently resolves explicit references and compares asset, priority, reservation intent, part and quantity against a limited grammar. The compiled reservation obtains its ticket ID from the confirmed ticket action, never from model output.

Approval authorizes the immutable hash of schema version, catalog version and canonical validated plan JSON. Later catalog changes do not rewrite an approved plan in this MVP. Catalog mutation is not a public endpoint.

## Failure handling

External timeouts, malformed success responses and unclassified server errors are ambiguous. Lookup uses the original key. Confirmed and skipped results are durable; unavailable lookup is bounded. Safe same-key replay requires an advertised deduplication contract. Limits are five mutation submissions, five lookups per ambiguous episode and two planner calls per workflow.

Review completion is independent of execution completion. Immutable review versions/steps/plans/decisions preserve earlier approvals as history, never as permission for a superseding version. Report queries use a bounded repeatable-read snapshot; notifications reread eligibility before sending. See [consistency.md](consistency.md) for partial outcomes, cancellation and assumptions, and [the review runbook](review-runbook.md) for states, limits and operation.
