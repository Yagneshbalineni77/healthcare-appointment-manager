"""Google Calendar integration (OAuth 2.0 authorization-code flow).

Model
-----
Each *user* (patient or doctor) connects their own Google account. On booking
we create the event on **each connected calendar separately**, rather than
creating one event and adding the other party as an attendee. Two reasons:

* adding attendees requires the organiser's calendar to be allowed to invite
  them, which is a Workspace-domain concern we cannot assume;
* if the patient later disconnects, only their copy disappears.

Reliability
-----------
Calendar writes go through the same outbox discipline as email
(``calendar_tasks``): a row is committed with the booking, and the worker
performs the HTTP call with exponential backoff. A Google outage delays the
invitation; it never fails a booking.

Only ``https://www.googleapis.com/auth/calendar.events`` is requested — the
narrowest scope that can create and delete our own events. We never ask to read
the user's existing calendar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import IntegrationDisabled
from app.models import (
    Appointment,
    CalendarAccount,
    CalendarTask,
    JobStatus,
    Role,
    User,
    utcnow,
)

logger = logging.getLogger("clinix.gcal")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
CALENDAR_ENDPOINT = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

SCOPES = ["https://www.googleapis.com/auth/calendar.events", "openid", "email"]

_STATE_TTL_SECONDS = 900


class CalendarError(RuntimeError):
    pass


class CalendarAuthError(CalendarError):
    """Token is dead and cannot be refreshed — the user must reconnect."""


# ==========================================================================
# OAuth 2.0
# ==========================================================================
def _require_enabled() -> None:
    if not settings.google_calendar_enabled:
        raise IntegrationDisabled(
            "Google Calendar is not configured on this deployment. "
            "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI."
        )


def build_state(user_id: int, redirect_to: str = "/") -> str:
    """Signed, expiring CSRF state. Encodes who started the flow."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"uid": user_id, "rt": redirect_to, "iat": int(now.timestamp()),
         "exp": int((now + timedelta(seconds=_STATE_TTL_SECONDS)).timestamp())},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def parse_state(state: str) -> dict:
    try:
        return jwt.decode(state, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise CalendarAuthError(f"Invalid or expired OAuth state: {exc}") from exc


def build_auth_url(user_id: int, redirect_to: str = "/") -> str:
    _require_enabled()
    from urllib.parse import urlencode

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",       # we need a refresh token
        "prompt": "consent",            # force a refresh token even on re-consent
        "include_granted_scopes": "true",
        "state": build_state(user_id, redirect_to),
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code(db: Session, *, code: str, user: User) -> CalendarAccount:
    """Swap the authorization code for tokens and persist the grant."""
    _require_enabled()

    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=25.0,
    )
    if response.status_code != 200:
        raise CalendarAuthError(f"Token exchange failed (HTTP {response.status_code}): {response.text[:300]}")

    tokens = response.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not access_token:
        raise CalendarAuthError("Google did not return an access token")

    google_email = None
    try:
        info = httpx.get(USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}, timeout=15.0)
        if info.status_code == 200:
            google_email = info.json().get("email")
    except httpx.HTTPError:  # non-fatal: we only use this for display
        logger.info("Could not fetch Google userinfo (non-fatal)")

    account = db.scalar(select(CalendarAccount).where(CalendarAccount.user_id == user.id))
    if account is None:
        account = CalendarAccount(user_id=user.id, provider="google")
        db.add(account)

    account.access_token = access_token
    # Google only returns a refresh token on first consent — keep the old one
    # if this is a re-consent that omitted it.
    if refresh_token:
        account.refresh_token = refresh_token
    account.token_expires_at = utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 60)
    account.scope = tokens.get("scope")
    account.google_email = google_email
    account.calendar_id = account.calendar_id or "primary"
    account.connected_at = utcnow()
    account.revoked_at = None

    db.commit()
    db.refresh(account)
    return account


def _valid_access_token(db: Session, account: CalendarAccount) -> str:
    """Return a live access token, refreshing it if needed."""
    if account.access_token and account.token_expires_at and account.token_expires_at > utcnow():
        return account.access_token

    if not account.refresh_token:
        raise CalendarAuthError("No refresh token stored — the user must reconnect Google Calendar.")

    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=25.0,
    )
    if response.status_code != 200:
        # invalid_grant = the user revoked access or changed their password.
        if "invalid_grant" in response.text:
            account.revoked_at = utcnow()
            account.access_token = None
            db.commit()
            raise CalendarAuthError("Google access was revoked — the user must reconnect.")
        raise CalendarError(f"Token refresh failed (HTTP {response.status_code}): {response.text[:200]}")

    tokens = response.json()
    account.access_token = tokens["access_token"]
    account.token_expires_at = utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 60)
    db.commit()
    return account.access_token


