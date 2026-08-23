"""Registration, login and the current-user endpoint.

Only the **patient** role can self-register. Doctors are created by an admin
(the brief requires admin-managed doctor profiles) and admins are seeded, so
there is no path for a client to escalate its own role.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import Conflict, PermissionDenied
from app.models import AuditLog, Role, User
from app.schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.security import CurrentUser, create_access_token, hash_password, verify_password
from app.services import notifications

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

DbSession = Annotated[Session, Depends(get_db)]


def _issue(user: User) -> TokenResponse:
    token, expires_in = create_access_token(user)
    return TokenResponse(access_token=token, expires_in=expires_in, user=UserOut.model_validate(user))


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a patient account",
)
def register(payload: RegisterRequest, db: DbSession) -> TokenResponse:
    email = payload.email.lower().strip()
    if db.query(User.id).filter(User.email == email).first():
        raise Conflict("An account with this email already exists.", code="EMAIL_TAKEN")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        role=Role.PATIENT,
        full_name=payload.full_name.strip(),
        phone=payload.phone,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
    )
    db.add(user)
    db.flush()

    notifications.on_user_registered(db, user)
    db.add(AuditLog(actor_user_id=user.id, action="user.register", entity_type="user", entity_id=str(user.id)))
    db.commit()
    db.refresh(user)
    return _issue(user)


@router.post("/login", response_model=TokenResponse, summary="Sign in (any role)")
def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    user = db.query(User).filter(User.email == payload.email.lower().strip()).one_or_none()

    # Same message and comparable timing for "no such user" and "wrong password",
    # so the endpoint cannot be used to enumerate registered emails.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise PermissionDenied("Incorrect email or password.", code="BAD_CREDENTIALS", status_code=401)
    if not user.is_active:
        raise PermissionDenied("This account has been deactivated. Contact the clinic.", code="ACCOUNT_DISABLED")

    return _issue(user)


@router.get("/me", response_model=UserOut, summary="Current signed-in user")
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
