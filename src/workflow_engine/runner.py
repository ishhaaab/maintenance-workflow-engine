"""One leased tick, one external mutation at most. No orchestration in HTTP handlers."""

import asyncio
import logging

from pydantic import ValidationError

from .domain import (
    MAX_LOOKUPS,
    MAX_PLANNER_CALLS,
    MAX_SUBMISSIONS,
    PASSIVE,
    RETRY_DELAYS,
    DomainEvent,
    LeaseLost,
    NeedsInput,
    TickResult,
    WorkflowCreate,
    canonical_hash,
    transition,
)

log = logging.getLogger(__name__)


class Runner:
    def __init__(
        self, store, planner, catalog, gateways, lease_s=30, heartbeat_s=5, planner_timeout_s=15
    ):
        self.store, self.planner, self.catalog, self.gateways = store, planner, catalog, gateways
        self.lease_s, self.heartbeat_s = lease_s, heartbeat_s
        self.planner_timeout_s = planner_timeout_s

    async def _heartbeat(self, lease, stop, lost):
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_s)
            except TimeoutError:
                try:
                    if not await self.store.renew(lease, self.lease_s):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return

    async def _save(self, workflow, event, lease, lost):
        if lost.is_set():
            raise LeaseLost("Heartbeat lost")
        saved = await self.store.save(transition(workflow, event), workflow.revision, lease.token)
        fields = {"workflow_id": saved.workflow_id, "state": saved.state}
        if event.action_id:
            action = next(a for a in saved.actions if a.action_id == event.action_id)
            fields.update(action_id=action.action_id, attempt=action.submissions)
        log.info("workflow_transition", extra=fields)
        return saved

    async def tick(self, workflow_id: str, worker_id: str) -> TickResult:
        lease = await self.store.claim(workflow_id, worker_id, self.lease_s)
        if lease is None:
            return TickResult(status="not_claimed")
        stop, lost = asyncio.Event(), asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(lease, stop, lost))
        try:
            workflow = await self._step(lease.workflow, lease, lost)
            return TickResult(status="finished" if workflow.state in PASSIVE else "progressed")
        except LeaseLost:
            return TickResult(status="lease_lost")
        finally:
            stop.set()
            await heartbeat
            try:
                await self.store.release(lease)
            except Exception:
                # The lease expires without a release. Never report a false durable result.
                log.warning("lease_release_unavailable", extra={"workflow_id": workflow_id})

    async def _step(self, w, lease, lost):
        from .policy import validate_and_compile

        if w.state == "RETRY_WAIT":
            return await self._save(w, DomainEvent(kind="retry_due"), lease, lost)
        if w.state == "PENDING_PLAN":
            if w.planning_attempts >= MAX_PLANNER_CALLS:
                return await self._save(
                    w, DomainEvent(kind="failed", error="planner_budget_exhausted"), lease, lost
                )
            w = await self._save(
                w, DomainEvent(kind="planning_started", metadata=self.planner.metadata), lease, lost
            )
            request = WorkflowCreate(text=w.request_text)
            catalog = self.catalog.get(w.tenant_id)
            try:
                async with asyncio.timeout(self.planner_timeout_s):
                    proposal = await self.planner.propose(request, catalog)
                compiled = validate_and_compile(
                    request, proposal, catalog, w.workflow_id, w.tenant_id
                )
            except (ValidationError, ValueError):
                compiled = NeedsInput(
                    questions=["Please clarify the asset, part and requested action."]
                )
            except (TimeoutError, ConnectionError):
                event = (
                    DomainEvent(
                        kind="retry", retry_phase="planning", delay_s=1, error="planner_unavailable"
                    )
                    if w.planning_attempts < MAX_PLANNER_CALLS
                    else DomainEvent(kind="failed", error="planner_budget_exhausted")
                )
                return await self._save(w, event, lease, lost)
            if isinstance(compiled, NeedsInput):
                event = DomainEvent(kind="clarification", clarification=compiled)
            else:
                event = DomainEvent(kind="planned", **compiled.model_dump())
            return await self._save(w, event, lease, lost)
        if w.state == "RUNNING":
            action = next(a for a in w.actions if a.status in {"IN_FLIGHT", "UNKNOWN"})
            return await self._save(
                w,
                DomainEvent(
                    kind="ambiguous", action_id=action.action_id, error="abandoned_inflight"
                ),
                lease,
                lost,
            )
        if w.state == "RECONCILING":
            return await self._reconcile(w, lease, lost)
        if w.state == "READY":
            action = next(a for a in w.actions if a.status == "PENDING")
            w = await self._save(
                w, DomainEvent(kind="action_started", action_id=action.action_id), lease, lost
            )
            # Recheck immediately before HTTP; fencing cannot cancel a request already sent.
            if lost.is_set() or not await self.store.renew(lease, self.lease_s):
                raise LeaseLost("Lease lost before submission")
            arguments = {
                **action.arguments,
                "logical_action_id": canonical_hash([w.tenant_id, w.workflow_id, action.action_id]),
            }
            if action.action_id == "reservation":
                arguments["ticket_id"] = w.actions[0].result["id"]
            outcome = await self.gateways[action.action_id].submit(
                action.model_copy(update={"arguments": arguments})
            )
            if outcome.status in {"confirmed", "skipped"}:
                event = DomainEvent(
                    kind="action_confirmed" if outcome.status == "confirmed" else "skipped",
                    action_id=action.action_id,
                    result=outcome.result,
                )
            elif outcome.status in {"declined", "retryable"}:
                current = next(a for a in w.actions if a.action_id == action.action_id)
                if current.has_ambiguous_submission:
                    event = DomainEvent(
                        kind="ambiguous",
                        action_id=action.action_id,
                        error="prior_submission_unresolved",
                    )
                elif outcome.status == "retryable" and current.submissions < MAX_SUBMISSIONS:
                    event = DomainEvent(
                        kind="retry",
                        action_id=action.action_id,
                        retry_phase="action",
                        delay_s=RETRY_DELAYS[current.submissions - 1],
                        error=outcome.error,
                    )
                else:
                    event = DomainEvent(
                        kind="failed", action_id=action.action_id, error=outcome.error
                    )
            else:
                event = DomainEvent(
                    kind="ambiguous", action_id=action.action_id, error=outcome.error
                )
            return await self._save(w, event, lease, lost)
        return w

    async def _reconcile(self, w, lease, lost):
        action = next(a for a in w.actions if a.status == "UNKNOWN")
        gateway = self.gateways[action.action_id]
        if action.last_error == "key_conflict" or action.reconciliation_count >= MAX_LOOKUPS:
            return await self._save(
                w,
                DomainEvent(
                    kind="review_required", action_id=action.action_id, error="unresolved_outcome"
                ),
                lease,
                lost,
            )
        delay = RETRY_DELAYS[min(action.reconciliation_count, len(RETRY_DELAYS) - 1)]
        w = await self._save(
            w,
            DomainEvent(kind="lookup_started", action_id=action.action_id, delay_s=delay),
            lease,
            lost,
        )
        action = next(a for a in w.actions if a.action_id == action.action_id)
        lookup = await gateway.lookup(action)
        if lookup.status == "confirmed":
            outcome = lookup.outcome
            event = DomainEvent(
                kind="action_confirmed" if outcome.status == "confirmed" else "skipped",
                action_id=action.action_id,
                result=outcome.result,
            )
        elif (
            lookup.status == "not_found"
            and gateway.supports_idempotent_replay
            and action.submissions < MAX_SUBMISSIONS
        ):
            event = DomainEvent(
                kind="retry",
                action_id=action.action_id,
                retry_phase="action",
                delay_s=RETRY_DELAYS[action.submissions - 1],
                error="safe_same_key_replay",
            )
        elif lookup.status == "not_found" or action.reconciliation_count >= MAX_LOOKUPS:
            event = DomainEvent(
                kind="review_required", action_id=action.action_id, error="unresolved_outcome"
            )
        else:
            return w  # lookup_started already persisted a bounded next lookup deadline.
        return await self._save(w, event, lease, lost)
