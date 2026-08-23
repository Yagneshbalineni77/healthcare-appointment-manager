"""Availability generation and race-safe slot reservation.

This module owns the two hardest requirements in the brief:

1. **"System must prevent double-booking and handle simultaneous booking
   attempts safely."**
   Correctness is enforced by the database, not by application logic. A
   *partial unique index* on ``(doctor_id, start_at) WHERE status IN
   ('held','confirmed')`` means the second of two concurrent inserts fails with
   an ``IntegrityError`` no matter how the requests interleave, across any
   number of workers or processes. The pre-checks below exist only to return a
   friendlier error in the common (uncontended) case.

2. **Slot hold mechanism.**
   Booking is two-phase. ``hold_slot`` reserves the slot for
   ``SLOT_HOLD_MINUTES`` while the patient fills the symptom form and the LLM
   runs; ``confirm_hold`` promotes it. Unconfirmed holds expire and the slot
   returns to the pool — swept lazily on read *and* by the background worker,
   so an idle system still frees slots.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import PatientDoubleBooking, SlotNotBookable, SlotUnavailable
from app.models import (
    BLOCKING_STATUSES,
    Appointment,
    AppointmentStatus,
    CancelActor,
    DoctorLeave,
    DoctorProfile,
    DoctorWorkingHour,
    User,
    utcnow,
)

_REF_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY3479"  # no 0/O/1/I/S5/B8 — unambiguous when read aloud


# --------------------------------------------------------------------------
# Time helpers (DB is UTC, humans are in the clinic timezone)
# --------------------------------------------------------------------------
def as_utc(dt: datetime) -> datetime:
    """Normalise any datetime to timezone-aware UTC, truncated to the minute."""
    if dt.tzinfo is None:
        # A naive datetime from a client is interpreted as clinic-local time.
        dt = dt.replace(tzinfo=settings.tz)
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(settings.tz)


def local_label(dt: datetime) -> str:
    """e.g. ``Mon, 25 Aug 2026 · 10:30 AM IST``."""
    local = to_local(dt)
    stamp = local.strftime("%a, %d %b %Y · %I:%M %p")
    return f"{stamp} {local.tzname() or ''}".strip()


def local_time_label(dt: datetime) -> str:
    return to_local(dt).strftime("%I:%M %p").lstrip("0")


def local_day(dt: datetime) -> date:
    return to_local(dt).date()


def generate_reference() -> str:
    return "APT-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


# --------------------------------------------------------------------------
# Hold expiry
# --------------------------------------------------------------------------
def expire_stale_holds(db: Session, *, commit: bool = True) -> int:
    """Release holds whose timer ran out. Idempotent and safe to call often.

    Called at the top of every availability/booking request (so a stale hold can
    never make a slot look busy) and periodically by the worker (so slots are
    freed even with no traffic).
    """
    result = db.execute(
        update(Appointment)
        .where(
            Appointment.status == AppointmentStatus.HELD,
            Appointment.hold_expires_at.is_not(None),
            Appointment.hold_expires_at <= utcnow(),
        )
        .values(
            status=AppointmentStatus.EXPIRED,
            cancelled_at=utcnow(),
            cancelled_by=CancelActor.SYSTEM_HOLD_EXPIRY,
            cancellation_reason="Hold expired before confirmation",
            hold_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if commit:
        db.commit()
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------
# Slot grid
# --------------------------------------------------------------------------
def _windows_for(doctor: DoctorProfile, day: date) -> list[tuple[time, time]]:
    return [
        (wh.start_time, wh.end_time)
        for wh in doctor.working_hours
        if wh.weekday == day.weekday()
    ]


def slot_starts_for_day(doctor: DoctorProfile, day: date) -> list[datetime]:
    """Every slot start (UTC) the doctor's schedule defines for ``day``.

    The grid step is ``slot_duration + buffer``; a slot is only emitted if it
    fits entirely inside the working window, so a 30-minute doctor working
    until 13:00 never gets a 12:45 slot.
    """
    step = timedelta(minutes=doctor.slot_step_minutes)
    length = timedelta(minutes=doctor.slot_duration_minutes)
    starts: list[datetime] = []

    for start_t, end_t in _windows_for(doctor, day):
        cursor = datetime.combine(day, start_t, tzinfo=settings.tz)
        window_end = datetime.combine(day, end_t, tzinfo=settings.tz)
        # Guard against a pathological config (step <= 0) causing an infinite loop.
        if step <= timedelta(0):
            break
        while cursor + length <= window_end:
            starts.append(cursor.astimezone(timezone.utc).replace(second=0, microsecond=0))
            cursor += step

    return sorted(set(starts))


def is_on_leave(db: Session, doctor_id: int, day: date) -> bool:
    return db.scalar(
        select(DoctorLeave.id).where(DoctorLeave.doctor_id == doctor_id, DoctorLeave.leave_date == day).limit(1)
    ) is not None


def _taken_starts(db: Session, doctor_id: int, day_start: datetime, day_end: datetime) -> dict[datetime, str]:
    rows = db.execute(
        select(Appointment.start_at, Appointment.status).where(
            Appointment.doctor_id == doctor_id,
            Appointment.status.in_(BLOCKING_STATUSES),
            Appointment.start_at >= day_start,
            Appointment.start_at < day_end,
        )
    ).all()
    return {as_utc(start): status for start, status in rows}


def day_availability(db: Session, doctor: DoctorProfile, day: date) -> dict:
    """Build the full slot picture for one day, including *why* a slot is closed."""
    expire_stale_holds(db)

    leave = is_on_leave(db, doctor.id, day)
    starts = slot_starts_for_day(doctor, day)

    day_start = datetime.combine(day, time.min, tzinfo=settings.tz).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    taken = _taken_starts(db, doctor.id, day_start, day_end)

    cutoff = utcnow() + timedelta(minutes=settings.min_lead_minutes)
    length = timedelta(minutes=doctor.slot_duration_minutes)

    slots = []
    for start in starts:
        reason: str | None = None
        if leave:
            reason = "leave"
        elif start in taken:
            reason = "booked" if taken[start] == AppointmentStatus.CONFIRMED else "held"
        elif start < cutoff:
            reason = "past"
        elif not doctor.is_accepting_patients:
            reason = "not_accepting"

        slots.append(
            {
                "start_at": start,
                "end_at": start + length,
                "label": local_time_label(start),
                "available": reason is None,
                "reason": reason,
            }
        )

    return {
        "doctor_id": doctor.id,
        "date": day,
        "is_leave": leave,
        "is_working_day": bool(starts),
        "timezone": settings.clinic_timezone,
        "slot_duration_minutes": doctor.slot_duration_minutes,
        "slots": slots,
    }


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def assert_slot_is_bookable(db: Session, doctor: DoctorProfile, start_at: datetime) -> datetime:
    """Every rule that does not depend on other bookings. Returns normalised UTC start."""
    start_at = as_utc(start_at)
    day = local_day(start_at)

    if not doctor.is_accepting_patients or not doctor.user.is_active:
        raise SlotNotBookable("This doctor is not currently accepting appointments.", code="DOCTOR_UNAVAILABLE")

    now = utcnow()
    if start_at < now + timedelta(minutes=settings.min_lead_minutes):
        raise SlotNotBookable(
            f"Appointments must be booked at least {settings.min_lead_minutes} minutes in advance.",
            code="TOO_LATE",
        )

    if start_at > now + timedelta(days=settings.booking_horizon_days):
        raise SlotNotBookable(
            f"Appointments can only be booked up to {settings.booking_horizon_days} days ahead.",
            code="TOO_FAR",
        )

    if is_on_leave(db, doctor.id, day):
        raise SlotNotBookable("The doctor is on leave on this date.", code="DOCTOR_ON_LEAVE")

    if start_at not in set(slot_starts_for_day(doctor, day)):
        raise SlotNotBookable(
            "That time is not one of the doctor's slots. Refresh the availability list and pick again.",
            code="OFF_GRID",
        )

    return start_at


def _assert_patient_free(db: Session, patient_id: int, start_at: datetime, end_at: datetime, *, exclude_id: int | None = None) -> None:
    """A patient cannot be in two places at once — even with different doctors.

    Unlike the doctor-side rule this is an *overlap* check rather than an exact
    equality one, because two doctors can have different slot grids.
    """
    stmt = select(Appointment.reference).where(
        Appointment.patient_id == patient_id,
        Appointment.status.in_(BLOCKING_STATUSES),
        Appointment.start_at < end_at,
        Appointment.end_at > start_at,
    )
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)

    clash = db.scalar(stmt.limit(1))
    if clash:
        raise PatientDoubleBooking(
            f"You already have an appointment ({clash}) that overlaps this time.",
            code="PATIENT_BUSY",
        )


def _lock_doctor_row(db: Session, doctor_id: int) -> None:
    """Serialise same-doctor bookings on Postgres.

    Purely a contention optimisation: it converts a burst of unique-violation
    rollbacks into an orderly queue. Correctness never depends on it, which is
    why it is skipped on SQLite (no ``SELECT ... FOR UPDATE``).
    """
    from app.database import supports_row_locking

    if not supports_row_locking():
        return
    db.execute(select(DoctorProfile.id).where(DoctorProfile.id == doctor_id).with_for_update())


# --------------------------------------------------------------------------
# Phase 1 — hold
# --------------------------------------------------------------------------
def hold_slot(
    db: Session,
    *,
    doctor: DoctorProfile,
    patient: User,
    start_at: datetime,
    reason_for_visit: str | None = None,
) -> Appointment:
    """Reserve a slot for ``SLOT_HOLD_MINUTES``.

    Raises :class:`SlotUnavailable` if another patient wins the race. The
    ``IntegrityError`` branch is the one that actually fires under real
    concurrency; the pre-check above it only shortens the happy path.
    """
    expire_stale_holds(db)
    start_at = assert_slot_is_bookable(db, doctor, start_at)
    end_at = start_at + timedelta(minutes=doctor.slot_duration_minutes)

    _assert_patient_free(db, patient.id, start_at, end_at)
    _lock_doctor_row(db, doctor.id)

    # Cheap advisory pre-check — nice error message, not a correctness guarantee.
    if db.scalar(
        select(Appointment.id)
        .where(
            Appointment.doctor_id == doctor.id,
            Appointment.start_at == start_at,
            Appointment.status.in_(BLOCKING_STATUSES),
        )
        .limit(1)
    ):
        raise SlotUnavailable("That slot was just taken. Please choose another time.")

    for _ in range(5):  # retry only on reference collisions
        appointment = Appointment(
            reference=generate_reference(),
            doctor_id=doctor.id,
            patient_id=patient.id,
            start_at=start_at,
            end_at=end_at,
            status=AppointmentStatus.HELD,
            hold_expires_at=utcnow() + timedelta(minutes=settings.slot_hold_minutes),
            reason_for_visit=reason_for_visit,
        )
        db.add(appointment)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            detail = str(getattr(exc, "orig", exc)).lower()
            if "reference" in detail:
                continue  # 1-in-244-million reference collision: regenerate and retry
            # Anything else on this INSERT is the slot index: we lost the race.
            # This is the authoritative rejection — no application check can be
            # skipped past it, however the concurrent requests interleaved.
            raise SlotUnavailable("That slot was just taken. Please choose another time.") from exc
        db.refresh(appointment)
        return appointment

    raise SlotUnavailable("Could not reserve the slot, please retry.", code="RETRY")


# --------------------------------------------------------------------------
# Phase 2 — confirm
# --------------------------------------------------------------------------
def confirm_hold(db: Session, appointment: Appointment) -> Appointment:
    """Promote ``held`` → ``confirmed``.

    Re-reads the row *inside* the transaction so a hold that expired a
    millisecond ago cannot be confirmed.
    """
    from app.errors import HoldExpired, InvalidState

    db.refresh(appointment)

    if appointment.status == AppointmentStatus.CONFIRMED:
        return appointment  # idempotent: a double-submit is not an error
    if appointment.status in {AppointmentStatus.EXPIRED, AppointmentStatus.CANCELLED}:
        raise HoldExpired("This reservation expired. Please pick the slot again.")
    if appointment.status != AppointmentStatus.HELD:
        raise InvalidState(f"Cannot confirm an appointment in state '{appointment.status}'.")
    if appointment.hold_expires_at and appointment.hold_expires_at <= utcnow():
        expire_stale_holds(db)
        raise HoldExpired("This reservation expired. Please pick the slot again.")

    appointment.status = AppointmentStatus.CONFIRMED
    appointment.confirmed_at = utcnow()
    appointment.hold_expires_at = None
    db.commit()
    db.refresh(appointment)
    return appointment


# --------------------------------------------------------------------------
# Reschedule / cancel
# --------------------------------------------------------------------------
def move_appointment(db: Session, appointment: Appointment, new_start: datetime, doctor: DoctorProfile) -> Appointment:
    """Move a confirmed booking to another slot, atomically."""
    from app.errors import InvalidState

    if appointment.status not in BLOCKING_STATUSES:
        raise InvalidState(f"Cannot reschedule an appointment in state '{appointment.status}'.")

    expire_stale_holds(db)
    new_start = assert_slot_is_bookable(db, doctor, new_start)
    if new_start == as_utc(appointment.start_at):
        return appointment

    new_end = new_start + timedelta(minutes=doctor.slot_duration_minutes)
    _assert_patient_free(db, appointment.patient_id, new_start, new_end, exclude_id=appointment.id)
    _lock_doctor_row(db, doctor.id)

    previous = as_utc(appointment.start_at)
    appointment.rescheduled_from_at = previous
    appointment.start_at = new_start
    appointment.end_at = new_end
    appointment.reschedule_count += 1
    appointment.reminder_sent_at = None  # a moved appointment earns a fresh reminder

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise SlotUnavailable("That slot was just taken. Please choose another time.") from exc

    db.refresh(appointment)
    return appointment


def cancel_appointment(
    db: Session,
    appointment: Appointment,
    *,
    actor: str,
    reason: str | None = None,
    commit: bool = True,
) -> Appointment:
    """Release the slot. Cancelled rows drop out of the partial unique index,
    so the slot is immediately bookable again with no cleanup job."""
    from app.errors import InvalidState

    if appointment.status in {AppointmentStatus.CANCELLED, AppointmentStatus.EXPIRED}:
        return appointment
    if appointment.status == AppointmentStatus.COMPLETED:
        raise InvalidState("A completed visit cannot be cancelled.")

    appointment.status = AppointmentStatus.CANCELLED
    appointment.cancelled_at = utcnow()
    appointment.cancelled_by = actor
    appointment.cancellation_reason = reason
    appointment.hold_expires_at = None

    if commit:
        db.commit()
        db.refresh(appointment)
    return appointment