def disconnect(db: Session, user: User) -> bool:
    account = db.scalar(select(CalendarAccount).where(CalendarAccount.user_id == user.id))
    if account is None:
        return False
    if account.refresh_token:
        try:
            httpx.post(REVOKE_ENDPOINT, data={"token": account.refresh_token}, timeout=15.0)
        except httpx.HTTPError:
            logger.info("Google token revoke call failed (non-fatal)")
    account.revoked_at = utcnow()
    account.access_token = None
    account.refresh_token = None
    db.commit()
    return True


# ==========================================================================
# Event payloads
# ==========================================================================
def build_event_payload(appointment: Appointment, *, viewer: str) -> dict:
    """Google Calendar event body. ``viewer`` is 'patient' or 'doctor'."""
    from app.services.slots import to_local

    doctor_name = appointment.doctor.user.full_name
    patient_name = appointment.patient.full_name
    spec = appointment.doctor.specialisation

    if viewer == "patient":
        summary = f"Doctor appointment — {doctor_name} ({spec})"
        description = (
            f"Appointment reference: {appointment.reference}\n"
            f"Doctor: {doctor_name} — {spec}\n"
            f"Clinic: {settings.clinic_name}\n"
            + (f"Room: {appointment.doctor.room}\n" if appointment.doctor.room else "")
            + (f"Reason: {appointment.reason_for_visit}\n" if appointment.reason_for_visit else "")
            + f"\nManage this appointment: {settings.public_base_url}/#/appointments"
        )
    else:
        summary = f"Consultation — {patient_name}"
        description = (
            f"Appointment reference: {appointment.reference}\n"
            f"Patient: {patient_name} ({appointment.patient.email})\n"
            + (f"Reason: {appointment.reason_for_visit}\n" if appointment.reason_for_visit else "")
            + f"\nOpen the doctor portal: {settings.public_base_url}/#/schedule"
        )

    return {
        "summary": summary,
        "description": description,
        "location": appointment.doctor.room or settings.clinic_name,
        "start": {"dateTime": to_local(appointment.start_at).isoformat(), "timeZone": settings.clinic_timezone},
        "end": {"dateTime": to_local(appointment.end_at).isoformat(), "timeZone": settings.clinic_timezone},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 60}, {"method": "email", "minutes": 24 * 60}],
        },
        "extendedProperties": {"private": {"clinix_reference": appointment.reference}},
        "source": {"title": settings.clinic_name, "url": settings.public_base_url},
    }


# ==========================================================================
# Calendar REST calls
# ==========================================================================
def _request(db: Session, account: CalendarAccount, method: str, path_suffix: str = "", json_body: dict | None = None) -> httpx.Response:
    token = _valid_access_token(db, account)
    url = CALENDAR_ENDPOINT.format(calendar_id=account.calendar_id or "primary") + path_suffix
    return httpx.request(
        method,
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=json_body,
        timeout=25.0,
    )


def create_event(db: Session, account: CalendarAccount, payload: dict) -> str:
    response = _request(db, account, "POST", "", payload)
    if response.status_code not in (200, 201):
        raise CalendarError(f"create failed HTTP {response.status_code}: {response.text[:300]}")
    return response.json()["id"]


def update_event(db: Session, account: CalendarAccount, event_id: str, payload: dict) -> str:
    response = _request(db, account, "PATCH", f"/{event_id}", payload)
    if response.status_code == 404:
        # Someone deleted it in Google — recreate rather than fail.
        return create_event(db, account, payload)
    if response.status_code != 200:
        raise CalendarError(f"update failed HTTP {response.status_code}: {response.text[:300]}")
    return response.json()["id"]


def delete_event(db: Session, account: CalendarAccount, event_id: str) -> None:
    response = _request(db, account, "DELETE", f"/{event_id}")
    # 404/410 mean it is already gone, which is the state we wanted.
    if response.status_code not in (200, 204, 404, 410):
        raise CalendarError(f"delete failed HTTP {response.status_code}: {response.text[:300]}")


