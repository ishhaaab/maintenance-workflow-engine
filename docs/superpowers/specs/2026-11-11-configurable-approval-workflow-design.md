# Configurable approval workflow engine design

Status: approved by the user on 2026-11-11 with "sure go on" after presentation of the design and its approval choices. Implementation has not started.

This document prepares the evolution of the existing repository. The user's approval accepts the proposed design and permits preparation of the Phase 1 implementation plan. Review that plan before product changes. Approval does not authorize a data import, deployment cutover, or work beyond the requested phase. The original proposal language below is retained to distinguish author recommendations from explicit requirements; those recommendations are now covered by the user's design approval unless noted otherwise.

## Preparation scope and checklist

- [x] Inspect repository instructions, current documentation, domain, store, authentication, runner, broker, runtime, simulators, and service-dependent tests.
- [x] Write the proposed entities, rules, interfaces, transaction ownership, compatibility changes, and subsequent phase acceptance checks.
- [x] Obtain a fresh builder check and resolve material gaps in this document.
- [x] Present the design and approval decisions to the user, then stop. The user subsequently approved the design; Phase 1 planning is next.

Only this file is to change during preparation. No schema migration runs now. No new failing behavior tests are appropriate until implementation is authorized. Verification during preparation is an existing-unit-suite run and a document/diff review. Each subsequent phase must first provide its own small plan with exact files, interfaces, migration revisions, initially failing tests, commands, and acceptance criteria. Stop if a prerequisite phase is missing.

## 1. Requirements versus recommendations

The user explicitly requires preserving execution safety, named sequential review, tenant isolation, fixture-based demos, transactional audit, UTC deadlines, overdue reminders, administrator exports, and phased delivery. MongoDB data, simulator ledgers, volumes, and existing untracked files must remain untouched. No dual authoritative writes, public administrator signup, generic DAG engine, plugin framework, paid-model requirement, or microservice rewrite.

The user's suggested default was PostgreSQL, SQLAlchemy 2, Alembic, existing FastAPI, Redis wakeups, and SQLite simulators. It was evaluated as a recommendation during drafting and is now covered by the design approval above.

The accepted recommendation makes PostgreSQL authoritative for BOTH review and execution. The following author-proposed choices were also presented and accepted with the design:

- Fresh relational deployment with no automatic MongoDB import or resumption of historical workflows.
- One tenant-scoped role per membership, local passwords and PostgreSQL-backed opaque sessions.
- Frozen cycle participants, item membership, and approval definitions on activation.
- No self-approval, no repeated reviewer in an item chain, required rejection/change comments, terminal rejection, and complete restart of review on a new version.
- Review completion independent of execution completion, automatic execution authorization at final approval, and no separate execute button.
- Same-origin server-rendered browser pages, not a separate SPA, for the first release.
- Retiring standalone workflow creation/direct approval in the relational deployment rather than keeping bypass-compatible endpoints.

Approval of this document would supersede the original plan's MongoDB-only and no-browser-UI choices. It would not supersede its execution guarantees. The old plan remains historical documentation and is not silently rewritten.

## 2. Current checkout findings

The inspection found no repository-local or ancestor `AGENTS.md` files along this checkout's path. The supplied global instructions still apply. The three existing untracked `INTERVIEW_*` files are unrelated user work.

- `domain.py` provides frozen Pydantic snapshots and pure `transition`. It hashes canonical plan JSON and separates passive review/terminal states from due work. Actions, confirmed results, and exact-hash approval are protected by `store.py`.
- `WorkflowStore` currently exposes `insert_or_get`, tenant-scoped `get`, `claim`, `save`, `renew`, `release`, and `due_wakeups`. MongoDB server time controls leases and scheduling. Saves preserve operational fields instead of replacing stale snapshots. A clock-injected memory fake supplies deterministic tests, not database evidence.
- `api.py` maps an API key to a tenant only. Every valid key for a tenant can currently approve or cancel that tenant's workflow. There are no users, roles, passwords, sessions, CSRF checks, review assignments, or browser pages. Direct approval accepts a revision and plan hash, then schedules execution.
- `Runner.tick` claims a 30-second lease, heartbeats every 5 seconds, performs one phase and at most one mutation, saves under revision/token fencing, and releases in `finally`. Planning records a durable attempt before calling the planner. No database transaction spans a planner or HTTP call.
- `policy.validate_and_compile` checks the restricted maintenance grammar and tenant catalog, hashes schema/catalog/validated plan, and compiles stable keys from tenant/workflow/hash/action. The model cannot choose tools or URLs. The reservation receives only the confirmed ticket ID.
- `broker.py` sends workflow ID and wake sequence only. The dispatcher scans durable due intent before publication and can republish after 5 seconds. Redis abandoned messages are reclaimed after 60 seconds. Redis is not authoritative.
- `gateways.py` distinguishes confirmed/skipped, proven-no-effect failures, and uncertainty. Mutation retries are bounded at five, reconciliation lookups at five per ambiguous episode, and planner calls at two. Unknown outcomes stop at `NEEDS_REVIEW` rather than claiming no effect.
- SQLite `Ledger.apply` uses `BEGIN IMMEDIATE` to commit an effect, stock change, and indefinite key ledger together. Logical action effect counts are independent of the idempotency key. Keep these contracts.
- `runtime.py` currently constructs MongoDB/Redis clients and initializes indexes at startup. PostgreSQL migrations must instead be an explicit deployment step, not concurrent worker startup DDL.

Evidence locations include `tests/unit/test_{domain,store,runner,runtime}.py`, `tests/integration/test_{store,api,broker,recovery,simulators,fault_matrix}.py`, `tests/fault_worker.py`, `tests/support.py`, and `tests/e2e/test_{workflow,compose}.py`.

