"""Runtime-role persistence contracts on invocation-owned PostgreSQL resources."""

import asyncio
import json
import math
import random
import struct
import sys

import pytest
from sqlalchemy import event, text

from tests.postgres_support import (
    PostgresTests,
    control_execution,
    cross_live_lease_expiry,
    owner_session,
    prepare_unapproved_run,
    runtime_store,
    seed_approved_run,
)
from tests.store_contract import StoreContract, candidate, completed, sealed
from workflow_engine.domain import (
    Action,
    Conflict,
    DomainEvent,
    ExecutablePlan,
    HistoryEntry,
    Lease,
    LeaseLost,
    NeedsInput,
    ReservationSpec,
    TicketSpec,
    Workflow,
    canonical_hash,
    transition,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
async def store():
    with PostgresTests.from_environment() as postgres:
        async with runtime_store(postgres) as store:
            yield store


class TestPostgresStoreContract(StoreContract):
    pass


_VALUE_PREFIX = "~workflow-value:v1:"
_STRUCTURAL_PATHS = {
    "request_text": ["request_text"],
    "action_key": ["actions", "0", "idempotency_key"],
    "action_error": ["actions", "0", "last_error"],
    "history_event": ["history", "0", "event"],
    "history_error": ["history", "0", "error"],
}


def structural_literal(kind, length):
    if kind == "plain":
        return "x" * length
    head = _VALUE_PREFIX + kind
    if kind == "f":
        return head + "0x1." + "0" * (length - len(head) - 7) + "p+0"
    if kind == "i":
        return head + "1" + "0" * (length - len(head) - 1)
    return head + "x" * (length - len(head))


@pytest.mark.parametrize("kind", ["plain", "f", "i", "s"])
@pytest.mark.parametrize(
    ("field", "length"),
    [
        ("request_text", 1980),
        ("request_text", 1981),
        ("request_text", 2000),
        ("action_key", 108),
        ("action_key", 109),
        ("action_key", 128),
        ("action_error", 1980),
        ("action_error", 1981),
        ("action_error", 2000),
        ("history_event", 44),
        ("history_event", 45),
        ("history_event", 64),
        ("history_error", 1980),
        ("history_error", 1981),
        ("history_error", 2000),
    ],
)
async def test_structural_prefix_boundaries_roundtrip_admission_replay_get_save(
    store, field, length, kind
):
    literal = structural_literal(kind, length)
    assert len(literal) == length
    workflow = candidate(text=literal if field == "request_text" else "Inspect P-17")
    data = workflow.model_dump()
    data["actions"] = [
        Action(
            action_id="ticket",
            arguments={"zero": -0.0, "literal": literal},
            idempotency_key=literal if field == "action_key" else "ticket-key",
            last_error=literal if field == "action_error" else None,
        ).model_dump()
    ]
    data["history"] = [
        HistoryEntry(
            at=workflow.updated_at,
            event=literal if field == "history_event" else "fixture_event",
            error=literal if field == "history_error" else None,
        ).model_dump()
    ]
    workflow = Workflow.model_validate(data)

    def value(snapshot):
        current = snapshot.model_dump(mode="json")
        for key in _STRUCTURAL_PATHS[field]:
            current = current[int(key)] if isinstance(current, list) else current[key]
        return current

    admitted = await prepare_unapproved_run(store, workflow)
    replay = await store.insert_or_get(candidate(text=workflow.request_text))
    assert admitted.created and not replay.created
    loaded = await store.get(admitted.workflow.workflow_id, workflow.tenant_id)
    assert type(value(loaded)) is str and value(loaded) == literal
    assert value(replay.workflow) == literal
    saved = await store.save(
        loaded.model_copy(
            update={
                "revision": loaded.revision + 1,
                "model_metadata": mixed_values(),
            },
            deep=True,
        ),
        loaded.revision,
        None,
    )
    if field in {"action_error", "history_event", "history_error"}:
        # Exercise actual edits too, not only rewriting an unchanged admitted value.
        for replacement in ["y" * length, literal]:
            changes = {"revision": saved.revision + 1}
            if field == "action_error":
                changes["actions"] = [
                    saved.actions[0].model_copy(update={"last_error": replacement})
                ]
            else:
                member = "event" if field == "history_event" else "error"
                changes["history"] = [saved.history[0].model_copy(update={member: replacement})]
            saved = await store.save(
                saved.model_copy(update=changes, deep=True), saved.revision, None
            )
    assert value(saved) == literal
    assert value(await store.get(saved.workflow_id, saved.tenant_id)) == literal
    assert (
        value((await store.insert_or_get(candidate(text=workflow.request_text))).workflow)
        == literal
    )
    async with store.database.sessions() as session:
        valid, stored = (
            await session.execute(
                text(
                    "SELECT public.workflow_execution_payload_valid(payload), "
                    "jsonb_extract_path_text(payload, VARIADIC CAST(:path AS text[])) "
                    "FROM execution_runs WHERE workflow_id = :id"
                ),
                {"path": _STRUCTURAL_PATHS[field], "id": workflow.workflow_id},
            )
        ).one()
    assert valid and stored == literal  # SQL sees the unchanged, bounded structural value.
    assert canonical_hash(saved.actions[0].arguments) == canonical_hash(
        {"zero": -0.0, "literal": literal}
    )


async def test_structural_tag_literals_and_opaque_payload_shaped_dictionaries_stay_literal(store):
    float_literal = _VALUE_PREFIX + "f0x1.0p+0"
    integer_literal = _VALUE_PREFIX + "i123"
    string_literal = _VALUE_PREFIX + "shello"
    plan = ExecutablePlan(
        asset_id=structural_literal("f", 128),
        ticket=TicketSpec(description=structural_literal("i", 2000)),
        reservation=ReservationSpec(part_id=structural_literal("s", 128), quantity=1),
    )
    workflow = candidate(text=float_literal)
    data = workflow.model_dump()
    # This nested ordinary dictionary deliberately has the complete payload key set.
    nested_payload = {
        key: data[key]
        for key in [
            "request_text",
            "plan",
            "clarification",
            "catalog_version",
            "prompt_version",
            "model_metadata",
            "actions",
            "planning_attempts",
            "retry_phase",
            "history",
        ]
    }
    nested_payload["request_text"] = float_literal
    nested_payload["model_metadata"] = {
        integer_literal: [integer_literal, string_literal, -0.0, 10**5000]
    }
    data.update(
        plan=plan.model_dump(),
        plan_hash=canonical_hash(plan.model_dump()),
        clarification=NeedsInput(questions=[structural_literal("s", 200)]).model_dump(),
        catalog_version=integer_literal,
        prompt_version=string_literal,
        model_metadata={"nested_payload": nested_payload},
        actions=[
            Action(
                action_id="ticket",
                arguments={string_literal: [float_literal, integer_literal]},
                idempotency_key=integer_literal,
                last_error=string_literal,
            ).model_dump()
        ],
        history=[
            HistoryEntry(
                at=workflow.updated_at,
                event=float_literal,
                action_id=string_literal,
                error=integer_literal,
            ).model_dump()
        ],
    )
    workflow = Workflow.model_validate(data)
    admitted = await prepare_unapproved_run(store, workflow)
    loaded = await store.get(admitted.workflow.workflow_id, workflow.tenant_id)
    saved = await store.save(
        loaded.model_copy(update={"revision": loaded.revision + 1}), loaded.revision, None
    )
    replay = await store.insert_or_get(candidate(text=workflow.request_text))
    assert not replay.created
    for actual in [loaded, saved, replay.workflow]:
        assert actual.plan == workflow.plan and actual.plan_hash == workflow.plan_hash
        assert actual.request_text == float_literal
        assert actual.catalog_version == integer_literal and actual.prompt_version == string_literal
        assert actual.clarification == workflow.clarification
        assert actual.actions == workflow.actions
        assert actual.history == workflow.history
        nested = actual.model_metadata["nested_payload"]
        assert nested["request_text"] == float_literal
        assert nested["model_metadata"][integer_literal][:2] == [integer_literal, string_literal]
        assert math.copysign(1, nested["model_metadata"][integer_literal][2]) == -1
        assert nested["model_metadata"][integer_literal][3] == 10**5000


@pytest.mark.parametrize("shape", ["many_keys", "positive_exponents", "negative_exponents"])
async def test_real_roundtrip_of_compact_budget_regressions(store, shape):
    metadata = (
        {f"k{i:05d}": 1 for i in range(21000)}
        if shape == "many_keys"
        else {"numbers": [1e308 if shape == "positive_exponents" else 5e-324] * 35000}
    )
    workflow = Workflow.model_validate({**candidate().model_dump(), "model_metadata": metadata})
    admitted = await store.insert_or_get(workflow)
    replay = await store.insert_or_get(candidate())
    loaded = await store.get(admitted.workflow.workflow_id, "tenant-a")
    assert not replay.created
    assert loaded == replay.workflow == admitted.workflow
    assert canonical_hash(loaded.model_metadata) == canonical_hash(metadata)
    if shape != "many_keys":
        assert all(type(value) is float for value in loaded.model_metadata["numbers"])
    proposed = transition(loaded, DomainEvent(kind="planning_started", metadata=metadata))
    saved = await store.save(proposed, loaded.revision, None)
    assert (await store.get(saved.workflow_id, "tenant-a")) == saved


def mixed_values():
    return {
        "numbers": [
            0,
            0.0,
            -0.0,
            1,
            1.0,
            10**20,
            1e20,
            10**308,
            1e308,
            2**53 + 1,
            2**63 - 1,
            2**63,
            -(2**63),
            -(2**63) - 1,
            sys.float_info.max,
            -sys.float_info.max,
            5e-324,
            -5e-324,
            sys.float_info.min,
            math.nextafter(sys.float_info.min, 0),
            math.nextafter(1, 0),
            math.nextafter(1, 2),
        ],
        "strings": ["1e308", "-0.0", "0.00", "null", "\\u0000", 'Ω 水 😀\n\t"\\'],
        "~workflow-value:v1:f0x1.0p+0": {
            "~workflow-value:v1:i123": [
                "~workflow-value:v1:f0x0.0p+0",
                "~workflow-value:v1:shello",
            ],
            "0.00": [None, True, False, {"nested": -0.0}],
        },
    }


async def test_numeric_types_bits_collisions_and_hashes_survive_all_persistence_paths(store):
    values = mixed_values()
    workflow = Workflow.model_validate({**candidate().model_dump(), "model_metadata": values})
    admitted = await store.insert_or_get(workflow)
    loaded = await store.get(admitted.workflow.workflow_id, "tenant-a")
    for expected, actual in zip(values["numbers"], loaded.model_metadata["numbers"], strict=True):
        assert type(actual) is type(expected)
        assert actual == expected
        if type(expected) is float:
            assert struct.pack("!d", expected) == struct.pack("!d", actual)
    assert canonical_hash(loaded.model_metadata) == canonical_hash(values)
    assert not (await store.insert_or_get(candidate())).created
    await store.database.close()  # New connection must install the same local codec.
    assert canonical_hash(
        (await store.get(loaded.workflow_id, "tenant-a")).model_metadata
    ) == canonical_hash(values)
    saved = await store.save(
        transition(loaded, DomainEvent(kind="planning_started", metadata=values)),
        loaded.revision,
        None,
    )
    assert canonical_hash(saved.model_metadata) == canonical_hash(values)


@pytest.mark.parametrize("digits", [4300, 4301, 5000, 131072, 131073, 260000])
async def test_large_integers_survive_insert_replay_get_and_save_without_global_guard_change(
    store, digits
):
    guard = sys.get_int_max_str_digits()
    integer = 10 ** (digits - 1) + 123
    metadata = {"positive": integer, "negative": -integer, "nested": [integer, "123"]}
    # Keep the full compact snapshot under the unchanged budget for the largest case.
    if digits > 80000:
        metadata = {"positive": integer}
    workflow = Workflow.model_validate({**candidate().model_dump(), "model_metadata": metadata})
    admitted = await store.insert_or_get(workflow)
    assert not (await store.insert_or_get(candidate())).created
    loaded = await store.get(admitted.workflow.workflow_id, "tenant-a")
    assert type(loaded.model_metadata["positive"]) is int
    assert loaded.model_metadata == metadata
    saved = await store.save(
        transition(loaded, DomainEvent(kind="planning_started", metadata=metadata)),
        loaded.revision,
        None,
    )
    assert (await store.get(saved.workflow_id, "tenant-a")).model_metadata == metadata
    assert sys.get_int_max_str_digits() == guard


async def test_fixture_approval_and_immutable_results_use_same_codec(store):
    workflow = await completed(store)
    assert math.copysign(1, workflow.actions[0].arguments["value"]) == -1
    assert math.copysign(1, workflow.actions[0].result["value"]) == -1
    assert workflow.plan_hash == workflow.approved_plan_hash
    assert [a.idempotency_key for a in workflow.actions] == ["ticket-key", "part-key"]


async def test_initialize_ping_and_runtime_credentials(store):
    await store.initialize()
    assert await store.ping()
    async with store.database.sessions() as session:
        assert (
            await session.execute(
                text(
                    "SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).scalar_one()


async def test_invalid_database_snapshot_is_not_returned_or_exposed(store):
    workflow = await sealed(store)
    # Runtime SQL can satisfy coarse SQL shape checks while violating nested domain rules.
    async with store.database.sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE execution_runs SET payload = jsonb_set(payload, '{plan}', "
                '\'{"private_invalid_field":"sensitive-marker"}\'::jsonb) WHERE workflow_id = :id'
            ),
            {"id": workflow.workflow_id},
        )
    from workflow_engine.domain import Conflict

    with pytest.raises(Conflict) as raised:
        await store.get(workflow.workflow_id, "tenant-a")
    assert "sensitive-marker" not in str(raised.value)


@pytest.mark.parametrize(("before", "after"), [(1, 1.0), (-0.0, 0.0), ([1], [1.0])])
async def test_numeric_type_only_metadata_edits_are_actually_persisted(store, before, after):
    original = (
        await store.insert_or_get(
            candidate().model_copy(
                update={"model_metadata": {"value": before}},
                deep=True,
            )
        )
    ).workflow
    proposed = original.model_copy(
        update={
            "revision": original.revision + 1,
            "model_metadata": {"value": after},
        },
        deep=True,
    )
    saved = await store.save(proposed, original.revision, None)
    loaded = await store.get(saved.workflow_id, saved.tenant_id)
    assert canonical_hash(loaded.model_metadata) == canonical_hash({"value": after})


async def test_raw_write_constraint_errors_do_not_expose_payload_or_driver_detail(store):
    from workflow_engine.domain import Conflict

    workflow = candidate().model_copy(
        update={
            "lease_owner": "private-payload-marker",
            "lease_token": None,
            "lease_until": None,
        }
    )
    with pytest.raises(Conflict) as raised:
        await store.insert_or_get(workflow)
    assert "private-payload-marker" not in str(raised.value)
    assert raised.value.__suppress_context__


async def test_mixed_values_in_action_arguments_results_and_protected_save(store):
    values = mixed_values()
    workflow = await completed(store, values)
    loaded = await store.get(workflow.workflow_id, workflow.tenant_id)
    assert canonical_hash(loaded.actions[0].arguments) == canonical_hash(values)
    assert canonical_hash(loaded.actions[0].result["wire"]) == canonical_hash(values)
    assert loaded.plan_hash == workflow.plan_hash == loaded.approved_plan_hash
    saved = await store.save(
        loaded.model_copy(
            update={
                "revision": loaded.revision + 1,
                "model_metadata": values,
            },
            deep=True,
        ),
        loaded.revision,
        None,
    )
    replay = await store.insert_or_get(candidate())
    assert not replay.created
    assert canonical_hash(replay.workflow.actions[0].arguments) == canonical_hash(values)
    assert canonical_hash(
        (await store.get(saved.workflow_id, saved.tenant_id)).actions[0].result["wire"]
    ) == canonical_hash(values)


async def test_binary_jsonb_and_default_driver_adapter_isolation():
    from uuid import UUID

    from psycopg.types.json import Jsonb

    with PostgresTests.from_environment() as postgres:
        allocation = postgres.allocate(migrate=True)
        with allocation.connect_runtime() as connection:
            connection.execute("INSERT INTO tenants (id, slug) VALUES ('tenant-a', 'tenant-a')")
        from workflow_engine.db import create_database
        from workflow_engine.postgres_store import PostgresWorkflowStore

        database = create_database(allocation.runtime_url)
        try:
            store = PostgresWorkflowStore(database)
            values = mixed_values()
            admitted = await store.insert_or_get(
                candidate().model_copy(update={"model_metadata": values})
            )
            async with database.engine.connect() as connection:
                raw = await connection.get_raw_connection()
                async with raw.driver_connection.cursor(binary=True) as cursor:
                    await cursor.execute(
                        "SELECT payload FROM execution_runs WHERE workflow_id = %s",
                        (UUID(admitted.workflow.workflow_id),),
                    )
                    payload = (await cursor.fetchone())[0]
                    assert canonical_hash(payload["model_metadata"]) == canonical_hash(values)
            # A separate default Psycopg connection still has unmodified JSON adapters.
            with allocation.connect_runtime() as connection:
                value = connection.execute(
                    "SELECT %s::jsonb", (Jsonb({"x": 1e20, "z": -0.0}),)
                ).fetchone()[0]
                assert type(value["x"]) is int
                assert math.copysign(1, value["z"]) == 1
        finally:
            await database.close()


async def test_finite_binary64_bit_patterns_roundtrip_under_the_existing_budget(store):
    generator = random.Random(8472)
    values = []
    while len(values) < 5000:
        value = struct.unpack("!d", generator.getrandbits(64).to_bytes(8))[0]
        if math.isfinite(value):
            values.append(value)
    metadata = {"numbers": values}
    admitted = await store.insert_or_get(
        candidate().model_copy(update={"model_metadata": metadata})
    )
    replay = await store.insert_or_get(candidate())
    assert not replay.created
    saved = await store.save(
        transition(admitted.workflow, DomainEvent(kind="planning_started", metadata=metadata)),
        admitted.workflow.revision,
        None,
    )
    loaded = await store.get(saved.workflow_id, saved.tenant_id)
    for expected, actual in zip(values, loaded.model_metadata["numbers"], strict=True):
        assert type(actual) is float
        assert struct.pack("!d", expected) == struct.pack("!d", actual)


async def test_unleased_save_blocks_active_lease_and_preserves_database_operational_fields(store):
    from workflow_engine.domain import Conflict

    workflow = (await store.insert_or_get(candidate())).workflow
    async with store.database.sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE execution_runs SET lease_owner = 'fixture-worker', "
                "lease_token = 'fixture-token', lease_until = clock_timestamp() + interval '30 seconds', "
                "last_enqueued_at = clock_timestamp() WHERE workflow_id = :id"
            ),
            {"id": workflow.workflow_id},
        )
    original = await store.get(workflow.workflow_id, workflow.tenant_id)
    proposed = transition(original, DomainEvent(kind="planning_started"))
    with pytest.raises(Conflict):
        await store.save(proposed, original.revision, None)
    async with store.database.sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE execution_runs SET lease_until = clock_timestamp() - interval '1 second' "
                "WHERE workflow_id = :id"
            ),
            {"id": workflow.workflow_id},
        )
    expired = await store.get(workflow.workflow_id, workflow.tenant_id)
    saved = await store.save(
        proposed.model_copy(
            update={
                "lease_owner": "forged",
                "lease_token": "forged",
                "last_enqueued_at": None,
            }
        ),
        original.revision,
        None,
    )
    assert saved.lease_owner == expired.lease_owner == "fixture-worker"
    assert saved.lease_token == expired.lease_token == "fixture-token"
    assert saved.lease_until == expired.lease_until
    assert saved.last_enqueued_at == expired.last_enqueued_at


