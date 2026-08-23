"""Pydantic request/response models.

These double as the API contract: FastAPI renders them into the OpenAPI schema
served at ``/docs``, so the field descriptions here are the API documentation.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

ORM = ConfigDict(from_attributes=True)

Password = Annotated[str, Field(min_length=8, max_length=128, description="At least 8 characters")]


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: Password
    full_name: Annotated[str, Field(min_length=2, max_length=160)]
    phone: str | None = Field(default=None, max_length=32)
    date_of_birth: date | None = None
    gender: str | None = Field(default=None, max_length=24)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ORM
    id: int
    email: EmailStr
    role: str
    full_name: str
    phone: str | None = None
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Seconds until the token expires")
    user: UserOut


# --------------------------------------------------------------------------
# Doctors
# --------------------------------------------------------------------------
class WorkingHourIn(BaseModel):
    weekday: Annotated[int, Field(ge=0, le=6, description="0 = Monday … 6 = Sunday")]
    start_time: time
    end_time: time

    @field_validator("end_time")
    @classmethod
    def _end_after_start(cls, v: time, info):
        start = info.data.get("start_time")
        if start and v <= start:
            raise ValueError("end_time must be after start_time")
        return v


class WorkingHourOut(WorkingHourIn):
    model_config = ORM
    id: int


class DoctorCreate(BaseModel):
    """Admin-only. Creates the login *and* the clinical profile in one call."""

    email: EmailStr
    password: Password
    full_name: Annotated[str, Field(min_length=2, max_length=160)]
    phone: str | None = Field(default=None, max_length=32)
    specialisation: Annotated[str, Field(min_length=2, max_length=120)]
    qualifications: str | None = Field(default=None, max_length=255)
    bio: str | None = None
    experience_years: Annotated[int, Field(ge=0, le=70)] = 0
    consultation_fee: Annotated[float, Field(ge=0)] = 0
    slot_duration_minutes: Annotated[int, Field(ge=5, le=240)] = 30
    buffer_minutes: Annotated[int, Field(ge=0, le=120)] = 0
    room: str | None = Field(default=None, max_length=64)
    working_hours: list[WorkingHourIn] = Field(default_factory=list)


class DoctorUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    specialisation: str | None = Field(default=None, max_length=120)
    qualifications: str | None = Field(default=None, max_length=255)
    bio: str | None = None
    experience_years: int | None = Field(default=None, ge=0, le=70)
    consultation_fee: float | None = Field(default=None, ge=0)
    slot_duration_minutes: int | None = Field(default=None, ge=5, le=240)
    buffer_minutes: int | None = Field(default=None, ge=0, le=120)
    room: str | None = Field(default=None, max_length=64)
    is_accepting_patients: bool | None = None
    is_active: bool | None = None
    working_hours: list[WorkingHourIn] | None = Field(
        default=None, description="If provided, replaces the entire weekly schedule"
    )


class DoctorOut(BaseModel):
    id: int
    user_id: int
    full_name: str
    email: EmailStr
    phone: str | None = None
    specialisation: str
    qualifications: str | None = None
    bio: str | None = None
    experience_years: int
    consultation_fee: float
    slot_duration_minutes: int
    buffer_minutes: int
    room: str | None = None
    is_accepting_patients: bool
    is_active: bool
    calendar_connected: bool = False
    working_hours: list[WorkingHourOut] = Field(default_factory=list)
    upcoming_leaves: list[date] = Field(default_factory=list)


class LeaveCreate(BaseModel):
    leave_date: date
    reason: str | None = Field(default=None, max_length=255)
    #: When false the endpoint only *reports* what would be cancelled.
    confirm: bool = Field(
        default=False,
        description="False = dry run returning the impact report; True = apply the leave and cancel affected bookings",
    )


class AffectedAppointment(BaseModel):
    appointment_id: int
    reference: str
    patient_name: str
    patient_email: EmailStr
    start_at: datetime
    start_at_local: str


class LeaveImpact(BaseModel):
    doctor_id: int
    leave_date: date
    applied: bool = Field(description="False for a dry run")
    affected_count: int
    affected: list[AffectedAppointment]
    notifications_queued: int = 0
    message: str


class LeaveOut(BaseModel):
    model_config = ORM
    id: int
    doctor_id: int
    leave_date: date
    reason: str | None = None
    affected_appointment_count: int
    created_at: datetime


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
class SlotOut(BaseModel):
    start_at: datetime = Field(description="UTC instant the slot begins")
    end_at: datetime
    label: str = Field(description="Clinic-local time, e.g. '10:30 AM'")
    available: bool
    reason: str | None = Field(default=None, description="Why it is unavailable: booked / held / past / leave")


class DayAvailability(BaseModel):
    doctor_id: int
    date: date
    is_leave: bool
    is_working_day: bool
    timezone: str
    slot_duration_minutes: int
    slots: list[SlotOut]


# --------------------------------------------------------------------------
# Appointments
# --------------------------------------------------------------------------
class HoldRequest(BaseModel):
    doctor_id: int
    start_at: datetime = Field(description="Slot start as returned by /api/doctors/{id}/availability")
    reason_for_visit: str | None = Field(default=None, max_length=255)


class SymptomFormIn(BaseModel):
    symptoms: Annotated[str, Field(min_length=5, max_length=4000)]
    duration_days: int | None = Field(default=None, ge=0, le=3650)
    severity: int | None = Field(default=None, ge=1, le=10)
    existing_conditions: str | None = Field(default=None, max_length=2000)
    current_medications: str | None = Field(default=None, max_length=2000)
    allergies: str | None = Field(default=None, max_length=1000)


class ConfirmRequest(BaseModel):
    symptom_form: SymptomFormIn


class RescheduleRequest(BaseModel):
    start_at: datetime
    reason: str | None = Field(default=None, max_length=255)


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


class PreVisitSummaryOut(BaseModel):
    model_config = ORM
    urgency: str
    chief_complaint: str
    suggested_questions: list[str]
    red_flags: list[str]
    summary_note: str | None = None
    source: str = Field(description="'llm' when generated by the model, 'fallback' when rule-based")
    model: str | None = None
    latency_ms: int | None = None
    error: str | None = None
    created_at: datetime


class SymptomReportOut(BaseModel):
    model_config = ORM
    symptoms: str
    duration_days: int | None = None
    severity: int | None = None
    existing_conditions: str | None = None
    current_medications: str | None = None
    allergies: str | None = None


class PersonBrief(BaseModel):
    id: int
    full_name: str
    email: EmailStr
    phone: str | None = None


class DoctorBrief(BaseModel):
    id: int
    full_name: str
    specialisation: str
    room: str | None = None


class AppointmentOut(BaseModel):
    id: int
    reference: str
    status: str
    start_at: datetime
    end_at: datetime
    start_at_local: str
    hold_expires_at: datetime | None = None
    hold_seconds_remaining: int | None = None
    reason_for_visit: str | None = None
    mode: str
    cancelled_by: str | None = None
    cancellation_reason: str | None = None
    reschedule_count: int = 0
    patient: PersonBrief
    doctor: DoctorBrief
    symptom_report: SymptomReportOut | None = None
    previsit_summary: PreVisitSummaryOut | None = None
    has_consultation: bool = False
    calendar_synced: bool = False
    created_at: datetime


# --------------------------------------------------------------------------
# Consultation / post-visit
# --------------------------------------------------------------------------
class PrescriptionItemIn(BaseModel):
    drug_name: Annotated[str, Field(min_length=1, max_length=160)]
    dosage: Annotated[str, Field(min_length=1, max_length=80)]
    frequency: Literal["OD", "BD", "TDS", "QID", "QHS", "SOS"]
    duration_days: Annotated[int, Field(ge=1, le=180)] = 5
    instructions: str | None = Field(default=None, max_length=255)
    start_date: date | None = None


class ConsultationIn(BaseModel):
    clinical_notes: Annotated[str, Field(min_length=10, max_length=8000)]
    diagnosis: str | None = Field(default=None, max_length=255)
    follow_up_date: date | None = None
    prescription_items: list[PrescriptionItemIn] = Field(default_factory=list)
    prescription_notes: str | None = None


class PrescriptionItemOut(PrescriptionItemIn):
    model_config = ORM
    id: int
    reminder_count: int = 0


class PostVisitSummaryOut(BaseModel):
    model_config = ORM
    patient_summary: str
    medication_schedule: list[dict]
    follow_up_steps: list[str]
    warning_signs: list[str]
    source: str
    model: str | None = None
    latency_ms: int | None = None
    error: str | None = None
    created_at: datetime


class ConsultationOut(BaseModel):
    model_config = ORM
    id: int
    appointment_id: int
    appointment_reference: str
    clinical_notes: str
    diagnosis: str | None = None
    follow_up_date: date | None = None
    prescription_items: list[PrescriptionItemOut] = Field(default_factory=list)
    postvisit_summary: PostVisitSummaryOut | None = None
    created_at: datetime


class MedicationReminderOut(BaseModel):
    model_config = ORM
    id: int
    drug_name: str
    dosage: str
    instructions: str | None = None
    due_at: datetime
    due_at_local: str
    status: str


# --------------------------------------------------------------------------
# Ops / admin
# --------------------------------------------------------------------------
class NotificationOut(BaseModel):
    model_config = ORM
    id: int
    template: str
    to_email: EmailStr
    subject: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    last_error: str | None = None
    provider: str | None = None
    sent_at: datetime | None = None
    created_at: datetime


class CalendarTaskOut(BaseModel):
    model_config = ORM
    id: int
    action: str
    appointment_id: int
    role: str
    status: str
    attempts: int
    last_error: str | None = None
    external_event_id: str | None = None
    created_at: datetime


class IntegrationStatus(BaseModel):
    name: str
    configured: bool
    mode: str
    detail: str


class SystemHealth(BaseModel):
    status: Literal["ok", "degraded"]
    environment: str
    database: str
    timezone: str
    worker_enabled: bool
    integrations: list[IntegrationStatus]
    version: str


class AdminStats(BaseModel):
    patients: int
    doctors: int
    appointments_total: int
    appointments_upcoming: int
    appointments_today: int
    cancellations: int
    notifications_pending: int
    notifications_dead: int
    calendar_tasks_pending: int
    llm_fallback_rate: float = Field(description="Share of AI summaries produced by the rule-based fallback")


class MessageOut(BaseModel):
    message: str


class ErrorOut(BaseModel):
    detail: str
    code: str | None = None
