"""Classify outcomes conservatively; a failed response is not proof of no effect."""

from typing import Literal, Protocol

import httpx

from .domain import Action, Model


class EffectResult(Model):
    status: Literal["confirmed", "skipped", "retryable", "declined", "unknown"]
    result: dict | None = None
    error: str | None = None


class LookupResult(Model):
    status: Literal["confirmed", "not_found", "unavailable"]
    outcome: EffectResult | None = None


class EffectGateway(Protocol):
    supports_idempotent_replay: bool

    async def submit(self, action: Action) -> EffectResult: ...
    async def lookup(self, action: Action) -> LookupResult: ...


def _outcome(body: dict) -> EffectResult:
    result = body.get("result")
    if not isinstance(result, dict):
        raise ValueError("Missing result")
    if (
        body.get("status") == "CONFIRMED"
        and isinstance(result.get("id"), str)
        and 1 <= len(result["id"]) <= 128
    ):
        return EffectResult(status="confirmed", result={"id": result["id"]})
    if body.get("status") == "SKIPPED" and result.get("reason") == "stock_unavailable":
        return EffectResult(status="skipped", result={"reason": "stock_unavailable"})
    raise ValueError("Unrecognized result")


class HttpEffectGateway:
    supports_idempotent_replay = True

    def __init__(
        self,
        client: httpx.AsyncClient,
        kind: Literal["ticket", "reservation"],
        timeout_s: float = 10,
    ):
        self.client = client
        self.path = "/tickets" if kind == "ticket" else "/reservations"
        self.timeout_s = timeout_s

    async def submit(self, action: Action) -> EffectResult:
        try:
            response = await self.client.post(
                self.path,
                json=action.arguments,
                headers={"Idempotency-Key": action.idempotency_key},
                timeout=self.timeout_s,
            )
            if len(response.content) > 65536:
                return EffectResult(status="unknown", error="oversized_response")
            body = response.json()
            if response.status_code == 200:
                return _outcome(body)
            category = body.get("category")
            if response.status_code == 503 and category == "retryable_no_effect":
                return EffectResult(status="retryable", error="known_no_effect")
            if response.status_code == 422 and category == "permanent_no_effect":
                return EffectResult(status="declined", error="business_decline")
            return EffectResult(
                status="unknown",
                error="key_conflict" if response.status_code == 409 else "uncertain_response",
            )
        except (httpx.TransportError, ValueError, TypeError, AttributeError):
            return EffectResult(status="unknown", error="transport_or_malformed_response")

    async def lookup(self, action: Action) -> LookupResult:
        try:
            response = await self.client.get(
                f"/operations/{action.idempotency_key}", timeout=self.timeout_s
            )
            if response.status_code != 200 or len(response.content) > 65536:
                return LookupResult(status="unavailable")
            body = response.json()
            if body.get("status") == "NOT_FOUND":
                return LookupResult(status="not_found")
            if body.get("status") == "CONFIRMED":
                return LookupResult(status="confirmed", outcome=_outcome(body["outcome"]))
        except (httpx.TransportError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return LookupResult(status="unavailable")
