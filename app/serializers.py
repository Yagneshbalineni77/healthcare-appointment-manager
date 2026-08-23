"""ORM -> response-model conversion.

Kept out of the routers so the same appointment payload is produced for the
patient, doctor and admin portals, with role-based redaction applied in exactly
one place.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Appointment, CalendarAccount, Consultation, DoctorProfile, Role, utcnow
from app.schemas import (
    AppointmentOut,
    ConsultationOut,
    DoctorBrief,
    DoctorOut,
    PersonBrief,
    PreVisitSummaryOut,
    PrescriptionItemOut,
    SymptomReportOut,
    WorkingHourOut,
)
from app.services.slots import local_label, to_local


def doctor_out(db: Session, profile: DoctorProfile, *, include_leaves: bool = True) -> DoctorOut:
    today = to_local(utcnow()).date()
    connected = False
    account = db.query(CalendarAccount).filter(CalendarAccount.user_id == profile.user_id).one_or_none()
    if account is not None:
        connected = account.is_connected

    return DoctorOut(
        id=profile.id,
        user_id=profile.user_id,
        full_name=profile.user.full_name,
        email=profile.user.email,
        phone=profile.user.phone,
        specialisation=profile.specialisation,
        qualifications=profile.qualifications,
        bio=profile.bio,
        experience_years=profile.experience_years,
        consultation_fee=profile.consultation_fee,
        slot_duration_minutes=profile.slot_duration_minutes,
        buffer_minutes=profile.buffer_minutes,
        room=profile.room,
        is_accepting_patients=profile.is_accepting_patients,
        is_active=profile.user.is_active,
        calendar_connected=connected,
        working_hours=[WorkingHourOut.model_validate(w) for w in profile.working_hours],
        upcoming_leaves=[l.leave_date for l in profile.leaves if l.leave_date >= today][:30] if include_leaves else [],
    )


def appointment_out(appointment: Appointment, *, viewer_role: str) -> AppointmentOut:
    """Serialise one appointment.

    Role-based redaction: the symptom form and the AI triage brief are clinical
    data. The patient sees their own; the doctor and admin see them for their
    appointments. Nobody sees another patient's.
    """
    hold_remaining = None
    if appointment.hold_expires_at:
        hold_remaining = max(0, int((appointment.hold_expires_at - utcnow()).total_seconds()))

    show_clinical = viewer_role in {Role.DOCTOR, Role.ADMIN, Role.PATIENT}

    return AppointmentOut(
        id=appointment.id,
        reference=appointment.reference,
        status=appointment.status,
        start_at=appointment.start_at,
        end_at=appointment.end_at,
        start_at_local=local_label(appointment.start_at),
        hold_expires_at=appointment.hold_expires_at,
        hold_seconds_remaining=hold_remaining,
        reason_for_visit=appointment.reason_for_visit,
        mode=appointment.mode,
        cancelled_by=appointment.cancelled_by,
        cancellation_reason=appointment.cancellation_reason,
        reschedule_count=appointment.reschedule_count,
        patient=PersonBrief(
            id=appointment.patient.id,
            full_name=appointment.patient.full_name,
            email=appointment.patient.email,
            phone=appointment.patient.phone,
        ),
        doctor=DoctorBrief(
            id=appointment.doctor.id,
            full_name=appointment.doctor.user.full_name,
            specialisation=appointment.doctor.specialisation,
            room=appointment.doctor.room,
        ),
        symptom_report=(
            SymptomReportOut.model_validate(appointment.symptom_report)
            if appointment.symptom_report and show_clinical
            else None
        ),
        previsit_summary=(
            PreVisitSummaryOut.model_validate(appointment.previsit_summary)
            if appointment.previsit_summary and show_clinical
            else None
        ),
        has_consultation=appointment.consultation is not None,
        calendar_synced=bool(appointment.patient_calendar_event_id or appointment.doctor_calendar_event_id),
        created_at=appointment.created_at,
    )


def consultation_out(consultation: Consultation) -> ConsultationOut:
    items = []
    if consultation.prescription:
        for item in consultation.prescription.items:
            items.append(
                PrescriptionItemOut(
                    id=item.id,
                    drug_name=item.drug_name,
                    dosage=item.dosage,
                    frequency=item.frequency,
                    duration_days=item.duration_days,
                    instructions=item.instructions,
                    start_date=item.start_date,
                    reminder_count=len(item.reminders),
                )
            )

    return ConsultationOut(
        id=consultation.id,
        appointment_id=consultation.appointment_id,
        appointment_reference=consultation.appointment.reference,
        clinical_notes=consultation.clinical_notes,
        diagnosis=consultation.diagnosis,
        follow_up_date=consultation.follow_up_date,
        prescription_items=items,
        postvisit_summary=(
            consultation.postvisit_summary if consultation.postvisit_summary else None
        ),
        created_at=consultation.created_at,
    )
