# Approval-workflow prompt implementation audit

## Result before this session's implementation

Three independent read-only subagents inspected the current source, tests, dependencies and deployment configuration against the saved implementation-prompt pack.

Only Prompt 0, the architecture/design checkpoint, was complete. None of the nine core implementation prompts, 1 through 9, had started product implementation. Neither optional prompt, 10 or 11, was implemented. A detailed, approved Phase 1 plan already existed, but it had stopped before product changes because the required development services were unavailable.

The existing execution engine remains working code. Its single approval gate, tenant API keys, bounded history, simulator fixtures and historical benchmark reports do not satisfy the new review-product requirements.

## Prompt status

Numbers below match the original prompt pack, not the approved design's reorganized five release phases.

| Prompt | Status | Evidence and remaining work |
| --- | --- | --- |
| 0. Architecture | Complete, design only | `docs/superpowers/specs/2026-11-11-configurable-approval-workflow-design.md` records the approved relational architecture and behavioral contract. |
| 1. PostgreSQL foundation | In progress, not complete | Task 1 of the six-task plan is implemented and independently approved. Task 2, admission/protected snapshot persistence, is underway. Store lease/recovery/runtime/Compose acceptance is still outstanding. |
| 2. Users and permissions | Not started | `src/workflow_engine/api.py:56-61` maps an API key to a tenant. No named users, sessions, passwords, roles or CSRF implementation exists. |
| 3. Immutable audit | Partial infrastructure only | Migration0001 now defines a separately append-only audit table with real runtime-role grant/retention checks. Semantic transaction append, human attribution and authorized reads are still outstanding. Diagnostic execution history remains a separate bounded feature. |
| 4. Review cycles and items | Not started | `src/workflow_engine/domain.py:25-40,161-189` models execution states and standalone runs, not cycles, requester-owned items or immutable submitted versions. |
| 5. Multi-level review | Not started | `src/workflow_engine/api.py:97-112` and `domain.py:295-300` still permit one tenant-key approval. There are no assigned levels, versioned decisions, rejection/change requests or atomic review-to-execution handoff. |
| 6. Browser application | Not started | `src/workflow_engine/api.py:52-147` exposes JSON/health routes. No product templates, static application or browser acceptance tests exist. Root interview HTML files are not product UI. |
| 7. Reminders | Not started | No SMTP settings, notification jobs, worker or local inbox exists in `settings.py` or `compose.yaml`. |
| 8. CSV/PDF reports | Not started | No review-report query, authorized export routes, formula-safe CSV or PDF generation exists. Benchmark reports are not business exports. |
| 9. Demo and walkthrough | Not started | `fixtures/catalog.jsonl` seeds assets/parts only. `scripts/demo.py` exercises the original single-plan executor, not named review stories. |
| 10. Operator resolution | Not started, optional | `domain.py:25-26,259-260` makes NEEDS_REVIEW terminal. The runner's earlier automatic reconciliation is not an authorized operator-resolution flow. |
| 11. CI quality gates | Implemented; independent review approved | Four lines add blocking Ruff lint/format steps after frozen dependency setup. Full local tests, Ruff and official actionlint validation passed. Remote GitHub Actions execution is not observed. |

## Fresh baseline

Observed on 2026-11-13 in the existing virtual environment:

- `.venv\Scripts\python.exe -m pytest -q`: **88 passed, 26 skipped in 6.14 seconds**.
- `.venv\Scripts\python.exe -m ruff check .`: **all checks passed**.
- `.venv\Scripts\python.exe -m ruff format --check .`: **68 files already formatted**.

The skipped tests require MongoDB, Redis or opt-in Compose. These results do not verify the proposed PostgreSQL implementation, human permissions, SMTP delivery, exports or browser flows.

## Initial Phase 1 blocker and correction

Initial PATH checks found no `uv`, `docker`, `podman`, `psql`, `pg_ctl` or `redis-server` command. No system-directory Docker Desktop, PostgreSQL, Podman or user uv installation was found. `wsl --list --quiet` listed no Linux distributions. No test PostgreSQL, Redis or MongoDB URL, or Docker host, was configured.

The approved Phase 1 plan requires real PostgreSQL migrations/grants/concurrency, Redis/process recovery, all 200 fault outcomes and an isolated Compose/restart check. Its existing ledger explicitly stops before implementation when these prerequisites are unavailable. Installing system software, starting a paid external environment or pushing a branch to obtain remote CI evidence is not inferred from the request.