async def test_large_integers_in_arguments_and_results_survive_owner_fixture_and_protected_save(
    store,
):
    from workflow_engine.domain import Conflict

    integer = 10**4999 + 123
    workflow = await completed(store, {"integer": integer, "negative": -integer})
    loaded = await store.get(workflow.workflow_id, workflow.tenant_id)
    assert type(loaded.actions[0].arguments["integer"]) is int
    assert loaded.actions[0].arguments["integer"] == integer
    assert loaded.actions[0].result["wire"]["negative"] == -integer
    proposed = loaded.model_copy(update={"revision": loaded.revision + 1}, deep=True)
    saved = await store.save(proposed, loaded.revision, None)
    assert saved.actions[0].arguments["integer"] == integer
    tampered = saved.model_copy(update={"revision": saved.revision + 1}, deep=True)
    tampered.actions[0].arguments["integer"] += 1
    with pytest.raises(Conflict):
        await store.save(tampered, saved.revision, None)


async def test_get_and_replay_enforce_full_projection_budget_on_coarsely_valid_sql_rows(store):
    from workflow_engine.domain import Conflict

    workflow = (await store.insert_or_get(candidate())).workflow
    async with store.database.sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE execution_runs SET payload = jsonb_set(payload, '{model_metadata}', "
                "jsonb_build_object('padding', repeat('x', 262144))) WHERE workflow_id = :id"
            ),
            {"id": workflow.workflow_id},
        )
    with pytest.raises(Conflict):
        await store.get(workflow.workflow_id, workflow.tenant_id)
    with pytest.raises(Conflict):
        await store.insert_or_get(candidate())


