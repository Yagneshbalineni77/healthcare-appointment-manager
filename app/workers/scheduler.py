"""In-process background worker.

Responsibilities per tick
-------------------------
1. expire abandoned slot holds
2. queue "your appointment is tomorrow" reminders
3. queue medication-dose reminders that have come due
4. backfill AI summaries that a crash left behind
5. drain the email outbox (with exponential backoff)
6. drain the Google Calendar outbox
7. mark long-past confirmed visits as no-shows

Why in-process rather than Celery/RQ
-----------------------------------
The submission guidelines ask for minimal dependencies, and a clinic's job
volume is tiny. This runs as an asyncio task in the FastAPI lifespan and needs
no broker, no extra dyno, and no extra service on the free hosting tier. Every
job is *idempotent and DB-driven* rather than in-memory, so the design still
holds if it is later moved to a real queue: point a separate process at the
same database, set ``WORKER_ENABLED=false`` on the web dynos, and run
``python -m app.workers.scheduler`` there instead. Nothing else changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import SessionLocal, session_scope
from app.models import (
    Appointment,
    AppointmentStatus,
    Consultation,
    DoctorProfile,
    utcnow,
)
from app.services import email as email_service
from app.services import gcal
from app.services import notifications as notify
from app.services.slots import expire_stale_holds

logger = logging.getLogger("clinix.worker")


def backfill_missing_summaries(db: Session, limit: int = 10) -> int:
    """Recover appointments confirmed without their AI summary.

    ``confirm`` commits the booking first and writes the summary second, so a
    crash in between leaves a confirmed appointment with a symptom form but no
    triage brief and no confirmation emails. This finds those (older than two
    minutes, so we never race a request that is mid-flight) and completes them.
    """
    from app.routers.appointments import persist_previsit_summary

    stale = (
        db.execute(
            select(Appointment)
            .options(
                selectinload(Appointment.patient),
                selectinload(Appointment.doctor).selectinload(DoctorProfile.user),
                selectinload(Appointment.symptom_report),
                selectinload(Appointment.previsit_summary),
            )
            .where(
                Appointment.status == AppointmentStatus.CONFIRMED,
                Appointment.previsit_summary == None,  # noqa: E711 — SQL NULL, not Python None
                Appointment.symptom_report != None,  # noqa: E711
                Appointment.confirmed_at < utcnow() - timedelta(minutes=2),
            )
            .limit(limit)
        )
        .scalars()
        .all()
    )

    repaired = 0
    for appointment in stale:
        try:
            persist_previsit_summary(db, appointment)
            notify.on_appointment_confirmed(db, appointment)
            db.commit()
            repaired += 1
            logger.info("Backfilled AI summary + notifications for %s", appointment.reference)
        except Exception:
            db.rollback()
            logger.exception("Backfill failed for appointment %s", appointment.id)
    return repaired


def sweep_no_shows(db: Session, limit: int = 50) -> int:
    """Confirmed visits that ended over a day ago with no notes filed."""
    cutoff = utcnow() - timedelta(hours=24)
    rows = (
        db.execute(
            select(Appointment)
            .outerjoin(Consultation, Consultation.appointment_id == Appointment.id)
            .where(
                Appointment.status == AppointmentStatus.CONFIRMED,
                Appointment.end_at < cutoff,
                Consultation.id.is_(None),
            )
            .limit(limit)
        )
        .scalars()
        .all()
    )
    for appointment in rows:
        appointment.status = AppointmentStatus.NO_SHOW
    if rows:
        db.commit()
    return len(rows)


def run_once() -> dict:
    """One full pass. Safe to call concurrently and from tests/endpoints."""
    report: dict = {}
    db = SessionLocal()
    try:
        report["holds_expired"] = expire_stale_holds(db)
        report["appointment_reminders_queued"] = notify.queue_due_appointment_reminders(db)
        report["medication_reminders_queued"] = notify.queue_due_medication_reminders(db)
        report["summaries_backfilled"] = backfill_missing_summaries(db)
        report["no_shows"] = sweep_no_shows(db)
        report["email"] = email_service.dispatch_pending(db)
        report["calendar"] = gcal.dispatch_pending(db)
    except Exception:
        db.rollback()
        logger.exception("Worker tick failed")
        report["error"] = "see server logs"
    finally:
        db.close()
    return report


async def _loop(stop: asyncio.Event) -> None:
    interval = max(5, settings.worker_interval_seconds)
    logger.info("Background worker started (every %ss)", interval)

    while not stop.is_set():
        try:
            # Offloaded to a thread: SQLAlchemy here is synchronous and must
            # never block the event loop serving HTTP requests.
            report = await asyncio.to_thread(run_once)
            interesting = {k: v for k, v in report.items() if v and v not in ({}, 0)}
            if interesting:
                logger.info("worker tick: %s", interesting)
        except Exception:
            logger.exception("Unhandled error in worker loop")

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


class Worker:
    """Lifespan-managed handle for the background loop."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if not settings.worker_enabled:
            logger.warning("WORKER_ENABLED=false — reminders and retries will not run automatically")
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(_loop(self._stop), name="clinix-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        logger.info("Background worker stopped")


worker = Worker()


if __name__ == "__main__":  # standalone mode: python -m app.workers.scheduler
    import time

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    logger.info("Standalone worker starting (interval %ss)", settings.worker_interval_seconds)
    while True:
        logger.info("tick: %s", run_once())
        time.sleep(max(5, settings.worker_interval_seconds))
