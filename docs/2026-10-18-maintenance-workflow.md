# Failure-Safe Maintenance Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. If those skills are unavailable, preserve the same task order, test-first checks, and review gates without pretending to have invoked them.

**Goal:** Build an async LLM-backed maintenance workflow that proposes a typed plan, waits for approval, and executes ticket creation and conditional part reservation reliably across duplicate messages, timeouts, and worker restarts.

**Architecture:** MongoDB stores the authoritative workflow aggregate, action intents, scheduling intent, leases, and bounded history using single-document atomic updates. Redis Streams carries replaceable wakeups; a dispatcher reconstructs work from MongoDB after message loss. The LLM proposes plans but never invokes mutation tools; a deterministic runner executes allowlisted actions through idempotent external adapters and reconciles ambiguous outcomes.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, async PyMongo `AsyncMongoClient`, redis-py asyncio, HTTPX, an OpenAI-compatible model adapter, SQLite-backed external simulators, pytest/pytest-asyncio, Docker Compose, structlog or standard JSON logging, and uv. Do not use Motor for a new implementation.

**Spec:** [Embedded project specification](#project-specification). This file contains both design and execution plan so it can be handed to an agent alone.

## Global constraints

- Create a separate repository named `maintenance-workflow-engine`; every code path below is relative to its root. The project must run without the speech project.
- Python 3.12; Pydantic models reject extra fields; all stored datetimes are UTC. Lock exact Python dependency versions and container image digests during setup.
- One workflow supports one asset, one ticket, and at most one conditional reservation, in that order. No arbitrary DAG, generated code, shell commands, or model-selected URLs.
- MongoDB is authoritative. Redis is never the only copy of a request, action intent, retry deadline, or approval.
- All state transitions use revision checks. Worker-owned transitions also require a current lease token. Lease duration is 30 seconds; heartbeat interval is 5 seconds.
- The dispatcher polls every 1 second and republishes due, unleased work at most once per 5 seconds per workflow. External request timeout is 10 seconds; planner timeout is 15 seconds.
- Maximum external mutation submissions are 5 per action, with retry delays of 1, 2, 4, and 8 seconds. Maximum reconciliation lookups are 5 per ambiguous episode. Maximum model calls are 2 per workflow. Disable hidden client/SDK retries.
- Persist a stable action idempotency key before any mutation call. Automatic replay of an ambiguous mutation is allowed only when the downstream contract guarantees same-key deduplication.
- No external mutation before explicit approval of the exact validated plan hash. Authentication supplies tenant identity; the model and request body cannot choose a tenant.
- Default tests and fault/load benchmarks use a deterministic fixture planner and local external simulators. A separately recorded real-model evaluation is required before calling the full project complete.
- Development ports bind to `127.0.0.1`; secrets come from environment variables and never enter Git. This is a local portfolio deployment, not a certified production or safety-critical control system.

## Review focus

1. A downstream mutation may commit before a worker loses its response. Tasks 4, 5, and 9 must reconcile by the persisted idempotency key rather than issuing a new key.
2. A crash may occur between a MongoDB commit and Redis publication. Tasks 3 and 6 must recover durable due work, including after Redis is emptied.
3. A stale worker may resume after another worker acquires its lease. Tasks 3, 7, and 9 must reject stale writes and prevent duplicated effects through the downstream contract.
4. A valid-looking model plan may reference another tenant's asset or an unrequested action. Tasks 2, 8, and 11 must enforce deterministic authorization and exact approval independently of the prompt.
5. Cancellation or approval may race with execution. Tasks 7 and 8 must implement the documented revision/lease rules and never imply that cancellation rolls back completed effects.

---

## Project specification

### Intent and JD fit

The FDE JD explicitly requires Python backend frameworks, REST/JSON, asynchronous programming, API integrations, LLM prompting, MongoDB, Redis, reliability, scalability, testing, and documentation. This project provides demonstrable evidence for each without becoming a generic customer-support chatbot.

The adjacent application is maintenance administration. It creates simulated inspection tickets and reserves simulated parts. It does not actuate machinery, certify repairs, schedule real shutdowns, or make safety decisions.

### Canonical domain terms

Copy this glossary into `CONTEXT.md` when the implementation repository is initialized. Keep implementation choices in the runbook rather than the glossary.

| Term | Meaning |
| --- | --- |
| Request | A user's description of a maintenance-administration task. |
| Proposal | Model output that is either a candidate plan or a request for clarification. |
| Validated plan | A proposal checked against the request, tenant catalog, and permitted workflow. |
| Approval | Permission to execute one exact validated plan, identified by its hash. |
| Workflow | The durable progress record for one admitted request and its plan. |
| Action | One permitted external mutation: create a ticket or reserve a part. |
| Attempt | One recorded submission of an action to an external system. |
| Confirmed effect | An external mutation whose outcome has been verified. |
| Ambiguous outcome | A call may have applied an effect, but the runner cannot yet establish the outcome. |
| Reconciliation | Looking up an existing action's outcome without creating a new logical action. |
| Wakeup | A notification that a workflow may have work due; it is not the workflow state. |
| Cancellation | Preventing remaining work where permitted; it does not undo confirmed effects. |

### Scope and non-goals

Example request: "Create an inspection ticket for pump P-17 and reserve one seal kit if available."

The fixed workflow has at most two actions:

1. `create_ticket(asset_id, priority, description)`.
2. Optional `reserve_part(part_id, quantity, ticket_id)` after confirmed ticket creation. If stock is unavailable, record `SKIPPED` and the reason; this satisfies the explicitly conditional request.

No automatic compensation is attempted. If ticket creation succeeds and reservation fails permanently, the record retains that partial outcome. Never report the workflow as if nothing happened.

Not MVP: payments, public multi-tenant SaaS, a browser UI, RAG, speech input, generalized scheduling, arbitrary tool libraries, tool-discovery frameworks, a general DAG engine, Kubernetes, event sourcing, distributed transactions across MongoDB/Redis/external systems, or an end-to-end exactly-once claim.

### Agent-selected defaults

- Default local containers are MongoDB 7.0 and Redis 7.2, with exact tested patch versions/digests recorded during setup. Use supported client versions compatible with Python 3.12; PyMongo must expose `AsyncMongoClient`.
- Use a single MongoDB workflow document for each workflow. A replica set and multi-document transactions are not needed for this deliberately bounded aggregate. Do not introduce a separate outbox collection and then forget atomicity.
- Redis Streams uses stream `workflow-wakeups`, consumer group `workflow-workers`, and at-least-once consumption. The default deployment has two worker processes.
- The default real-model adapter is an OpenAI-compatible endpoint, with a local `Qwen/Qwen2.5-7B-Instruct` deployment as one concrete option. Freeze actual model revision, provider settings, prompt hash, and decoding settings at evaluation time. The user has not committed to paid inference.
- Two FastAPI external simulators use separate SQLite files/volumes and transactional idempotency ledgers. Their behavior is intentionally under our control for repeatable failure tests; do not claim they are real enterprise integrations.

The only possible pre-build human decisions are target repository and resource limits. Missing paid credentials must not block fixture-based development. Switching database, language, workflow scope, approval policy, or effect semantics is not a minor implementation choice.

### Request and plan contracts

All public IDs are opaque strings. Workflow IDs are server-generated UUIDs. Tenant IDs come only from a configured API-key map. Seed two tenants, each with separate assets and parts, for isolation tests.

```text
WorkflowCreate(text: str)                       # 1..2000 characters; no extra fields
ApprovalRequest(expected_revision: int, plan_hash: str)
CancelRequest(expected_revision: int)

TicketSpec(priority: Literal['low','normal','high'], description: str)
ReservationSpec(part_id: str, quantity: int)     # quantity 1..10
ExecutablePlan(asset_id: str, ticket: TicketSpec,
               reservation: ReservationSpec | None)
NeedsInput(questions: list[str])                # 1..3 questions, each <=200 chars
PlanProposal = ExecutablePlan | NeedsInput      # explicit 'kind' discriminator
```

The planner receives request text plus an authorized, read-only catalog snapshot. It cannot read another tenant's catalog, execute a tool, select a network address, or change the action order. Default priority is `normal`; default quantity is 1 only when the request asks for a singular kit and the catalog resolves exactly one compatible part. An unknown/ambiguous asset, uncertain quantity, or incompatible/ambiguous part becomes `NEEDS_INPUT`, not a guessed mutation.

The catalog provides known asset IDs, authorized aliases, compatible part IDs, and the allowed actions for each tenant. A deterministic request-intent extractor checks explicit asset/part references and whether reservation was actually requested; it need not understand unrestricted natural language. Limit the supported input grammar to maintenance ticket creation with optional reservation, publish examples, and route unsupported/ambiguous requests to clarification. LLM semantic checks are not an authorization control.

Compile a valid plan into stable action IDs `ticket` and optional `reservation`; the reservation's ticket ID comes from the confirmed first action, not a model-generated arbitrary reference. Compute `plan_hash = SHA256(canonical JSON of schema_version + catalog_version + validated plan)`. Canonical JSON uses sorted keys, UTF-8, and compact separators. Preserve the exact hash and validated plan; do not re-prompt or rewrite them after approval.

### Workflow states and transitions

| State | Meaning and allowed progression |
| --- | --- |
| `PENDING_PLAN` | Due planning work. Goes to `AWAITING_APPROVAL`, `NEEDS_INPUT`, `RETRY_WAIT`, or `FAILED`. |
| `NEEDS_INPUT` | No external effects; no automatic work. User may submit a clarified NEW request in the MVP. |
| `AWAITING_APPROVAL` | Immutable validated plan exists; approval can make it `READY`. No automatic execution. |
| `READY` | Approved plan has a next action due. Goes to `RUNNING`. |
| `RUNNING` | One action has durable in-flight intent. Goes to `READY`, `SUCCEEDED`, `RETRY_WAIT`, `RECONCILING`, or `FAILED`. Recovery treats an abandoned in-flight call as ambiguous. |
| `RETRY_WAIT` | Known safe retry is scheduled. Carries phase `planning` or `action`; may become `PENDING_PLAN` or `READY` when due. |
| `RECONCILING` | Outcome lookup is due for an in-flight/unknown action. Goes to `READY`, `SUCCEEDED`, `RETRY_WAIT`, `FAILED`, or `NEEDS_REVIEW`. |
| `SUCCEEDED` | Required ticket confirmed and reservation either confirmed or conditionally skipped. No work due. |
| `FAILED` | A required step definitively failed or its allowed safe attempts were exhausted. Confirmed earlier effects remain visible. No automatic work. |
| `CANCELLED` | No further actions will start. Earlier confirmed effects remain visible. No automatic work. |
| `NEEDS_REVIEW` | Automation cannot safely establish an effect outcome. No automatic mutation or retry. |

Action status is one of `PENDING`, `IN_FLIGHT`, `CONFIRMED`, `SKIPPED`, `FAILED`, `UNKNOWN`. Confirmed action results and stable idempotency keys are immutable. Do not put raw model reasoning in the domain model or logs.

Cancellation is allowed only in `PENDING_PLAN`, `NEEDS_INPUT`, `AWAITING_APPROVAL`, `READY`, or `RETRY_WAIT`, with no current lease and no in-flight/unknown action. `RUNNING`/`RECONCILING` and active-lease cancellation return 409. This deliberately avoids pretending in-flight HTTP effects can be cancelled. A cancelled workflow can still show a previously confirmed ticket.

Approval checks tenant, revision, and exact plan hash atomically. First approval returns 202 and creates due work; repeating approval of the already-approved same hash returns 200 without a second execution, even if the caller has the earlier revision. A different hash or stale unapproved revision returns 409. Approval must never mutate the plan.

### Authoritative workflow document

```text
Workflow(
  workflow_id, tenant_id, admission_key, request_hash, request_text,
  schema_version=1, revision, state, created_at, updated_at,
  plan, plan_hash, catalog_version, prompt_version, model_metadata,
  approved_plan_hash, approved_at,
  actions: list[Action], planning_attempts,
  retry_phase, next_run_at, wake_seq, last_enqueued_at,
  lease_owner, lease_token, lease_until,
  history: list[HistoryEntry]
)
```

Each action includes `action_id`, validated arguments, immutable `idempotency_key`, status, submission count, reconciliation count, next retry time, confirmed external ID/result, and last error category. Derive the key once from tenant/workflow/plan-hash/action-ID and persist it when compiling the plan. No PII or credentials belong in the key.

Cap history at 256 bounded entries and individual diagnostic strings at 2,000 characters. Do not record every lease renewal or queue duplicate in the aggregate. Keep model/raw-provider diagnostics separately in local redacted evaluation artifacts. Enforce a serialized workflow document budget of 256 KiB. This is bounded workflow state with history, not an event-sourced system.

Required Mongo indexes:

- Unique `(tenant_id, admission_key)` for request idempotency.
- Unique `workflow_id`.
- Index `(state, next_run_at, lease_until)` for due work.
- Index `(tenant_id, created_at)` for operational lookup.

An HTTP admission with the same tenant/key/payload returns the existing workflow. The same tenant/key with a different validated payload returns 409. Hash canonical validated JSON without semantic paraphrasing. Across tenants, the same key is independent. A client disconnect after admission does not undo the persisted request.

MongoDB's document update is the atomic unit. Semantic `revision` increments on state/plan/action/approval/cancellation changes; heartbeat and enqueue timestamps do not increment it. Leases and retry eligibility use MongoDB server time, not assumed synchronized application clocks. Save a worker transition only if workflow/revision/current unexpired lease token all match; a lease-free user command must also match the no-active-lease predicate. Preserve immutable admission/plan/action fields and scheduling metadata when updating, rather than replacing a stale whole document.

### Scheduling without a cross-store transaction

A durable transition that makes work due atomically writes `next_run_at` and increments `wake_seq` in the same workflow document. Waiting-for-user and terminal states have `next_run_at=null`.

The dispatcher finds due documents with no live lease. It atomically marks `last_enqueued_at` using server time, returns wakeups, and publishes them to Redis. If it dies before publication, the same document becomes publishable after 5 seconds. If Redis accepted a message before the dispatcher lost its connection, publication may duplicate. Both cases are expected.

Each wakeup includes workflow ID and observed `wake_seq`, not trusted action arguments. A worker rereads authoritative state and atomically claims a 30-second lease only if the workflow is currently due and unleased/expired. A stale sequence is a harmless wakeup; state eligibility, not the message payload, decides whether work runs. Renew at 5-second intervals. A stale token cannot commit or release a newer worker's lease.

After a tick records its durable result, release the current lease and acknowledge the Redis message. If a duplicate cannot claim due work, acknowledge it. Reclaim abandoned Redis pending entries after 60 seconds with `XAUTOCLAIM`; the MongoDB dispatcher provides independent recovery after Redis loss. Never hold an open MongoDB transaction across an LLM or HTTP call.

### External idempotency and reconciliation contract

Both simulated external adapters implement:

```text
POST /tickets or POST /reservations
  Header: Idempotency-Key
  Same key + same canonical payload -> same stored effect/result
  Same key + different payload -> 409, no second effect
GET /operations/{idempotency_key}
  CONFIRMED(result), NOT_FOUND, or UNAVAILABLE
```

Store the idempotency ledger and effect atomically in each simulator's SQLite transaction. For inventory, decrement stock in that same transaction. Use durable volumes. Keep ledgers indefinitely in the MVP; no TTL may expire a key while a workflow could retry. An unavailable part has a stored deterministic `SKIPPED` outcome, not repeated stock changes.

Runner protocol:

1. Validate exact approval and next action. Persist `IN_FLIGHT`, the existing key, and increment submission count before sending a mutation.
2. Send exactly one client request, with transport/SDK retries disabled.
3. A confirmed response commits the result before starting a dependent action. An explicit no-effect business decline is handled according to its typed category.
4. A timeout, transport loss, unclassified 5xx, crash after intent, or other uncertain response becomes `UNKNOWN`/`RECONCILING`, never an immediate blind retry.
5. Look up the same key. If confirmed, record that result. If absent and the adapter guarantees durable same-key replay, schedule a bounded retry using the SAME key. `NOT_FOUND` alone is not proof an earlier request cannot finish later.
6. If lookup stays unavailable, same-key guarantees are absent, or the outcome remains unresolved, stop at `NEEDS_REVIEW`. Never transform uncertainty into `FAILED` with an implication that no effect happened.

Two workers may overlap at an external system after lease expiry. MongoDB fencing prevents stale local writes; downstream idempotency prevents duplicate effects. Neither a Redis lock nor a MongoDB lease alone guarantees external exactly-once behavior.

### HTTP interface and isolation

| Method/path | Contract |
| --- | --- |
| `POST /workflows` | Requires API key and 1..128-character `Idempotency-Key`; validated `WorkflowCreate`. 202 new, 200 replay, 409 changed payload. |
| `GET /workflows/{id}` | Returns tenant-scoped state, revision, proposal/plan, approvals, action outcomes, bounded history, and scheduling status. Foreign/missing ID returns 404. |
| `POST /workflows/{id}/approve` | `ApprovalRequest`; applies the exact approval semantics above. |
| `POST /workflows/{id}/cancel` | `CancelRequest`; 200 after cancellation or if already cancelled, otherwise 409 when unsafe/stale. |
| `GET /health/live` | Process liveness, no credential or model-call requirement. |
| `GET /health/ready` | MongoDB readiness and broker status. Mongo failure is 503; Redis failure is visible as degraded readiness while durable admission can still work. |

Missing/invalid API key returns 401. Input validation returns 422, except a missing/invalid idempotency header returns 400. The API never waits for the model or an external mutation during admission. If MongoDB is unavailable, reject admission with 503 rather than claiming a queued workflow. If Redis is unavailable after a successful MongoDB admission, still return the durable workflow and let the dispatcher recover later.

Do not accept user-supplied tenant fields, external URLs, arbitrary tools, or catalog mutations. Limit JSON bodies to 16 KiB before parsing and request text to 2,000 characters. The API-key map is a demo authentication mechanism, not production IAM. Logs redact API keys, provider keys, request text, raw model output, and connection credentials.

### Evaluation and acceptance

Keep model quality, execution correctness, and throughput as separate measurements.

- Planner corpus: 40 development and 80 held-out synthetic requests with expected plans/clarification outcomes. Include ambiguous references, unknown assets, unauthorized IDs, unsupported actions, malformed-output fixtures, and prompt injection. Label the requests synthetic and document who verified expected outputs. Freeze prompts using only development cases.
- Full completion requires at least one real provider/local-model adapter run on the 80-case held-out set with model revision/settings, prompt hash, schema-valid rate, exact required-field accuracy, unauthorized proposal rate, and clarification precision/recall. An unsafe proposal blocked by policy is not a successful planner prediction.
- Fault benchmark: at least 20 named scenarios, each repeated with seeds 0 through 9. Use the deterministic fixture planner and programmatic explicit approval so the execution experiment is reproducible and has no paid-model cost.
- Load benchmark: 100 workflows at client concurrency 1, 5, and 20 using the fixture planner. Separately report durable admission p50/p95, active processing duration excluding approval wait, queue delay, effect completion, error rates, and environment. Do not set hardware-independent throughput claims.
- Required invariants: no effect without matching approval; no unauthorized action; no duplicate confirmed external effect for one logical action under the simulator's idempotency contract; durable accepted work survives worker/Redis restart; stale workers cannot overwrite newer state; ambiguous unsupported outcomes stop for review.
- No 100% success target is imposed on the LLM. All 200 fault runs must match their scenario's expected state/effects or produce a visible failing test. Cases intentionally ending in `NEEDS_REVIEW` are counted separately from successful completion.

No benchmark was run during planning. Do not present mock throughput as enterprise throughput, the fixture planner as live model integration, or fault-free execution as evidence of crash recovery.

## File map

```text
maintenance-workflow-engine/
  pyproject.toml, uv.lock, .gitignore, .env.example, README.md, CONTEXT.md
  compose.yaml, Dockerfile
  src/workflow_engine/
    domain.py                            state, schemas, immutable invariants
    policy.py                            tenant catalog, validation, plan compilation
    planner.py                           fixture and real model adapters
    store.py                             protocol, in-memory fake, MongoDB implementation
    broker.py                            Redis Streams and dispatcher
    gateways.py                          typed HTTP external adapters
    runner.py                            one leased workflow tick and reconciliation
    api.py                               FastAPI factory, auth, admission, commands
    settings.py                          validated environment and resource limits
    telemetry.py                         redacted logs and metric definitions
    cli.py                               API/worker/dispatcher/init/eval entry points
  src/external_simulators/
    ledger.py                            SQLite idempotency/effect transaction
    ticketing.py                         controlled ticket endpoint
    inventory.py                         controlled reservation endpoint
    faults.py                            seedable failures and opt-in control endpoints
  prompts/maintenance_v1.txt
  fixtures/{catalog,planner_dev,planner_test,fault_cases}.jsonl
  tests/unit/test_{domain,policy,planner,runner,redaction}.py
  tests/integration/test_{store,simulators,gateways,broker,api,recovery}.py
  tests/e2e/test_workflow.py
  scripts/{evaluate_planner,benchmark_faults,benchmark_load}.py
  reports/{planner,faults,load}.md
  docs/{architecture,consistency,runbook,evaluation}.md
```

Use modules with small interfaces. The runner owns orchestration complexity; API handlers and workers must not duplicate state-machine logic. The store seam has two real implementations, an in-memory test fake and MongoDB. The planner seam has a fixture and model adapter. Do not add pass-through layers, an ORM, a generic repository framework, or a graph engine.

## Shared interfaces

```text
domain.py:
  Workflow, Action, Lease, Wakeup, TickResult, PlanProposal, ExecutablePlan,
  WorkflowCreate, ApprovalRequest, CancelRequest, WorkflowState, ActionState,
  Conflict, NotFound, InvalidPlan, LeaseLost
  transition(workflow: Workflow, event: DomainEvent) -> Workflow
policy.py:
  validate_and_compile(request: WorkflowCreate, proposal: PlanProposal,
                       catalog: TenantCatalog, workflow_id: str,
                       tenant_id: str) -> CompiledPlan | NeedsInput
planner.py:
  Planner.propose(request: WorkflowCreate, catalog: TenantCatalog) -> PlanProposal   # async
store.py:                                                                        # async methods
  WorkflowStore.insert_or_get(workflow: Workflow) -> AdmissionResult
  WorkflowStore.get(workflow_id: str, tenant_id: str) -> Workflow
  WorkflowStore.claim(workflow_id: str, worker_id: str, lease_s: int = 30) -> Lease | None
  WorkflowStore.save(workflow: Workflow, expected_revision: int,
                     lease_token: str | None) -> Workflow
  WorkflowStore.renew(lease: Lease, lease_s: int = 30) -> bool
  WorkflowStore.release(lease: Lease) -> None
  WorkflowStore.due_wakeups(limit: int = 100, republish_after_s: int = 5) -> list[Wakeup]
gateways.py:
  EffectGateway.submit(action: Action) -> EffectResult                             # async
  EffectGateway.lookup(action: Action) -> LookupResult                             # async
broker.py:
  WakeupBroker.publish(wakeup: Wakeup) -> str                                       # async
  WakeupBroker.receive(consumer: str) -> BrokerMessage | None                       # async
  WakeupBroker.ack(message_id: str) -> None                                         # async
  dispatch_once(store: WorkflowStore, broker: WakeupBroker) -> int                  # async
runner.py:
  Runner.tick(workflow_id: str, worker_id: str) -> TickResult                        # async
api.py:
  create_app(settings: Settings, store: WorkflowStore,
             catalog: CatalogProvider) -> FastAPI
```

Define event types, timestamps, catalog types, `CompiledPlan`, `AdmissionResult`, `EffectResult`, `LookupResult`, `BrokerMessage`, and validated `Settings` in the files that own them. `Lease` includes workflow/tenant ID, token, owner, expiry, and the claimed workflow snapshot; the runner does not need a non-tenant-scoped public GET. Gateways advertise a fixed `supports_idempotent_replay: bool` capability.

`WorkflowStore.save` consumes a proposed domain transition but MUST filter immutable fields and use conditional Mongo updates, not `replace_one` of a stale snapshot. `transition` determines legal state/action changes; store checks enforce concurrency. A live worker's state cannot be changed through a lease-free write. `release` ignores an obsolete token rather than releasing another worker's lease.

Use domain exceptions at module interfaces. Translate them to HTTP status codes only in `api.py`. `TickResult` distinguishes `not_claimed`, `progressed`, `waiting`, `finished`, and `lease_lost`. A worker may submit at most one external mutation per tick.

## Implementation tasks

Each task uses failing tests, minimal implementation, green verification, and a focused commit. Do not commit secrets, database volumes, generated benchmark claims, or another agent's work. Dependency order: 1 -> 2/3/4 -> 5/6 -> 7 -> 8/9 -> 10 -> 11/12 -> 13. Tasks 2, 3, and 4 can run independently after Task 1; integration ownership remains with one lead.

### Task 1: Domain, state transitions, and offline package

**Files:** Create package/lock files, `domain.py`, `settings.py`, `cli.py`, `tests/unit/test_domain.py`, `.gitignore`, `.env.example`, and `CONTEXT.md`.

**Interfaces:** Produce shared domain types, `DomainEvent`, and `transition`. Events cover planned/clarification/approved/action-started/action-confirmed/skipped/failed/ambiguous/retry-due/cancelled/review-required. Configure all numerical constraints from the specification centrally.

- [x] Write `test_no_effect_before_approval`, `test_confirmed_action_immutable`, `test_terminal_workflow_not_rescheduled`, and `test_cancel_preserves_confirmed_ticket`. Assert invalid transitions, quantity 0/11, extra fields, naive timestamps, oversized request/history, and reservation before confirmed ticket are rejected.
- [x] Run `uv run pytest tests/unit/test_domain.py -q`; confirm failure from absent implementation.
- [x] Implement the typed state machine and package setup. A plan cannot be edited after approval. Define settings validation, fixed action ordering, canonical JSON/hash helpers, and bounded history rules. Generate a compatible exact dependency lock.
- [x] Run tests and `uv run workflow-engine --help`; both must work without MongoDB, Redis, or model credentials.
- [x] Commit domain/setup/glossary. Later tasks consume these definitions rather than inventing alternate status strings.

### Task 2: Catalog policy, fixture planner, and exact plan compilation

**Files:** Create `policy.py`, fixture implementation in `planner.py`, `prompts/maintenance_v1.txt`, `fixtures/catalog.jsonl`, `tests/unit/test_policy.py`, and `tests/unit/test_planner.py`.

**Interfaces:** Produce `TenantCatalog`, `CatalogProvider`, `CompiledPlan`, `validate_and_compile`, and async `Planner.propose`. The fixture planner returns a predefined proposal for a fixture request; it is not a production fallback that invents plans.

- [x] Write tests for authorized ticket/reservation compilation, unknown asset, foreign asset, incompatible part, missing reservation intent, ambiguous quantity, unsupported tool/URL, extra JSON fields, and injected instructions to bypass approval. Assert deterministic plan hash/action keys and zero mutation access from the planner.
- [x] Run `uv run pytest tests/unit/test_policy.py tests/unit/test_planner.py -q`; confirm red.
- [x] Implement catalog resolution, conservative supported-request intent checks, schema validation, and stable action compilation. Return `NeedsInput` for ambiguity. Persist keys derived from tenant/workflow/plan/action, not from attempt count or model-generated UUIDs.
- [x] Run the tests; verify a prompt requesting a reservation cannot authorize a different tenant's asset even when the proposal is valid JSON.
- [x] Commit policy/fixtures/prompt. Document supported request grammar and intentionally unsupported requests rather than implying general language understanding.

### Task 3: MongoDB authority, admission idempotency, and leases

**Files:** Create `store.py`, `tests/integration/test_store.py`, store fake tests, and index initialization in `cli.py`.

**Interfaces:** Produce `WorkflowStore`, `AdmissionResult`, the in-memory fake, and MongoDB adapter with the exact shared methods. Use server time in production lease/due updates; inject a clock only into the fake for deterministic tests.

- [x] Write integration tests for concurrent same-key admission, payload conflict, tenant-isolated keys, 20 concurrent claims with one winner, expired lease recovery, stale-token save/release/renew, revision conflict, protected-field mutation, and due-intent persistence. Test a crash after creation but before any Redis call.
- [ ] Start only the local test MongoDB and run `uv run pytest tests/integration/test_store.py -q`; verify red. Use a disposable test database, never an unscoped database drop or a user's existing database.
- [x] Implement unique indexes, single-document conditional updates, protected-field validation, server-time leases, bounded history, atomic due/wake-sequence changes, and 5-second enqueue-intent marking in `due_wakeups`. Fail save on expired/stale tokens with `LeaseLost`.
- [x] Run real Mongo tests plus fake-store contract tests. Confirm operations need no replica set, multi-document transaction, or Redis availability. For lease-expiry tests, set a test lease deadline in the past instead of sleeping 30 seconds.
- [x] Commit store/index initialization and runbook test-database isolation instructions. Do not call an in-memory mock proof of MongoDB atomicity.

### Task 4: Durable idempotent external simulators

**Files:** Create `external_simulators/{ledger,ticketing,inventory,faults}.py` and `tests/integration/test_simulators.py`.

**Interfaces:** Produce the ticket/reservation/lookup HTTP contracts. `ledger.apply(key: str, payload: dict, operation: Callable) -> StoredOutcome` executes effect + key ledger in one SQLite transaction. Simulator fault controls are disabled unless `ENABLE_FAULTS=true`.

- [x] Write tests that 20 concurrent identical keys create exactly one ticket/decrement, changed payload returns 409, stock shortage is stably skipped, committed outcomes survive process restart, and commit-then-error remains discoverable by lookup.
- [x] Run `uv run pytest tests/integration/test_simulators.py -q`; confirm red.
- [x] Implement separate durable SQLite ledgers with safe concurrent transaction handling, canonical payload hashes, no key TTL, and seedable pre-commit failure/post-commit response-loss/lookup-unavailable profiles. Run blocking SQLite work outside FastAPI's event loop.
- [x] Run tests using independent temporary database files and restarted app instances. Verify simulator effect counts group by logical workflow/action identity, so an accidentally changed key cannot hide duplication.
- [x] Commit simulators and fault-profile documentation. State that their idempotency guarantee is an explicit benchmark assumption.

### Task 5: Typed HTTP gateways and ambiguity classification

**Files:** Create `gateways.py` and `tests/integration/test_gateways.py`.

**Interfaces:** Produce `EffectGateway`, `EffectResult`, and `LookupResult`. Results distinguish confirmed, no-effect business decline, retryable known-no-effect rejection, and unknown outcome. Lookup distinguishes confirmed, not found, and unavailable.

- [x] Write tests for normal success, stable replay, stock skip, payload conflict, request timeout after commit, malformed success JSON, 5xx with unknown commit status, and lookup failure. Assert no hidden HTTP retry and preservation of the original key/payload.
- [x] Run `uv run pytest tests/integration/test_gateways.py -q`; confirm red.
- [x] Implement HTTPX adapters with 10-second timeout and retries disabled. Pin external base URLs in settings, not model/user fields. Treat uncertain transport/5xx/malformed-response outcomes conservatively; never equate them with proven no effect.
- [x] Run gateway tests against actual simulator apps/HTTP transports and at least one subprocess network test. Assert unknown status cannot be converted to an automatic new-key submission.
- [x] Commit adapters and contract classifications. Any future real integration must pass these contracts or advertise no safe automatic replay.

### Task 6: Redis wakeups and recoverable dispatcher

**Files:** Create `broker.py`, worker/dispatcher command wiring in `cli.py`, and `tests/integration/test_broker.py`.

**Interfaces:** Produce `WakeupBroker`, `BrokerMessage`, and `dispatch_once`. Consume MongoDB `due_wakeups`, not user/action payloads. Worker acknowledgment is separate from the durable runner result.

- [x] Write tests for duplicate publication, dispatcher death before publication, Redis failure after MongoDB admission, Redis data loss after an earlier publish, abandoned pending entries, and stale wake sequences. Assert due work is republished after the 5-second interval using controlled test timestamps.
- [ ] Run `uv run pytest tests/integration/test_broker.py -q`; confirm red using local test Redis and MongoDB.
- [x] Implement stream/group creation idempotently, `XREADGROUP`, `XACK`, 60-second `XAUTOCLAIM`, dispatcher loop, and bounded exponential reconnect delay. Queue messages contain only workflow ID and wake sequence. Redis failure must not erase/unschedule MongoDB work.
- [x] Run integration tests and verify the dispatcher neither modifies approved plans nor treats enqueue acknowledgment as workflow completion.
- [x] Commit queue/dispatcher wiring. Document that the embedded MongoDB due intent replaces a multi-store atomic outbox transaction, with duplicate wakeups expected.

### Task 7: Leased runner and approval-safe happy path

**Files:** Create `runner.py`, `tests/unit/test_runner.py`, and `tests/e2e/test_workflow.py` happy-path cases.

**Interfaces:** Produce `Runner.tick` and `TickResult`. Inject store, planner, catalog, gateways, and heartbeat control. A tick claims the workflow, does one phase/at most one external mutation, records state, and releases its current lease in `finally`.

- [x] Write tests for plan -> awaiting approval with zero effects, approved ticket then reservation in separate ticks, conditional skip, policy rejection, cancelled/terminal wakeup, and lease loss before submission. Before recovery is implemented, every ambiguous path must stop safely rather than blindly retry.
- [x] Run `uv run pytest tests/unit/test_runner.py tests/e2e/test_workflow.py -q`; confirm red.
- [x] Implement planning-call accounting, deterministic validation, approval checks, durable in-flight intent, single mutation submission, confirmed-result persistence, dependent ticket-ID resolution, heartbeat handling, and release/ack instructions. Never hold a database transaction during a call.
- [x] Run tests with fake store/planner/gateways plus one real-store/simulator happy path. Confirm an expired worker cannot write a result after another token owns the lease.
- [x] Commit the runner and happy path. Recovery acceptance is intentionally still incomplete until Task 9, so do not claim crash safety yet.

### Task 8: Authenticated HTTP admission, status, approval, and cancellation

**Files:** Create `api.py`, complete `settings.py`, and `tests/integration/test_api.py`.

**Interfaces:** Produce `create_app` and all specified HTTP routes. API commands use domain transitions and conditional store writes, not direct collection mutation or queue-only admission.

- [x] Write tests for 202 new/200 replay/409 changed admission, missing idempotency key 400, invalid key 401, foreign workflow 404, 16-KiB rejection, extra tenant field rejection, stale/hash-mismatched approval, replayed approval, cancellation races, and cancelled workflows retaining confirmed effects.
- [x] Run `uv run pytest tests/integration/test_api.py -q`; confirm red.
- [x] Implement tenant identity from API-key settings, body limits, domain-exception mapping, durable admission, exact approval, safe cancellation, status projection, and distinct liveness/readiness. Mongo-down admission is 503; Redis-down durable admission still succeeds with degraded broker status.
- [ ] Run API tests with HTTPX ASGI transport and real-store race cases. Prove no handler performs an LLM or external mutation call while handling admission/approval/cancellation.
- [x] Commit HTTP routes and request examples. Public responses/logs must not expose another tenant, provider secret, raw credentials, or mutable internal storage objects.

### Task 9: Bounded retry, reconciliation, and stale-worker recovery

**Files:** Complete `runner.py`, add `tests/integration/test_recovery.py`, and write `docs/consistency.md`.

**Interfaces:** Extend the existing tick implementation without changing public types. Implement the specified 5-submission/5-lookup/2-planner-call limits and 1/2/4/8-second scheduling.

- [x] Write cases for crash before mutation, commit then lost response, confirmed effect then crash before Mongo save, lease expiry during call, two overlapping workers, repeated lookup unavailability, key-conflict response, and non-idempotent adapter ambiguity. Assert no new key, no dependent action before confirmation, and `NEEDS_REVIEW` rather than false no-effect failure for unresolved outcomes.
- [x] Run `uv run pytest tests/integration/test_recovery.py -q`; verify new failure tests are actually exercised.
- [x] Implement abandoned `IN_FLIGHT` recovery via lookup, same-key replay only under the advertised contract, safe retry scheduling, bounded lookup exhaustion, and partial-outcome reporting. On `LeaseLost`, stop new submissions; let the next owner reconcile any call already sent.
- [x] Run recovery tests with MongoDB, both simulators, and real worker-process termination. Assert exactly the expected number of simulator effects per logical action, correct terminal state, and no stale state overwrite.
- [x] Commit recovery logic and the failure-model/guarantee document. Explicitly distinguish local fencing, downstream deduplication, durability, and the limits of cancellation.

### Task 10: Compose deployment and clean-start operations

**Files:** Create `compose.yaml`, `Dockerfile`, complete `.env.example`, CLI process entry points, `tests/e2e/test_workflow.py`, and `docs/runbook.md`.

**Interfaces:** Produce independently startable `api`, `worker`, `dispatcher`, `ticketing`, `inventory`, `mongo`, and `redis` processes. Model serving is an optional separately configured endpoint, not a default large download.

- [x] Write an opt-in end-to-end test that starts isolated containers, initializes indexes/catalogs, submits a fixture-planned request, approves its returned hash, and verifies final state/effects after a worker restart.
- [ ] Run `uv run pytest tests/e2e/test_workflow.py -m compose -q`; confirm the missing deployment fails meaningfully.
- [x] Implement non-root application images, healthy dependency startup/reconnect, persistent Mongo/SQLite volumes, two worker processes, local-only ports, redacted environment validation, graceful shutdown, and separate initialization commands. Record tested image digests.
- [x] Run `docker compose config`, `docker compose up --build -d`, index/catalog initialization, and the opt-in end-to-end test. Verify a fresh environment works without paid keys and a second startup does not duplicate catalog/ledger records.
- [x] Commit deployment/runbook and stop only project-owned test containers. Never use broad Docker pruning or volume deletion; document explicit opt-in reset commands for this project's disposable data.

### Task 11: Real-model planner adapter and held-out evaluation

**Files:** Complete real adapter in `planner.py`, create `fixtures/planner_{dev,test}.jsonl`, `scripts/evaluate_planner.py`, and `reports/planner.md`.

**Interfaces:** Produce the real async `Planner.propose` with provider settings, timeout, max two accounted calls, prompt/schema versions, and structured result validation. `evaluate_planner(cases_path: Path, planner: Planner, catalog: CatalogProvider) -> PlannerReport` has no mutation gateway.

- [x] Write tests for schema-valid/invalid JSON, fenced prose, provider timeout, refusal, injected actions, a second exhausted model call, and fixture/model metadata separation. Add dataset tests for 40 dev/80 test unique IDs, no duplicate normalized request text across splits, and complete expected labels.
- [x] Run `uv run pytest tests/unit/test_planner.py tests/unit/test_policy.py -q`; confirm the added real-adapter tests fail before implementation. Use a mocked HTTP provider for offline tests.
- [x] Implement the OpenAI-compatible adapter with SDK retries disabled, requested schema where supported, strict post-validation everywhere, conservative clarification on invalid/unsupported outputs, and no mutation tools. Construct/document synthetic evaluation cases and review their labels before use.
- [ ] With user-approved local GPU or provider budget, tune only on 40 dev cases, freeze prompt/model/settings, and run all 80 held-out cases once. Report schema validity, field correctness, unsafe proposals separately from policy-blocked effects, clarification behavior, latency, and actual token/call counts.
- [ ] Commit evaluation code, frozen prompt, permitted synthetic cases, and measured report. If no live model was run, mark this gate incomplete; do not relabel the fixture planner as GenAI integration.

### Task 12: Reproducible fault-injection benchmark

**Files:** Create `fixtures/fault_cases.jsonl`, `scripts/benchmark_faults.py`, extend recovery tests, and populate `reports/faults.md`.

**Interfaces:** Produce `run_fault_case(case: FaultCase, seed: int) -> FaultResult` and a 20-case by 10-seed report. `FaultCase` declares expected state, expected logical effect counts, injection point, bounded deadline, and whether human review is expected.

- [x] Write report tests that missing runs, mismatched effect counts, unknown states, or substituted fixture/live-model labels fail rather than disappear from the denominator. Pin the 20 scenario IDs listed below.
- [x] Run fault/report tests red, then implement only benchmark control, polling with a deadline, artifact capture, and assertion/report generation. Do not add retry-until-green behavior to the benchmark.
- [x] Execute all 200 cases against isolated local processes, using fixture plans and explicit benchmark approval. Capture request IDs, keys, state histories, simulator ledgers, and relevant redacted logs for every failure.
- [x] Verify every case against its expected state/effects. Expected manual-review cases are neither hidden errors nor successful automatic completion. Reruns are new labeled runs, not replacements for failed records.
- [x] Commit fault definitions, benchmark script, and actual report. If resource limits prevent the full matrix, state the exact completed subset and leave the full gate incomplete.

Required cases: duplicate admission; changed-payload replay; duplicate Redis message; dispatcher death before publish; Redis unavailable at admission; Redis data loss after publish; worker death before effect; ticket commit then response loss; reservation commit then response loss; worker death after effect before checkpoint; expired lease and stale save; concurrent workers; transient proven-no-effect rejection; permanent rejection; inventory unavailable; lookup repeatedly unavailable; non-idempotent downstream ambiguity; stale/mismatched approval; cross-tenant request/injection; safe cancellation with a previously confirmed ticket.

### Task 13: Observability, load, CI, and final handoff

**Files:** Create `telemetry.py`, `tests/unit/test_redaction.py`, `scripts/benchmark_load.py`, CI, and complete README/architecture/evaluation/runbook/report files.

**Interfaces:** Produce JSON logs keyed by workflow/action/attempt without raw request text/secrets, duration/counter definitions, fixture load script, and a clean-clone verification path.

- [x] Write tests proving API keys, provider keys, Mongo credentials, raw request text, and raw provider output are absent from logs. Write a load-report test separating admission time, approval wait, queue delay, processing duration, errors, and expected-review outcomes.
- [x] Run tests red, then implement redaction, structured event naming, CLI metrics/report output, and CI groups. Default CI is offline unit tests; real Mongo/Redis/HTTP integration and compose jobs are explicit separate jobs with visible skips/failures.
- [ ] Run `uv sync --frozen --extra dev`, `uv run pytest tests/unit -q`, integration tests with isolated Mongo/Redis/simulators, and the compose end-to-end suite. Record exact commands, counts, and environment.
- [x] Execute 100 workflows each at concurrency 1, 5, and 20 with the fixture planner and explicit approvals. Report observed p50/p95 and correctness, not an invented SLO. Repeat live-model latency separately if budget permits; never mix fixture and live numbers.
- [ ] Commit all evidence and a two-minute reproducible demo script. Final review checks real-model status, tenant isolation, exact approval, effect counts, crash/Redis recovery, bounded uncertainty handling, secrets, license notices, and reproducible setup. Write resume claims only from completed measurements.

## Suggested milestone demos

1. **After Task 8:** submit a request, inspect a validated plan, approve its hash, observe one ticket and optional reservation. Demonstrate that merely prompting does not execute anything.
2. **After Task 10:** kill a worker after a downstream commit and restart it; show lookup recovery and one effect. Delete only the isolated test Redis data and show MongoDB-driven redispatch.
3. **After Task 13:** run the frozen planner evaluation, fault matrix, and load report; explain which guarantees rely on downstream idempotency and why unresolved ambiguity stops automation.

## Agent handoff prompt

```text
Implement the standalone maintenance-workflow plan in this file in a new,
explicitly confirmed repository. Read its specification, state machine, shared
interfaces, and tasks before editing. Start at Task 1 with offline tests. Keep
one lead responsible for the shared domain/store contracts and integration.
MongoDB is authoritative; Redis carries replaceable wakeups. Persist action
intent and stable idempotency keys before mutations. Never bypass exact plan
approval, tenant checks, lease/revision checks, or ambiguity reconciliation.
Use fixture planning and local simulators until live-model budget is approved.
Do not build a chatbot, generic DAG engine, UI, or machinery-control system.
Keep task checkboxes current and commit narrowly. At each milestone return
changed files, observed test commands/results, artifacts, remaining risks, and
the next task. Mark unrun live-model or fault benchmarks incomplete explicitly.
```

## Primary implementation references

- FastAPI: https://fastapi.tiangolo.com/
- Pydantic strict models: https://docs.pydantic.dev/latest/concepts/models/
- PyMongo async API: https://pymongo.readthedocs.io/en/stable/api/pymongo/asynchronous/mongo_client.html
- MongoDB single-document atomicity: https://www.mongodb.com/docs/manual/core/write-operations-atomicity/
- Redis Streams and consumer groups: https://redis.io/docs/latest/develop/data-types/streams/
- Redis XAUTOCLAIM: https://redis.io/docs/latest/commands/xautoclaim/
- HTTPX async clients/timeouts: https://www.python-httpx.org/async/ and https://www.python-httpx.org/advanced/timeouts/
- SQLite transactions: https://docs.python.org/3/library/sqlite3.html
- Optional local model: https://huggingface.co/Qwen/Qwen2.5-7B-Instruct

Check actual dependency/container compatibility during setup. No local database, model endpoint, throughput benchmark, recovery experiment, or provider credential was exercised while writing this plan.