async def audit_rows(store):
    async with store.database.sessions() as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT actor, event, revision, correlation_id, safe_metadata FROM audit_records "
                        "ORDER BY revision, occurred_at"
                    )
                )
            )
            .mappings()
            .all()
        )


async def test_twenty_claims_have_one_winner(store):
    workflow = (await store.insert_or_get(candidate())).workflow
    leases = await asyncio.gather(
        *(store.claim(workflow.workflow_id, f"worker-{index}") for index in range(20))
    )
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    winner = winners[0]
    current = await store.get(workflow.workflow_id, workflow.tenant_id)
    assert current.lease_token == winner.token and current.lease_owner == winner.owner
    assert current.revision == workflow.revision
    async with store.database.sessions() as session:
        assert (
            await session.execute(
                text(
                    "SELECT lease_until > clock_timestamp() AND "
                    "lease_until <= clock_timestamp() + interval '30 seconds' "
                    "FROM execution_runs WHERE workflow_id = :id"
                ),
                {"id": workflow.workflow_id},
            )
        ).scalar_one()
    assert len(await audit_rows(store)) == 1  # Claims are operational, not business events.


async def test_expired_token_cannot_save_or_renew(store):
    workflow = (await store.insert_or_get(candidate())).workflow
    lease = await store.claim(workflow.workflow_id, "worker-a")
    control_execution(store, workflow, lease_for_s=-1)
    before = await store.get(workflow.workflow_id, workflow.tenant_id)
    with pytest.raises(LeaseLost):
        await store.save(
            transition(lease.workflow, DomainEvent(kind="planning_started")),
            workflow.revision,
            lease.token,
        )
    assert not await store.renew(lease)
    assert await store.get(workflow.workflow_id, workflow.tenant_id) == before
    successor = await store.claim(workflow.workflow_id, "worker-b")
    assert successor is not None and successor.token != lease.token
    with pytest.raises(LeaseLost):
        await store.save(
            transition(lease.workflow, DomainEvent(kind="planning_started")),
            workflow.revision,
            lease.token,
        )
    assert not await store.renew(lease)


