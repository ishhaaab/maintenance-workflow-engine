# Review walkthrough

Use the fresh setup and one-time seed output in [the runbook](review-runbook.md). App: http://127.0.0.1:18000. Captured inbox: http://127.0.0.1:18025. Use separate browser profiles/private windows for each role, not tabs sharing the same session cookie. Passwords are the generated values for that particular deployment.

## Start a new review

1. Sign in as tenant `tenant-a`, login `tenant-a.admin`. On **Cycles**, enter `Walkthrough maintenance` in **Cycle title**, then click **Create cycle**.
2. Select `tenant-a.requester` in **Requester account**, then click **Admit requester**. Keep the cycle URL.
3. In the requester window, sign in as `tenant-a.requester` for `tenant-a`, open that URL and click **Create item**. Copy the item URL to the administrator window.
4. As administrator, open the item. Assign level 1 to `tenant-a.reviewer-1` and level 2 to `tenant-a.reviewer-2`. For this new demo item only, set level 1's duration to `1` second and level 2 to `3600`. Click **Save approval chain**. These are not global deadline changes.
5. Reload as requester. In **Maintenance request**, enter:

   ```text
   Create an inspection ticket for pump P-17 and reserve one seal kit if available.
   ```

   Click **Submit version**. Wait for **Validated maintenance plan**; reload if the background planner has not finished. Planning is read-only and no external effect is authorized yet.
6. As administrator, return to the cycle and click **Activate cycle**. It becomes ACTIVE; item membership and approval definitions are frozen. Level one has a UTC deadline. Being overdue does not auto-approve it.

## Capture mail, request changes and restart the chain

7. Wait for the notification worker, then inspect Mailpit. A message titled **Maintenance review is overdue** goes to `tenant-a.reviewer-1@workflow.invalid`. It links to the authenticated item without copying request content. ADMIN can see reminder state and attempt counts on the item.
8. Sign in in distinct windows as `tenant-a.reviewer-1` and `tenant-a.reviewer-2`, both tenant `tenant-a`. Open the item. Only reviewer one can currently approve. Reviewer two has no decision controls yet.
9. Reviewer one clicks **Approve**. Reviewer two reloads, enters `Confirm the maintenance window.` in **Decision comment**, then clicks **Request changes**. The cycle stays ACTIVE. Execution is not authorized.
10. The requester reloads, reads the earlier decisions and clicks **Submit version** with the safe fixture request. A new immutable version is planned; the prior version remains visible as superseded. No earlier approval transfers.
11. Reviewer two reloads and still cannot decide before level one. Reviewer one opens the item and clicks **Approve** again. Reviewer two reloads and clicks **Approve**.
12. The cycle becomes COMPLETED when every item's current review is approved or rejected. The execution section progresses separately from READY to SUCCEEDED through the two workers. A permitted conditional inventory skip would also satisfy execution; approval alone is never shown as successful effects.

## Download reports and check isolation

13. As administrator, open the cycle and click **Download CSV** and **Download PDF**. Open both. Expect both versions, two reviewer levels each, old REQUEST_CHANGES/APPROVE facts and the new approved result, UTC times, plan hashes and separate execution outcomes. Each file records its own consistent snapshot; files requested at different times need not be byte-identical.
14. A reviewer/requester requesting that cycle's export URL gets 403. In a fifth isolated profile, sign in as tenant `tenant-b`, login `tenant-b.admin`. The tenant-a cycle, item and report URLs must return 404, not disclose their details.
15. Rerun the seed command. It returns `created=false` with no credentials. This new cycle, sessions, passwords and decisions remain unchanged. Restart application processes:

    ```sh
    docker compose -f compose.postgres.yaml -p review-demo restart api worker-a worker-b notifications
    ```

    Wait for readiness and reload. The existing session still works unless it expired, and the cycle remains COMPLETED. Do not remove volumes or reset simulator ledgers.

The other seven seeded story cycles per tenant can be inspected independently. **Rejected request** is final; **Changes and resubmission** already has a newer version; **Completed approvals** separates review completion from executor status. Seed facts are labeled synthetic system activity.

## What was actually exercised

`tests/e2e/test_review_walkthrough.py::test_fresh_seeded_compose_walkthrough_browser_smtp_exports_restart` ran against a new disposable project on 2026-11-17 and passed in 85.10 seconds, zero skips. It used the production image, real PostgreSQL/Redis, Mailpit, Chromium, two workers and the exact sequence above. Ports were dynamically isolated with a matching explicit origin. Test cleanup removed only that project's label-verified resources. Representative reports were retained outside the repository and opened; credentials were not saved to artifacts.

The final post-review rerun also passed, alongside the broader clean-start/restart Compose test: two tests, zero skips in 326.20 seconds. The walkthrough itself took 58.50 seconds. Its final reports contain four CSV rows across two versions and a two-page PDF, with complete associated decision/execution/reminder audit. Safe artifacts are under `artifacts/review-compose-final-20261117-03/`.

This is a local acceptance result, not a real-SMTP deliverability test, production deployment, throughput measurement or proof of exactly-once delivery. Full regression and fault-matrix results are recorded separately in [acceptance](review-acceptance.md).