Several tests under `tests/integration`, including most recovery tests, actually use the memory store and in-process SQLite simulators. Real MongoDB fixtures skip without `TEST_MONGO_URL`; Redis checks skip without `TEST_REDIS_URL`; Compose is opt-in. The fault matrix uses real stores and HTTP subprocesses, and defaults to one seed unless configured otherwise. Existing CI excludes that matrix from its ordinary integration command and runs the full benchmark only on selected dispatches. Preserve these distinctions when porting tests.

Preparation verification: `.venv\Scripts\python.exe -m pytest tests/unit -q` returned **53 passed**. No live PostgreSQL, MongoDB, Redis, SMTP, Compose, or browser checks were run for this design. The earlier 88-pass/26-skip baseline is historical, not a current full-suite result. The Repowise CLI was unavailable, and its MCP default overview described another repository, so neither was used as evidence about this checkout.

## 3. Architecture and cutover

Keep one Python application with separate API, worker, dispatcher, and notification-worker processes. Add PostgreSQL through SQLAlchemy 2 async sessions and a PostgreSQL driver. Retain Redis Streams and both SQLite simulators. Pin compatible versions and images during Phase 1, not now.

The execution aggregate remains bounded and projects to the existing `Workflow` type. Store its runtime snapshot, actions, retry counters, due intent, revision, lease, and bounded history in one `execution_runs` row, using JSONB for the bounded snapshot. Relational columns hold identity, review links, revision, state, plan hash, authorization hash, scheduling, and leases. The adapter assembles a `Workflow` on reads. Do not maintain competing copies of these operational fields inside JSONB. Normalize review records; do not attempt to normalize every existing action transition during this migration.

This is an incremental code evolution, but not a live mixed-database execution architecture. The relational deployment starts empty. Preserve the old MongoDB volume, credentials, and old deployment configuration for explicitly managed historical access. Never connect new workers to old streams or let both deployments consume the same logical work. Use separate Redis stream/group names and separate simulator databases for the fresh relational demo. Reusing old simulator ledgers would require a separately approved operational plan.

Before replacing any existing deployment, an operator must inventory its nonterminal workflows and choose whether to drain them under the old version or leave that deployment stopped with durable state retained. Do not abandon an in-flight action while claiming it never happened. No automatic cutover, data import, rollback replay, or ledger reset is authorized here. Rolling the application back to MongoDB does not make PostgreSQL work disappear; stop new processes and inspect outstanding effects first.

### Alternatives considered

Keeping MongoDB as executor authority reduces the immediate store-port work, but final approval could not atomically commit a PostgreSQL decision and MongoDB execution authorization. It would need a PostgreSQL outbox, an idempotent authorization consumer, exact-hash receipts, cross-store recovery, and decisions about cancellation races. PostgreSQL would own review permission while MongoDB owned executable permission. That is extra coordination at the most safety-sensitive point. I do not recommend it for this release. If PostgreSQL fencing cannot pass the existing real-service recovery acceptance tests, stop the migration and ask the user to choose; do not silently implement this fallback.

## 4. Domain model, ownership, and constraints

The accepted design defines the following new terms for the evolution. A **cycle** groups a fixed set of review items. An **item** is a requester's maintenance request within a cycle. An **item version** is immutable submitted content. A **validated plan** is immutable policy-checked maintenance intent for one version. An **approval definition** names a reviewer and order. A **decision** records that assigned reviewer's response to that version and plan. **Review progress** is mutable current-step state. An **execution run** retains the existing durable planning/execution state. An **audit record** is a transactional record of a committed change, not the source from which current state is rebuilt.

Use opaque UUIDs for new IDs and timezone-aware `timestamptz`. Every tenant-owned table has `tenant_id NOT NULL`. Give each referenced object a unique `(tenant_id, id)` key and use composite foreign keys throughout. A bare object UUID is never sufficient to authorize a lookup. No cascading deletion of review, execution, or audit evidence.

| Table | Ownership and important fields | Constraints and indexes |
| --- | --- | --- |
| `tenants` | Operator-created tenant, stable slug | Unique normalized slug |
| `users` | Local login identity, normalized login, password hash, enabled flag | Unique normalized login; no public creation |
| `tenant_memberships` | User/tenant, one role `ADMIN`, `REQUESTER`, or `REVIEWER`, enabled flag | Unique tenant/user; composite tenant/member key; role check |
| `auth_sessions` | Hash of random token, user, selected membership, CSRF secret, created/last-seen/idle/absolute expiry, revoked timestamp | Unique token hash; expiry and user/revocation indexes; tenant membership FK |
| `review_cycles` | Tenant admin creator, title, `DRAFT/ACTIVE/COMPLETED`, revision, timestamps | Tenant/state/created index; completed timestamp/state check |
| `cycle_requesters` | Named requester memberships admitted by admin to a draft cycle | Unique tenant/cycle/member; same-tenant membership FK |
| `review_items` | Cycle, immutable requester owner, current version pointer, revision | Tenant/cycle index; current version composite FK includes item identity |
| `item_versions` | Item, increasing version number, original submitting actor, immutable request text/hash, catalog snapshot/version, predecessor | Unique tenant/item/version number; immutable source rows; same-item predecessor FK |
| `approval_definitions` | Item, 1-based level, assigned reviewer membership, positive due-after seconds | Unique tenant/item/level and tenant/item/reviewer; same-tenant membership FK |
| `version_steps` | Immutable copy of each definition for a version | Unique tenant/version/level and tenant/version/reviewer; reviewer cannot equal item requester or original submitter |
| `validated_plans` | Version/run identity, canonical validated plan, plan hash, schema/catalog versions, compiled action identities | Unique tenant/version and tenant/run; immutable after insertion; bounded JSON; hash format check |
| `review_progress` | One row per version, `PREPARING/NEEDS_INPUT/PLAN_FAILED/PENDING/CHANGES_REQUESTED/APPROVED/REJECTED/SUPERSEDED`, current level, step-opened/deadline, revision | Unique tenant/version; index tenant/status/deadline; terminal progress has no current deadline |
| `decisions` | Version, step, reviewer actor, plan hash, `APPROVE/REJECT/REQUEST_CHANGES`, bounded comment, timestamp, command ID and payload hash | Unique tenant/version/level; unique tenant/actor/command ID; nonblank comments for reject/changes |
| `execution_runs` | One per version, existing workflow UUID, request/admission identity, planning/execution snapshot, due and lease columns, approved plan reference/hash | Unique tenant/version and workflow ID; unique tenant/admission key; tenant/created and partial due-state/next-run/lease indexes |
| `audit_records` | Tenant, actor or system principal, command correlation, entity IDs, event type, old/new revision and safe event details, UTC time | Tenant/cycle/time/id and tenant/entity/time indexes; append-only application grants |
| `reminder_jobs` | Version, level, recipient membership, due deadline, state, attempts, lease token/expiry, delivered/failed timestamps | Unique tenant/version/level; partial pending/retry due index |

