# Core approval release review

Review scope was the then-uncommitted working tree against `b90567beaeda4deddaea9e0f252b822805904d0e`, including new files, not an empty HEAD-to-HEAD diff. The verified implementation was subsequently packaged into logical local commits. One independent read-only reviewer inspected the implementation, approved design, prompt pack, tests, migrations and operations docs. No sub-review or service-suite run was delegated. Final acceptance evidence is recorded below.

## Findings and disposition

The reviewer found no confirmed Critical issue. Its Important findings were:

1. CSV lost decision/run audit associations and the report query excluded reminder identities. The fix selects related job identities, associates cycle/item/version/decision/run/job events explicitly, and serializes references through an indexed lookup. Empty cycles retain their own audit context.
2. Disabled current reviewers permanently cancelled their reminder. The fix distinguishes a temporarily unavailable recipient from obsolete work, defers the same unique job for 60 seconds without spending an SMTP attempt, and resumes after re-enable.
3. Failed schema-head startup verification left business routes open. A per-request schema gate now closes business pages/commands with 503, retaining health access. The regression changes the actual isolated PostgreSQL revision record, not a substituted storage adapter.

The reviewer labeled these two findings Minor. The implementer regraded them Important by their effect on the accepted report contract and included them in the same TDD fix pass:

4. Configured unsubmitted items omitted their assignments. Reports now read draft approval definitions for those rows, retaining an empty version identity and UNSUBMITTED status.
5. CSV generated a complete oversized file before checking its cap. The byte sink enforces the 10 MiB budget incrementally, including UTF-8/BOM bytes, and stops before processing the remaining rows.

Five focused reproductions failed against the prior code before implementation. Subsequent targeted runs exercised the changes, with no remaining assertion failure after updating the intentional empty-cycle CSV summary-row contract. Windows host TCP exhaustion interrupted larger verification batches; those failed attempts remain documented, not counted as acceptance. The subsequent Linux-container full suite verified the fixes. No second review was commissioned.

## Other regression fixes

The serial whole suite also exposed execution failure incorrectly trying to reseal a plan, missing authentication returning 403 before 401, and an old test fixture that had not provisioned tenants `a` and `b`. Focused production regressions returned RED→GREEN and the four-test rerun passed. No old effect-count, key, lease or uncertainty assertion was weakened. The in-process CLI bootstrap test now restores its logging handlers, so its captured stream cannot poison later tests.

## Boundaries

The reviewer declined optional uncertain-execution resolution, password reset/SSO, production import/cutover, external SMTP deliverability and throughput certification. These exclusions match the approved core scope and remain explicit non-goals. No operator override, reset flow or production-delivery guarantee was added. No independent visual approval was issued.

Post-fix verification passed 610 Linux tests plus both fresh-Compose tests, 612 total, with zero failures/errors/skips. This includes the five reproducing review regressions and the separate production regressions above. The independent review was not rerun; the author verified its fix pass through tests as planned. Fresh Alembic upgrade/check and exact runtime-role/cleanup verification also passed. The ten-seed fault matrix passed all 200 observations in 890.81 seconds; independent standard-library validation confirmed coverage, original keys, expected outcomes/effects and all 200 cleanup records. A 150-file postimage comparison matched the tested implementation to the checkout. No review finding remains deferred. See [review acceptance](../../review-acceptance.md) for exact commands and evidence. No push, merge or PR was performed; README and historical reports remain unchanged. The feature branch and its logical local commits are preserved for the user's next decision.