Further inspection found Docker Desktop installed per-user at `%LOCALAPPDATA%\Programs\DockerDesktop`, outside PATH. WSL 2.7.3.0, kernel 6.6.114.1 and the hypervisor were already present. The earlier inference that Docker was absent was wrong. No reinstall was needed.

After the user said "go on", the existing Docker Desktop was started without automatic license acceptance or elevation. Docker Engine 29.9.3 and Compose v5.7.0 responded. A portable uv 0.13.6 archive was downloaded to the approved temp directory and its official SHA256 verified before execution. Neither global PATH nor registry/system installation was changed.

The approved Phase 1 Task 1 is now dispatched to provision fresh owned test services and implement migrations/grants/tests. Existing MongoDB data, simulator ledgers and volumes remain untouched. Core implementation remains incomplete until the required real-service checks pass.

## PostgreSQL Task 1 implementation and review

Task 1 is now complete and its saved checklist is synchronized. SQLAlchemy2.0.57, Alembic1.20.1 and Psycopg3.3.9 are pinned without changing existing package versions. Explicit migration0001 adds the bounded execution projection, constraints, audit table and separate runtime/migration role privileges. Startup checks schema state but does not migrate.

Fresh invocation-owned PostgreSQL17.12 and Redis7.2.7 containers run on loopback. The PostgreSQL image was started by observed official digest `postgres@sha256:846a7ddaa67a4b354ae1d21087b683901aa6113a4ed626d3787cfbf6c85e3e07`. No old deployment or data was imported, started, stopped or reset.

Initial independent review found two Important issues: a literal system actor could not retain worker identities, and a256KiB JSONB-text guard rejected valid compact snapshots because PostgreSQL expands its rendering. Both received witnessed real-database failing regressions and fixes. Scoped re-review approved the128-character machine actor and a conservative16MiB rendering defense, with the original256KiB complete Workflow budget unchanged.

Final Task1 evidence reports51 foundation tests passing, including50 actual PostgreSQL cases, full pytest140 passed/25 historical skips, fresh double Alembic upgrade/check and Ruff passing. The two nonblocking cleanup-recovery findings remain recorded for final review. These results do not establish the store adapter, transactional audit append, runtime cutover or complete Phase1 recovery acceptance.

Task2 is in progress. It includes a private numeric-preserving storage codec and an explicit shared validation rule rejecting actual NUL characters, which PostgreSQL cannot store in text/JSONB. No strings are silently stripped or reidentified, and no existing data is rewritten. This is a deliberate input-contract tightening; any NUL-bearing historical records would need a separate remediation/import policy.

## Contract differences to preserve

- The approved design selects same-origin server-rendered HTML, not the older prompt's suggested React SPA. Missing React is not a defect.
- The target brief and Prompt 8 still require both CSV and PDF. The design's detailed CSV discussion is not a delivered PDF implementation and must not silently remove that outcome.
- Direct API-key approval is current legacy behavior. It must be retired in the new relational deployment; its continued existence does not mean multi-level review is partially done.
- The user's previously recorded preference is to work on main, without automatic commits, pushes, deployment cutover or deletion. Existing `.gitignore` and design-file changes are unrelated work and are preserved.

## Subagent audit coverage

- Foundation/permissions: Prompts 0-3, `ses_e2c5a2faaffeZzWzYiEvrP7NgV`.
- Review application: Prompts 4-6, `ses_e2c5a2f97ffe2Jyl3mMi4jVje0`.
- Delivery/operations: Prompts 7-11, `ses_e2c5a2f95ffecux652Esq6kpL9`.

The audits were read-only. The coordinator ran the fresh baseline.

## Prompt 11 implementation and review

Only `.github/workflows/ci.yml` changed for this implementation. The existing unit job now runs `uv run ruff check .` and `uv run ruff format --check .` after its existing frozen development sync. Existing service jobs, test commands, benchmark conditions, permissions and triggers were preserved exactly.

The implementer observed the missing-gates assertion fail before the edit and pass afterward. Its post-change checks were Ruff lint passing, 69 files formatted, pytest 88 passed/26 skipped in 5.65 seconds, actionlint 1.7.12 passing and `git diff --check` passing. These checks used the existing virtualenv at that point, not a remote Actions runner. Actionlint was a checksum-verified official temporary binary, not a project dependency.

An independent reviewer approved both spec compliance and task quality with no findings. Reports are in the prompt pack's ignored workspace under `.superpowers/sdd/1794391971989-configurable-approval-workflow-implementation-prompts/`: `task-11-report.md`, `task-11-review.md`, and the scoped review package. Remote Actions remains pending an authorized future push/run.
