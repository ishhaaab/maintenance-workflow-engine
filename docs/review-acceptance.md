# Review acceptance checklist

The implementation follows the [approved design](superpowers/specs/2026-11-11-configurable-approval-workflow-design.md) and the approved continuous execution of core phases 2 through 9. PostgreSQL, Redis, SMTP and browser results are distinct from offline tests and from historical MongoDB measurements.

## Requirement to test

| Requirement | Automated evidence |
| --- | --- |
| Named Argon2id accounts, opaque durable sessions, rotation/logout/expiry, throttle | `tests/unit/test_authorization.py`, `tests/integration/test_auth.py` |
| Origin/CSRF, no client-role trust, disable/re-enable, no legacy approval | `test_auth.py`, `test_api.py`, `test_review_browser.py` |
| Atomic audit, named/system attribution, >256 events, denied runtime rewrite | `tests/integration/test_audit.py`, `test_migrations.py`, `test_postgres_store.py` |
| Draft admission/ownership, revision guards, foreign IDs | `tests/integration/test_review_commands.py` |
| Pure lifecycle, one to five distinct reviewers, comments and sequential levels | `tests/unit/test_review_domain.py` |
| Activation rejects empty/unready chains; participant-first lock ordering | `test_review_commands.py`, `test_review_races.py`, `test_review_safety.py` |
| Immutable versions/steps/plans/decisions, same-item ancestry constraints | `test_review_safety.py`, `test_approval_execution.py` |
| NEEDS_INPUT, frozen catalog and supersession with live-lease conflict | `test_review_safety.py` |
| Exact hash, assignment, revision, command replay and one concurrent winner | `test_approval_execution.py`, `test_review_safety.py` |
| Request changes, historical approvals unusable, full-chain restart, rejection final | `test_approval_execution.py`, `tests/e2e/test_review_browser.py` |
| Final decision/audit/authorization/due intent rollback together | `test_review_safety.py::test_final_decision_authorization_audit_and_due_intent_roll_back_together` |
| Concurrent last-item decisions complete cycle exactly once | `test_review_safety.py::test_different_last_item_reviewers_cannot_miss_cycle_completion` |
| Hash-only or general store-save forgery cannot authorize mutation | `test_review_safety.py`, shared store contracts |
| No preapproval effects; original keys, due recovery, PostgreSQL/Redis outages | `test_approval_execution.py`, `test_postgres_recovery.py`, `test_runtime.py`, full fault benchmark |
| Safe, responsive, escaped browser forms; honest stale-tab recovery | `test_web.py`, `tests/e2e/test_review_browser.py`, `test_review_layout.py` |
| UTC boundary/current-step eligibility, unique reminder, competing claims | `tests/unit/test_notifications.py`, `tests/integration/test_notifications.py` |
| Decision-before-send suppression, bounded retry, accepted/unacked duplicate, audited exhaustion | `tests/integration/test_notifications.py` |
| Disabled assignee defers the same reminder without spending SMTP budget; re-enable resumes | `test_notifications.py::test_disabled_current_reviewer_defers_without_spending_smtp_budget_and_resumes` |
| Real SMTP message in isolated Mailpit | `test_notifications.py::test_real_smtp_message_appears_in_local_mailpit`, seeded Compose walkthrough |
| Admin-only tenant-scoped downloads; repeatable-read concurrent consistency | `tests/integration/test_exports.py`, seeded Compose walkthrough |
| >50 row coverage, empty and explicit oversized errors, formula safety/Unicode | `tests/integration/test_exports.py`, `tests/unit/test_exports.py` |
| Actual multipage embedded-font PDF; download and parse both formats | `tests/unit/test_exports.py`, `tests/e2e/test_review_walkthrough.py` |
| CSV contains decision/execution/reminder audit, empty-cycle facts and draft assignments; incremental output cap | `test_exports.py`, `tests/unit/test_exports.py` |
| Real wrong migration head closes business routes without hiding health | `test_review_safety.py::test_business_routes_fail_closed_when_real_database_revision_is_not_current` |
| Failed external execution leaves sealed approval/completed cycle intact | `test_review_safety.py::test_execution_failure_does_not_reclassify_a_sealed_approved_review` |
| Explicit local-only repeat-safe seed preserves passwords/activity | `tests/integration/test_seed_demo.py`, seeded Compose walkthrough |
| Two tenants, fourteen story cycles, eight one-time credentials | `tests/integration/test_seed_demo.py` |
| Previous-schema records retained; tenant identities not guessed | `tests/integration/test_identity_upgrade.py` |
| Fresh deployment, migration/runtime separation, two workers, retained volumes | `tests/e2e/test_compose.py` |
| Fresh seeded UI, two reviewers, resubmission, execution, SMTP, exports, denied roles/tenant, restart | `tests/e2e/test_review_walkthrough.py` |