async def test_stale_release_preserves_new_lease(store):
    workflow = (await store.insert_or_get(candidate())).workflow
    old = await store.claim(workflow.workflow_id, "old-worker")
    control_execution(store, workflow, lease_for_s=-1)
    current = await store.claim(workflow.workflow_id, "new-worker")
    await store.release(old)
    assert await store.get(workflow.workflow_id, workflow.tenant_id) == current.workflow
    await store.release(current)
    released = await store.get(workflow.workflow_id, workflow.tenant_id)
    assert released.lease_token is released.lease_owner is released.lease_until is None
    assert released.revision == workflow.revision
    assert len(await audit_rows(store)) == 1


async def test_user_save_rejects_live_lease(store):
    workflow = (await store.insert_or_get(candidate())).workflow
    lease = await store.claim(workflow.workflow_id, "worker-a")
    with pytest.raises(Conflict):
        await store.save(
            transition(workflow, DomainEvent(kind="cancelled")), workflow.revision, None
        )
    assert await store.get(workflow.workflow_id, workflow.tenant_id) == lease.workflow


async def test_semantic_revision_has_one_winner(store):
    workflow = (await store.insert_or_get(candidate())).workflow
    lease = await store.claim(workflow.workflow_id, "worker-a")
    proposed = transition(lease.workflow, DomainEvent(kind="planning_started"))
    results = await asyncio.gather(
        *(store.save(proposed, workflow.revision, lease.token) for _ in range(20)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Workflow) for result in results) == 1
    assert sum(isinstance(result, Conflict) for result in results) == 19
    assert [row["revision"] for row in await audit_rows(store)] == [0, 1]


async def test_save_preserves_newer_heartbeat_and_enqueue_marker(store):
    workflow = (await store.insert_or_get(candidate())).workflow
    assert len(await store.due_wakeups()) == 1
    lease = await store.claim(workflow.workflow_id, "real-worker")
    # Force a different expiry before renewal, without advancing an application clock.
    control_execution(store, workflow, lease_for_s=10)
    assert await store.renew(lease)
    current = await store.get(workflow.workflow_id, workflow.tenant_id)
    proposed = transition(lease.workflow, DomainEvent(kind="planning_started"))
    proposed = proposed.model_copy(
        update={
            "lease_owner": "forged-worker",
            "lease_token": "forged-token",
            "lease_until": lease.expiry,
            "last_enqueued_at": None,
        }
    )
    saved = await store.save(proposed, workflow.revision, lease.token)
    assert saved.lease_until == current.lease_until
    assert saved.last_enqueued_at == current.last_enqueued_at
    assert saved.lease_owner == "real-worker" and saved.lease_token == lease.token
    assert saved.wake_seq == workflow.wake_seq + 1
    rows = await audit_rows(store)
    assert [row["actor"] for row in rows] == ["system", "real-worker"]
    assert "forged" not in repr(rows)


