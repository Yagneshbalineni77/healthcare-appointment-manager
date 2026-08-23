"""Health, version and public bootstrap configuration."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import engine, get_db
from app.schemas import IntegrationStatus, SystemHealth
from app.services import llm

router = APIRouter(prefix="/api", tags=["System"])

DbSession = Annotated[Session, Depends(get_db)]

VERSION = "1.0.0"


def _integrations() -> list[IntegrationStatus]:
    breaker = llm.breaker.snapshot()
    return [
        IntegrationStatus(
            name="llm",
            configured=settings.llm_enabled,
            mode=settings.gemini_model if settings.llm_enabled else "rule-based fallback",
            detail=(
                f"circuit breaker open, reopening in {breaker['reopens_in_s']}s"
                if breaker["open"]
                else "Google Gemini, structured JSON output"
                if settings.llm_enabled
                else "GEMINI_API_KEY not set — deterministic triage and template summaries are used instead"
            ),
        ),
        IntegrationStatus(
            name="email",
            configured=settings.email_live,
            mode=settings.resolved_email_provider,
            detail=(
                "Live delivery via the configured provider"
                if settings.email_live
                else f"Messages are written as .eml files to {settings.outbox_dir} and listed in the admin portal"
            ),
        ),
        IntegrationStatus(
            name="google_calendar",
            configured=settings.google_calendar_enabled,
            mode="oauth2" if settings.google_calendar_enabled else "disabled",
            detail=(
                "Users can connect their own calendar; events are created on booking"
                if settings.google_calendar_enabled
                else "GOOGLE_CLIENT_ID / SECRET / REDIRECT_URI not set — calendar tasks are skipped, bookings are unaffected"
            ),
        ),
    ]


@router.get("/health", response_model=SystemHealth, summary="Liveness + integration status")
def health(db: DbSession) -> SystemHealth:
    try:
        db.execute(text("SELECT 1"))
        database = f"ok ({engine.dialect.name})"
        healthy = True
    except Exception as exc:
        database = f"error: {type(exc).__name__}"
        healthy = False

    return SystemHealth(
        status="ok" if healthy else "degraded",
        environment=settings.environment,
        database=database,
        timezone=settings.clinic_timezone,
        worker_enabled=settings.worker_enabled,
        integrations=_integrations(),
        version=VERSION,
    )


@router.get("/config", summary="Public bootstrap config for the frontend")
def public_config() -> dict:
    """Everything the SPA needs before a user signs in. No secrets."""
    return {
        "app_name": settings.app_name,
        "clinic_name": settings.clinic_name,
        "timezone": settings.clinic_timezone,
        "slot_hold_minutes": settings.slot_hold_minutes,
        "booking_horizon_days": settings.booking_horizon_days,
        "min_lead_minutes": settings.min_lead_minutes,
        "reminder_lead_hours": settings.reminder_lead_hours,
        "version": VERSION,
        "integrations": {
            "llm": settings.llm_enabled,
            "email_live": settings.email_live,
            "email_provider": settings.resolved_email_provider,
            "google_calendar": settings.google_calendar_enabled,
        },
    }


@router.get("/llm/status", tags=["System"], summary="LLM client health and circuit-breaker state")
def llm_status() -> dict:
    return llm.status()
