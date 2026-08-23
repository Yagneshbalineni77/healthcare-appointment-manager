"""Operational endpoints for demos, tests and manual recovery."""

from __future__ import annotations

from fastapi import APIRouter

from app.security import AdminUser
from app.workers.scheduler import run_once

router = APIRouter(prefix="/api/admin", tags=["Admin"])


@router.post(
    "/worker/run-once",
    summary="Run one background-worker pass immediately",
    description=(
        "Forces a tick instead of waiting for the interval: expires holds, queues due "
        "reminders, backfills missing AI summaries, and drains the email and calendar "
        "outboxes. Returns the counters from the pass."
    ),
)
def trigger_worker(_admin: AdminUser) -> dict:
    return {"ran": True, "report": run_once()}