# ==========================================================================
# Outbox
# ==========================================================================
def queue_task(db: Session, *, appointment: Appointment, action: str, user_id: int, role: str, key_suffix: str = "") -> CalendarTask | None:
    """Enqueue one calendar operation. Does not commit (caller's transaction)."""
    key = f"cal:{appointment.id}:{role}:{action}:{key_suffix or appointment.updated_at or ''}"[:180]
    if db.scalar(select(CalendarTask.id).where(CalendarTask.idempotency_key == key).limit(1)):
        return None

    task = CalendarTask(
        idempotency_key=key,
        action=action,
        appointment_id=appointment.id,
        user_id=user_id,
        role=role,
        payload=build_event_payload(appointment, viewer=role) if action != "delete" else {},
        max_attempts=settings.notification_max_attempts,
        next_attempt_at=utcnow(),
    )
    db.add(task)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return None
    return task


def queue_for_both(db: Session, appointment: Appointment, action: str, key_suffix: str = "") -> int:
    """Queue the same action on the patient's and the doctor's calendars."""
    queued = 0
    targets = [(appointment.patient_id, Role.PATIENT), (appointment.doctor.user_id, Role.DOCTOR)]
    for user_id, role in targets:
        if queue_task(db, appointment=appointment, action=action, user_id=user_id, role=str(role), key_suffix=key_suffix):
            queued += 1
    return queued


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(60 * (2 ** max(0, attempts - 1)), 1800))


def dispatch_pending(db: Session, limit: int = 25) -> dict:
    """Execute due calendar tasks. Returns counters."""
    now = utcnow()
    due = (
        db.execute(
            select(CalendarTask)
            .where(CalendarTask.status.in_([JobStatus.PENDING, JobStatus.FAILED]), CalendarTask.next_attempt_at <= now)
            .order_by(CalendarTask.next_attempt_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    done = failed = dead = skipped = 0

    for task in due:
        appointment = db.get(Appointment, task.appointment_id)
        account = db.scalar(select(CalendarAccount).where(CalendarAccount.user_id == task.user_id))

        # Not an error: this user simply never connected Google Calendar.
        if appointment is None or account is None or not account.is_connected or not settings.google_calendar_enabled:
            task.status = JobStatus.CANCELLED
            task.last_error = "Calendar not connected for this user" if account is None or not account.is_connected else "Calendar integration disabled"
            task.completed_at = utcnow()
            skipped += 1
            db.commit()
            continue

        task.attempts += 1
        try:
            existing_id = (
                appointment.patient_calendar_event_id if task.role == Role.PATIENT else appointment.doctor_calendar_event_id
            )

            if task.action == "create":
                event_id = update_event(db, account, existing_id, task.payload) if existing_id else create_event(db, account, task.payload)
            elif task.action == "update":
                event_id = update_event(db, account, existing_id, task.payload) if existing_id else create_event(db, account, task.payload)
            else:  # delete
                if existing_id:
                    delete_event(db, account, existing_id)
                event_id = None

            if task.role == Role.PATIENT:
                appointment.patient_calendar_event_id = event_id
            else:
                appointment.doctor_calendar_event_id = event_id

            task.external_event_id = event_id
            task.status = JobStatus.SENT
            task.completed_at = utcnow()
            task.last_error = None
            done += 1

        except CalendarAuthError as exc:
            # Retrying will not help until the user reconnects.
            task.status = JobStatus.DEAD
            task.last_error = str(exc)[:1000]
            task.completed_at = utcnow()
            dead += 1
        except Exception as exc:
            task.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            if task.attempts >= task.max_attempts:
                task.status = JobStatus.DEAD
                dead += 1
                logger.error("Calendar task %s DEAD: %s", task.id, task.last_error)
            else:
                task.status = JobStatus.FAILED
                task.next_attempt_at = utcnow() + _backoff(task.attempts)
                failed += 1

        db.commit()

    return {"picked": len(due), "completed": done, "retrying": failed, "dead": dead, "skipped": skipped}


def status_for(db: Session, user_id: int) -> dict:
    account = db.scalar(select(CalendarAccount).where(CalendarAccount.user_id == user_id))
    return {
        "integration_configured": settings.google_calendar_enabled,
        "connected": bool(account and account.is_connected),
        "google_email": account.google_email if account else None,
        "connected_at": account.connected_at.isoformat() if account and account.is_connected else None,
    }
