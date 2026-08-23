"""Lifecycle orchestration: what gets emailed and calendared, and when.

Every function here only *enqueues* work (email rows, calendar rows, reminder
rows). Nothing performs I/O. That keeps the request path fast and, more
importantly, keeps side effects inside the same transaction as the state change
that caused them — see :mod:`app.services.email` for why that matters.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    FREQUENCY_TIMES,
    Appointment,
    AppointmentStatus,
    Consultation,
    JobStatus,
    MedicationReminder,
    PrescriptionItem,
    Role,
    User,
    utcnow,
)
from app.services import email as email_service
from app.services import gcal
from app.services.slots import local_label, to_local

logger = logging.getLogger("clinix.notifications")


def _appointment_ctx(appointment: Appointment) -> dict:
    return {
        "reference": appointment.reference,
        "patient_name": appointment.patient.full_name,
        "doctor_name": appointment.doctor.user.full_name,
        "specialisation": appointment.doctor.specialisation,
        "room": appointment.doctor.room,
        "when_local": local_label(appointment.start_at),
    }


# ==========================================================================
# Account lifecycle
# ==========================================================================
def on_user_registered(db: Session, user: User) -> None:
    email_service.queue(
        db,
        template="welcome",
        to_email=user.email,
        to_name=user.full_name,
        ctx={"patient_name": user.full_name},
        idempotency_key=email_service.make_key("welcome", user.id),
        user_id=user.id,
    )


# ==========================================================================
# Appointment lifecycle
# ==========================================================================
def on_appointment_confirmed(db: Session, appointment: Appointment) -> None:
    """Booking confirmation to both parties + calendar events on both calendars."""
    ctx = _appointment_ctx(appointment)

    email_service.queue(
        db,
        template="booking_confirmed_patient",
        to_email=appointment.patient.email,
        to_name=appointment.patient.full_name,
        ctx=ctx,
        idempotency_key=email_service.make_key("confirm-p", appointment.id),
        user_id=appointment.patient_id,
        appointment_id=appointment.id,
    )

    summary = appointment.previsit_summary
    email_service.queue(
        db,
        template="booking_confirmed_doctor",
        to_email=appointment.doctor.user.email,
        to_name=appointment.doctor.user.full_name,
        ctx={
            **ctx,
            "urgency": summary.urgency if summary else None,
            "chief_complaint": summary.chief_complaint if summary else (appointment.reason_for_visit or ""),
        },
        idempotency_key=email_service.make_key("confirm-d", appointment.id),
        user_id=appointment.doctor.user_id,
        appointment_id=appointment.id,
    )

    gcal.queue_for_both(db, appointment, "create", key_suffix="v1")


def on_appointment_rescheduled(db: Session, appointment: Appointment, previous_start: datetime) -> None:
    ctx = {**_appointment_ctx(appointment), "previous_when": local_label(previous_start)}
    version = f"v{appointment.reschedule_count}"

    for user, role in ((appointment.patient, "patient"), (appointment.doctor.user, "doctor")):
        email_service.queue(
            db,
            template="appointment_rescheduled",
            to_email=user.email,
            to_name=user.full_name,
            ctx={**ctx, "patient_name": user.full_name if role == "patient" else appointment.patient.full_name},
            idempotency_key=email_service.make_key("resched", appointment.id, role, version),
            user_id=user.id,
            appointment_id=appointment.id,
        )

    gcal.queue_for_both(db, appointment, "update", key_suffix=version)


def on_appointment_cancelled(
    db: Session,
    appointment: Appointment,
    *,
    actor: str,
    reason: str | None = None,
    leave_date: str | None = None,
) -> None:
    """Cancellation notice. A leave-driven cancellation gets its own apologetic template."""
    ctx = {**_appointment_ctx(appointment), "reason": reason or "—", "cancelled_by": actor.replace("_", " ")}
    template = "appointment_cancelled"
    if leave_date:
        template = "leave_cancellation"
        ctx["leave_date"] = leave_date

    email_service.queue(
        db,
        template=template,
        to_email=appointment.patient.email,
        to_name=appointment.patient.full_name,
        ctx=ctx,
        idempotency_key=email_service.make_key("cancel-p", appointment.id),
        user_id=appointment.patient_id,
        appointment_id=appointment.id,
    )

    # The doctor does not need the apology template, just the fact.
    email_service.queue(
        db,
        template="appointment_cancelled",
        to_email=appointment.doctor.user.email,
        to_name=appointment.doctor.user.full_name,
        ctx={**ctx, "patient_name": appointment.doctor.user.full_name},
        idempotency_key=email_service.make_key("cancel-d", appointment.id),
        user_id=appointment.doctor.user_id,
        appointment_id=appointment.id,
    )

    gcal.queue_for_both(db, appointment, "delete", key_suffix="cancel")


def on_postvisit_summary_ready(db: Session, consultation: Consultation) -> None:
    appointment = consultation.appointment
    summary = consultation.postvisit_summary
    if summary is None:
        return

    email_service.queue(
        db,
        template="postvisit_ready",
        to_email=appointment.patient.email,
        to_name=appointment.patient.full_name,
        ctx={
            **_appointment_ctx(appointment),
            "summary": summary.patient_summary.replace("\n", "<br>"),
            "steps": list(summary.follow_up_steps or []),
            "warnings": list(summary.warning_signs or []),
        },
        idempotency_key=email_service.make_key("postvisit", consultation.id),
        user_id=appointment.patient_id,
        appointment_id=appointment.id,
    )


# ==========================================================================
# Medication reminders
# ==========================================================================
def schedule_medication_reminders(db: Session, item: PrescriptionItem, patient_id: int) -> int:
    """Materialise one row per dose for the whole course.

    Doses in the past are skipped, so writing a prescription at 3pm does not
    immediately fire the 8am reminder. ``SOS`` (as-needed) drugs get none.
    """
    times = FREQUENCY_TIMES.get(item.frequency.upper(), ())
    if not times:
        return 0

    start_date = item.start_date or to_local(utcnow()).date()
    now = utcnow()
    created = 0

    for day_offset in range(item.duration_days):
        day = start_date + timedelta(days=day_offset)
        for hhmm in times:
            hour, minute = (int(part) for part in hhmm.split(":"))
            local_due = datetime.combine(day, time(hour, minute), tzinfo=settings.tz)
            due_at = local_due.astimezone(now.tzinfo).replace(second=0, microsecond=0)
            if due_at <= now:
                continue
            db.add(
                MedicationReminder(
                    prescription_item_id=item.id,
                    patient_id=patient_id,
                    due_at=due_at,
                    status=JobStatus.PENDING,
                )
            )
            created += 1

    db.flush()
    return created


def cancel_medication_reminders(db: Session, prescription_item_ids: list[int]) -> int:
    if not prescription_item_ids:
        return 0
    rows = (
        db.execute(
            select(MedicationReminder).where(
                MedicationReminder.prescription_item_id.in_(prescription_item_ids),
                MedicationReminder.status == JobStatus.PENDING,
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = JobStatus.CANCELLED
    return len(rows)


def queue_due_medication_reminders(db: Session, limit: int = 100) -> int:
    """Turn due doses into outbox emails. Called by the worker."""
    due = (
        db.execute(
            select(MedicationReminder)
            .where(MedicationReminder.status == JobStatus.PENDING, MedicationReminder.due_at <= utcnow())
            .order_by(MedicationReminder.due_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    queued = 0
    for reminder in due:
        item = reminder.item
        patient = db.get(User, reminder.patient_id)
        if item is None or patient is None:
            reminder.status = JobStatus.CANCELLED
            continue

        doctor_name = ""
        prescription = item.prescription
        if prescription and prescription.consultation and prescription.consultation.appointment:
            doctor_name = prescription.consultation.appointment.doctor.user.full_name

        email_service.queue(
            db,
            template="medication_reminder",
            to_email=patient.email,
            to_name=patient.full_name,
            ctx={
                "patient_name": patient.full_name,
                "drug_name": item.drug_name,
                "dosage": item.dosage,
                "instructions": item.instructions,
                "doctor_name": doctor_name,
            },
            idempotency_key=email_service.make_key("med", reminder.id),
            user_id=patient.id,
        )
        reminder.status = JobStatus.SENT
        reminder.sent_at = utcnow()
        queued += 1

    db.commit()
    return queued


# ==========================================================================
# Appointment reminders
# ==========================================================================
def queue_due_appointment_reminders(db: Session, limit: int = 100) -> int:
    """Queue the "your appointment is tomorrow" email.

    ``reminder_sent_at`` is the idempotency marker; it is cleared on reschedule
    so a moved appointment earns a fresh reminder.
    """
    now = utcnow()
    window_end = now + timedelta(hours=settings.reminder_lead_hours)

    upcoming = (
        db.execute(
            select(Appointment)
            .where(
                Appointment.status == AppointmentStatus.CONFIRMED,
                Appointment.reminder_sent_at.is_(None),
                Appointment.start_at > now,
                Appointment.start_at <= window_end,
            )
            .order_by(Appointment.start_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    queued = 0
    for appointment in upcoming:
        ctx = _appointment_ctx(appointment)
        email_service.queue(
            db,
            template="appointment_reminder",
            to_email=appointment.patient.email,
            to_name=appointment.patient.full_name,
            ctx=ctx,
            idempotency_key=email_service.make_key("remind-p", appointment.id),
            user_id=appointment.patient_id,
            appointment_id=appointment.id,
        )
        email_service.queue(
            db,
            template="appointment_reminder",
            to_email=appointment.doctor.user.email,
            to_name=appointment.doctor.user.full_name,
            ctx={**ctx, "patient_name": appointment.doctor.user.full_name},
            idempotency_key=email_service.make_key("remind-d", appointment.id),
            user_id=appointment.doctor.user_id,
            appointment_id=appointment.id,
        )
        appointment.reminder_sent_at = utcnow()
        queued += 1

    db.commit()
    return queued
