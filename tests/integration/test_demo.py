"""Retired standalone commands must fail before sending even one HTTP request."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from scripts import benchmark_load, demo


@pytest.mark.parametrize("approve", [False, True])
async def test_demo_is_explicitly_unavailable_before_any_request(approve):
    requests = []

    async def spy(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(spy), base_url="http://api"
    ) as client:
        with pytest.raises(SystemExit) as error:
            await demo.run_demo(client, approve=approve)
    assert error.value.code and "Phase 1" in str(error.value)
    assert requests == []


async def test_load_run_one_cannot_send_admission_or_approval():
    requests = []

    async def spy(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(spy), base_url="http://api"
    ) as client:
        with pytest.raises(SystemExit) as error:
            await benchmark_load.run_one(client, asyncio.Semaphore(1), 1)
    assert error.value.code and "Phase 1" in str(error.value)
    assert requests == []


@pytest.mark.parametrize("command", [demo.main, benchmark_load.run])
async def test_retired_entrypoints_fail_before_client_or_report_creation(
    command, monkeypatch, tmp_path
):
    def forbidden_client(*args, **kwargs):
        pytest.fail("Retired command constructed a network client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    monkeypatch.setenv("WORKFLOW_DEMO_API_KEY", "legacy")
    monkeypatch.setenv("WORKFLOW_BENCHMARK_API_KEY", "legacy")
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(SystemExit) as error:
        await command(
            SimpleNamespace(url="http://api", approve=True, approve_plans=True, output=str(output))
        )
    assert error.value.code and "Phase 1" in str(error.value)
    assert not output.exists()
