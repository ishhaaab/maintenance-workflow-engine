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
- [x] 3. MongoDB store and fake. Missing-module RED, fake contracts passed locally. Real MongoDB admission, claim, stale-token and enqueue tests passed in GitHub Actions run 40521208198.
- [x] 4. SQLite-backed external simulators. Missing-module RED, then 33 passed / 2 MongoDB skips. Concurrent same-key submissions, stock skip, persisted restart lookup, payload conflict and post-commit failure covered. Test setup uses independent temporary files.
- [x] 5. Typed HTTP gateways. Missing-module RED; simulator contracts and real subprocess HTTP timeout-after-commit test passed. Lookup recovered the original effect without a duplicate.
- [x] 6. Redis Streams and dispatcher. Offline publish-loss recovery passed locally; real stream pending reclamation and recreation passed in GitHub Actions run 40521208198. Full process fault matrix is tracked under Task 12.
- [x] 7. Leased runner. RED then 46 passed locally, with service tests explicitly skipped there. Real-store/simulator happy path passed in GitHub Actions run 40521208198.
- [ ] 8. HTTP admission/status/approval/cancellation written. RED then 49 passed / 4 service skips. Auth, tenant isolation, body limits, replayed approval, active-lease cancellation and partial-effect cancellation passed. Real-store race checks pending.
- [ ] 9. Bounded retry and reconciliation implemented. Eight expected RED failures, then 59 passed / 4 service skips. Same-key recovery, lookup exhaustion, stale workers, two planner calls and submission exhaustion covered. OS-level worker termination verification follows.
- [x] 10. CLI processes and pinned Compose deployment. GitHub Actions run 40521208198 built and ran the isolated Compose stack and verified workflow completion after worker restart. Image digests are verified by that run; local Docker remains unavailable.
- [ ] 11. OpenAI-compatible planner, strict post-validation and 40/80 synthetic splits implemented. 66 passed / 4 service skips before Compose test addition. No live inference run; label human review and approved inference budget remain required.
- [ ] 12. All 20 named process-backed scenarios and ten-seed benchmark driver are implemented. Full matrix is pending its first CI run. Every run writes a distinct artifact and failures remain in the denominator.
- [ ] 13. Redacted JSON logging, report summaries, fixture load driver and CI are implemented. Local suite: 73 passed, 25 explicit service/Compose skips. Runtime reconnect logging regression reproduced as KeyError on reserved LogRecord.process, then fixed to process_role. Final review and measured load are pending.