Use composite relationships that also preserve ancestry: plan-to-run/version, decision-to-version-step, item-to-current-version, and item/version-to-cycle cannot cross-link objects in the same tenant but different items. The current-version FK may be deferred until commit to insert an item and its first version together. SQL checks enforce statuses, nonnegative counters, positive level/duration, and hash shape. SQL constraints do not prove contiguous levels, actor roles, no-self-approval across joins, frozen activation configuration, or legal transitions. The command module enforces these under row locks, with adversarial real-PostgreSQL tests. Immutable-row update/delete guards and restricted runtime grants protect versions, plans, version steps, decisions, and audit records. Migration credentials are separate from runtime credentials. Sessions and reminder delivery state remain mutable.

Admission idempotency for new item/version commands uses `command_receipts`, keyed by tenant/actor/command kind/idempotency key, storing canonical payload hash and resulting IDs. Insert receipt, item/version/run, and audit in one transaction. Exact replay returns the existing result; changed payload returns 409. Keep execution's existing tenant/admission-key uniqueness using a server-generated version admission identity. Do not expose arbitrary run creation as a new public command.

### Migration sequence

- `alembic/versions/0001_relational_execution.py`: execution rows, constraints, due indexes, and audit records sufficient for transactional worker changes.
- `0002_identity_sessions.py`: tenants, users, memberships, sessions, login throttling persistence. Backfill no users from API keys.
- `0003_review_model.py`: cycles, requester membership, items, versions, definitions, version steps, immutable plans, progress, decisions, command receipts; add composite review links to execution rows. Historical Phase 1 test-only standalone rows cannot become production-authorized review work.
- `0004_reminder_jobs.py`: durable reminder jobs and delivery indexes.

Tenant references and runtime grants must exist before production data is admitted. Phase 1 execution/audit tenant columns are non-null identifiers; Phase 2 adds tenant FKs after explicit validation, without guessing tenants from historical MongoDB. No destructive production downgrade or automatic backfill. Each migration must be tested on an empty PostgreSQL database and by upgrading the previous phase's schema with representative retained records. Stop processes for this small release's schema upgrades. No zero-downtime migration claim.

## 5. Identity, roles, and browser security

An operator runs an explicit local CLI bootstrap command to create the first tenant administrator, entering a password through a hidden prompt. No default password, environment dump, public signup, or HTTP role-assignment shortcut. An authenticated tenant ADMIN can create tenant-local requester/reviewer accounts, disable accounts, and perform an explicit password reset through a protected admin command. Creating another ADMIN or changing an existing role is operator-CLI only in this release and audited. No password recovery email flow.

Login takes login name, tenant slug, and password; the server resolves an enabled membership. The client never supplies an authoritative tenant UUID or role. A session selects exactly one membership; switching tenants requires a new authenticated session. Multiple tenant memberships are supported without sharing privileges. Check current user/membership enabled status and role on every command, not just a cached session claim.

| Command | ADMIN | REQUESTER | REVIEWER |
| --- | --- | --- | --- |
| Manage tenant-local non-admin accounts | Yes | No | No |
| Create/edit draft cycle, admit requesters, configure item chains, activate | Yes | No | No |
| Create item or submit/resubmit content | No proxy submission | Own item in admitted cycle | No |
| Read cycle and items | All in tenant | Participating cycle metadata and own items | Cycle metadata and items assigned to them |
| Decide current step | No override | Never | Only exact assigned current step |
| Export cycle/audit | Yes | No | No |
| Cancel authorized execution | Yes, existing safety guards | No | No |
| Read execution results | All in tenant | Own item | Assigned item |

ADMIN is not a super-reviewer and cannot approve on another user's behalf. A requester can create items only in an admitted DRAFT cycle. The item's owner cannot change. There is no proxy submitter, delegation, reassignment after activation, or impersonation. Freeze membership roles/removal while referenced by an active cycle. Emergency account disable and session revocation are still allowed; a disabled assignee blocks progress. Recovery requires re-enabling the same identity or a future explicitly designed reassignment feature. Do not bypass the chain to unstick a demo.

Use an Argon2id password library with parameters measured on the supported deployment. Hash passwords off the event loop in a bounded worker pool. Minimum 12 characters, bounded input length, generic login failures, constant-work unknown-user checks, and a PostgreSQL-backed per-account/per-source login throttle. Password resets revoke all user sessions and require changing the temporary password before other commands.

Generate at least 256 bits of session-token entropy. Store only its hash in PostgreSQL; cookie contains the opaque token, not user/tenant/role data. Rotate on login and password reset. Proposed timeouts are 30 minutes idle and 12 hours absolute. Logout revokes the server row and expires the cookie. Expiry/revocation works across API replicas and process restart; Redis failure must not log everyone out or grant access.

Production-mode cookie is host-only `__Host-workflow_session`, `Secure`, `HttpOnly`, `SameSite=Lax`, path `/`, no Domain. Reject production startup without configured HTTPS origin. Explicit `WORKFLOW_ENV=local` plus loopback-only origin permits a differently named non-Secure development cookie on HTTP. Never infer development mode from missing secrets. No permissive CORS.