Unqualified names in this table are under `tests/integration`, except where a path is explicit. These are coverage pointers, not claims that every arbitrary input is proven safe.

## Reproduce the checks

Install the exact Python 3.12 environment and real Chromium:

```sh
uv sync --frozen --extra dev
uv run --no-sync python -m playwright install chromium
```

On Linux runners, use `playwright install --with-deps chromium`. Docker Compose checks build the locked production image with its packaged templates, CSS, fonts and migrations.

Set these process-scoped values for dedicated, isolated local services. Do not use an existing product deployment:

| Variable | Meaning |
| --- | --- |
| `TEST_POSTGRES_URL` | Administrative PostgreSQL URL, kept private. Fixtures allocate random databases/non-owner roles and remove only their exact allocations. |
| `TEST_REDIS_URL` | Redis URL; tests delete only their own random streams, not the database. |
| `TEST_SMTP_PORT` | Local Mailpit SMTP port on 127.0.0.1. |
| `TEST_MAILPIT_URL` | Local Mailpit HTTP API URL. |
| `RUN_BROWSER=1` | Require real Chromium. |
| `RUN_COMPOSE=1` | Require Docker Compose and fresh project tests. |

Run all checks with skip rejection:

```sh
uv run --no-sync python -m ruff check .
uv run --no-sync python -m ruff format --check .
uv run --no-sync python -m pytest -p tests.required_checks --fail-on-skip -q
uv run --no-sync python scripts/benchmark_faults.py --output-dir artifacts/review-faults-UNIQUE --seeds 0,1,2,3,4,5,6,7,8,9
docker compose -f compose.postgres.yaml config --quiet
git diff --check
```

Each benchmark output directory must be new. Keep failed evidence, never retry into it. Required matrix acceptance is all twenty original cases for each seed, 200 validated observations, original action keys, expected terminal states, logical effect identities, bounded attempts and verified exact-allocation cleanup. Planning setup is test-owned internal admission; authorization now uses two named HTTP reviewer sessions through production decision commands. It is not a public arbitrary workflow-creation interface. Existing isolated store/process fixtures remain labeled test-only and do not ship in the production image.

For a narrower diagnostic run:

```sh
uv run --no-sync python -m pytest tests/unit -q
uv run --no-sync python -m pytest -p tests.required_checks tests/integration tests/e2e/test_workflow.py tests/e2e/test_review_browser.py tests/e2e/test_review_layout.py --ignore=tests/integration/test_fault_matrix.py --fail-on-skip -q
uv run --no-sync python -m pytest -p tests.required_checks tests/e2e/test_compose.py tests/e2e/test_review_walkthrough.py --fail-on-skip -q
```

Without explicit services/flags, the ordinary suite reports skips. Such a run is useful offline but is not release acceptance. A supplied broken service must fail rather than skip. `COMPOSE_ARTIFACT_DIR` saves selected safe deployment logs/results. `REVIEW_REPORT_DIR` optionally saves downloaded synthetic CSV/PDF files in a new directory, never account passwords or session cookies.

## Observed release evidence

