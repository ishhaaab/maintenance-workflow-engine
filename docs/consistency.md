# Failure model and guarantees

MongoDB owns each workflow, compiled action keys, approval, retry deadlines and leases in one document. A semantic revision fences every domain update. A worker update also requires its current unexpired token, using MongoDB server time. Heartbeats and dispatch timestamps do not change the semantic revision. Saves update selected fields; they never replace stale snapshots.

Every due transition stores its deadline and advances its wake sequence atomically. The dispatcher marks enqueue intent before Redis publication. Publication loss recovers after five seconds. Redis messages are notifications, not action arguments. Workers reread MongoDB eligibility, so duplicate and stale messages are harmless. Redis deletion does not delete accepted work.

Before an HTTP mutation, the runner persists an IN_FLIGHT intent and increments the submission count. A crash after that point is ambiguous, even when the request may never have left the process. Recovery looks up the same persisted idempotency key. Only an adapter with durable same-key replay may resubmit after NOT_FOUND. NOT_FOUND does not exclude a late commit.

The local SQLite simulators atomically commit effects and indefinite idempotency ledgers. This controlled contract prevents duplicate effects across overlapping workers; a MongoDB lease alone cannot do that. Effect counts use a logical workflow/action ID independent of the idempotency key, so changing a key cannot hide a duplicate in benchmarks.

Unknown results, malformed success bodies and unclassified 5xx responses require reconciliation. Five submissions and five lookups per ambiguous episode are hard limits. Final unresolved ambiguity is NEEDS_REVIEW, including when a later known-no-effect rejection cannot exclude an earlier ambiguous submission. Key/payload conflict also stops for review.

Cancellation prevents remaining work only when no current lease or uncertain/in-flight action exists. An action waiting for same-key replay still has an unresolved earlier submission, even though its scheduling status is PENDING; cancellation rejects it until the outcome is confirmed. It never rolls back a ticket or stock reservation. A FAILED or CANCELLED workflow can retain a confirmed ticket. Stock shortage is a stored SKIPPED outcome and satisfies the conditional request.

MongoDB uses its default acknowledged journaled storage deployment here. This is a local single-node deployment, not protection against permanent disk loss. Backups, replicated disaster recovery, real integration contracts and production identity are outside the portfolio scope. No end-to-end exactly-once claim is made.