Use synchronizer CSRF tokens tied to server-side session state, validated on every unsafe browser command, including logout. Login uses a short-lived anonymous server-side pre-session and CSRF token, rotates both after successful authentication, and validates configured Origin. Reject cross-site Origin/Referer and missing/mismatched CSRF; SameSite alone is not the defense. Same-origin HTML forms carry hidden tokens; JSON uses a header. GET never mutates business state. Escape request text/comments, use CSP and no arbitrary inline HTML, and do not put raw passwords, tokens, request text, or comments in logs.

## 6. Cycle and review rules

### Draft and activation

ADMIN owns cycle configuration. In DRAFT the admin admits requester memberships, and each requester creates their own items and submits versions. ADMIN assigns each item a sequential chain of 1 to 5 distinct enabled named REVIEWER memberships, with contiguous levels and positive deadline durations. Proposed default is 48 hours per step, maximum 30 days, configurable per definition before activation. Different items may have different chains. No self-approval or repeated reviewer within an item; a reviewer may review different items.

An item can exist without a submitted version while ADMIN configures its chain. First submission requires a complete valid chain. Version submission creates a planning run immediately. Planning is allowed in DRAFT because it has no external effects. A chain edit in DRAFT produces a new version-step snapshot for a newly submitted version; existing immutable steps are not rewritten. If definitions change after a version is submitted, activation rejects it until the requester submits a new version matching the current definition fingerprint. This avoids pretending a requester submitted a different review configuration.

`activate_cycle(actor, cycle_id, expected_revision)` locks the cycle and checks: at least one item, all latest versions have sealed validated plans, all chains match the current definitions, all requester/reviewer memberships remain valid, distinct-reviewer/no-self rules hold, and there is no unfinished planning or `NEEDS_INPUT/PLAN_FAILED`. It freezes item membership, requesters, and definitions, changes DRAFT to ACTIVE, opens each item's level 1, sets deadlines using database UTC time, creates reminder jobs, and audits in one transaction. Cycle configuration and item ownership cannot change afterward. No empty cycle auto-completes.

### Sequential decisions

Only an ACTIVE cycle's current version and current level accept a decision. The caller must be the enabled assigned REVIEWER, not the requester or submitting actor. Request includes expected item/progress revision, immutable version ID, current level, exact plan hash, decision, comment, and command ID. Both item revision and progress revision are returned in projections; commands specify which revision they fence. The decision command fences both when changing the current item or step.

- APPROVE closes that step. If another level exists, open it at database time and start its own deadline. Later reviewers cannot decide early.
- REJECT requires a nonblank comment of at most 2,000 characters. It terminates this item as REJECTED, without execution authorization. No resubmission of a rejected item in this release. A new cycle/item is needed for a new request.
- REQUEST_CHANGES requires the same comment bound. It closes the current step and puts this version in CHANGES_REQUESTED. No review deadline remains. Earlier approvals are historical, not reusable permission.
- In DRAFT, an owner may replace any unapproved current version in PREPARING, NEEDS_INPUT, PLAN_FAILED, or PENDING, including a successfully planned version after a chain edit. Replacement uses the latest valid draft definitions. In ACTIVE, resubmission is allowed only for an owner's current NEEDS_INPUT, PLAN_FAILED, or CHANGES_REQUESTED version and copies the frozen chain. Both operations atomically mark old progress SUPERSEDED, append a new immutable version and step snapshots, create a fresh planning run, and advance the item's revision/current pointer. They cancel a nonterminal old planning/awaiting-approval run through the existing safe cancellation transition, or retain an already-terminal failed run, with no due work remaining. A live planning lease causes 409. Neither path may replace an authorized run, any run with submitted actions, or a terminal rejected item. Review restarts at level 1 once planning succeeds in ACTIVE. No decision or plan is copied as approval.

Identical decision-command replay by the same authorized actor returns its persisted result with no duplicate event or execution. A changed payload for the same command ID, stale current version/step, or conflicting decision returns 409. Replays do not confer access after account disable or outside the tenant. Unique version/level decisions ensure two different command IDs cannot vote twice.

### Completion versus execution

The last decision transaction computes completion while holding the cycle lock. ACTIVE becomes COMPLETED when every item's current version is APPROVED or REJECTED. REQUEST_CHANGES, PREPARING, NEEDS_INPUT, PLAN_FAILED, or pending steps prevent completion. Completion is automatic, audited, and irreversible. Locking the cycle serializes competing last-item decisions and prevents missed completion. Resubmission cannot race past completion because it needs the same cycle lock and valid nonterminal source progress.

A completed cycle may contain approved items whose execution is READY, RUNNING, RETRY_WAIT, FAILED, CANCELLED, or NEEDS_REVIEW. Display review status and execution status separately. Neither execution success nor failure reopens review. A reservation shortage remains a successful conditional SKIPPED result. Cancellation does not revoke or rewrite the historical approval and does not undo effects. No release-one-more-action endpoint and no manual resolution of uncertain outcomes in the first release.

## 7. Planning, final approval, and transaction ownership

### Interfaces

Keep `Planner.propose`, `policy.validate_and_compile`, `EffectGateway.submit/lookup`, `Runner.tick`, and Redis notification payloads. Add focused modules, not a generic repository or workflow framework:

