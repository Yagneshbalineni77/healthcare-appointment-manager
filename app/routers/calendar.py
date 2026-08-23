"""Google Calendar connection endpoints (OAuth 2.0 authorization-code flow).

Flow, end to end:

1. Signed-in user calls ``GET /api/calendar/connect`` -> we return Google's
   consent URL containing a **signed, 15-minute state token** that encodes the
   user id. The state is what makes the callback safe: it is the CSRF defence
   and the only way we learn who came back.
2. Browser goes to Google, user consents, Google redirects to
   ``GET /api/calendar/callback`` — a **public** endpoint, because the browser
   arrives without our Authorization header.
3. We verify the state signature, exchange the code for tokens, store the
   refresh token, and redirect back into the SPA with a status flag.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.errors import NotFound
from app.models import User
from app.schemas import MessageOut
from app.security import CurrentUser
from app.services import gcal

router = APIRouter(prefix="/api/calendar", tags=["Google Calendar"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("/status", summary="Is this user's Google Calendar connected?")
def calendar_status(db: DbSession, user: CurrentUser) -> dict:
    return gcal.status_for(db, user.id)


@router.get(
    "/connect",
    summary="Get the Google consent URL",
    description="Returns the URL the browser should navigate to. Returns 503 if the deployment has no Google credentials.",
)
def connect(user: CurrentUser, redirect_to: Annotated[str, Query()] = "/#/settings") -> dict:
    return {"authorization_url": gcal.build_auth_url(user.id, redirect_to)}


@router.get(
    "/callback",
    include_in_schema=True,
    summary="OAuth 2.0 redirect target (called by Google, not by the SPA)",
    response_class=RedirectResponse,
)
def callback(
    db: DbSession,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    def back(**params) -> RedirectResponse:
        return RedirectResponse(f"{settings.public_base_url}/#/settings?{urlencode(params)}", status_code=302)

    if error:
        return back(calendar="error", reason=error)
    if not code or not state:
        return back(calendar="error", reason="missing_code_or_state")

    try:
        claims = gcal.parse_state(state)
    except gcal.CalendarAuthError:
        return back(calendar="error", reason="invalid_state")

    user = db.get(User, int(claims.get("uid", 0)))
    if user is None:
        return back(calendar="error", reason="unknown_user")

    try:
        account = gcal.exchange_code(db, code=code, user=user)
    except Exception as exc:  # surfaced to the user as a banner, not a 500 page
        return back(calendar="error", reason=type(exc).__name__)

    return back(calendar="connected", email=account.google_email or "")


@router.post("/disconnect", response_model=MessageOut, summary="Revoke and forget the Google grant")
def disconnect(db: DbSession, user: CurrentUser) -> MessageOut:
    if not gcal.disconnect(db, user):
        raise NotFound("No Google Calendar connection to remove.")
    return MessageOut(message="Google Calendar disconnected. Existing events were left on your calendar.")
