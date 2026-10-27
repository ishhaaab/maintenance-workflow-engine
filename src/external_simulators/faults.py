"""Opt-in fault controls for isolated experiments. Never enabled by default."""
import threading
import time
from typing import Literal

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import Field

from workflow_engine.domain import Model
from .ledger import KeyConflict, Ledger


class FaultProfile(Model):
    mode: Literal["none", "before_commit", "permanent", "after_commit", "lookup_unavailable", "post_commit_delay"] = "none"
    remaining: int = Field(default=1, ge=0, le=1000)
    delay_s: float = Field(default=12, ge=0, le=30)


class Faults:
    def __init__(self):
        self._profile = FaultProfile()
        self._lock = threading.Lock()

    def set(self, profile):
        with self._lock:
            self._profile = profile

    def take(self, lookup=False):
        with self._lock:
            p = self._profile
            if p.remaining and ((p.mode == "lookup_unavailable") == lookup):
                self._profile = p.model_copy(update={"remaining": p.remaining - 1})
                return p
            return FaultProfile(mode="none")


def simulator_app(ledger: Ledger, kind: str, body_model: type, enable_faults: bool = False):
    app = FastAPI(title=f"Simulated {kind} system")
    faults = Faults()
    app.state.ledger = ledger

    def submit(payload, idempotency_key: str | None = Header(default=None)):
        if not idempotency_key or len(idempotency_key) > 128:
            return JSONResponse({"category": "invalid_key"}, status_code=400)
        fault = faults.take()
        if fault.mode in {"before_commit", "permanent"}:
            return JSONResponse({"category": "retryable_no_effect" if fault.mode == "before_commit" else "permanent_no_effect"}, status_code=503 if fault.mode == "before_commit" else 422)
        data = payload.model_dump()
        try:
            outcome = ledger.apply(idempotency_key, data, lambda db: ledger.effect(db, kind, data))
        except KeyConflict:
            return JSONResponse({"category": "key_conflict"}, status_code=409)
        if fault.mode == "after_commit":
            return JSONResponse({"category": "unknown"}, status_code=503)
        if fault.mode == "post_commit_delay":
            time.sleep(fault.delay_s)
        return outcome

    # A sync endpoint runs blocking SQLite work in Starlette's thread pool.
    submit.__annotations__["payload"] = body_model
    app.post("/tickets" if kind == "ticket" else "/reservations")(submit)

    @app.get("/operations/{key}")
    def lookup(key: str):
        if faults.take(lookup=True).mode == "lookup_unavailable":
            return JSONResponse({"status": "UNAVAILABLE"}, status_code=503)
        return ledger.lookup(key)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    if enable_faults:
        @app.put("/__faults")
        def set_fault(profile: FaultProfile):
            faults.set(profile)
            return {"status": "configured"}

        @app.get("/__effects")
        def effects():
            return ledger.effect_counts()

    return app
