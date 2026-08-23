"""Password hashing, JWT issuing/verification, and role-based access dependencies."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import DoctorProfile, Role, User

_bearer = HTTPBearer(auto_error=False, description="Paste the `access_token` from /api/auth/login")


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def _prehash(password: str) -> bytes:
    """SHA-256 + base64 before bcrypt.

    bcrypt silently ignores everything past 72 bytes (and raises in bcrypt>=5),
    which would make two long passwords sharing a prefix interchangeable.
    Pre-hashing gives every password a fixed 44-byte representation, so the
    limit can never be reached. This is the same construction as
    ``passlib.bcrypt_sha256``.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
def create_access_token(user: User) -> tuple[str, int]:
    """Return ``(jwt, expires_in_seconds)``."""
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "name": user.full_name,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": secrets.token_hex(8),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, int(ttl.total_seconds())


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------
_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED
    try:
        payload = decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired, please sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except jwt.PyJWTError:
        raise _UNAUTHENTICATED from None

    user = db.get(User, int(payload.get("sub", 0)))
    if user is None or not user.is_active:
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


class RequireRole:
    """Dependency factory: ``Depends(RequireRole(Role.ADMIN))``."""

    def __init__(self, *roles: str) -> None:
        self.roles = {str(r) for r in roles}

    def __call__(self, user: CurrentUser) -> User:
        if user.role not in self.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This endpoint requires role: {', '.join(sorted(self.roles))}",
            )
        return user


require_admin = RequireRole(Role.ADMIN)
require_doctor = RequireRole(Role.DOCTOR)
require_patient = RequireRole(Role.PATIENT)
require_staff = RequireRole(Role.DOCTOR, Role.ADMIN)

AdminUser = Annotated[User, Depends(require_admin)]
DoctorUser = Annotated[User, Depends(require_doctor)]
PatientUser = Annotated[User, Depends(require_patient)]
StaffUser = Annotated[User, Depends(require_staff)]


def current_doctor_profile(user: DoctorUser, db: Annotated[Session, Depends(get_db)]) -> DoctorProfile:
    profile = db.query(DoctorProfile).filter(DoctorProfile.user_id == user.id).one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No doctor profile is linked to this account. Ask an admin to create one.",
        )
    return profile


DoctorProfileDep = Annotated[DoctorProfile, Depends(current_doctor_profile)]
