"""FastAPI application factory.

Serves the JSON API under ``/api`` and the three-portal SPA from ``/``.
Interactive API docs live at ``/docs`` (Swagger) and ``/redoc``.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config import BASE_DIR, settings
from app.database import init_db
from app.errors import DomainError
from app.routers import admin, appointments, auth, calendar, consultations, doctors, meta, ops
from app.workers.scheduler import worker

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("clinix")

WEB_DIR = BASE_DIR / "web"

DESCRIPTION = """
Healthcare appointment platform with separate **patient**, **doctor** and **admin** portals.

* **Booking is two-phase** — `POST /api/appointments/hold` reserves a slot while the patient fills
  the symptom form, then `POST /api/appointments/{id}/confirm` submits it and confirms.
* **Double-booking is prevented by the database**, via a partial unique index on
  `(doctor_id, start_at)` restricted to active statuses — not by application checks.
* **AI summaries never break the app.** If the LLM is unavailable, a deterministic fallback is
  stored instead and `source` records which was used.
* **Email and calendar writes go through a transactional outbox** with exponential backoff, so a
  third-party outage delays a notification but never fails a booking.

Sign in via `POST /api/auth/login`, then click **Authorize** and paste the `access_token`.
"""

TAGS = [
    {"name": "Authentication", "description": "Register, sign in, and inspect the current session."},
    {"name": "Doctors", "description": "Search doctors and read their live slot availability."},
    {"name": "Appointments", "description": "Hold, confirm, reschedule and cancel appointments."},
    {"name": "Consultations", "description": "Post-visit notes, prescriptions and patient-friendly summaries."},
    {"name": "Patients", "description": "Patient-facing medication schedule."},
    {"name": "Admin", "description": "Doctor profiles, leave management and operational visibility."},
    {"name": "Google Calendar", "description": "OAuth 2.0 connection and event synchronisation."},
    {"name": "System", "description": "Health checks and public configuration."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    for warning in settings.assert_production_ready():
        logger.warning("CONFIG: %s", warning)

    init_db()

    if settings.seed_demo_data:
        from app.seed import seed_if_empty

        seed_if_empty()

    await worker.start()
    logger.info(
        "%s ready | env=%s db=%s email=%s llm=%s calendar=%s",
        settings.app_name,
        settings.environment,
        "postgres" if not settings.is_sqlite else "sqlite",
        settings.resolved_email_provider,
        "gemini" if settings.llm_enabled else "fallback-only",
        "on" if settings.google_calendar_enabled else "off",
    )
    try:
        yield
    finally:
        await worker.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=meta.VERSION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        contact={"name": "Clinix API"},
        license_info={"name": "MIT"},
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    # ---- middleware --------------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Tag every request so a log line can be traced back from a response."""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
        if request.url.path.startswith("/api") and (elapsed_ms > 1500 or response.status_code >= 500):
            logger.warning("%s %s -> %s in %.0fms [%s]", request.method, request.url.path, response.status_code, elapsed_ms, request_id)
        return response

    # ---- error handling ----------------------------------------------
    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        """Business-rule failures become predictable JSON with a stable `code`."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "code": exc.code, **(exc.extra or {})},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "request"
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"{field}: {first.get('msg', 'invalid value')}",
                "code": "VALIDATION_ERROR",
                "errors": [{"field": ".".join(str(p) for p in e.get("loc", [])[1:]), "message": e.get("msg")} for e in exc.errors()[:10]],
            },
        )

    @app.exception_handler(IntegrityError)
    async def integrity_handler(request: Request, exc: IntegrityError):
        # A unique/FK violation that escaped a specific handler. Never leak SQL.
        logger.warning("Unhandled IntegrityError on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=409,
            content={"detail": "That change conflicts with existing data. Refresh and try again.", "code": "CONFLICT"},
        )

    @app.exception_handler(SQLAlchemyError)
    async def db_error_handler(request: Request, exc: SQLAlchemyError):
        logger.exception("Database error on %s", request.url.path)
        return JSONResponse(
            status_code=503,
            content={"detail": "The database is temporarily unavailable. Please try again.", "code": "DB_UNAVAILABLE"},
        )

    # ---- routes -------------------------------------------------------
    for module in (meta, auth, doctors, appointments, consultations, admin, ops, calendar):
        app.include_router(module.router)

    # ---- SPA ----------------------------------------------------------
    if WEB_DIR.is_dir():
        # Mounted last so it never shadows an /api route. `html=True` serves
        # index.html at "/"; the SPA uses hash routing, so no catch-all needed.
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:  # pragma: no cover
        logger.warning("Frontend directory %s not found — API only", WEB_DIR)

    return app


app = create_app()