- `review_domain.py`: pure `activate`, `decide`, `resubmit`, `compute_completion` rules on snapshots with explicit time. No ORM, HTTP, or SMTP imports.
- `authorization.py`: `authorize(actor, command, scope)` with tenant, role, ownership, and assigned-step rules shared by JSON and HTML commands.
- `review_commands.py`: `ReviewCommands.create_cycle`, `edit_draft_cycle`, `admit_requester`, `create_item`, `submit_version`, `configure_chain`, `activate_cycle`, and `decide`. Each method owns one SQLAlchemy transaction and its audit/receipt/reminder writes. Private helpers receive the same session and never commit independently.
- `postgres_store.py`: `PostgresWorkflowStore` implements the existing worker interface. Public `save` cannot introduce an approval, even through a lease-free write. `insert_or_get` is internal planning admission and unavailable as an HTTP standalone creation route.
- `execution_bridge.py`: `record_plan(session, leased_transition)` and `authorize_execution(session, version, final_decision)` are internal operations. They do not open another transaction or publish Redis messages. They are the one implementation of review-to-run synchronization, used by planning saves and final decisions.
- `auth.py`: login/session validation/revocation, password reset, and CSRF. HTTP adapters derive `Actor` from this module, never request-body roles.
- `notifications.py`: claim/validate/send/record reminder delivery, separate from review decisions.

No public method accepts a caller-supplied `authorized=True`. Production execution authorization can only be created by the bridge inside `ReviewCommands.decide`, after verifying the complete persisted chain. The store rejects general `DomainEvent(kind='approved')` saves. Tests that exercise the executor in isolation may arrange approved database fixtures, but must also test that neither public routes nor the general store interface can mint permission. Do not ship a test-fixture approval switch in the CLI or settings.

### From version to reviewable plan

`submit_version` persists the source version, frozen catalog snapshot, step snapshots, PREPARING progress, an unapproved PENDING_PLAN run, durable planning deadline/wake sequence, receipt, and audit atomically. The execution run ID is allocated once here and passed as the existing `workflow_id` to compilation, preserving the action-key formula and simulator logical identity. There is exactly one run per version. New versions have new identities, but superseding any version already execution-authorized is prohibited.

Runner planning uses the version's saved tenant catalog snapshot, not whatever catalog file happens to be loaded after restart. It still persists each counted planner attempt before the call. A successful policy check is saved under the current run lease and revision. `record_plan` inserts a sealed `validated_plans` row, stores the existing AWAITING_APPROVAL execution snapshot/actions, and updates progress atomically. In DRAFT progress becomes PENDING with no current level or deadline and waits for activation; in ACTIVE it opens level 1 and schedules that step's reminder. No mutation is due in either case. A sealed plan is never regenerated in place.

`NeedsInput` stores clarification questions and marks progress NEEDS_INPUT, with no action permission or due work. Two exhausted planner attempts mark the run FAILED and progress PLAN_FAILED. These states cannot be approved and do not complete the cycle. The requester must submit a new version. Supersession retains original questions, diagnostics, and the failed planning history. There is no unbounded automatic planning retry.

### Atomic final approval

`ReviewCommands.decide` owns a single PostgreSQL transaction. It:

1. Resolves and locks current actor/membership, then cycle, item, progress, and execution run in the established order. Rechecks identity, current version, expected revisions, ACTIVE lifecycle, assignment, prior approvals, plan hash, and no live run lease.
2. Inserts the immutable final decision. `authorize_execution` checks the complete chain against that exact version and sealed hash, and transitions the existing unapproved AWAITING_APPROVAL run to READY using the existing pure execution rules.
3. Stores `approved_plan_hash`, approved-plan identity, approval timestamp, semantic revision increment, database-time `next_run_at`, and wake sequence together. Marks progress APPROVED, closes the reminder, and recomputes cycle completion.
4. Appends decision, execution-authorization, and any cycle-completion audit records and commits once. There is no Redis or external mutation inside the transaction.

Rollback leaves no decision, authorization, audit, or due work from this command. A crash after commit but before response/publication is recovered through command replay and the PostgreSQL due scanner. A unique run/version constraint prevents a replay from creating another execution. A nonfinal approval cannot set an approved hash, READY state, or mutation deadline.

Worker mutation admission additionally verifies persisted authorization, version/plan linkage, exact matching hash, and approved progress. A matching hash alone on an unrelated or forged snapshot is insufficient. New review columns are immutable once authorized. No UI, API key, worker planning transition, or admin command can skip the chain.

## 8. PostgreSQL concurrency and execution guarantees

Use short READ COMMITTED transactions with explicit locking and conditional updates. Take locks consistently: relevant actor memberships in stable ID order, cycle, item, progress, run, reminder. Worker review-affecting saves use the same order; heartbeats touch only the run and commit immediately. No operation may hold a run lock and then wait on a cycle lock. Establish database time with `clock_timestamp()` after obtaining relevant locks, rather than a transaction-start timestamp that could be stale after waiting.

- Claim is a conditional update of a due, eligible run with no unexpired lease, assigning a fresh token and 30-second deadline. Twenty concurrent claims must have one winner.
- Save locks/reads the current run, validates immutable fields and expected semantic revision, then updates only owned fields with predicates for revision and token/current database-time expiry. A worker save without a current token fails with LeaseLost. A user command requires no current lease. Heartbeats and enqueue markers do not increment semantic revision and cannot be overwritten by a stale snapshot.
- Renew requires the same current unexpired token. Release never clears another owner's token. Do not allow expired tokens to resurrect leases. A process can still complete an already-sent HTTP request after lease expiry; PostgreSQL fencing is not downstream deduplication.
- Due scanning uses a small `FOR UPDATE SKIP LOCKED` batch, database-time eligibility, and atomic last-enqueued marking with a 5-second republish interval. Commit before Redis publication. A dispatcher death on either side of publication must be recoverable with duplicates permitted.
- Preserve planning/external timeouts, disabled transport retries, two planner calls, five mutation submissions, five lookups per ambiguous episode, 1/2/4/8-second retry delays, one mutation per tick, and ticket-before-reservation confirmation.
- Persist IN_FLIGHT intent and counted submission before external HTTP. Crash recovery uses the original key. A NOT_FOUND lookup is not proof of no late commit. Only advertised durable same-key replay allows resubmission. Preserve uncertainty flags through retries and cancellation checks. Confirmed/skipped results never change.
- Keep the existing 256 KiB execution snapshot budget, bounded diagnostics, and 256-entry operational history. Audit is separate durable history and is not truncated to 256 entries.

