"""Appointment booking, confirmation, reschedule and cancellation.

The booking flow is deliberately **two-phase**:

``POST /api/appointments/hold``    -> reserves the slot for SLOT_HOLD_MINUTES
``POST /api/appointments/{id}/confirm`` -> symptom form + AI triage + confirm

Phase one exists because the brief requires a symptom form *before* confirming
and an LLM call in between. Without a hold, that thinking time is a wide-open
race window: two patients could both be filling in forms for the same 10:30
slot and only discover the clash at submit. With it, the slot is already the
first patient's, and the second is told immediately.

Ordering inside ``confirm`` also matters. The appointment is committed as
``confirmed`` **before** the LLM is called, so a slow or dead model can never
cost a patient the slot they already reserved. The summary and the emails are
written in a second transaction; if the process dies in between, the background
worker backfills them (see ``app.workers.scheduler.backfill_missing_summaries``).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.errors import InvalidState, NotFound, PermissionDenied
from app.models import (
    BLOCKING_STATUSES,
    Appointment,
    AppointmentStatus,
    AuditLog,
    CancelActor,
    DoctorProfile,
    PreVisitSummary,
    Role,
    SymptomReport,
    User,
    utcnow,
)
from app.schemas import (
    AppointmentOut,
    CancelRequest,
    ConfirmRequest,
    HoldRequest,
    PreVisitSummaryOut,
    RescheduleRequest,
)
from app.security import CurrentUser
from app.serializers import appointment_out
from app.services import llm
from app.services import notifications as notify
from app.services.slots import (
    as_utc,
    cancel_appointment,
    confirm_hold,
    expire_stale_holds,
    hold_slot,
    move_appointment,
)

router = APIRouter(prefix="/api/appointments", tags=["Appointments"])

DbSession = Annotated[Session, Depends(get_db)]

_LOADS = (
    selectinload(Appointment.patient),
    selectinload(Appointment.doctor).selectinload(DoctorProfile.user),
    selectinload(Appointment.previsit_summary),
    selectinload(Appointment.symptom_report),
    selectinload(Appointment.consultation),
)


def _load(db: Session, appointment_id: int) -> Appointment:
    appointment = db.scalar(select(Appointment).options(*_LOADS).where(Appointment.id == appointment_id))
    if appointment is None:
        raise NotFound("Appointment not found.")
    return appointment


def _authorise(appointment: Appointment, user: User, *, action: str = "view") -> None:
    """Patients see their own; doctors see theirs; admins see everything."""
    if user.role == Role.ADMIN:
        return
    if user.role == Role.PATIENT and appointment.patient_id == user.id:
        return
    if user.role == Role.DOCTOR and appointment.doctor.user_id == user.id:
        return
    raise PermissionDenied(f"You are not allowed to {action} this appointment.")


def _symptom_form_dict(report: SymptomReport, patient: User) -> dict:
    age_sex = None
    if patient.date_of_birth:
        today = date.today()
        born = patient.date_of_birth
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        age_sex = f"{age}y {patient.gender or ''}".strip()
    elif patient.gender:
        age_sex = patient.gender

    return {
        "symptoms": report.symptoms,
        "duration_days": report.duration_days,
        "severity": report.severity,
        "existing_conditions": report.existing_conditions,
        "current_medications": report.current_medications,
        "allergies": report.allergies,
        "age_sex": age_sex,
    }


def persist_previsit_summary(db: Session, appointment: Appointment) -> PreVisitSummary:
    """Run the model (or its fallback) and store the result. Never raises."""
    result = llm.generate_previsit_summary(_symptom_form_dict(appointment.symptom_report, appointment.patient))

    summary = appointment.previsit_summary or PreVisitSummary(appointment_id=appointment.id)
    summary.urgency = result.urgency
    summary.chief_complaint = result.chief_complaint
    summary.suggested_questions = result.suggested_questions
    summary.red_flags = result.red_flags
    summary.summary_note = result.summary_note
    summary.source = result.source
    summary.model = result.model
    summary.prompt_version = llm.PROMPT_VERSION
    summary.latency_ms = result.latency_ms
    summary.attempts = result.attempts
    summary.error = result.error
    summary.raw_response = result.raw_response

    if summary.id is None:
        db.add(summary)
    db.flush()
    appointment.previsit_summary = summary
    return summary


# ==========================================================================
# Phase 1 — hold
# ==========================================================================
@router.post(
    "/hold",
    response_model=AppointmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Phase 1 — reserve a slot while the patient fills the symptom form",
    responses={
        409: {"description": "`SLOT_TAKEN` — another patient won the race, or `PATIENT_BUSY`"},
        422: {"description": "`SLOT_NOT_BOOKABLE` — leave day, outside working hours, past, or off-grid"},
    },
)
def hold(payload: HoldRequest, db: DbSession, user: CurrentUser) -> AppointmentOut:
    if user.role != Role.PATIENT:
        raise PermissionDenied("Only patients can book appointments.")

    doctor = db.scalar(
        select(DoctorProfile)
        .options(selectinload(DoctorProfile.user), selectinload(DoctorProfile.working_hours))
        .where(DoctorProfile.id == payload.doctor_id)
    )
    if doctor is None:
        raise NotFound("Doctor not found.")

    appointment = hold_slot(
        db,
        doctor=doctor,
        patient=user,
        start_at=payload.start_at,
        reason_for_visit=payload.reason_for_visit,
    )
    return appointment_out(_load(db, appointment.id), viewer_role=user.role)


# ==========================================================================
# Phase 2 — symptom form + AI triage + confirm
# ==========================================================================
@router.post(
    "/{appointment_id}/confirm",
    response_model=AppointmentOut,
    summary="Phase 2 — submit the symptom form and confirm the booking",
    description=(
        "Saves the symptom form, promotes the hold to `confirmed`, then generates the "
        "AI pre-visit summary. If the LLM is unavailable the booking still succeeds and the "
        "summary is produced by the rule-based fallback — `previsit_summary.source` says which."
    ),
    responses={410: {"description": "`HOLD_EXPIRED` — the reservation timed out; pick the slot again"}},
)
def confirm(appointment_id: int, payload: ConfirmRequest, db: DbSession, user: CurrentUser) -> AppointmentOut:
    appointment = _load(db, appointment_id)
    _authorise(appointment, user, action="confirm")
    if user.role != Role.PATIENT:
        raise PermissionDenied("Only the patient can submit the symptom form.")

    form = payload.symptom_form

    # ---- transaction 1: the slot is secured, whatever happens next -------
    report = appointment.symptom_report or SymptomReport(appointment_id=appointment.id)
    report.symptoms = form.symptoms.strip()
    report.duration_days = form.duration_days
    report.severity = form.severity
    report.existing_conditions = form.existing_conditions
    report.current_medications = form.current_medications
    report.allergies = form.allergies
    if report.id is None:
        db.add(report)
    db.flush()
    appointment.symptom_report = report

    confirm_hold(db, appointment)  # commits

    # ---- LLM: outside any transaction, and it cannot raise ---------------
    db.refresh(appointment)
    persist_previsit_summary(db, appointment)

    # ---- transaction 2: notifications + calendar -------------------------
    notify.on_appointment_confirmed(db, appointment)
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="appointment.confirm",
            entity_type="appointment",
            entity_id=str(appointment.id),
            meta={"reference": appointment.reference, "ai_source": appointment.previsit_summary.source},
        )
    )
    db.commit()

    return appointment_out(_load(db, appointment.id), viewer_role=user.role)


# ==========================================================================
# Reads
# ==========================================================================
@router.get("", response_model=list[AppointmentOut], summary="My appointments (role-aware)")
def my_appointments(
    db: DbSession,
    user: CurrentUser,
    scope: Annotated[str, Query(description="upcoming | past | all")] = "all",
    appointment_status: Annotated[str | None, Query(alias="status")] = None,
    on: Annotated[date | None, Query(description="Clinic-local date filter")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[AppointmentOut]:
    expire_stale_holds(db)

    stmt = select(Appointment).options(*_LOADS)
    if user.role == Role.PATIENT:
        stmt = stmt.where(Appointment.patient_id == user.id)
    elif user.role == Role.DOCTOR:
        profile_id = db.scalar(select(DoctorProfile.id).where(DoctorProfile.user_id == user.id))
        if profile_id is None:
            return []
        stmt = stmt.where(Appointment.doctor_id == profile_id)

    now = utcnow()
    if scope == "upcoming":
        stmt = stmt.where(Appointment.start_at >= now, Appointment.status.in_(BLOCKING_STATUSES)).order_by(Appointment.start_at)
    elif scope == "past":
        stmt = stmt.where(or_(Appointment.start_at < now, Appointment.status.notin_(BLOCKING_STATUSES))).order_by(
            Appointment.start_at.desc()
        )
    else:
        stmt = stmt.order_by(Appointment.start_at.desc())

    if appointment_status:
        stmt = stmt.where(Appointment.status == appointment_status)
    if on:
        day_start = datetime.combine(on, time.min, tzinfo=settings.tz).astimezone(timezone.utc)
        stmt = stmt.where(Appointment.start_at >= day_start, Appointment.start_at < day_start + timedelta(days=1))

    return [appointment_out(a, viewer_role=user.role) for a in db.scalars(stmt.limit(limit))]


@router.get("/{appointment_id}", response_model=AppointmentOut, summary="One appointment")
def get_appointment(appointment_id: int, db: DbSession, user: CurrentUser) -> AppointmentOut:
    appointment = _load(db, appointment_id)
    _authorise(appointment, user)
    return appointment_out(appointment, viewer_role=user.role)


@router.get(
    "/{appointment_id}/previsit-summary",
    response_model=PreVisitSummaryOut,
    summary="AI triage brief for the doctor",
)
def previsit_summary(appointment_id: int, db: DbSession, user: CurrentUser) -> PreVisitSummaryOut:
    appointment = _load(db, appointment_id)
    _authorise(appointment, user)
    if appointment.previsit_summary is None:
        raise NotFound("No pre-visit summary yet — the patient has not submitted the symptom form.")
    return PreVisitSummaryOut.model_validate(appointment.previsit_summary)


@router.post(
    "/{appointment_id}/previsit-summary/regenerate",
    response_model=PreVisitSummaryOut,
    summary="Re-run the AI triage (doctor/admin)",
    description="Useful when the summary was produced by the fallback because the model was down.",
)
def regenerate_previsit(appointment_id: int, db: DbSession, user: CurrentUser) -> PreVisitSummaryOut:
    appointment = _load(db, appointment_id)
    _authorise(appointment, user, action="regenerate the summary for")
    if user.role == Role.PATIENT:
        raise PermissionDenied("Only the doctor or an admin can regenerate the triage summary.")
    if appointment.symptom_report is None:
        raise InvalidState("There is no symptom form to summarise.")

    summary = persist_previsit_summary(db, appointment)
    db.commit()
    db.refresh(summary)
    return PreVisitSummaryOut.model_validate(summary)


# ==========================================================================
# Mutations
# ==========================================================================
@router.post("/{appointment_id}/reschedule", response_model=AppointmentOut, summary="Move to another slot")
def reschedule(appointment_id: int, payload: RescheduleRequest, db: DbSession, user: CurrentUser) -> AppointmentOut:
    appointment = _load(db, appointment_id)
    _authorise(appointment, user, action="reschedule")

    previous_start = as_utc(appointment.start_at)
    doctor = db.scalar(
        select(DoctorProfile)
        .options(selectinload(DoctorProfile.user), selectinload(DoctorProfile.working_hours))
        .where(DoctorProfile.id == appointment.doctor_id)
    )

    move_appointment(db, appointment, payload.start_at, doctor)  # commits

    db.refresh(appointment)
    notify.on_appointment_rescheduled(db, appointment, previous_start)
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="appointment.reschedule",
            entity_type="appointment",
            entity_id=str(appointment.id),
            meta={"from": previous_start.isoformat(), "to": appointment.start_at.isoformat(), "reason": payload.reason},
        )
    )
    db.commit()
    return appointment_out(_load(db, appointment.id), viewer_role=user.role)


@router.post("/{appointment_id}/cancel", response_model=AppointmentOut, summary="Cancel an appointment")
def cancel(appointment_id: int, payload: CancelRequest, db: DbSession, user: CurrentUser) -> AppointmentOut:
    appointment = _load(db, appointment_id)
    _authorise(appointment, user, action="cancel")

    actor = {Role.PATIENT: CancelActor.PATIENT, Role.DOCTOR: CancelActor.DOCTOR, Role.ADMIN: CancelActor.ADMIN}[user.role]
    was_confirmed = appointment.status == AppointmentStatus.CONFIRMED

    cancel_appointment(db, appointment, actor=str(actor), reason=payload.reason, commit=False)

    # A hold that the patient abandons needs no apology email.
    if was_confirmed:
        notify.on_appointment_cancelled(db, appointment, actor=str(actor), reason=payload.reason)

    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="appointment.cancel",
            entity_type="appointment",
            entity_id=str(appointment.id),
            meta={"reason": payload.reason, "by": str(actor)},
        )
    )
    db.commit()
    return appointment_out(_load(db, appointment.id), viewer_role=user.role)
