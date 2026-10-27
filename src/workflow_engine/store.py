"""Single-document authority. Redis and external systems never participate in these writes."""
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from bson import BSON
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .domain import (
    MAX_DOCUMENT_BYTES, PASSIVE, Conflict, Lease, LeaseLost, Model, NotFound,
    Wakeup, Workflow, utcnow,
)

_IMMUTABLE = {"workflow_id", "tenant_id", "admission_key", "request_hash", "request_text", "schema_version", "created_at"}
_OPERATIONAL = {"lease_owner", "lease_token", "lease_until", "last_enqueued_at"}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class AdmissionResult(Model):
    workflow: Workflow
    created: bool


class WorkflowStore(Protocol):
    async def insert_or_get(self, workflow: Workflow) -> AdmissionResult: ...
    async def get(self, workflow_id: str, tenant_id: str) -> Workflow: ...
    async def claim(self, workflow_id: str, worker_id: str, lease_s: int = 30) -> Lease | None: ...
    async def save(self, workflow: Workflow, expected_revision: int, lease_token: str | None) -> Workflow: ...
    async def renew(self, lease: Lease, lease_s: int = 30) -> bool: ...
    async def release(self, lease: Lease) -> None: ...
    async def due_wakeups(self, limit: int = 100, republish_after_s: int = 5) -> list[Wakeup]: ...


def _validate_change(old: Workflow, new: Workflow, revision: int):
    if old.revision != revision or new.revision != revision + 1:
        raise Conflict("Revision conflict")
    if any(getattr(old, key) != getattr(new, key) for key in _IMMUTABLE):
        raise Conflict("Immutable admission field")
    if old.plan is not None:
        if any(getattr(old, key) != getattr(new, key) for key in ("plan", "plan_hash", "catalog_version")):
            raise Conflict("Immutable validated plan")
        if len(old.actions) != len(new.actions):
            raise Conflict("Immutable action list")
    if old.approved_plan_hash and (old.approved_plan_hash != new.approved_plan_hash or old.approved_at != new.approved_at):
        raise Conflict("Immutable approval")
    for before, after in zip(old.actions, new.actions):
        if (before.action_id, before.arguments, before.idempotency_key) != (after.action_id, after.arguments, after.idempotency_key):
            raise Conflict("Immutable action identity")
        if before.status in {"CONFIRMED", "SKIPPED"} and before != after:
            raise Conflict("Immutable confirmed outcome")
    validated = Workflow.model_validate(new.model_dump())
    if len(BSON.encode(validated.model_dump())) > MAX_DOCUMENT_BYTES:
        raise Conflict("Workflow exceeds BSON budget")


def _lease(workflow: Workflow) -> Lease:
    return Lease(workflow_id=workflow.workflow_id, tenant_id=workflow.tenant_id,
                 token=workflow.lease_token, owner=workflow.lease_owner,
                 expiry=workflow.lease_until, workflow=workflow)


def _live(workflow: Workflow, now: datetime) -> bool:
    return workflow.lease_until is not None and workflow.lease_until > now


def _due(workflow: Workflow, now: datetime) -> bool:
    return workflow.state not in PASSIVE and workflow.next_run_at is not None and workflow.next_run_at <= now and not _live(workflow, now)


def _delay(workflow: Workflow) -> int:
    return max(0, round((workflow.next_run_at - workflow.updated_at).total_seconds() * 1000)) if workflow.next_run_at else 0


