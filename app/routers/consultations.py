"""Post-visit notes, prescriptions, the patient-friendly summary, and reminders."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.errors import Conflict, InvalidState, NotFound, PermissionDenied
from app.models import (
    Appointment,
    AppointmentStatus,
    AuditLog,
    Consultation,
    DoctorProfile,
    JobStatus,
    MedicationReminder,
    PostVisitSummary,
    Prescription,
    PrescriptionItem,
    Role,
    User,
    utcnow,
)
from app.schemas import ConsultationIn, ConsultationOut, MedicationReminderOut, MessageOut
from app.security import CurrentUser
from app.serializers import consultation_out
from app.services import llm
from app.services import notifications as notify
from app.services.slots import local_label

router = APIRouter(prefix="/api", tags=["Consultations"])

DbSession = Annotated[Session, Depends(get_db)]


def _load_appointment(db: Session, appointment_id: int) -> Appointment:
    appointment = db.scalar(
        select(Appointment)
        .options(
            selectinload(Appointment.patient),
            selectinload(Appointment.doctor).selectinload(DoctorProfile.user),
            selectinload(Appointment.consultation).selectinload(Consultation.prescription).selectinload(Prescription.items),
            selectinload(Appointment.consultation).selectinload(Consultation.postvisit_summary),
        )
        .where(Appointment.id == appointment_id)
    )
    if appointment is None:
        raise NotFound("Appointment not found.")
    return appointment


def _persist_postvisit_summary(db: Session, consultation: Consultation, items: list[dict]) -> PostVisitSummary:
    """Run the model (or its fallback) and store the result. Never raises."""
    result = llm.generate_postvisit_summary(
        notes=consultation.clinical_notes,
        diagnosis=consultation.diagnosis,
        follow_up_date=consultation.follow_up_date.isoformat() if consultation.follow_up_date else None,
        prescription_items=items,
    )

    summary = consultation.postvisit_summary or PostVisitSummary(consultation_id=consultation.id)
    summary.patient_summary = result.patient_summary
    summary.medication_schedule = result.medication_schedule
    summary.follow_up_steps = result.follow_up_steps
    summary.warning_signs = result.warning_signs
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
    consultation.postvisit_summary = summary
    return summary


@router.post(
    "/appointments/{appointment_id}/consultation",
    response_model=ConsultationOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Consultations"],
    summary="File post-visit notes and prescription (doctor only)",
    description=(
        "Marks the appointment `completed`, stores the clinical notes and prescription, "
        "materialises a medication reminder for every scheduled dose, generates the "
        "patient-friendly summary with the LLM (falling back to a template if it is down), "
        "and queues the summary email."
    ),
)
def create_consultation(
    appointment_id: int, payload: ConsultationIn, db: DbSession, user: CurrentUser
) -> ConsultationOut:
    appointment = _load_appointment(db, appointment_id)

    if user.role == Role.DOCTOR:
        if appointment.doctor.user_id != user.id:
            raise PermissionDenied("This is not your appointment.")
    elif user.role != Role.ADMIN:
        raise PermissionDenied("Only the treating doctor can file consultation notes.")

    if appointment.consultation is not None:
        raise Conflict(
            "Notes have already been filed for this appointment. Use PATCH to amend them.",
            code="CONSULTATION_EXISTS",
        )
    if appointment.status in {AppointmentStatus.CANCELLED, AppointmentStatus.EXPIRED}:
        raise InvalidState(f"Cannot file notes for a {appointment.status} appointment.")

    # ---- clinical record -------------------------------------------------
    consultation = Consultation(
        appointment_id=appointment.id,
        doctor_id=appointment.doctor_id,
        clinical_notes=payload.clinical_notes.strip(),
        diagnosis=payload.diagnosis,
        follow_up_date=payload.follow_up_date,
    )
    db.add(consultation)
    db.flush()

    item_dicts: list[dict] = []
    if payload.prescription_items:
        prescription = Prescription(
            consultation_id=consultation.id,
            patient_id=appointment.patient_id,
            notes=payload.prescription_notes,
        )
        db.add(prescription)
        db.flush()

        for entry in payload.prescription_items:
            item = PrescriptionItem(
                prescription_id=prescription.id,
                drug_name=entry.drug_name.strip(),
                dosage=entry.dosage.strip(),
                frequency=entry.frequency,
                duration_days=entry.duration_days,
                instructions=entry.instructions,
                start_date=entry.start_date,
            )
            db.add(item)
            db.flush()
            notify.schedule_medication_reminders(db, item, appointment.patient_id)
            item_dicts.append(
                {
                    "drug_name": item.drug_name,
                    "dosage": item.dosage,
                    "frequency": item.frequency,
                    "duration_days": item.duration_days,
                    "instructions": item.instructions,
                }
            )

    appointment.status = AppointmentStatus.COMPLETED
    db.commit()  # the clinical record is durable before we touch the model

    # ---- AI summary + email (never blocks the record) --------------------
    db.refresh(consultation)
    _persist_postvisit_summary(db, consultation, item_dicts)
    notify.on_postvisit_summary_ready(db, consultation)
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="consultation.create",
            entity_type="appointment",
            entity_id=str(appointment.id),
            meta={"medicines": len(item_dicts), "ai_source": consultation.postvisit_summary.source},
        )
    )
    db.commit()

    # Re-read through this session's identity map: `appointment` was loaded
    # before the consultation existed, so its cached `.consultation` is stale.
    db.refresh(consultation)
    return consultation_out(consultation)


@router.get(
    "/appointments/{appointment_id}/consultation",
    response_model=ConsultationOut,
    summary="Read the consultation record and patient summary",
)
def get_consultation(appointment_id: int, db: DbSession, user: CurrentUser) -> ConsultationOut:
    appointment = _load_appointment(db, appointment_id)

    allowed = (
        user.role == Role.ADMIN
        or (user.role == Role.PATIENT and appointment.patient_id == user.id)
        or (user.role == Role.DOCTOR and appointment.doctor.user_id == user.id)
    )
    if not allowed:
        raise PermissionDenied("You are not allowed to view this consultation.")
    if appointment.consultation is None:
        raise NotFound("The doctor has not filed notes for this visit yet.")

    return consultation_out(appointment.consultation)


@router.post(
    "/appointments/{appointment_id}/consultation/regenerate-summary",
    response_model=ConsultationOut,
    summary="Re-run the patient-friendly summary (doctor/admin)",
)
def regenerate_postvisit(appointment_id: int, db: DbSession, user: CurrentUser) -> ConsultationOut:
    appointment = _load_appointment(db, appointment_id)
    if user.role == Role.PATIENT:
        raise PermissionDenied("Only the doctor or an admin can regenerate the summary.")
    if user.role == Role.DOCTOR and appointment.doctor.user_id != user.id:
        raise PermissionDenied("This is not your appointment.")
    if appointment.consultation is None:
        raise NotFound("No consultation notes to summarise.")

    consultation = appointment.consultation
    items = [
        {
            "drug_name": i.drug_name,
            "dosage": i.dosage,
            "frequency": i.frequency,
            "duration_days": i.duration_days,
            "instructions": i.instructions,
        }
        for i in (consultation.prescription.items if consultation.prescription else [])
    ]
    _persist_postvisit_summary(db, consultation, items)
    db.commit()
    db.refresh(consultation)
    return consultation_out(consultation)


# ==========================================================================
# Patient-facing medication schedule
# ==========================================================================
@router.get(
    "/me/medication-reminders",
    response_model=list[MedicationReminderOut],
    tags=["Patients"],
    summary="My upcoming medication reminders",
)
def my_medication_reminders(
    db: DbSession,
    user: CurrentUser,
    upcoming_only: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=300)] = 100,
) -> list[MedicationReminderOut]:
    if user.role != Role.PATIENT:
        raise PermissionDenied("Only patients have a medication schedule.")

    stmt = (
        select(MedicationReminder)
        .options(selectinload(MedicationReminder.item))
        .where(MedicationReminder.patient_id == user.id)
        .order_by(MedicationReminder.due_at)
        .limit(limit)
    )
    if upcoming_only:
        stmt = stmt.where(MedicationReminder.status == JobStatus.PENDING, MedicationReminder.due_at >= utcnow())

    return [
        MedicationReminderOut(
            id=r.id,
            drug_name=r.item.drug_name,
            dosage=r.item.dosage,
            instructions=r.item.instructions,
            due_at=r.due_at,
            due_at_local=local_label(r.due_at),
            status=r.status,
        )
        for r in db.scalars(stmt)
    ]


@router.delete(
    "/me/medication-reminders/{reminder_id}",
    response_model=MessageOut,
    tags=["Patients"],
    summary="Stop one medication reminder",
)
def cancel_medication_reminder(reminder_id: int, db: DbSession, user: CurrentUser) -> MessageOut:
    reminder = db.get(MedicationReminder, reminder_id)
    if reminder is None or reminder.patient_id != user.id:
        raise NotFound("Reminder not found.")
    reminder.status = JobStatus.CANCELLED
    db.commit()
    return MessageOut(message="Reminder cancelled. Keep taking your medicine as prescribed.")
