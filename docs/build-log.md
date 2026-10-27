# Build record

Source plan: `docs/2026-10-18-maintenance-workflow.md`.

## Decisions

- The user approved building the supplied specification and creating/pushing a new repository. The new local repository is `maintenance-workflow-engine`, beside the source folder. The GitHub repository defaults to private. This new repository is the isolated workspace; no existing implementation is being changed.
- Execute the supplied 13-task plan inline, with test-first checks, focused commits and one fresh final review. The existing specification and plan are retained rather than replaced.
- Ruling: unresolved ambiguity after the final allowed submission ends in `NEEDS_REVIEW`, even after `NOT_FOUND`. A late external commit remains possible.
- Ruling: lookup returns a confirmed stored outcome that distinguishes `CONFIRMED` and `SKIPPED`. Reconciliation must preserve a stock-shortage skip.
- Ruling: Redis-down readiness returns HTTP 200 with `status=degraded` and `broker=false`; Mongo-down readiness returns 503. Admission depends only on MongoDB.
- Real-model inference requires an approved local resource or provider budget. Offline tests must not spend inference credits.

## Interface preflight

| Tasks | Shared contract | Resolution |
| --- | --- | --- |
| 1 → 2–9 | Workflow, events, lease, typed plan | Domain owns state legality; store owns concurrency. |
| 2 → 7/11 | Planner and compiled plan | Schema validation and authorization remain independent of provider. |
| 3 → 6–9 | Store save/claim/due work | Conditional field updates preserve lease and dispatch metadata. |
| 4 → 5/9 | Submit/lookup | Lookup retains stored skipped outcomes; same-key replay only. |
| 5/6 → 7/9 | Gateway and wakeup | One mutation per tick; message contains no trusted action arguments. |
| 7/8 → 10–13 | Runner and HTTP | Durable approval and admission precede dispatch. |

## Progress

- [x] 1. Domain and offline package. Missing-package RED, then 10 tests passed; CLI help works offline. Python 3.12.16, exact dependencies in `uv.lock`.
- [x] 2. Catalog policy and fixture planner. Missing-module RED, then full suite 27 passed. Grammar and two-tenant catalog documented.
- [ ] 3. Store implementations written. Missing-module RED; 30 passed, 2 explicit MongoDB skips. Native MongoDB test binary download in progress; real-store verification pending.
- [x] 4. SQLite-backed external simulators. Missing-module RED, then 33 passed / 2 MongoDB skips. Concurrent same-key submissions, stock skip, persisted restart lookup, payload conflict and post-commit failure covered. Test setup uses independent temporary files.
- [ ] 5. Typed HTTP gateways written and tested through actual simulator apps and failure transports. Missing-module RED, then 42 passed / 2 MongoDB skips. Subprocess network check follows with CLI wiring.
- [ ] 6. Redis Streams and recoverable dispatcher written. Missing-module RED; offline publish-loss/redis-loss contract passed, 43 tests passed / 3 service skips. Real Redis verification pending.
- [ ] 7. Leased runner
- [ ] 8. HTTP interface
- [ ] 9. Recovery
- [ ] 10. Compose deployment
- [ ] 11. Real-model adapter and evaluation
- [ ] 12. Fault benchmark
- [ ] 13. Observability, load, CI and handoff
