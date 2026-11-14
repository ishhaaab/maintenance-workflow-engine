# PostgreSQL foundation handoff

Phase 1 is implemented and independently approved. This is deliberately a health-only release, not the completed approval application. Local commits were authorized after review; no push, merge, import, old deployment operation or volume reset is included.

## Evidence

- Full suite with PostgreSQL, Redis, subprocesses, Compose and skip rejection: **543 passed, zero skips**, 683.67s.
- Offline units: **209 passed**. Ruff lint/format and whitespace checks passed.
- Fault acceptance: **200 matched outcomes**, including 150 automatic completions and 20 expected review outcomes. Independent inspection checked original keys, ordering and 170 ticket/140 reservation logical effects.
- Fresh migration upgrade/check passed. All 42 fault execution inputs remain byte-identical after deployment work.
- Exact fixture cleanup and label-verified Compose cleanup passed. The interrupted unregistered Task 2 allocation was deliberately untouched.
- Reviewer `ses_e26fbccccfferP158yx98DhZsF` approved with no Critical/Important findings. It independently checked fault records, unchanged inputs and narrow service-free probes, and inspected rather than reran the full service evidence.
- The controller completed the reviewer's unfinished five-line-context comparison: all 69 postimages match reviewed patch SHA256 `224f0520eb2f3e4a38a985a73dc1e5069499dd17b61d758597584f674c784194`.

Hosted Actions execution is unobserved. See [verification](../../verification.md) for artifacts and retained development failures.

## Prompt status

Completed: Prompt 0 design, Prompt 1 PostgreSQL foundation and optional Prompt 11 quality gates. Prompt 3 has execution-audit infrastructure but not its identity-aware authorized read interface. Prompts 2–9 and optional Prompt 10 are not complete. The approved foundation plan stops before Phase 2.

## Deferred minors

1. Native fresh-only bootstrap output failure can strand its own allocation. Persistent Compose uses the durable wrapper; the native weakness remains.
2. Test teardown cannot reliably resume partial role-drop failure. Ownership gates remain intact.
3. The static schema-warning event is redacted, reducing diagnostic clarity.

An exactly-full internal snapshot can lack operational headroom. Rejection rolls back safely and cannot starve healthy rows. Before user-facing admission, decide and test an explicit policy without silently increasing the 256 KiB budget.

## Rulings made

1. Work on existing `main` and ignore exactly the three unrelated interview files, as requested. Cost if wrong: remove those ignore entries without losing their contents.
2. Use a Markdown ledger instead of unavailable WSL lifecycle scripts. Cost if wrong: manual bookkeeping requires observed verification before completion entries.
3. A system actor is the actual bounded machine principal, not always literal `system`; preserve the 128-character worker identity. Cost if wrong: adjust the unreleased constraint, not a user deployment.
4. Preserve floats, signed zero, large integers and literal codec tags through a private reversible JSONB codec, without changing hashes/budget. Cost if wrong: adapter complexity and less literal raw JSONB inspection, not changed domain values.
5. Explicitly reject actual U+0000 in strings/keys consistently. Preserve JSON null, literal backslash-u0000 and other supported text. Cost if wrong: NUL-containing clients need an approved remediation policy; old records are untouched.
6. Extend preparation narrowly for durable started/canonical/split publication and verified reuse. Pre-canonical crashes require operator recovery, not silent allocation/rotation. Cost if wrong: manual recovery of fresh setup, never a committed-data reset.
7. Reuse the task baseline and keep private evidence after the usage limit rather than repeating implementation. The no-commit restriction remained until the user's later explicit authorization. Cost if wrong: extra local evidence to inspect, not lost work.

## Review boundaries adjudicated

- Later identity/review/UI/reminder/export/demo work remains outside Phase 1. Cost: the human workflow is not yet usable; no full-product claim is made.
- Import, cutover, historical integrity and orphan cleanup remain unauthorized. Cost: no migration/cleanup outcome is promised.
- Remote Actions, production throughput, model accuracy, external contracts and disaster recovery remain unverified. Cost: local simulator evidence cannot justify those claims.
- Persistence follows the JSON projection while supported numeric identities remain protected. Cost: arbitrary Python-only object identity is not guaranteed.
- No visual acceptance applies without a UI. Cost: later browser work needs its own acceptance.
- Exact-budget headroom is accepted only for the closed Phase 1 interface. Cost: pathological internal runs can remain unclaimable; this is not public-admission policy approval.

Private review history, transcripts and ownership evidence remain in the ignored plan workspace. Handoff documentation does not alter the approved executable source.