SQLAlchemy async sessions are not shared between heartbeat and tick tasks. Each operation uses its own short session/transaction from a shared engine factory. Worker commands close HTTP clients, Redis, and engine resources on shutdown. Startup verifies migration head and dependency access; it does not call Alembic automatically. PostgreSQL unavailability returns 503; Redis loss remains visible degraded readiness with durable accepted work intact. No end-to-end exactly-once claim.

## 9. HTTP compatibility and safe intermediate behavior

The relational mode is an intentional authentication and review-command contract change, not a transparent backend swap.

| Existing route | Relational-release contract |
| --- | --- |
| `GET /health/live` | Retain unauthenticated liveness |
| `GET /health/ready` | Retain 503 for authority failure and 200 degraded for Redis failure; replace `mongo` field with `postgres`, require current migration head |
| `POST /workflows` | Return 410 with replacement guidance; create versions through item commands, never standalone executable work |
| `GET /workflows/{id}` | Retain execution-state/revision/plan/actions projection and hide lease credentials, but require session and item-aware tenant/ownership/assignment visibility |
| `POST /workflows/{id}/approve` | Always 410; no API-key or ADMIN compatibility bypass |
| `POST /workflows/{id}/cancel` | Session + CSRF, ADMIN-only; retain expected execution revision, idempotent replay, lease/uncertainty guards, and confirmed partial results |

Legacy API keys have no authority in the relational application. Key-only protected requests return 401, even if an old key remains in an untouched `.env`. Never silently fall back from a failed session to an API key. Retired routes disclose no item state and may return unconditional 410. Do not alter the old deployment to pretend its API key has become a named identity.

New JSON routes live under `/api`: session/CSRF login/logout/me; admin accounts; cycle create/detail/configure/activate/export; item create/detail; version submit; and version decision. Route handlers validate schemas, derive Actor, call commands, and translate errors. Shared command logic is also called by HTML form handlers. Reject unknown tenant/role/URL fields. Preserve 16 KiB body limits, bounded request text/comments, 401 unauthenticated, 403 visible-but-forbidden commands, 404 for foreign or inaccessible objects, 409 conflicts, and 422 schema validation.

Before assignment-aware decisions exist, the relational deployment is fail-closed: direct approval retired, no command can grant execution authorization, and fixture-planned requests only wait for review. Phase 1's runtime defaults to this restriction. No temporary key-based approval or admin escape hatch. The old demo's `--approve` and benchmark approval helpers must be retired or adapted through real named reviewers when the new command becomes available, not secretly reused. Until then, isolated executor fixtures can test safety without exposing production authorization.

## 10. Audit, deadlines, reminders, exports, and demo

Every accepted business command and semantic worker transition inserts its audit record in the SAME PostgreSQL transaction as the state change. Audit failure rolls back the change. Store event type, actor membership or system process identity, affected cycle/item/version/run, revision, command ID, and a safe bounded summary. Decisions/comments remain in their immutable source records; normal logs do not copy them. No secrets or raw provider output in audit. Identical command replays create no extra business event. Rejected authentication/authorization attempts use redacted security logging, not a business event claiming a change committed.

When a step opens, set `opened_at` and `deadline_at = database_now + configured_duration`. Store UTC instants, display timezone explicitly, and reject naive timestamps. Deadlines belong to the currently open step, not an absolute activation date for all future levels. Approving or requesting changes closes its deadline and cancels any undelivered reminder. New versions restart deadlines at level 1. Being overdue does not auto-approve, auto-reject, or unlock another reviewer.

Create exactly one durable reminder job for each version/step as that step opens, unique even under dispatcher overlap. It becomes eligible at deadline, not repeatedly on every scan. A separate notification worker claims jobs with PostgreSQL leases, rereads current version/step/recipient eligibility immediately before sending, sends outside a database transaction, and records success/failure under its token. Superseded/closed steps cancel unsent jobs; a job already sent cannot be recalled. A decision can race the final send check, so a late email is possible and its link must always reread current permissions/state.

Use bounded delivery attempts, proposed five with 1/2/4/8-second waits, a stable Message-ID, an SMTP timeout, and no hidden transport retries. A crash after SMTP accepts mail but before recording success can duplicate delivery. One job is not exactly one email. SMTP acceptance is not proof of inbox delivery. Exhausted failures remain visible to ADMIN and do not affect approval or execution. Local demo uses an explicit mail-capture service; real SMTP requires configured TLS/auth secrets. No email-based decision links, tracking pixels, or attachments containing plan secrets.

ADMIN-only cycle export is a synchronous, bounded CSV response from a read-only repeatable-read snapshot. Proposed cap is 10,000 item/version rows with an explicit error rather than truncation. Include cycle/review status, version identities, plan hashes, step assignments, decisions/comments, UTC deadlines, audit IDs/times, execution outcomes, and confirmed partial effects. Use separate CSV files in a ZIP if one flat table would duplicate/confuse audit and action rows. Final approval does not imply effect success. Protect CSV cells against spreadsheet formula injection, exclude credentials/session data/raw model output, and send no-store/content-disposition headers. Record an export-request audit event before streaming; it means the export was requested, not that the client received every byte. No unauthenticated download URL or scheduled public export.

`workflow-engine seed-demo` is explicit and local-only. It creates two isolated tenants, one ADMIN, one REQUESTER, and at least two REVIEWER identities per tenant, a draft cycle, safe fixture requests, and named chains. Generate passwords or prompt for them; show one-time credentials only to the operator, not logs or committed fixtures. Stable seed IDs make reruns no-ops for existing objects. Do not reset passwords, stock, ledgers, decisions, or active cycles on rerun. Stop on conflicting existing records. Demo login uses real sessions and CSRF. Walk through one changes-request/resubmission, sequential approvals, separate review/execution outcomes, and cross-tenant denial. The fixture planner remains the default and no model download is required.

## 11. Staged layout and dependency order