- On 2026-11-17 the standalone fresh-Compose seeded walkthrough passed, 1 test in 85.10 seconds with zero skips. It built the production image, generated eight accounts/two tenants, captured a current-step reminder, completed a newly created two-level changes/resubmission journey, downloaded and parsed CSV/PDF, denied foreign/unauthorized reports, retained sessions across API/worker/notification restarts and verified a no-op seed rerun. Safe logs: `artifacts/review-compose-walkthrough-20261117-01/`.
- Representative downloaded files were opened from an external temporary directory. CSV has four decision-level rows spanning two versions. The PDF preserves the accented title, old/current outcomes and successful execution text. File downloads are tested in actual Chromium, not substituted with fixture byte strings.
- After the review fixes, both fresh-Compose checks passed together: **2 passed, zero skips in 326.20 seconds**. Safe evidence: `artifacts/review-compose-final-20261117-03/`. The new downloaded reports were reopened: CSV 27,938 bytes/four rows/two versions, PDF 33,521 bytes/two pages. CSV contains `review_decided`, `execution_authorized`, worker `transitioned`, `reminder_scheduled` and `reminder_sent` audit facts. CSV SHA256: `03a81ebc11276fb3417c30991b2fa722baa75e1bf475758b65516b76f2e9f42b`; PDF SHA256: `6a433689bf5b663f546e92a393e1bfe7608d0c138af09128c404869b24174a70`.
- A concurrent local core-suite run returned 263 passed and five database-fixture setup errors, not a passing acceptance. The subsequent Windows full run returned 569 passed, 13 failures and 33 errors in 841.30 seconds. Its retained JUnit is `artifacts/review-full-20261117-02/results.xml`. Transport traces identified Winsock 10048, address already in use, with 12,492 TIME_WAIT sockets in the 16,384-port dynamic range. PostgreSQL had no OOM/restart/connection-limit exhaustion. Linux-container acceptance avoids host TCP churn; Windows networking was not changed and connection failures were not hidden by product retries.
- Regression investigation also reproduced and corrected two product issues under RED→GREEN: unauthenticated mutation errors prioritized Origin over missing identity, and post-approval execution FAILED incorrectly entered planning synchronization. A legacy two-test store fixture was missing its explicit tenants after tenant-FK introduction; those identities are now provisioned by test setup, with safety assertions retained.
- The independent whole-change reviewer confirmed no Critical finding and requested changes for audit completeness, disabled-recipient reminder recovery and fail-closed schema routing. Its two nominally Minor report findings were treated as Important because draft assignments and bounded generation are part of the accepted report contract. All five reproducing tests were observed failing before the corresponding fixes, then passed in the final complete suite. No second reviewer approval is implied, and no Minor finding from this review remains deferred.
- Ruff lint/format, `git diff --check`, Compose configuration and container-side `uv lock --check` passed before final acceptance. No remote Actions result or throughput claim is implied.
- Final post-fix Linux run: **610 passed, two Compose tests deselected, zero skips in 610.39 seconds**. Command: `python -m pytest -p tests.required_checks -m 'not compose' --fail-on-skip -q --tb=short --junitxml=/evidence/results.xml`, inside an isolated Python 3.12/Linux validation container using the locked environment, real Chromium and the owned PostgreSQL/Redis/Mailpit services over private networking. Combined with the separately passed Compose checks this is **612 tests: 226 unit, 381 integration and five end-to-end**, no failures/errors/skips. Both JUnit files were independently parsed. Evidence: `artifacts/review-linux-full-20261117-03/results.xml` and `artifacts/review-compose-final-20261117-03/results.xml`.
- A separate fresh owned database upgraded to `0007_demo_seed_records`, passed `alembic check`, and was read through the exact non-owner runtime role; its database and both roles were verified removed. Final Ruff check and formatting passed on 147 Python files; whitespace and Compose model checks passed.
- Final ten-seed fault benchmark: **200 passed in 890.81 seconds**, all twenty cases across seeds 0 through 9. Evidence: `artifacts/review-faults-final-20261117-04/matrix/`. The benchmark validator and a separate standard-library journal inspection both passed. They confirmed original action keys, bounded attempts, ticket-before-reservation ordering, logical effect identities, 170 ticket/140 reservation effects, and 200 exact database cleanup records. Outcomes: 150 SUCCEEDED, ten NEEDS_INPUT, twenty NEEDS_REVIEW, ten FAILED and ten CANCELLED, each as expected. Authorization used two named HTTP reviewer sessions through production decision commands. Summary SHA256: `bf62a17cc1a9f7be6d36adab3823dc530c2b142b383ec76d2f989c320c1ee35c`. Independent result: `matrix/independent-validation.json`.
- A postimage comparison verified all 150 source/test/migration/script/fixture/prompt/CI/dependency files in the tested Linux image byte-for-byte against this checkout. Manifest SHA256: `320e9d5caa9576e44b2fdb3bde5763e011b67d746f5a81b2d894bde853816a26`. Only documentation/ledger completion changed after the passing suite. The three recorded session-owned service containers and private service configuration were removed; unrelated containers/volumes were not selected. README, historical Compose and historical reports are unchanged.

Core phases 2 through 9 are complete. These are local Linux/Docker acceptance results with separately exercised Windows-host browser/Compose orchestration, not a passing native Windows full-suite claim. The failed native run remains documented above. The verified implementation is recorded in logical local commits on `configurable-approval-workflow`; no push, merge or PR was performed. Optional phase 10 remains excluded. Remote Actions execution, production SMTP deliverability, cutover/import and throughput certification remain unverified and are not release claims.

Before commit packaging, a fresh native run passed all 226 unit tests, Ruff lint/format and whitespace checks. A recomputation of all 150 tested-file hashes matched the saved verified-postimage fingerprint above. Docker was stopped, so service acceptance was not rerun or relabeled as a fresh result. Each implementation batch was also exported from its exact Git index into a disposable directory and passed its available unit suite plus lint/format checks, rather than accidentally testing later unstaged files.

Historical Phase 1 and MongoDB evidence stays in [verification](verification.md), explicitly separate from this release.
