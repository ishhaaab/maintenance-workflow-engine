"""Shared rule assertions. Only the PostgreSQL fixture proves database concurrency."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from tests.postgres_support import seed_approved_run
from workflow_engine.domain import (
    MAX_DOCUMENT_BYTES,
    Action,
    Conflict,
    DomainEvent,
    ExecutablePlan,
    NotFound,
    ReservationSpec,
    TicketSpec,
    Workflow,
    WorkflowCreate,
    canonical_hash,
    transition,
)
from workflow_engine.store import MemoryWorkflowStore


def candidate(key="key", text="Inspect P-17", tenant="tenant-a"):
    return Workflow.new(tenant, key, WorkflowCreate(text=text))


async def sealed(store, values=None):
    workflow = (await store.insert_or_get(candidate())).workflow
    plan = ExecutablePlan(
        asset_id="P-17",
        ticket=TicketSpec(description="Inspection"),
        reservation=ReservationSpec(part_id="seal", quantity=1),
    )
    proposed = transition(
        workflow,
        DomainEvent(
            kind="planned",
            at=workflow.updated_at,
            plan=plan,
            plan_hash=canonical_hash(plan.model_dump()),
            catalog_version="v1",
            actions=[
                Action(
                    action_id="ticket",
                    arguments=values if values is not None else {"value": -0.0},
                    idempotency_key="ticket-key",
                ),
                Action(
                    action_id="reservation", arguments={"quantity": 1}, idempotency_key="part-key"
                ),
            ],
        ),
    )
    return await store.save(proposed, workflow.revision, None)


async def completed(store, values=None, skipped_result=None):
    workflow = await sealed(store, values)
    workflow = await seed_approved_run(store, workflow, workflow.revision, workflow.plan_hash)
    for kind, action_id, result in [
        ("action_started", "ticket", None),
        (
            "action_confirmed",
            "ticket",
            {"id": "t1", "wire": values} if values is not None else {"id": "t1", "value": -0.0},
        ),
        ("action_started", "reservation", None),
        (
            "skipped",
            "reservation",
            skipped_result if skipped_result is not None else {"reason": "unavailable"},
        ),
    ]:
        proposed = transition(
            workflow,
            DomainEvent(
                kind=kind,
                action_id=action_id,
                result=result,
                at=workflow.updated_at,
            ),
        )
        workflow = await store.save(proposed, workflow.revision, None)
    return workflow


IMMUTABLE_CASES = [
    "workflow_id",
    "tenant_id",
    "admission_key",
    "request_hash",
    "request_text",
    "schema_version",
    "created_at",
    "plan",
    "plan_hash",
    "catalog_version",
    "approved_plan_hash",
    "approved_at",
    "action_key",
    "arguments",
    "argument_zero_sign",
    "argument_numeric_type",
    "action_order",
    "action_count",
    "confirmed_result",
    "confirmed_status",
    "confirmed_counter",
    "confirmed_zero_sign",
    "skipped_result",
]


class StoreContract:
    @pytest.mark.parametrize("mutation", ["identity", "approval"])
    async def test_rich_snapshot_fixture_cannot_rewrite_identity_or_erase_approval(
        self, store, mutation
    ):
        from tests.postgres_support import prepare_snapshot

        original = await sealed(store)
        if mutation == "approval":
            original = await seed_approved_run(
                store, original, original.revision, original.plan_hash
            )
            changes = {"approved_plan_hash": None, "approved_at": None}
        else:
            changes = {"admission_key": "rewritten"}
        with pytest.raises(Conflict):
            await prepare_snapshot(store, original.model_copy(update=changes))
        assert await store.get(original.workflow_id, original.tenant_id) == original

    @pytest.mark.parametrize("worker", [False, True], ids=["unleased", "worker"])
    @pytest.mark.parametrize(
        "proposal", ["domain_approval", "forged_ready", "hash_only", "time_only"]
    )
    async def test_general_save_cannot_introduce_approval(self, store, worker, proposal):
        original = await sealed(store)
        lease = None
        if worker:
            # A worker can already hold the lease while it seals the plan.
            from tests.postgres_support import prepare_snapshot

            original = await prepare_snapshot(
                store,
                original.model_copy(
                    update={"next_run_at": original.updated_at, "state": "PENDING_PLAN"}
                ),
            )
            lease = await store.claim(original.workflow_id, "forged-approver")
            assert lease is not None
            original = lease.workflow
        if proposal == "domain_approval":
            # Keep pure approval legal for the later transaction-owned review bridge.
            source = original.model_copy(update={"state": "AWAITING_APPROVAL", "next_run_at": None})
            proposed = transition(source, DomainEvent(kind="approved", plan_hash=source.plan_hash))
        else:
            changes = {"revision": original.revision + 1}
            if proposal in {"forged_ready", "hash_only"}:
                changes["approved_plan_hash"] = original.plan_hash
            if proposal in {"forged_ready", "time_only"}:
                changes["approved_at"] = original.updated_at
            if proposal == "forged_ready":
                changes.update(state="READY", next_run_at=original.updated_at)
            proposed = original.model_copy(update=changes, deep=True)
        with pytest.raises(Conflict):
            await store.save(proposed, original.revision, lease.token if lease else None)
        assert await store.get(original.workflow_id, original.tenant_id) == original

    @pytest.mark.parametrize("state", ["READY", "AWAITING_APPROVAL", "RUNNING", "RETRY_WAIT"])
    async def test_admission_cannot_insert_an_already_approved_workflow(self, store, state):
        template = await sealed(store)
        approved = transition(template, DomainEvent(kind="approved", plan_hash=template.plan_hash))
        duplicate = approved.model_copy(
            update={
                "workflow_id": str(uuid4()),
                "admission_key": "forged",
                "state": state,
                "next_run_at": None if state == "AWAITING_APPROVAL" else approved.updated_at,
            },
            deep=True,
        )
        with pytest.raises(Conflict):
            await store.insert_or_get(duplicate)
        with pytest.raises(NotFound):
            await store.get(duplicate.workflow_id, duplicate.tenant_id)

    @pytest.mark.parametrize(
        "changes",
        [
            {"state": "READY"},
            {"state": "RUNNING"},
            {"state": "RECONCILING"},
            {"state": "RETRY_WAIT", "retry_phase": "action"},
            {"approved_at": "2026-01-01T00:00:00Z"},
        ],
    )
    async def test_admission_accepts_only_unapproved_planning_work(self, store, changes):
        proposed = candidate().model_copy(update=changes, deep=True)
        with pytest.raises(Conflict):
            await store.insert_or_get(proposed)
        with pytest.raises(NotFound):
            await store.get(proposed.workflow_id, proposed.tenant_id)

    @pytest.mark.parametrize("phase", ["arguments", "confirmed_result", "skipped_result"])
    @pytest.mark.parametrize(
        ("before", "after", "wire_before", "wire_after"),
        [
            ({"nested": (-0.0,)}, {"nested": (0.0,)}, {"nested": [-0.0]}, {"nested": [0.0]}),
            ({1: "same"}, {1.0: "same"}, {"1": "same"}, {"1.0": "same"}),
            ({-0.0: "same"}, {0.0: "same"}, {"-0.0": "same"}, {"0.0": "same"}),
            ({True: "same"}, {1: "same"}, {"true": "same"}, {"1": "same"}),
        ],
        ids=["tuple_zero_sign", "integer_float_key", "key_zero_sign", "boolean_integer_key"],
    )
    async def test_nested_identity_edits_reject_hash_changes(
        self,
        store,
        phase,
        before,
        after,
        wire_before,
        wire_after,
    ):
        assert canonical_hash(before) != canonical_hash(after)
        if phase == "arguments":
            original = await sealed(store, before)
            index, field = 0, "arguments"
        elif phase == "confirmed_result":
            original = await completed(store, before)
            index, field = 0, "result"
        else:
            original = await completed(store, skipped_result=before)
            index, field = 1, "result"

        def protected_value(workflow):
            value = getattr(workflow.actions[index], field)
            return value["wire"] if phase == "confirmed_result" else value

        assert canonical_hash(protected_value(original)) == canonical_hash(wire_before)
        # PostgreSQL stores the JSON-mode array/string-key projection, not Python tuples/keys.
        replacement = after if isinstance(store, MemoryWorkflowStore) else wire_after
        if "nested" in before:
            expected_type = tuple if isinstance(store, MemoryWorkflowStore) else list
            assert type(protected_value(original)["nested"]) is expected_type
        if phase == "confirmed_result":
            replacement = {**original.actions[index].result, "wire": replacement}
        actions = list(original.actions)
        actions[index] = actions[index].model_copy(update={field: replacement}, deep=True)
        proposed = original.model_copy(
            update={
                "revision": original.revision + 1,
                "actions": actions,
            },
            deep=True,
        )
        with pytest.raises(Conflict):
            await store.save(proposed, original.revision, None)
        loaded = await store.get(original.workflow_id, original.tenant_id)
        assert loaded.revision == original.revision
        assert canonical_hash(protected_value(loaded)) == canonical_hash(wire_before)

    async def test_nested_identity_keeps_dictionary_order_non_authoritative(self, store):
        original = await sealed(store, {"z": {"nested": (-0.0,)}, "a": {1: "same"}})
        arguments = dict(reversed(list(original.actions[0].arguments.items())))
        actions = list(original.actions)
        actions[0] = actions[0].model_copy(update={"arguments": arguments}, deep=True)
        proposed = original.model_copy(
            update={
                "revision": original.revision + 1,
                "actions": actions,
            },
            deep=True,
        )
        saved = await store.save(proposed, original.revision, None)
        assert canonical_hash(saved.actions[0].arguments) == canonical_hash(
            original.actions[0].arguments
        )

    async def test_twenty_identical_admissions_have_one_created_result(self, store):
        results = await asyncio.gather(*(store.insert_or_get(candidate()) for _ in range(20)))
        assert sum(result.created for result in results) == 1
        assert len({result.workflow.workflow_id for result in results}) == 1
        original = next(result.workflow for result in results if result.created)
        replay = await store.insert_or_get(candidate())
        assert not replay.created
        assert replay.workflow == original

    async def test_same_object_twenty_admissions_have_one_created_result(self, store):
        workflow = candidate()
        results = await asyncio.gather(*(store.insert_or_get(workflow) for _ in range(20)))
        assert sum(result.created for result in results) == 1

    async def test_competing_payload_admissions_cannot_create_two_authorities(self, store):
        results = await asyncio.gather(
            *(
                store.insert_or_get(candidate(text="First" if index % 2 else "Second"))
                for index in range(20)
            ),
            return_exceptions=True,
        )
        accepted = [result for result in results if not isinstance(result, Exception)]
        assert len(accepted) == 10
        assert sum(result.created for result in accepted) == 1
        assert sum(isinstance(result, Conflict) for result in results) == 10
        assert len({result.workflow.workflow_id for result in accepted}) == 1

    async def test_changed_payload_conflicts(self, store):
        original = (await store.insert_or_get(candidate())).workflow
        with pytest.raises(Conflict):
            await store.insert_or_get(candidate(text="Changed"))
        assert await store.get(original.workflow_id, original.tenant_id) == original

    async def test_changed_request_text_cannot_reuse_a_copied_request_hash(self, store):
        original = (await store.insert_or_get(candidate())).workflow
        with pytest.raises(Conflict):
            await store.insert_or_get(original.model_copy(update={"request_text": "Changed"}))

    async def test_same_key_is_independent_across_tenants(self, store):
        first = await store.insert_or_get(candidate())
        second = await store.insert_or_get(candidate(tenant="tenant-b", text="Different"))
        assert first.created and second.created
        assert first.workflow.workflow_id != second.workflow.workflow_id

    async def test_foreign_workflow_is_not_found(self, store):
        workflow = (await store.insert_or_get(candidate())).workflow
        for workflow_id, tenant in [(workflow.workflow_id, "foreign"), (str(uuid4()), "tenant-a")]:
            with pytest.raises(NotFound):
                await store.get(workflow_id, tenant)

    async def test_duplicate_workflow_identity_cannot_replace_original(self, store):
        original = (await store.insert_or_get(candidate())).workflow
        duplicate = candidate(key="other", tenant="other").model_copy(
            update={"workflow_id": original.workflow_id},
            deep=True,
        )
        with pytest.raises(Conflict):
            await store.insert_or_get(duplicate)
        assert await store.get(original.workflow_id, original.tenant_id) == original

    async def test_reads_and_admission_results_are_detached(self, store):
        workflow = candidate().model_copy(update={"model_metadata": {"nested": [1]}}, deep=True)
        original = await store.insert_or_get(workflow)
        workflow.model_metadata["nested"].append(2)
        original.workflow.model_metadata["nested"].append(3)
        loaded = await store.get(original.workflow.workflow_id, "tenant-a")
        assert loaded.model_metadata == {"nested": [1]}
        loaded.model_metadata["nested"].append(4)
        replay = await store.insert_or_get(candidate())
        assert replay.workflow.model_metadata == {"nested": [1]}

    @pytest.mark.parametrize("field", IMMUTABLE_CASES)
    async def test_immutable_fields_and_confirmed_outcomes_reject_edits(self, store, field):
        original = await completed(store)
        data = original.model_dump()
        data["revision"] += 1
        if field == "workflow_id":
            data[field] = str(uuid4())
        elif field in {"tenant_id", "admission_key", "request_text", "catalog_version"}:
            data[field] = "changed"
        elif field in {"request_hash", "plan_hash", "approved_plan_hash"}:
            data[field] = "f" * 64
        elif field == "schema_version":
            data[field] = 2
        elif field in {"created_at", "approved_at"}:
            data[field] += timedelta(seconds=1)
        elif field == "plan":
            data[field]["ticket"]["description"] = "Changed"
        elif field == "action_key":
            data["actions"][0]["idempotency_key"] = "changed"
        elif field in {"arguments", "argument_zero_sign", "argument_numeric_type"}:
            data["actions"][0]["arguments"]["value"] = {
                "arguments": 3,
                "argument_zero_sign": 0.0,
                "argument_numeric_type": 0,
            }[field]
        elif field == "action_order":
            data["actions"].reverse()
        elif field == "action_count":
            data["actions"].pop()
        elif field == "confirmed_result":
            data["actions"][0]["result"]["id"] = "other"
        elif field == "confirmed_zero_sign":
            data["actions"][0]["result"]["value"] = 0.0
        elif field == "confirmed_status":
            data["actions"][0]["status"] = "PENDING"
        elif field == "confirmed_counter":
            data["actions"][0]["submissions"] += 1
        else:
            data["actions"][1]["result"] = {"reason": "changed"}
        # Deliberately bypass Pydantic here; the store must validate untrusted copies.
        data["actions"] = [Action.model_validate(action) for action in data["actions"]]
        data["plan"] = ExecutablePlan.model_validate(data["plan"])
        data["history"] = original.history
        proposed = original.model_copy(update=data, deep=True)
        with pytest.raises((Conflict, NotFound)):
            await store.save(proposed, original.revision, None)
        assert await store.get(original.workflow_id, original.tenant_id) == original

    async def test_revision_and_detached_protected_save(self, store):
        original = (await store.insert_or_get(candidate())).workflow
        proposed = transition(original, DomainEvent(kind="planning_started", metadata={"x": [1]}))
        saved = await store.save(proposed, original.revision, None)
        with pytest.raises(Conflict):
            await store.save(proposed, original.revision, None)
        proposed.model_metadata["x"].append(2)
        saved.model_metadata["x"].append(3)
        assert (await store.get(original.workflow_id, "tenant-a")).model_metadata == {"x": [1]}

    async def test_competing_saves_cannot_both_use_the_same_revision(self, store):
        original = (await store.insert_or_get(candidate())).workflow
        proposed = transition(original, DomainEvent(kind="planning_started"))
        results = await asyncio.gather(
            store.save(proposed, original.revision, None),
            store.save(proposed, original.revision, None),
            return_exceptions=True,
        )
        assert sum(isinstance(result, Workflow) for result in results) == 1
        assert sum(isinstance(result, Conflict) for result in results) == 1

    async def test_test_only_approval_checks_exact_revision_hash_and_records_provenance(
        self, store
    ):
        workflow = await sealed(store)
        for revision, plan_hash in [
            (workflow.revision + 1, workflow.plan_hash),
            (workflow.revision, "f" * 64),
        ]:
            with pytest.raises(Conflict):
                await seed_approved_run(store, workflow, revision, plan_hash)
        assert await store.get(workflow.workflow_id, workflow.tenant_id) == workflow
        approved = await seed_approved_run(store, workflow, workflow.revision, workflow.plan_hash)
        assert approved.history[-1].event == "fixture_approved"
        assert approved.approved_plan_hash == workflow.plan_hash
        assert [action.idempotency_key for action in approved.actions] == ["ticket-key", "part-key"]

    @pytest.mark.parametrize("operation", ["admission", "replay", "save"])
    @pytest.mark.parametrize("field", ["tenant_id", "request_text", "model_metadata"])
    async def test_model_copy_cannot_bypass_nul_rejection(self, store, operation, field):
        original = (await store.insert_or_get(candidate())).workflow
        changes = {
            field: {"private\0key": [None]} if field == "model_metadata" else "secret\0value"
        }
        if operation == "admission":
            proposed = candidate(key="new").model_copy(update=changes, deep=True)
        else:
            proposed = original.model_copy(update=changes, deep=True)
        if operation == "save":
            proposed = proposed.model_copy(update={"revision": original.revision + 1})
        with pytest.raises(Conflict) as raised:
            if operation == "save":
                await store.save(proposed, original.revision, None)
            else:
                await store.insert_or_get(proposed)
        assert "secret" not in str(raised.value) and "private" not in str(raised.value)
        assert await store.get(original.workflow_id, original.tenant_id) == original

    @pytest.mark.parametrize("operation", ["admission", "save"])
    async def test_exact_full_snapshot_budget_and_one_byte_excess(self, store, operation):
        original = (await store.insert_or_get(candidate())).workflow
        base = candidate(key="boundary") if operation == "admission" else original
        changes = {"model_metadata": {"padding": ""}}
        if operation == "save":
            changes.update(revision=original.revision + 1, state="CANCELLED", next_run_at=None)
        base = base.model_copy(update=changes, deep=True)
        padding = (
            "x" * (MAX_DOCUMENT_BYTES - len(base.model_dump_json().encode("utf-8")) - 7) + "水😀"
        )
        exact = base.model_copy(update={"model_metadata": {"padding": padding}}, deep=True)
        assert len(exact.model_dump_json().encode("utf-8")) == MAX_DOCUMENT_BYTES
        excess = exact.model_copy(update={"model_metadata": {"padding": padding + "x"}}, deep=True)
        assert len(excess.model_dump_json().encode("utf-8")) == MAX_DOCUMENT_BYTES + 1
        with pytest.raises(Conflict):
            if operation == "admission":
                await store.insert_or_get(excess)
            else:
                await store.save(excess, original.revision, None)
        if operation == "admission":
            saved = (await store.insert_or_get(exact)).workflow
        else:
            saved = await store.save(exact, original.revision, None)
        loaded = await store.get(saved.workflow_id, saved.tenant_id)
        assert loaded == saved
        assert len(loaded.model_dump_json().encode("utf-8")) == MAX_DOCUMENT_BYTES