Keep `domain.py`, `runner.py`, `planner.py`, `policy.py`, `gateways.py`, and `broker.py` focused on execution. Retain `store.py` as the protocol/fake seam; isolate MongoDB implementation as legacy only when needed for historical tests. Do not maintain two production authority modes behind an implicit default. Proposed new files:

```text
alembic.ini
alembic/env.py
alembic/versions/0001_relational_execution.py
alembic/versions/0002_identity_sessions.py
alembic/versions/0003_review_model.py
alembic/versions/0004_reminder_jobs.py
src/workflow_engine/db.py
src/workflow_engine/db_models.py
src/workflow_engine/postgres_store.py
src/workflow_engine/auth.py
src/workflow_engine/authorization.py
src/workflow_engine/review_domain.py
src/workflow_engine/review_commands.py
src/workflow_engine/execution_bridge.py
src/workflow_engine/review_api.py
src/workflow_engine/notifications.py
src/workflow_engine/exports.py
src/workflow_engine/web.py
src/workflow_engine/templates/{base,login,cycles,cycle,item,admin}.html
src/workflow_engine/static/app.css
tests/unit/test_{review_domain,authorization,auth,notifications,exports}.py
tests/integration/test_{postgres_store,migrations,auth,review_commands,approval_execution,notifications,exports}.py
tests/e2e/test_review_browser.py
```

No frontend dependency install during design. Server-rendered FastAPI/Jinja templates are the proposed narrow UI implementation. Use accessible semantic forms, visible conflict/errors, keyboard operation, CSRF fields, escaped text, and separate review/execution labels. No client-side authorization or duplicated transition engine.

The following phase numbers are proposed handoff boundaries, not permission to execute them now. Each phase includes tests, documentation, and CI changes relevant to its new behavior.

### Phase 1: PostgreSQL execution parity, fail-closed authorization

Files: `pyproject.toml`, `uv.lock`, `alembic.ini`, `alembic/env.py`, migration `0001`, `db.py`, `db_models.py`, `postgres_store.py`, `store.py`, `runtime.py`, `settings.py`, `api.py`, `broker.py`, `cli.py`, `compose.yaml`, `.env.example`, `tests/conftest.py`, existing store/broker/recovery/fault/process/e2e tests, `tests/integration/test_{postgres_store,migrations}.py`, `.github/workflows/ci.yml`, `README.md`, `docs/{architecture,consistency,runbook}.md`.

Start with failing real-PostgreSQL tests for concurrent admission/claim, stale/expired fencing, revision races, immutable keys/results, preserved heartbeat/enqueue fields, due-intent recovery, audit rollback, and forbidden general approval saves. Port process-kill checkpoints and SQL test-time expiry without weakening assertions. Verify all 20 existing fault scenarios over seeds 0 through 9 with PostgreSQL, Redis, and HTTP simulator subprocesses. Pure executor fixtures may arrange approved runs; clearly separate that from production approval tests. Core acceptance is PostgreSQL parity plus denial of key/direct approval, not a runnable review product. CI must run the real fault matrix rather than hide it behind an optional benchmark.

### Phase 2: Tenant identity and secure sessions

Depends on Phase 1. Files: migration `0002`, `auth.py`, `authorization.py`, `db_models.py`, `api.py`, `runtime.py`, `settings.py`, `cli.py`, `tests/unit/test_{auth,authorization}.py`, `tests/integration/test_auth.py`, `web.py`, `templates/{base,login,admin}.html`, `tests/e2e/test_review_browser.py`, dependency locks, Compose/env/docs/CI.

Start with failing password/session/CSRF tests: no public signup, no key fallback, tenant switching denial, role isolation, session fixation rotation, logout/reset revocation, restart durability, idle/absolute expiry, throttling, cross-origin login/logout denial, secure versus explicit-local cookie settings. Add browser tests for login, logout, protected admin account provisioning, and keyboard/form error handling in this phase. Protected execution writes remain disabled because there are no review commands yet. Core acceptance uses PostgreSQL-backed sessions and real browser forms, not just cookie inspection in a fake.

### Phase 3: Draft cycles, immutable versions, planning integration

Depends on Phase 2. Files: migration `0003`, `review_domain.py`, `review_commands.py`, `execution_bridge.py`, `review_api.py`, `db_models.py`, `authorization.py`, `postgres_store.py`, `runner.py`, `policy.py`, `runtime.py`, `web.py`, `templates/{cycles,cycle,item}.html`, `tests/unit/test_review_domain.py`, `tests/integration/test_{review_commands,migrations}.py`, browser tests, docs/CI.

Start with pure no-self/repeated-reviewer/contiguous-level/freeze and ownership tests, then PostgreSQL composite-FK cross-tenant/cross-item attacks, immutable-row tests, idempotent concurrent version creation, and version/run/audit rollback. Integrate actual fixture planning and immutable catalog snapshots. Test NEEDS_INPUT, exhausted planning, successful draft-version replacement after chain edits, supersession with lease conflicts, and activation guards. UI supports draft configuration, owner submission, planning status, and activation. ACTIVE review can wait safely, but no decisions or execution authorization exist yet. Add opened-step reminders transactionally in Phase 5; before then store/show deadlines but do not write nonexistent reminder tables or promise email. Core acceptance proves no effects from activation or planning.

### Phase 4: Assigned decisions and atomic execution authorization

Depends on Phase 3. Files: `review_domain.py`, `review_commands.py`, `execution_bridge.py`, `authorization.py`, `postgres_store.py`, `review_api.py`, `web.py`, templates, `tests/unit/test_review_domain.py`, `tests/integration/test_approval_execution.py`, existing API/recovery/fault tests, `tests/support.py`, `tests/fault_worker.py`, `scripts/{demo,benchmark_faults,benchmark_load}.py`, browser tests, docs/CI.

