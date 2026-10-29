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
- [x] 8. HTTP admission/status/approval/cancellation implemented. Auth, tenant isolation, body limits, replayed approval, active-lease cancellation and partial-effect cancellation passed. Real-store approval/cancellation race checks passed in GitHub Actions runs 40751664059 and 40751719978.
- [x] 9. Recovery. Eight expected RED failures, then local recovery tests passed. GitHub run 40523526226 additionally passed abrupt worker termination before/after effects, stale leases, same-key replay and bounded review outcomes across the full fault matrix.
- [x] 10. CLI processes and pinned Compose deployment. GitHub Actions run 40521208198 built and ran the isolated Compose stack and verified workflow completion after worker restart. Image digests are verified by that run; local Docker remains unavailable.
- [ ] 11. OpenAI-compatible planner, strict post-validation and 40/80 synthetic splits implemented. Labels were authored and reviewed by the implementation agent against the bounded grammar and catalog; no human review is claimed. No live inference run; approved inference resources remain required.
- [x] 12. Corrected 20×10 fault matrix passed in GitHub run 40751719978 at 581a740: 200 exact expected states/effect counts, including 20 expected-review runs and 150 automatic completions. Fresh Redis-down admission passed all ten seeds. The original run 40523526226 at df98593 remains recorded separately; both measured summaries are checked in.
- [x] 13. Logging, demo, docs, CI and load evidence implemented. GitHub run 40700067125 completed all 300 fixture workflows with 600 confirmed effects and zero errors. Final local suite: 87 passed, 26 explicit service skips; Ruff check and formatting passed. Run 40751664059 passed 53 unit tests, 39 real-service integration tests and the Compose/restart test on 581a740. The corrected full fault matrix and all three independent-review fixes are verified. Clean-clone installation and CLI checks are recorded in docs/verification.md.

## Final review corrections

- Cancellation regression: both RETRY_WAIT and READY previously returned HTTP 200 despite an unresolved earlier request; now 409. A confirmed late ticket remains visible and permits later safe cancellation. RED → GREEN, two cases.
- Model report regression: 80 provider 503s previously yielded real_model_gate=true; now false with zero inference responses. A complete but inaccurate real-model response set remains measurable. RED → GREEN, two cases.
- Benchmark gap: the Redis-down case now requires a new 202 admission while Redis is unreachable, then proves recovery from MongoDB. The previous replay-only case was insufficient evidence. Corrected run 40751719978 passed all 200 cases, and the first run remains recorded separately.
- Ruling: benchmark-only timeout increased from 900 to 2,400 seconds after logs showed the specified five-second redispatch cadence. Product scheduling and workload are unchanged; an append-only journal preserves interrupted-run observations.
- Review scope ruling: local skipped integration tests are not evidence of service correctness; use linked remote CI measurements. Live-model quality remains unmeasured pending approved inference resources. Label provenance is explicitly agent-reviewed rather than human-reviewed. No minor findings were deferred.
- Verification portability: the console-script pytest invocation in Linux CI exposed that demo tests imported the repository's scripts without an explicit root import path. Reproduced locally with `.venv/Scripts/pytest`, fixed with pytest's `pythonpath=["."]`, then verified with `uv sync --frozen --extra dev` and `uv run pytest`: 87 passed, 26 explicit service skips. Run 40750863369 failed at collection before the corrected matrix could start; it is retained as failed evidence.
- Historical process gates: the original plan's pre-implementation RED runs requiring workstation MongoDB/Redis/Compose remain unchecked because those services were unavailable locally. Missing-module RED checks ran locally; real service and container checks ran in GitHub Actions. Those unchecked historical steps are not represented as completed retroactively.

## Handoff

All implementation, review corrections, fault/load measurements and repository documentation are complete. Task 11's real-model measurement remains open because no inference resources or budget were approved. This limits model-quality claims, not the completed fixture-backed reliability measurements. The [verification record](verification.md) links exact commands, counts and CI runs; [review notes](review.md) explain the three corrected findings.

## Task 11 continuation: approved local inference

The user approved downloading a 7B model for live evaluation on 2026-10-29. This supersedes the earlier resource blocker. The machine has an RTX 3060 Laptop GPU with 6 GiB VRAM, a Ryzen 7 5800H and 13.9 GiB usable RAM. Use the existing Qwen2.5-7B-Instruct choice in Q4_K_M format and a pinned portable llama.cpp CUDA runtime.

The user cancelled the slow agent download and supplied both Q4_0 shards in `model/`. Both files match the official pinned Hugging Face revision's SHA256 hashes. Use this supplied quantization for the measured run and identify it as Q4_0 throughout. The already downloaded CUDA runtime archives also match their published SHA256 hashes. Local weights are excluded from Git and Docker build contexts.

- [x] Download and verify model/runtime hashes; start a localhost-only endpoint.
- [x] Run development cases, inspect failures and settle prompt/settings using development evidence only.
- [ ] Freeze model, prompt, corpus and server settings; run the 80 held-out cases once.
- [ ] Publish measured evidence, verify the completed gate, stop the evaluation server and sync GitHub.

Development run 1 completed 40 genuine inference responses with valid schema, 14 exact matches and two policy-blocked proposals. All 24 valid-plan mismatches omitted the required final period in ticket descriptions; the two other mismatches guessed an asset from unsupported location references. Median request latency was 1.829 seconds. Preserve this baseline in `artifacts/planner-qwen-q4_0-dev-1`. First tuning hypothesis: explicitly describe the period as a concatenated part of the JSON field value.

Development run 2 confirmed the punctuation hypothesis: all 24 valid requests now match exactly. Overall results were 36/40 exact matches, one schema-invalid output and three policy-blocked proposals. Remaining failures guessed an unknown/ambiguous asset or substituted a part with an invalid quantity. Second tuning hypothesis: make exact catalog matching and quantity validation explicit prerequisites to constructing a plan. No held-out responses have been generated or used for tuning.

Development run 3 removed all schema-valid unauthorized proposals, but exact/schema accuracy fell to 31/40. Eight otherwise correct plans used the schema class name `ExecutablePlan` instead of the literal discriminator `plan`; one clarification question exceeded 200 characters. Third tuning hypothesis: explicitly distinguish schema names from JSON discriminator values and state the question length bound. Preserve the regression as a separate measured development run.

Development run 4 matched all 40 labels, with schema-valid rate 1.0, zero unauthorized proposals and clarification precision/recall 1.0. Median latency was 1.825 seconds. Freeze this prompt, Q4_0 weights, corpus, catalog and server settings in `reports/planner-qwen-q4_0-freeze.json` before the held-out pass. No held-out tuning is permitted. The complete local suite still passes: 87 passed, 26 service-dependent skips.