class MemoryWorkflowStore:
    """Contract fake with an injected clock, not evidence of MongoDB durability."""
    def __init__(self, clock=utcnow):
        self._clock = clock
        self._documents: dict[str, Workflow] = {}
        self._admissions: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def ping(self):
        return True

    async def insert_or_get(self, workflow: Workflow) -> AdmissionResult:
        async with self._lock:
            key = (workflow.tenant_id, workflow.admission_key)
            if key in self._admissions:
                existing = self._documents[self._admissions[key]]
                if existing.request_hash != workflow.request_hash:
                    raise Conflict("Admission payload mismatch")
                return AdmissionResult(workflow=existing.model_copy(deep=True), created=False)
            now = self._clock()
            workflow = workflow.model_copy(update={"created_at": now, "updated_at": now, "next_run_at": now}, deep=True)
            self._admissions[key] = workflow.workflow_id
            self._documents[workflow.workflow_id] = workflow
            return AdmissionResult(workflow=workflow.model_copy(deep=True), created=True)

    async def get(self, workflow_id, tenant_id):
        workflow = self._documents.get(workflow_id)
        if not workflow or workflow.tenant_id != tenant_id:
            raise NotFound("Workflow not found")
        return workflow.model_copy(deep=True)

    async def claim(self, workflow_id, worker_id, lease_s=30):
        async with self._lock:
            w = self._documents.get(workflow_id)
            now = self._clock()
            if w is None or not _due(w, now):
                return None
            w = w.model_copy(update={"lease_owner": worker_id, "lease_token": uuid4().hex, "lease_until": now + timedelta(seconds=lease_s)}, deep=True)
            self._documents[workflow_id] = w
            return _lease(w.model_copy(deep=True))

    async def save(self, workflow, expected_revision, lease_token):
        async with self._lock:
            old = self._documents[workflow.workflow_id]
            now = self._clock()
            if lease_token:
                if old.lease_token != lease_token or not _live(old, now):
                    raise LeaseLost("Lease lost")
            elif _live(old, now):
                raise Conflict("Active worker lease")
            _validate_change(old, workflow, expected_revision)
            data = workflow.model_dump()
            data.update({key: getattr(old, key) for key in _OPERATIONAL})
            data.update(updated_at=now, next_run_at=now + timedelta(milliseconds=_delay(workflow)) if workflow.next_run_at else None)
            data["wake_seq"] = old.wake_seq + int(workflow.next_run_at is not None)
            saved = Workflow.model_validate(data)
            self._documents[workflow.workflow_id] = saved
            return saved.model_copy(deep=True)

    async def renew(self, lease, lease_s=30):
        async with self._lock:
            w = self._documents.get(lease.workflow_id)
            now = self._clock()
            if not w or w.lease_token != lease.token or not _live(w, now):
                return False
            self._documents[w.workflow_id] = w.model_copy(update={"lease_until": now + timedelta(seconds=lease_s)})
            return True

    async def release(self, lease):
        async with self._lock:
            w = self._documents.get(lease.workflow_id)
            if w and w.lease_token == lease.token:
                self._documents[w.workflow_id] = w.model_copy(update={"lease_owner": None, "lease_token": None, "lease_until": None})

    async def due_wakeups(self, limit=100, republish_after_s=5):
        async with self._lock:
            now = self._clock()
            wakeups = []
            for w in self._documents.values():
                if _due(w, now) and (not w.last_enqueued_at or w.last_enqueued_at <= now - timedelta(seconds=republish_after_s)):
                    self._documents[w.workflow_id] = w.model_copy(update={"last_enqueued_at": now})
                    wakeups.append(Wakeup(workflow_id=w.workflow_id, wake_seq=w.wake_seq))
                    if len(wakeups) == limit:
                        break
            return wakeups


def _free_expression():
    return {"$lte": [{"$ifNull": ["$lease_until", _EPOCH]}, "$$NOW"]}


def _due_query():
    return {"state": {"$nin": sorted(PASSIVE)}, "next_run_at": {"$ne": None}, "$expr": {"$and": [
        {"$lte": ["$next_run_at", "$$NOW"]}, _free_expression(),
    ]}}


def _decode(document):
    return Workflow.model_validate({key: value for key, value in document.items() if key != "_id"})