async def test_due_intent_recovers_after_publication_gap(store):
    workflow = (await store.insert_or_get(candidate())).workflow
    first = await store.due_wakeups()
    assert [(wake.workflow_id, wake.wake_seq) for wake in first] == [(workflow.workflow_id, 0)]
    # Publication never happened. The committed marker must not consume the due intent.
    marked = await store.get(workflow.workflow_id, workflow.tenant_id)
    assert marked.last_enqueued_at is not None and marked.next_run_at == workflow.next_run_at
    assert await store.due_wakeups() == []
    control_execution(store, workflow, enqueue_due=True)
    assert await store.due_wakeups() == first
    lease = await store.claim(workflow.workflow_id, "worker-a")
    control_execution(store, workflow, enqueue_due=True)
    assert await store.due_wakeups() == []
    await store.release(lease)
    assert await store.due_wakeups() == first
    assert len(await audit_rows(store)) == 1


@pytest.mark.parametrize("operation", ["save", "renew"])
async def test_lock_wait_crossing_expiry_rejects_worker(store, operation):
    workflow = (await store.insert_or_get(candidate())).workflow
    lease = await store.claim(workflow.workflow_id, "worker-a")
    proposed = transition(lease.workflow, DomainEvent(kind="planning_started"))
    async with owner_session(store) as holder:
        deadline = (
            await holder.execute(
                text(
                    "UPDATE execution_runs SET lease_until = clock_timestamp() + interval '0.5 seconds' "
                    "WHERE workflow_id = :id AND tenant_id = :tenant RETURNING lease_until"
                ),
                {"id": workflow.workflow_id, "tenant": workflow.tenant_id},
            )
        ).scalar_one()
        task = asyncio.create_task(
            store.save(proposed, workflow.revision, lease.token)
            if operation == "save"
            else store.renew(lease)
        )
        # Observe a real independent runtime connection waiting on our transaction lock.
        for _ in range(100):
            await holder.execute(text("SELECT pg_stat_clear_snapshot()"))
            blocked = (
                await holder.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND usename = current_setting('workflow.runtime_role') "
                        "AND pg_backend_pid() = ANY(pg_blocking_pids(pid))"
                    )
                )
            ).scalar_one()
            if blocked:
                break
            if task.done():
                await task  # Missing implementation must fail, not masquerade as a lock test.
                pytest.fail("Worker returned without waiting for the row lock")
            await asyncio.sleep(0.01)
        assert blocked == 1
        await holder.execute(
            text(
                "SELECT pg_sleep(GREATEST(0, EXTRACT(epoch FROM "
                "CAST(:deadline AS timestamptz) - clock_timestamp())) + 0.01)"
            ),
            {"deadline": deadline},
        )
    if operation == "save":
        with pytest.raises(LeaseLost):
            await asyncio.wait_for(task, 5)
    else:
        assert not await asyncio.wait_for(task, 5)
    current = await store.get(workflow.workflow_id, workflow.tenant_id)
    assert current.revision == workflow.revision and current.lease_until == deadline
    assert len(await audit_rows(store)) == 1


@pytest.mark.parametrize("operation", ["save", "renew"])
async def test_write_window_crossing_expiry_rejects_worker(store, operation):
    workflow = (await store.insert_or_get(candidate())).workflow
    lease = await store.claim(workflow.workflow_id, "worker-a")
    proposed = transition(lease.workflow, DomainEvent(kind="planning_started"))
    deadline = control_execution(store, workflow, lease_for_s=0.4)
    barrier_hits = 0

    def expire_before_write(connection, cursor, statement, parameters, context, executemany):
        nonlocal barrier_hits
        if statement.startswith("UPDATE execution_runs"):
            barrier_hits += 1
            cross_live_lease_expiry(store, lease, deadline)

    event.listen(store.database.engine.sync_engine, "before_cursor_execute", expire_before_write)
    try:
        if operation == "save":
            with pytest.raises(LeaseLost):
                await store.save(proposed, workflow.revision, lease.token)
        else:
            assert not await store.renew(lease)
    finally:
        event.remove(
            store.database.engine.sync_engine, "before_cursor_execute", expire_before_write
        )
    assert barrier_hits == 1, "expiry barrier was not reached exactly once"
    current = await store.get(workflow.workflow_id, workflow.tenant_id)
    assert current.revision == workflow.revision and current.lease_until == deadline
    assert len(await audit_rows(store)) == 1


async def test_validation_crossing_expiry_rejects_worker(store, monkeypatch):
    import workflow_engine.postgres_store as postgres_store

    workflow = (await store.insert_or_get(candidate())).workflow
    lease = await store.claim(workflow.workflow_id, "worker-a")
    deadline = control_execution(store, workflow, lease_for_s=0.4)
    validate = postgres_store._validate_change
    barrier_hits = 0

    def expensive_validation(*args):
        nonlocal barrier_hits
        validate(*args)
        barrier_hits += 1
        cross_live_lease_expiry(store, lease, deadline)

    monkeypatch.setattr(postgres_store, "_validate_change", expensive_validation)
    with pytest.raises(LeaseLost):
        await store.save(
            transition(lease.workflow, DomainEvent(kind="planning_started")),
            workflow.revision,
            lease.token,
        )
    assert barrier_hits == 1, "expiry barrier was not reached exactly once"
    assert (await store.get(workflow.workflow_id, workflow.tenant_id)).revision == 0
    assert len(await audit_rows(store)) == 1


@pytest.mark.parametrize("window", ["write_save", "write_renew", "validation"])
async def test_expiry_window_evidence_rejects_a_missed_barrier(store, monkeypatch, window):
    # Reproduce the review's false-positive path using a real already-expired
    # PostgreSQL lease, not a fake store or a substituted clock.
    control = control_execution

    def expired_before_operation(store, workflow, **kwargs):
        return control(store, workflow, lease_for_s=-1)

    monkeypatch.setattr(sys.modules[__name__], "control_execution", expired_before_operation)
    with pytest.raises(AssertionError, match="expiry barrier"):
        if window == "validation":
            await test_validation_crossing_expiry_rejects_worker(store, monkeypatch)
        else:
            await test_write_window_crossing_expiry_rejects_worker(
                store, "save" if window == "write_save" else "renew"
            )


