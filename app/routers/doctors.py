"""Doctor discovery and availability (patient-facing, read-only)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.errors import NotFound
from app.models import DoctorProfile, User, utcnow
from app.schemas import DayAvailability, DoctorOut
from app.security import CurrentUser
from app.serializers import doctor_out
from app.services.slots import day_availability, to_local

router = APIRouter(prefix="/api/doctors", tags=["Doctors"])

DbSession = Annotated[Session, Depends(get_db)]


def _load(db: Session, doctor_id: int, *, active_only: bool = True) -> DoctorProfile:
    stmt = (
        select(DoctorProfile)
        .options(
            selectinload(DoctorProfile.user),
            selectinload(DoctorProfile.working_hours),
            selectinload(DoctorProfile.leaves),
        )
        .where(DoctorProfile.id == doctor_id)
    )
    profile = db.scalar(stmt)
    if profile is None or (active_only and not profile.user.is_active):
        raise NotFound("Doctor not found.")
    return profile


@router.get("/specialisations", response_model=list[str], summary="Distinct specialisations offered")
def specialisations(db: DbSession) -> list[str]:
    rows = db.execute(
        select(DoctorProfile.specialisation)
        .join(User, User.id == DoctorProfile.user_id)
        .where(User.is_active.is_(True))
        .distinct()
        .order_by(DoctorProfile.specialisation)
    ).scalars()
    return list(rows)


@router.get("", response_model=list[DoctorOut], summary="Search doctors by specialisation or name")
def list_doctors(
    db: DbSession,
    specialisation: Annotated[str | None, Query(description="Exact specialisation, case-insensitive")] = None,
    q: Annotated[str | None, Query(description="Free text over name, specialisation and bio")] = None,
    accepting_only: Annotated[bool, Query(description="Hide doctors who have paused new bookings")] = True,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DoctorOut]:
    stmt = (
        select(DoctorProfile)
        .join(User, User.id == DoctorProfile.user_id)
        .options(
            selectinload(DoctorProfile.user),
            selectinload(DoctorProfile.working_hours),
            selectinload(DoctorProfile.leaves),
        )
        .where(User.is_active.is_(True))
    )

    if specialisation:
        stmt = stmt.where(func.lower(DoctorProfile.specialisation) == specialisation.lower().strip())
    if q:
        pattern = f"%{q.lower().strip()}%"
        stmt = stmt.where(
            or_(
                func.lower(User.full_name).like(pattern),
                func.lower(DoctorProfile.specialisation).like(pattern),
                func.lower(func.coalesce(DoctorProfile.bio, "")).like(pattern),
                func.lower(func.coalesce(DoctorProfile.qualifications, "")).like(pattern),
            )
        )
    if accepting_only:
        stmt = stmt.where(DoctorProfile.is_accepting_patients.is_(True))

    stmt = stmt.order_by(DoctorProfile.specialisation, User.full_name).limit(limit).offset(offset)
    return [doctor_out(db, profile) for profile in db.scalars(stmt)]


@router.get("/{doctor_id}", response_model=DoctorOut, summary="One doctor's public profile")
def get_doctor(doctor_id: int, db: DbSession) -> DoctorOut:
    return doctor_out(db, _load(db, doctor_id))


@router.get(
    "/{doctor_id}/availability",
    response_model=DayAvailability,
    summary="Slot grid for one day",
    description=(
        "Returns **every** slot the doctor's schedule defines for the day, each marked "
        "available or not with a machine-readable reason (`booked`, `held`, `past`, "
        "`leave`, `not_accepting`). Expired holds are swept before the grid is built, so "
        "an abandoned booking never keeps a slot looking busy."
    ),
)
def availability(
    doctor_id: int,
    db: DbSession,
    _user: CurrentUser,
    day: Annotated[date | None, Query(alias="date", description="Clinic-local date (YYYY-MM-DD). Defaults to today.")] = None,
) -> DayAvailability:
    profile = _load(db, doctor_id)
    target = day or to_local(utcnow()).date()
    return DayAvailability(**day_availability(db, profile, target))


@router.get(
    "/{doctor_id}/availability-range",
    response_model=list[DayAvailability],
    summary="Slot grid for several consecutive days",
)
def availability_range(
    doctor_id: int,
    db: DbSession,
    _user: CurrentUser,
    start: Annotated[date | None, Query(alias="from", description="First clinic-local date")] = None,
    days: Annotated[int, Query(ge=1, le=14, description="How many days to return")] = 7,
) -> list[DayAvailability]:
    profile = _load(db, doctor_id)
    first = start or to_local(utcnow()).date()
    horizon = to_local(utcnow()).date() + timedelta(days=settings.booking_horizon_days)

    out: list[DayAvailability] = []
    for offset in range(days):
        target = first + timedelta(days=offset)
        if target > horizon:
            break
        out.append(DayAvailability(**day_availability(db, profile, target)))
    return out
