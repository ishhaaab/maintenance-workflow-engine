import httpx

from tests.integration.test_gateways import action
from tests.processes import simulator_process
from workflow_engine.gateways import HttpEffectGateway


async def test_real_http_timeout_after_commit_remains_discoverable(tmp_path):
    with simulator_process("ticketing", tmp_path) as (url, process):
        async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
            await client.put("/__faults", json={"mode": "post_commit_delay", "delay_s": 1})
            gateway = HttpEffectGateway(client, "ticket", timeout_s=0.1)
            assert (await gateway.submit(action())).status == "unknown"
            assert (await gateway.lookup(action())).status == "confirmed"
            assert sum((await client.get("/__effects")).json().values()) == 1


async def test_simulator_process_restart_preserves_ledger_and_effect(tmp_path):
    with simulator_process("ticketing", tmp_path) as (url, _process):
        async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
            first = await HttpEffectGateway(client, "ticket").submit(action())
            assert first.status == "confirmed"
    with simulator_process("ticketing", tmp_path) as (url, _process):
        async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
            gateway = HttpEffectGateway(client, "ticket")
            assert (await gateway.lookup(action())).outcome.result == first.result
            assert (await gateway.submit(action())).result == first.result
            assert sum((await client.get("/__effects")).json().values()) == 1