async def test_audit_failure_rolls_back_admission_and_transition(store, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    import workflow_engine.postgres_store as postgres_store

    append = getattr(postgres_store, "_append_audit", None)

    async def fail_after_append(*args, **kwargs):
        if append is not None:
            await append(*args, **kwargs)
        raise SQLAlchemyError("private-database-detail")

    with monkeypatch.context() as patch:
        patch.setattr(postgres_store, "_append_audit", fail_after_append, raising=False)
        with pytest.raises(Conflict) as raised:
            await store.insert_or_get(candidate())
        assert "private" not in str(raised.value)
    async with store.database.sessions() as session:
        assert (
            await session.execute(text("SELECT count(*) FROM execution_runs"))
        ).scalar_one() == 0
    assert await audit_rows(store) == []
    workflow = (await store.insert_or_get(candidate())).workflow
    proposed = transition(workflow, DomainEvent(kind="planning_started"))
    before_audit = await audit_rows(store)
    monkeypatch.setattr(postgres_store, "_append_audit", fail_after_append, raising=False)
    with pytest.raises(Conflict):
        await store.save(proposed, workflow.revision, None)
    assert await store.get(workflow.workflow_id, workflow.tenant_id) == workflow
    assert await audit_rows(store) == before_audit
    lease = await store.claim(workflow.workflow_id, "worker-a")
    with pytest.raises(Conflict):
        await store.save(
            transition(lease.workflow, DomainEvent(kind="planning_started")),
            workflow.revision,
            lease.token,
        )
    assert await store.get(workflow.workflow_id, workflow.tenant_id) == lease.workflow
    assert await audit_rows(store) == before_audit


async def test_more_than_256_audit_rows_survive(store):
    workflow = (await store.insert_or_get(candidate())).workflow
    for _ in range(260):
        proposed = workflow.model_copy(
            update={
                "revision": workflow.revision + 1,
                "history": (
                    workflow.history + [HistoryEntry(at=workflow.updated_at, event="checkpoint")]
                )[-256:],
            }
        )
        workflow = await store.save(proposed, workflow.revision, None)
    assert len(workflow.history) == 256
    rows = await audit_rows(store)
    assert len(rows) == 261 and [row["revision"] for row in rows] == list(range(261))
    async with store.database.sessions() as session:
        assert (
            await session.execute(
                text(
                    "SELECT has_table_privilege(current_user, 'audit_records', 'INSERT'), "
                    "has_table_privilege(current_user, 'audit_records', 'UPDATE'), "
                    "has_table_privilege(current_user, 'audit_records', 'DELETE'), "
                    "has_table_privilege(current_user, 'audit_records', 'TRUNCATE')"
                )
            )
        ).one() == (True, False, False, False)


async def test_audit_and_logs_exclude_request_and_connection_secrets(store, caplog):
    import logging

    markers = [
        "private_request_marker",
        "private_model_marker",
        "private_provider_marker",
        "postgresql://private-user:private-password@private-host/database",
    ]
    workflow = (
        await store.insert_or_get(
            candidate(text=markers[0]).model_copy(
                update={
                    "model_metadata": {"raw": markers[1], "connection": markers[3]},
                    "history": [
                        HistoryEntry(at=candidate().updated_at, event=markers[2], error=markers[3])
                    ],
                }
            )
        )
    ).workflow
    with caplog.at_level(logging.INFO):
        replay = await store.insert_or_get(workflow)
        assert not replay.created
        lease = await store.claim(workflow.workflow_id, "actual-worker")
        proposed = transition(lease.workflow, DomainEvent(kind="failed", error=markers[2]))
        saved = await store.save(proposed, workflow.revision, lease.token)
        assert await store.renew(lease)
        await store.release(lease)
        assert await store.due_wakeups() == []
    rows = await audit_rows(store)
    assert [row["actor"] for row in rows] == ["system", "actual-worker"]
    assert [row["revision"] for row in rows] == [0, 1]
    assert rows[0]["event"] == "admitted"
    assert rows[1]["safe_metadata"]["state"] == "FAILED"
    assert rows[1]["correlation_id"] == f"{saved.workflow_id}:1"
    exposed = json.dumps([dict(row) for row in rows]) + caplog.text
    assert all(marker not in exposed for marker in markers)


async def test_due_scanners_skip_locked_rows_and_do_not_duplicate_batches(store):
    workflows = [
        (await store.insert_or_get(candidate(key=f"batch-{i}"))).workflow for i in range(8)
    ]
    async with owner_session(store) as holder:
        await holder.execute(
            text("SELECT workflow_id FROM execution_runs WHERE workflow_id = :id FOR UPDATE"),
            {"id": workflows[0].workflow_id},
        )
        batches = await asyncio.wait_for(
            asyncio.gather(store.due_wakeups(limit=3), store.due_wakeups(limit=3)), 5
        )
        ids = [wake.workflow_id for batch in batches for wake in batch]
        assert len(ids) == len(set(ids)) == 6
        assert workflows[0].workflow_id not in ids
    remaining = await store.due_wakeups()
    assert len(remaining) == 2
    assert {wake.workflow_id for wake in remaining}.isdisjoint(ids)
    assert await store.due_wakeups(limit=0) == []


@pytest.mark.parametrize("delay_s", [1, 2, 4, 8])
async def test_due_deadline_uses_database_time_not_event_clock(store, delay_s):
    from datetime import UTC, datetime

    workflow = (await store.insert_or_get(candidate())).workflow
    lease = await store.claim(workflow.workflow_id, "worker-a")
    proposed = transition(
        lease.workflow,
        DomainEvent(
            kind="retry",
            retry_phase="planning",
            delay_s=delay_s,
            at=datetime(2001, 1, 1, tzinfo=UTC),
        ),
    )
    saved = await store.save(proposed, workflow.revision, lease.token)
    assert abs((saved.next_run_at - saved.updated_at).total_seconds() - delay_s) < 0.01
    async with store.database.sessions() as session:
        assert (
            await session.execute(
                text(
                    "SELECT updated_at <= clock_timestamp() AND "
                    "updated_at > clock_timestamp() - interval '5 seconds' FROM execution_runs "
                    "WHERE workflow_id = :id"
                ),
                {"id": workflow.workflow_id},
            )
        ).scalar_one()
    assert await store.due_wakeups() == []
    await store.release(lease)
    assert await store.claim(workflow.workflow_id, "too-early") is None
    control_execution(store, workflow, run_due=True)
    assert await store.claim(workflow.workflow_id, "due-worker") is not None


async def test_owner_approval_fixture_appends_explicit_audit_provenance(store):
    workflow = await sealed(store)
    approved = await seed_approved_run(store, workflow, workflow.revision, workflow.plan_hash)
    rows = await audit_rows(store)
    assert rows[-1]["revision"] == approved.revision
    assert rows[-1]["actor"] == "system"
    assert rows[-1]["safe_metadata"]["reason_code"] == "fixture_approved"


async def admit_full_budget(store, key="full-budget", tenant="tenant-a"):
    from workflow_engine.domain import MAX_DOCUMENT_BYTES

    base = candidate(key=key, tenant=tenant).model_copy(update={"model_metadata": {"padding": ""}})
    proposed = base.model_copy(
        update={
            "model_metadata": {
                "padding": "x" * (MAX_DOCUMENT_BYTES - len(base.model_dump_json().encode("utf-8")))
            }
        }
    )
    workflow = (await store.insert_or_get(proposed)).workflow
    assert len(workflow.model_dump_json().encode("utf-8")) == MAX_DOCUMENT_BYTES
    return workflow


async def test_claim_cannot_leave_over_budget_snapshot(store):
    workflow = await admit_full_budget(store)
    with pytest.raises(Conflict):
        await store.claim(workflow.workflow_id, "worker-a")
    assert await store.get(workflow.workflow_id, workflow.tenant_id) == workflow
    assert len(await audit_rows(store)) == 1


@pytest.mark.parametrize("healthy_positions", ["earlier", "later", "both"])
async def test_due_marker_failure_is_row_local_across_tenants(store, healthy_positions):
    healthy = []
    if healthy_positions in {"earlier", "both"}:
        healthy.append((await store.insert_or_get(candidate(key="earlier"))).workflow)
    poison = await admit_full_budget(store)
    if healthy_positions in {"later", "both"}:
        healthy.append(
            (await store.insert_or_get(candidate(key="later", tenant="tenant-b"))).workflow
        )
    before_audit = await audit_rows(store)
    expected = {workflow.workflow_id for workflow in healthy}
    for _ in range(3):
        wakes = await store.due_wakeups(limit=3)
        assert {wake.workflow_id for wake in wakes} == expected
        assert await store.get(poison.workflow_id, poison.tenant_id) == poison
        for workflow in healthy:
            marked = await store.get(workflow.workflow_id, workflow.tenant_id)
            assert marked.last_enqueued_at is not None
            assert (
                marked.model_copy(update={"last_enqueued_at": workflow.last_enqueued_at})
                == workflow
            )
            control_execution(store, marked, enqueue_due=True)
        assert await audit_rows(store) == before_audit


async def test_due_limit_one_progresses_past_failed_first_row_and_wraps(store, monkeypatch):
    import workflow_engine.postgres_store as postgres_store

    poison = [await admit_full_budget(store, key=f"full-{i}") for i in range(4)]
    later = (await store.insert_or_get(candidate(key="later", tenant="tenant-b"))).workflow
    decode = postgres_store._decode
    visited = []

    def observe_decode(row):
        visited.append(str(row.workflow_id))
        return decode(row)

    monkeypatch.setattr(postgres_store, "_decode", observe_decode)

    async def bounded_scan():
        visited.clear()
        wakes = await store.due_wakeups(limit=1)
        assert len(set(visited)) <= 1  # At most one candidate is validated/updated per call.
        return wakes, set(visited)

    wakes, seen = await bounded_scan()
    assert wakes == [] and seen == {poison[0].workflow_id}
    # Insert an earlier job after the bounded sweep has started. Equal due times
    # exercise the UUID tie-breaker; this key sorts before every random fixture UUID.
    earlier = candidate(key="new-earlier", tenant="tenant-c").model_copy(
        update={
            "workflow_id": "00000000-0000-0000-0000-000000000001",
        }
    )
    earlier = (await store.insert_or_get(earlier)).workflow
    async with owner_session(store) as session:
        await session.execute(
            text("UPDATE execution_runs SET next_run_at = :due WHERE workflow_id = :id"),
            {"due": poison[0].next_run_at, "id": earlier.workflow_id},
        )
    earlier = await store.get(earlier.workflow_id, earlier.tenant_id)
    tail = (await store.insert_or_get(candidate(key="new-tail", tenant="tenant-d"))).workflow
    expected = {earlier.workflow_id, later.workflow_id, tail.workflow_id}
    received = set()
    for _ in range(12):
        wakes, _ = await bounded_scan()
        received.update(wake.workflow_id for wake in wakes)
        if received == expected:
            break
    assert received == expected
    for workflow in poison:
        assert await store.get(workflow.workflow_id, workflow.tenant_id) == workflow
    # A fresh store reconstructs all durable eligibility without the old cursor.
    for workflow in [earlier, later, tail]:
        control_execution(store, workflow, enqueue_due=True)
    fresh = postgres_store.PostgresWorkflowStore(store.database)
    assert {wake.workflow_id for wake in await fresh.due_wakeups(limit=10)} == expected
    assert len(await audit_rows(store)) == len(poison) + 3


async def test_due_scan_has_a_hard_candidate_bound_and_finite_sweep(store, monkeypatch):
    import workflow_engine.postgres_store as postgres_store

    poison = [await admit_full_budget(store, key=f"full-{i:03d}") for i in range(101)]
    healthy = (await store.insert_or_get(candidate(key="healthy", tenant="tenant-b"))).workflow
    visited = set()
    decode = postgres_store._decode

    def observe_decode(row):
        visited.add(str(row.workflow_id))
        return decode(row)

    monkeypatch.setattr(postgres_store, "_decode", observe_decode)
    assert await store.due_wakeups(limit=10000) == []
    assert len(visited) == 100
    # Growing the tail cannot make the in-progress sweep extend forever. Work
    # inserted behind its cursor is reconsidered at the finite sweep's wrap.
    earlier = candidate(key="earlier", tenant="tenant-c").model_copy(
        update={
            "workflow_id": "00000000-0000-0000-0000-000000000001",
        }
    )
    earlier = (await store.insert_or_get(earlier)).workflow
    async with owner_session(store) as session:
        await session.execute(
            text("UPDATE execution_runs SET next_run_at = :due WHERE workflow_id = :id"),
            {"due": poison[0].next_run_at, "id": earlier.workflow_id},
        )
    for index in range(3):
        await store.insert_or_get(candidate(key=f"tail-{index}", tenant="tenant-d"))
    visited.clear()
    assert [wake.workflow_id for wake in await store.due_wakeups(limit=10000)] == [
        healthy.workflow_id
    ]
    assert len(visited) <= 100
    visited.clear()
    wakes = await store.due_wakeups(limit=1)
    assert [wake.workflow_id for wake in wakes] == [earlier.workflow_id]
    assert visited == {earlier.workflow_id}
    for workflow in poison:
        assert await store.get(workflow.workflow_id, workflow.tenant_id) == workflow


async def test_due_infrastructure_failure_aborts_all_markers_and_does_not_advance(store):
    workflows = [
        (await store.insert_or_get(candidate(key=f"healthy-{i}"))).workflow for i in range(2)
    ]
    before_audit = await audit_rows(store)
    updates = 0

    def fail_second_update(connection, cursor, statement, parameters, context, executemany):
        nonlocal updates
        if statement.startswith("UPDATE execution_runs"):
            updates += 1
            if updates == 2:
                connection.exec_driver_sql("SELECT 1 / 0")  # Real runtime-connection SQL error.

    event.listen(store.database.engine.sync_engine, "after_cursor_execute", fail_second_update)
    try:
        with pytest.raises(Conflict, match="PostgreSQL due scan failed"):
            await store.due_wakeups()
    finally:
        event.remove(store.database.engine.sync_engine, "after_cursor_execute", fail_second_update)
    assert updates == 2
    for workflow in workflows:
        assert await store.get(workflow.workflow_id, workflow.tenant_id) == workflow
    assert await audit_rows(store) == before_audit
    assert {wake.workflow_id for wake in await store.due_wakeups()} == {
        workflow.workflow_id for workflow in workflows
    }


async def test_worker_save_keeps_codec_and_confirmed_skipped_identity(store):
    values = {**mixed_values(), "large": 10**4999 + 123}
    workflow = await sealed(store, values)
    workflow = await seed_approved_run(store, workflow, workflow.revision, workflow.plan_hash)
    owner = "worker/" + "w" * 121
    lease = await store.claim(workflow.workflow_id, owner)
    workflow = lease.workflow
    for kind, action_id, result in [
        ("action_started", "ticket", None),
        ("action_confirmed", "ticket", {"wire": values}),
        ("action_started", "reservation", None),
        ("skipped", "reservation", {"reason": "unavailable", "value": -0.0}),
    ]:
        workflow = await store.save(
            transition(
                workflow,
                DomainEvent(
                    kind=kind,
                    action_id=action_id,
                    result=result,
                ),
            ),
            workflow.revision,
            lease.token,
        )
    loaded = await store.get(workflow.workflow_id, workflow.tenant_id)
    assert loaded.state == "SUCCEEDED"
    for actual in [loaded.actions[0].arguments, loaded.actions[0].result["wire"]]:
        assert type(actual["large"]) is int and actual["large"] == values["large"]
        assert canonical_hash(
            {key: value for key, value in actual.items() if key != "large"}
        ) == canonical_hash(mixed_values())
    rows = await audit_rows(store)
    assert [row["actor"] for row in rows[-4:]] == [owner] * 4
    for index, field, replacement in [
        (0, "arguments", {**values, "large": 1}),
        (0, "result", {"wire": {**values, "large": 1}}),
        (1, "result", {"reason": "unavailable", "value": 0.0}),
    ]:
        actions = list(loaded.actions)
        actions[index] = actions[index].model_copy(update={field: replacement})
        proposed = loaded.model_copy(update={"revision": loaded.revision + 1, "actions": actions})
        with pytest.raises(Conflict):
            await store.save(proposed, loaded.revision, lease.token)
        assert await store.get(workflow.workflow_id, workflow.tenant_id) == loaded
    assert await audit_rows(store) == rows
    await store.release(lease)
    assert await store.claim(workflow.workflow_id, "terminal-worker") is None
    assert await store.due_wakeups() == []


@pytest.mark.parametrize("field", ["tenant_id", "owner", "token"])
async def test_renew_and_release_require_exact_current_lease_identity(store, field):
    workflow = (await store.insert_or_get(candidate())).workflow
    lease = await store.claim(workflow.workflow_id, "worker-a")
    wrong = lease.model_copy(update={field: "foreign-identity"})
    assert not await store.renew(wrong)
    await store.release(wrong)
    assert await store.get(workflow.workflow_id, workflow.tenant_id) == lease.workflow


async def test_heartbeat_opens_an_independent_session_from_a_saving_tick(store, monkeypatch):
    import workflow_engine.postgres_store as postgres_store

    workflow = (await store.insert_or_get(candidate())).workflow
    lease = await store.claim(workflow.workflow_id, "worker-a")
    entered, resume = asyncio.Event(), asyncio.Event()
    append = postgres_store._append_audit

    async def paused_audit(*args):
        entered.set()
        await resume.wait()
        await append(*args)

    monkeypatch.setattr(postgres_store, "_append_audit", paused_audit)
    save = asyncio.create_task(
        store.save(
            transition(lease.workflow, DomainEvent(kind="planning_started")),
            workflow.revision,
            lease.token,
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    heartbeat = asyncio.create_task(store.renew(lease))
    try:
        async with owner_session(store) as observer:
            for _ in range(100):
                await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                connections, blocked = (
                    await observer.execute(
                        text(
                            "SELECT count(*), count(*) FILTER (WHERE "
                            "cardinality(pg_blocking_pids(pid)) > 0) FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND usename = current_setting('workflow.runtime_role')"
                        )
                    )
                ).one()
                if blocked:
                    break
                await asyncio.sleep(0.01)
            assert connections == 2 and blocked == 1
    finally:
        resume.set()
        saved, renewed = await asyncio.wait_for(asyncio.gather(save, heartbeat), 5)
    assert renewed
    current = await store.get(workflow.workflow_id, workflow.tenant_id)
    assert current.revision == saved.revision == 1
    assert current.lease_until >= saved.lease_until
    assert len(await audit_rows(store)) == 2


async def test_renew_cannot_grow_expiry_past_full_snapshot_budget(store):
    from workflow_engine.domain import MAX_DOCUMENT_BYTES

    async with store.database.sessions() as session:
        expiry = (
            await session.execute(
                text("SELECT date_trunc('second', clock_timestamp()) + interval '30 seconds'")
            )
        ).scalar_one()
    base = candidate().model_copy(
        update={
            "lease_owner": "fixture-worker",
            "lease_token": "fixture-token",
            "lease_until": expiry,
            "model_metadata": {"padding": ""},
        }
    )
    base = base.model_copy(
        update={
            "model_metadata": {
                "padding": "x" * (MAX_DOCUMENT_BYTES - len(base.model_dump_json().encode("utf-8")))
            }
        }
    )
    workflow = (await prepare_unapproved_run(store, base)).workflow
    assert len(workflow.model_dump_json().encode("utf-8")) == MAX_DOCUMENT_BYTES
    lease = Lease(
        workflow_id=workflow.workflow_id,
        tenant_id=workflow.tenant_id,
        token=workflow.lease_token,
        owner=workflow.lease_owner,
        expiry=expiry,
        workflow=workflow,
    )
    with pytest.raises(Conflict):
        await store.renew(lease)
    assert await store.get(workflow.workflow_id, workflow.tenant_id) == workflow


async def test_optional_audit_metadata_does_not_restrict_stored_bigint_wake_identity(store):
    workflow = (
        await store.insert_or_get(
            candidate().model_copy(
                update={
                    "wake_seq": 10**18,
                }
            )
        )
    ).workflow
    saved = await store.save(
        transition(workflow, DomainEvent(kind="planning_started")), workflow.revision, None
    )
    assert saved.wake_seq == 10**18 + 1
    assert len(await audit_rows(store)) == 2
