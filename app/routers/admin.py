"""Admin portal API: doctor management, leave handling, and ops visibility."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.errors import Conflict, NotFound
from app.models import (
    BLOCKING_STATUSES,
    Appointment,
    AppointmentStatus,
    AuditLog,
    CalendarTask,
    CancelActor,
    DoctorLeave,
    DoctorProfile,
    DoctorWorkingHour,
    JobStatus,
    Notification,
    PostVisitSummary,
    PreVisitSummary,
    Role,
    User,
    utcnow,
)
from app.schemas import (
    AdminStats,
    AffectedAppointment,
    AppointmentOut,
    CalendarTaskOut,
    DoctorCreate,
    DoctorOut,
    DoctorUpdate,
    LeaveCreate,
    LeaveImpact,
    LeaveOut,
    MessageOut,
    NotificationOut,
)
from app.security import AdminUser, hash_password
from app.serializers import appointment_out, doctor_out
from app.services import email as email_service
from app.services import notifications as notify
from app.services.slots import cancel_appointment, expire_stale_holds, local_label, to_local

router = APIRouter(prefix="/api/admin", tags=["Admin"])

DbSession = Annotated[Session, Depends(get_db)]

_APPOINTMENT_LOADS = (
    selectinload(Appointment.patient),
    selectinload(Appointment.doctor).selectinload(DoctorProfile.user),
    selectinload(Appointment.previsit_summary),
    selectinload(Appointment.symptom_report),
    selectinload(Appointment.consultation),
)


def _load_profile(db: Session, doctor_id: int) -> DoctorProfile:
    profile = db.scalar(
        select(DoctorProfile)
        .options(selectinload(DoctorProfile.user), selectinload(DoctorProfile.working_hours), selectinload(DoctorProfile.leaves))
        .where(DoctorProfile.id == doctor_id)
    )
    if profile is None:
        raise NotFound("Doctor not found.")
    return profile


def _audit(db: Session, admin: User, action: str, entity_type: str, entity_id, **meta) -> None:
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            meta=meta,
        )
    )


# ==========================================================================
# Doctor management
# ==========================================================================
@router.post(
    "/doctors",
    response_model=DoctorOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a doctor (login + clinical profile + weekly schedule)",
)
def create_doctor(payload: DoctorCreate, db: DbSession, admin: AdminUser) -> DoctorOut:
    email = payload.email.lower().strip()
    if db.query(User.id).filter(User.email == email).first():
        raise Conflict("An account with this email already exists.", code="EMAIL_TAKEN")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        role=Role.DOCTOR,
        full_name=payload.full_name.strip(),
        phone=payload.phone,
    )
    db.add(user)
    db.flush()

    profile = DoctorProfile(
        user_id=user.id,
        specialisation=payload.specialisation.strip(),
        qualifications=payload.qualifications,
        bio=payload.bio,
        experience_years=payload.experience_years,
        consultation_fee=payload.consultation_fee,
        slot_duration_minutes=payload.slot_duration_minutes,
        buffer_minutes=payload.buffer_minutes,
        room=payload.room,
    )
    db.add(profile)
    db.flush()

    for window in payload.working_hours:
        db.add(
            DoctorWorkingHour(
                doctor_id=profile.id,
                weekday=window.weekday,
                start_time=window.start_time,
                end_time=window.end_time,
            )
        )

    _audit(db, admin, "doctor.create", "doctor", profile.id, email=email, specialisation=profile.specialisation)
    db.commit()
    return doctor_out(db, _load_profile(db, profile.id))


@router.get("/doctors", response_model=list[DoctorOut], summary="All doctors, including deactivated")
def list_all_doctors(db: DbSession, _admin: AdminUser) -> list[DoctorOut]:
    profiles = db.scalars(
        select(DoctorProfile)
        .options(selectinload(DoctorProfile.user), selectinload(DoctorProfile.working_hours), selectinload(DoctorProfile.leaves))
        .join(User, User.id == DoctorProfile.user_id)
        .order_by(User.full_name)
    )
    return [doctor_out(db, profile) for profile in profiles]


@router.patch("/doctors/{doctor_id}", response_model=DoctorOut, summary="Update a doctor profile or schedule")
def update_doctor(doctor_id: int, payload: DoctorUpdate, db: DbSession, admin: AdminUser) -> DoctorOut:
    profile = _load_profile(db, doctor_id)
    data = payload.model_dump(exclude_unset=True)

    for field in ("full_name", "phone"):
        if field in data and data[field] is not None:
            setattr(profile.user, field, data[field])
    if "is_active" in data and data["is_active"] is not None:
        profile.user.is_active = data["is_active"]

    for field in (
        "specialisation", "qualifications", "bio", "experience_years", "consultation_fee",
        "slot_duration_minutes", "buffer_minutes", "room", "is_accepting_patients",
    ):
        if field in data and data[field] is not None:
            setattr(profile, field, data[field])

    if data.get("working_hours") is not None:
        # Replace the whole week atomically — simpler and less error-prone than
        # diffing windows, and the schedule is small.
        for existing in list(profile.working_hours):
            db.delete(existing)
        db.flush()
        for window in payload.working_hours or []:
            db.add(
                DoctorWorkingHour(
                    doctor_id=profile.id,
                    weekday=window.weekday,
                    start_time=window.start_time,
                    end_time=window.end_time,
                )
            )

    _audit(db, admin, "doctor.update", "doctor", profile.id, fields=sorted(data.keys()))
    db.commit()
    return doctor_out(db, _load_profile(db, doctor_id))


@router.delete("/doctors/{doctor_id}", response_model=MessageOut, summary="Deactivate a doctor (soft delete)")
def deactivate_doctor(doctor_id: int, db: DbSession, admin: AdminUser) -> MessageOut:
    """Soft delete only.

    Hard-deleting a doctor would orphan clinical history, which a clinic must
    retain. Deactivating hides them from search and blocks new bookings; past
    appointments and consultations stay intact.
    """
    profile = _load_profile(db, doctor_id)
    profile.user.is_active = False
    profile.is_accepting_patients = False
    _audit(db, admin, "doctor.deactivate", "doctor", profile.id)
    db.commit()
    return MessageOut(message=f"{profile.user.full_name} has been deactivated. Existing appointments were left untouched.")


# ==========================================================================
# Leave management — the conflict path
# ==========================================================================
@router.post(
    "/doctors/{doctor_id}/leave",
    response_model=LeaveImpact,
    summary="Mark a doctor on leave (dry run by default)",
    description=(
        "**Two-step by design.** Call with `confirm: false` (the default) to get an impact "
        "report listing exactly which patients would lose their booking — nothing is changed. "
        "Call again with `confirm: true` to apply it. Applying is a single transaction: the "
        "leave row, every cancellation, every apology email and every calendar deletion commit "
        "together or not at all."
    ),
)
def mark_leave(doctor_id: int, payload: LeaveCreate, db: DbSession, admin: AdminUser) -> LeaveImpact:
    profile = _load_profile(db, doctor_id)
    leave_date = payload.leave_date

    if db.scalar(select(DoctorLeave.id).where(DoctorLeave.doctor_id == doctor_id, DoctorLeave.leave_date == leave_date)):
        raise Conflict(f"{profile.user.full_name} is already marked on leave on {leave_date}.", code="LEAVE_EXISTS")

    expire_stale_holds(db)

    # Clinic-local day boundaries -> UTC, because start_at is stored in UTC.
    day_start = datetime.combine(leave_date, time.min, tzinfo=settings.tz).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)

    affected = (
        db.execute(
            select(Appointment)
            .options(*_APPOINTMENT_LOADS)
            .where(
                Appointment.doctor_id == doctor_id,
                Appointment.status.in_(BLOCKING_STATUSES),
                Appointment.start_at >= day_start,
                Appointment.start_at < day_end,
            )
            .order_by(Appointment.start_at)
        )
        .scalars()
        .all()
    )

    report = [
        AffectedAppointment(
            appointment_id=a.id,
            reference=a.reference,
            patient_name=a.patient.full_name,
            patient_email=a.patient.email,
            start_at=a.start_at,
            start_at_local=local_label(a.start_at),
        )
        for a in affected
    ]

    if not payload.confirm:
        return LeaveImpact(
            doctor_id=doctor_id,
            leave_date=leave_date,
            applied=False,
            affected_count=len(affected),
            affected=report,
            message=(
                f"Dry run — nothing changed. Applying this leave will cancel {len(affected)} appointment(s) "
                f"and email {len(affected)} patient(s)."
                if affected
                else "Dry run — no appointments are booked on this date, so applying the leave affects nobody."
            ),
        )

    # ---- apply ---------------------------------------------------------
    leave = DoctorLeave(
        doctor_id=doctor_id,
        leave_date=leave_date,
        reason=payload.reason,
        created_by_user_id=admin.id,
        affected_appointment_count=len(affected),
    )
    db.add(leave)

    queued = 0
    for appointment in affected:
        cancel_appointment(
            db,
            appointment,
            actor=CancelActor.SYSTEM_LEAVE,
            reason=payload.reason or f"{profile.user.full_name} is on leave on {leave_date}",
            commit=False,
        )
        notify.on_appointment_cancelled(
            db,
            appointment,
            actor=CancelActor.SYSTEM_LEAVE,
            reason=payload.reason,
            leave_date=leave_date.isoformat(),
        )
        queued += 1

    _audit(db, admin, "doctor.leave", "doctor", doctor_id, leave_date=leave_date.isoformat(), affected=len(affected))
    db.commit()

    return LeaveImpact(
        doctor_id=doctor_id,
        leave_date=leave_date,
        applied=True,
        affected_count=len(affected),
        affected=report,
        notifications_queued=queued,
        message=(
            f"Leave applied. {len(affected)} appointment(s) cancelled and patients notified."
            if affected
            else "Leave applied. No appointments were affected."
        ),
    )


@router.get("/doctors/{doctor_id}/leaves", response_model=list[LeaveOut], summary="A doctor's leave days")
def list_leaves(doctor_id: int, db: DbSession, _admin: AdminUser) -> list[LeaveOut]:
    rows = db.scalars(
        select(DoctorLeave).where(DoctorLeave.doctor_id == doctor_id).order_by(DoctorLeave.leave_date.desc())
    )
    return [LeaveOut.model_validate(row) for row in rows]


@router.delete("/leaves/{leave_id}", response_model=MessageOut, summary="Remove a leave day")
def delete_leave(leave_id: int, db: DbSession, admin: AdminUser) -> MessageOut:
    """Removing a leave reopens the slots but never resurrects cancelled bookings.

    Those patients have already been told the appointment is off, and their slot
    may since have been taken by someone else. They rebook.
    """
    leave = db.get(DoctorLeave, leave_id)
    if leave is None:
        raise NotFound("Leave entry not found.")
    day = leave.leave_date
    db.delete(leave)
    _audit(db, admin, "doctor.leave.remove", "doctor", leave.doctor_id, leave_date=day.isoformat())
    db.commit()
    return MessageOut(
        message=f"Leave on {day} removed — those slots are bookable again. "
        "Previously cancelled patients were not automatically restored."
    )


# ==========================================================================
# Ops visibility
# ==========================================================================
@router.get("/stats", response_model=AdminStats, summary="Dashboard counters")
def stats(db: DbSession, _admin: AdminUser) -> AdminStats:
    now = utcnow()
    today_local = to_local(now).date()
    day_start = datetime.combine(today_local, time.min, tzinfo=settings.tz).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)

    def count(stmt) -> int:
        return int(db.scalar(stmt) or 0)

    ai_total = count(select(func.count(PreVisitSummary.id))) + count(select(func.count(PostVisitSummary.id)))
    ai_fallback = count(
        select(func.count(PreVisitSummary.id)).where(PreVisitSummary.source == "fallback")
    ) + count(select(func.count(PostVisitSummary.id)).where(PostVisitSummary.source == "fallback"))

    return AdminStats(
        patients=count(select(func.count(User.id)).where(User.role == Role.PATIENT)),
        doctors=count(select(func.count(DoctorProfile.id))),
        appointments_total=count(select(func.count(Appointment.id))),
        appointments_upcoming=count(
            select(func.count(Appointment.id)).where(
                Appointment.status == AppointmentStatus.CONFIRMED, Appointment.start_at >= now
            )
        ),
        appointments_today=count(
            select(func.count(Appointment.id)).where(
                Appointment.status.in_(BLOCKING_STATUSES),
                Appointment.start_at >= day_start,
                Appointment.start_at < day_end,
            )
        ),
        cancellations=count(
            select(func.count(Appointment.id)).where(Appointment.status == AppointmentStatus.CANCELLED)
        ),
        notifications_pending=count(
            select(func.count(Notification.id)).where(
                Notification.status.in_([JobStatus.PENDING, JobStatus.FAILED])
            )
        ),
        notifications_dead=count(select(func.count(Notification.id)).where(Notification.status == JobStatus.DEAD)),
        calendar_tasks_pending=count(
            select(func.count(CalendarTask.id)).where(CalendarTask.status.in_([JobStatus.PENDING, JobStatus.FAILED]))
        ),
        llm_fallback_rate=round(ai_fallback / ai_total, 3) if ai_total else 0.0,
    )


@router.get("/notifications", response_model=list[NotificationOut], summary="Email outbox")
def list_notifications(
    db: DbSession,
    _admin: AdminUser,
    job_status: Annotated[str | None, Query(alias="status", description="pending / sent / failed / dead")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
) -> list[NotificationOut]:
    stmt = select(Notification).order_by(Notification.created_at.desc()).limit(limit)
    if job_status:
        stmt = stmt.where(Notification.status == job_status)
    return [NotificationOut.model_validate(row) for row in db.scalars(stmt)]


@router.post(
    "/notifications/{notification_id}/requeue",
    response_model=NotificationOut,
    summary="Retry a dead notification",
)
def requeue_notification(notification_id: int, db: DbSession, admin: AdminUser) -> NotificationOut:
    note = email_service.requeue(db, notification_id)
    if note is None:
        raise NotFound("Notification not found.")
    _audit(db, admin, "notification.requeue", "notification", notification_id)
    db.commit()
    return NotificationOut.model_validate(note)


@router.get("/calendar-tasks", response_model=list[CalendarTaskOut], summary="Google Calendar outbox")
def list_calendar_tasks(
    db: DbSession,
    _admin: AdminUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
) -> list[CalendarTaskOut]:
    rows = db.scalars(select(CalendarTask).order_by(CalendarTask.created_at.desc()).limit(limit))
    return [CalendarTaskOut.model_validate(row) for row in rows]


@router.get("/appointments", response_model=list[AppointmentOut], summary="All appointments")
def list_appointments(
    db: DbSession,
    _admin: AdminUser,
    appointment_status: Annotated[str | None, Query(alias="status")] = None,
    doctor_id: Annotated[int | None, Query()] = None,
    on: Annotated[date | None, Query(description="Clinic-local date filter")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[AppointmentOut]:
    expire_stale_holds(db)
    stmt = select(Appointment).options(*_APPOINTMENT_LOADS).order_by(Appointment.start_at.desc()).limit(limit)

    if appointment_status:
        stmt = stmt.where(Appointment.status == appointment_status)
    if doctor_id:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if on:
        day_start = datetime.combine(on, time.min, tzinfo=settings.tz).astimezone(timezone.utc)
        stmt = stmt.where(Appointment.start_at >= day_start, Appointment.start_at < day_start + timedelta(days=1))

    return [appointment_out(a, viewer_role=Role.ADMIN) for a in db.scalars(stmt)]


@router.get("/audit", response_model=list[dict], summary="Recent admin actions")
def audit_trail(db: DbSession, _admin: AdminUser, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
    return [
        {
            "id": row.id,
            "actor_user_id": row.actor_user_id,
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "meta": row.meta,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
