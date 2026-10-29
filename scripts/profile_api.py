"""Diagnostic-only server entry point. Records timings, never request bodies or credentials."""

import cProfile
import functools
import json
import os
import pstats
import time
from contextvars import ContextVar
from pathlib import Path

import uvicorn
from pymongo import monitoring

from workflow_engine import runtime
from workflow_engine.store import MongoWorkflowStore

current = ContextVar("request_timing", default=None)
records = []


class Commands(monitoring.CommandListener):
    def started(self, event):
        pass

    def succeeded(self, event):
        timing = current.get()
        if timing is not None:
            timing["mongo_command_ms"] += event.duration_micros / 1000
            timing["mongo_commands"] += 1

    def failed(self, event):
        self.succeeded(event)


def time_store(method):
    @functools.wraps(method)
    async def timed(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return await method(self, *args, **kwargs)
        finally:
            timing = current.get()
            if timing is not None:
                timing["store_ms"] += (time.perf_counter() - started) * 1000

    return timed


class RequestTimings:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        route = (
            "admit"
            if scope["method"] == "POST" and path == "/workflows"
            else "status"
            if scope["method"] == "GET" and path.startswith("/workflows/")
            else "other"
        )
        timing = {"route": route, "store_ms": 0.0, "mongo_command_ms": 0.0, "mongo_commands": 0}
        token = current.set(timing)
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send)
        finally:
            timing["server_ms"] = (time.perf_counter() - started) * 1000
            records.append(timing)
            current.reset(token)


if __name__ == "__main__":
    output = Path(os.environ["PROFILE_OUTPUT_DIR"])
    runtime.AsyncMongoClient = functools.partial(
        runtime.AsyncMongoClient, event_listeners=[Commands()]
    )
    MongoWorkflowStore.insert_or_get = time_store(MongoWorkflowStore.insert_or_get)
    MongoWorkflowStore.get = time_store(MongoWorkflowStore.get)
    profile = cProfile.Profile()
    profile.enable()
    try:
        uvicorn.run(
            RequestTimings(runtime.api_application()), host="0.0.0.0", port=8000, access_log=False
        )
    finally:
        profile.disable()
        profile.dump_stats(str(output / "api.prof"))
        with (output / "requests.jsonl").open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")
        with (output / "cpu.txt").open("w", encoding="utf-8") as stream:
            pstats.Stats(profile, stream=stream).sort_stats("tottime").print_stats(60)