Start with failing current-assignee/level/hash/revision tests; reject/changes comments; terminal rejection; resubmission restarting every level; final-approval rollback; concurrent final decisions; decision-versus-resubmission/cancellation; and last-item cycle completion races. Test that forged worker saves, old API keys, direct approval, requester, admin, and later reviewers cannot authorize. Crash after decision commit before response/publication and recover through PostgreSQL due intent plus Redis. Verify one run, original action keys, one effect per logical action, and preserved partial outcomes. Adapt the demo and fault matrix through named reviewer commands and rerun safety acceptance. Browser tests use distinct real sessions for requesters/reviewers/admin, including stale tabs and resubmission. Core acceptance is end-to-end exact-plan review-to-execution with real PostgreSQL/Redis and process recovery.

### Phase 5: Reminders, exports, and repeatable demo delivery

Depends on Phase 4. Files: migration `0004`, `notifications.py`, `exports.py`, `review_commands.py`, `execution_bridge.py`, `review_api.py`, `cli.py`, `settings.py`, `runtime.py`, templates, `tests/unit/test_{notifications,exports}.py`, `tests/integration/test_{notifications,exports,migrations}.py`, browser tests, Compose/env/dependency locks/docs/CI.

The reminder migration inserts at most one job for each currently open step, including already-overdue steps. Use an idempotent INSERT SELECT with the unique version/level constraint, not a new recurring schedule. Cancel unsent jobs transactionally on step change. Failing tests cover concurrent job creation/claim, deadlines/timezones, superseded-step suppression, crash-after-SMTP acceptance, bounded failure, ADMIN-only repeatable-read export, no hidden truncation, CSV injection, and cross-tenant export denial. Use real PostgreSQL concurrency and a local SMTP capture service. Seed-demo rerun tests must prove no overwrites or simulator resets. Browser tests cover overdue display, separate execution outcomes, and exports.

Core acceptance is a fresh isolated Compose demo, seeded accounts, sequential review/resubmission, execution, reminders, exports, two workers, restart recovery, and rerunning without user-data loss. This is the first complete relational-review release. Earlier phases are explicitly partial.

### Verification commands for implementation phases

Commands below are targets, not results. Use `uv run --no-sync` after the phase's explicitly approved dependency setup. Never install implicitly to run a design check.

```sh
uv run --no-sync pytest tests/unit -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync alembic upgrade head
uv run --no-sync alembic check
uv run --no-sync pytest tests/integration/test_migrations.py tests/integration/test_postgres_store.py -q
uv run --no-sync pytest tests/integration tests/e2e/test_workflow.py -q
uv run --no-sync pytest tests/integration/test_fault_matrix.py -q
uv run --no-sync pytest tests/e2e/test_review_browser.py -q
uv run --no-sync pytest tests/e2e/test_compose.py -m compose -q
docker compose config --quiet
```

Set `TEST_POSTGRES_URL` and `TEST_REDIS_URL` to isolated services, `FAULT_SEEDS=0,1,2,3,4,5,6,7,8,9` for the 200-case gate, and `RUN_COMPOSE=1` only for an explicitly isolated deployment check. Run Alembic against that test database, never a default user database. Browser dependencies and a local SMTP capture service become explicit CI requirements when introduced. Retain historical MongoDB tests separately if useful, but do not count them as PostgreSQL evidence or leave stale MongoDB fixtures gating the new integration suite.

Tests allocate a uniquely named PostgreSQL database per test group with validated test-only prefix and ownership token; migrations and multiple processes share that group's isolated database. Cleanup drops only databases the harness created. Runtime uses a non-owner role; immutable-write/grant tests use that role. A supplied-but-broken service URL fails, and a required CI service check may not skip. Read optional URL presence without printing credentials. Random Redis streams and temporary SQLite files remain test-owned. Do not use SQLite or the memory fake to claim PostgreSQL correctness.

Every phase handoff reports changed paths, exact observed commands/results, red-to-green evidence, skips, contract changes, remaining limitations, and whether its core acceptance ran. If core acceptance was skipped, that phase is incomplete. Stop at the requested boundary. Do not push, merge, create a PR, move worktrees, or reset data without permission.

## 12. Non-goals and approval record

First release excludes arbitrary workflows/DAGs, quorum or parallel approval, delegation, active-cycle reassignment, policy scripting, attachments, bulk imports, live MongoDB migration, automatic compensation, production SSO/MFA/password recovery, cross-tenant admin UI, configurable catalog editing, manual uncertainty resolution, scheduled exports, recurring reminder escalation, paid-model dependencies, and machinery control. PostgreSQL is still a local durable deployment, not proof of replicated disaster recovery. Historical performance/model reports do not establish relational-release performance or correctness; rerun and label new evidence before making claims.

Material choices presented for approval and accepted by the user's 2026-11-11 response:

1. PostgreSQL as the single review AND execution authority, fresh relational state, and an explicit operator-managed historical MongoDB cutover rather than live import.
2. Local password/session identity, one role per tenant membership, operator-only administrator/role provisioning, and no privileged approval override.
3. Frozen ACTIVE configuration, terminal rejection, new-version/full-chain restart for changes, and review-only automatic cycle completion.
4. Planning run created at version submission, immutable sealed plan/catalog, final approval as the sole atomic execution authorization command, and no post-approval edits.
5. Intentional API-key/direct-approval/standalone-admission retirement, including temporary fail-closed behavior and required demo/client updates.
6. Server-rendered first-release browser UI and the proposed five implementation phases, including reminders/exports/demo before declaring the release complete.

The proposed defaults resolved the technical design without assuming agreement at drafting time. No blocking technical choice is intentionally deferred to a later agent. The user has now accepted the design. Prepare and present the phase-specific implementation plan before product changes; do not treat design approval as permission to execute the entire roadmap.

Builder-check record: a fresh reader identified one material gap, replacing a successfully planned draft version after a chain edit. The proposal now explicitly permits that replacement, requires a valid chain before first submission, and defines draft plan-ready progress. This is an author-proposed rule, not new user approval. The reader rechecked the revision and reported no remaining blocking buildability questions.
