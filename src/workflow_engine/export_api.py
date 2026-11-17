"""Authorized downloads, with no public export files or background URLs."""

import asyncio
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response

from .auth_api import authenticated
from .authorization import Denied
from .exports import csv_report, pdf_report, query_report


def routes(database):
    router, slots = APIRouter(), asyncio.Semaphore(2)

    @router.get("/api/cycles/{cycle_id}/export.{kind}")
    @router.get("/cycles/{cycle_id}/export.{kind}")
    async def download(cycle_id: UUID, kind: Literal["csv", "pdf"], request: Request):
        context = await authenticated(request)
        async with slots:
            report = await query_report(database, context.actor, cycle_id)
            content = await asyncio.to_thread(pdf_report if kind == "pdf" else csv_report, report)
        if len(content) > 10 * 1024 * 1024:
            raise Denied("export_too_large", 413)
        return Response(
            content,
            media_type="application/pdf" if kind == "pdf" else "text/csv; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="cycle-{cycle_id}.{kind}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