class MongoWorkflowStore:
    def __init__(self, database):
        self.database = database
        self.collection = database.workflows

    async def initialize(self):
        await self.collection.create_index([("tenant_id", 1), ("admission_key", 1)], unique=True)
        await self.collection.create_index("workflow_id", unique=True)
        await self.collection.create_index([("state", 1), ("next_run_at", 1), ("lease_until", 1)])
        await self.collection.create_index([("tenant_id", 1), ("created_at", 1)])

    async def ping(self):
        await self.database.command("ping")
        return True

    async def insert_or_get(self, workflow):
        query = {"tenant_id": workflow.tenant_id, "admission_key": workflow.admission_key}
        # Conditional pipeline upsert timestamps new admissions using MongoDB time.
        fields = {key: {"$cond": [{"$eq": [{"$type": "$workflow_id"}, "missing"]},
                                  "$$NOW" if key in {"created_at", "updated_at", "next_run_at"} else {"$literal": value},
                                  "$" + key]} for key, value in workflow.model_dump().items()}
        try:
            document = await self.collection.find_one_and_update(query, [{"$set": fields}], upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            document = await self.collection.find_one(query)
        existing = _decode(document)
        if existing.request_hash != workflow.request_hash:
            raise Conflict("Admission payload mismatch")
        return AdmissionResult(workflow=existing, created=existing.workflow_id == workflow.workflow_id)

    async def get(self, workflow_id, tenant_id):
        document = await self.collection.find_one({"workflow_id": workflow_id, "tenant_id": tenant_id})
        if document is None:
            raise NotFound("Workflow not found")
        return _decode(document)

    async def claim(self, workflow_id, worker_id, lease_s=30):
        query = {**_due_query(), "workflow_id": workflow_id}
        document = await self.collection.find_one_and_update(query, [{"$set": {
            "lease_owner": {"$literal": worker_id}, "lease_token": uuid4().hex,
            "lease_until": {"$dateAdd": {"startDate": "$$NOW", "unit": "second", "amount": lease_s}},
        }}], return_document=ReturnDocument.AFTER)
        return _lease(_decode(document)) if document else None

    async def save(self, workflow, expected_revision, lease_token):
        old = await self.get(workflow.workflow_id, workflow.tenant_id)
        if lease_token and old.lease_token != lease_token:
            raise LeaseLost("Lease lost")
        _validate_change(old, workflow, expected_revision)
        query = {"workflow_id": workflow.workflow_id, "tenant_id": workflow.tenant_id, "revision": expected_revision}
        if lease_token:
            query.update(lease_token=lease_token, **{"$expr": {"$gt": ["$lease_until", "$$NOW"]}})
        else:
            query["$expr"] = _free_expression()
        fields = {key: {"$literal": value} for key, value in workflow.model_dump().items() if key not in _IMMUTABLE | _OPERATIONAL}
        fields.update(updated_at="$$NOW", next_run_at={"$dateAdd": {"startDate": "$$NOW", "unit": "millisecond", "amount": _delay(workflow)}} if workflow.next_run_at else None)
        fields["wake_seq"] = {"$add": ["$wake_seq", int(workflow.next_run_at is not None)]}
        document = await self.collection.find_one_and_update(query, [{"$set": fields}], return_document=ReturnDocument.AFTER)
        if document is None:
            if lease_token:
                # A revision conflict is still a conflict; callers stop the tick either way.
                raise LeaseLost("Lease or revision changed")
            raise Conflict("Revision or active lease conflict")
        return _decode(document)

    async def renew(self, lease, lease_s=30):
        result = await self.collection.update_one({"workflow_id": lease.workflow_id, "lease_token": lease.token, "$expr": {"$gt": ["$lease_until", "$$NOW"]}}, [{"$set": {
            "lease_until": {"$dateAdd": {"startDate": "$$NOW", "unit": "second", "amount": lease_s}},
        }}])
        return result.matched_count == 1

    async def release(self, lease):
        await self.collection.update_one({"workflow_id": lease.workflow_id, "lease_token": lease.token}, {"$set": {"lease_owner": None, "lease_token": None, "lease_until": None}})

    async def due_wakeups(self, limit=100, republish_after_s=5):
        query = _due_query()
        query["$expr"]["$and"].append({"$lte": [
            {"$ifNull": ["$last_enqueued_at", _EPOCH]},
            {"$dateSubtract": {"startDate": "$$NOW", "unit": "second", "amount": republish_after_s}},
        ]})
        result = []
        for _ in range(limit):
            document = await self.collection.find_one_and_update(query, [{"$set": {"last_enqueued_at": "$$NOW"}}], sort=[("next_run_at", 1)], return_document=ReturnDocument.AFTER, projection={"workflow_id": 1, "wake_seq": 1})
            if not document:
                break
            result.append(Wakeup(workflow_id=document["workflow_id"], wake_seq=document["wake_seq"]))
        return result
