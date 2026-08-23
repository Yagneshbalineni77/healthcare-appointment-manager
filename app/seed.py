"""Demo data.

Runs once on first boot (``SEED_DEMO_DATA=true``) so a reviewer opening the
hosted URL sees a working clinic rather than three empty portals. It is written
against the **real services** — the seeded appointments go through the same
hold/confirm path a patient would, so the demo data proves the flow works.

Idempotent: if any user exists, seeding is skipped entirely.
"""

from __future__ import annotations

import logging
from datetime import date, time, timedelta

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import DoctorProfile, DoctorWorkingHour, Role, User, utcnow
from app.security import hash_password
from app.services.slots import day_availability, to_local

logger = logging.getLogger("clinix.seed")

DEMO_PASSWORD = "Password@123"

_WEEKDAYS = [0, 1, 2, 3, 4, 5]  # Mon-Sat

DOCTORS = [
    {
        "email": "meera.iyer@clinix.health", "full_name": "Dr. Meera Iyer", "specialisation": "Cardiology",
        "qualifications": "MBBS, MD (Medicine), DM (Cardiology)", "experience_years": 14, "consultation_fee": 900,
        "slot_duration_minutes": 30, "buffer_minutes": 5, "room": "C-204", "phone": "+91 98200 11223",
        "bio": "Interventional cardiologist with a focus on preventive heart care and post-MI rehabilitation.",
        "hours": [(time(10, 0), time(13, 0)), (time(17, 0), time(20, 0))],
    },
    {
        "email": "arjun.rao@clinix.health", "full_name": "Dr. Arjun Rao", "specialisation": "General Medicine",
        "qualifications": "MBBS, MD (General Medicine)", "experience_years": 9, "consultation_fee": 500,
        "slot_duration_minutes": 15, "buffer_minutes": 0, "room": "G-101", "phone": "+91 98200 44556",
        "bio": "First point of contact for fever, infections, lifestyle disorders and routine health checks.",
        "hours": [(time(9, 0), time(13, 0)), (time(16, 0), time(19, 0))],
    },
    {
        "email": "sana.qureshi@clinix.health", "full_name": "Dr. Sana Qureshi", "specialisation": "Dermatology",
        "qualifications": "MBBS, MD (Dermatology)", "experience_years": 7, "consultation_fee": 700,
        "slot_duration_minutes": 20, "buffer_minutes": 5, "room": "D-310", "phone": "+91 98200 77889",
        "bio": "Medical and cosmetic dermatology — skin, hair and nail conditions, with a special interest in chronic eczema and acne.",
        "hours": [(time(11, 0), time(15, 0))],
    },
    {
        "email": "vikram.nair@clinix.health", "full_name": "Dr. Vikram Nair", "specialisation": "Orthopaedics",
        "qualifications": "MBBS, MS (Orthopaedics)", "experience_years": 18, "consultation_fee": 1000,
        "slot_duration_minutes": 30, "buffer_minutes": 10, "room": "O-115", "phone": "+91 98200 33445",
        "bio": "Joint replacement and sports injuries. Runs a weekly fracture follow-up clinic.",
        "hours": [(time(9, 30), time(12, 30)), (time(15, 0), time(18, 0))],
    },
    {
        "email": "priya.deshmukh@clinix.health", "full_name": "Dr. Priya Deshmukh", "specialisation": "Paediatrics",
        "qualifications": "MBBS, DCH, MD (Paediatrics)", "experience_years": 11, "consultation_fee": 600,
        "slot_duration_minutes": 20, "buffer_minutes": 0, "room": "P-020", "phone": "+91 98200 66778",
        "bio": "Newborn care, immunisation schedules and childhood growth monitoring.",
        "hours": [(time(10, 0), time(13, 30)), (time(17, 0), time(19, 0))],
    },
    {
        "email": "rohit.menon@clinix.health", "full_name": "Dr. Rohit Menon", "specialisation": "Psychiatry",
        "qualifications": "MBBS, MD (Psychiatry)", "experience_years": 12, "consultation_fee": 1200,
        "slot_duration_minutes": 45, "buffer_minutes": 15, "room": "M-405", "phone": "+91 98200 99001",
        "bio": "Anxiety, mood disorders and sleep. Consultations are 45 minutes to allow proper history taking.",
        "hours": [(time(12, 0), time(17, 0))],
    },
]

PATIENTS = [
    {"email": "aarav.sharma@example.com", "full_name": "Aarav Sharma", "phone": "+91 90000 10001",
     "date_of_birth": date(1991, 4, 12), "gender": "Male"},
    {"email": "neha.gupta@example.com", "full_name": "Neha Gupta", "phone": "+91 90000 10002",
     "date_of_birth": date(1986, 11, 3), "gender": "Female"},
    {"email": "kabir.singh@example.com", "full_name": "Kabir Singh", "phone": "+91 90000 10003",
     "date_of_birth": date(2001, 7, 27), "gender": "Male"},
]

#: A finished visit, so a reviewer can see the post-visit summary and the
#: medication schedule without having to play both patient and doctor first.
DEMO_VISIT = {
    "symptoms": "Sore throat and fever for three days, painful to swallow. No cough, no breathlessness.",
    "duration_days": 3,
    "severity": 6,
    "existing_conditions": "None",
    "current_medications": "Paracetamol as needed",
    "allergies": "None known",
    "clinical_notes": (
        "O/E: temp 38.4C, tonsils enlarged b/l with exudate, tender anterior cervical nodes. "
        "Chest clear. Centor 3/4. Imp: acute bacterial tonsillitis. "
        "Advised warm saline gargles, adequate oral fluids and rest. R/v if no better in 48h."
    ),
    "diagnosis": "Acute bacterial tonsillitis",
    "prescription": [
        {"drug_name": "Amoxicillin", "dosage": "500 mg", "frequency": "TDS",
         "duration_days": 5, "instructions": "after food, finish the full course"},
        {"drug_name": "Paracetamol", "dosage": "650 mg", "frequency": "BD",
         "duration_days": 3, "instructions": "for fever or pain"},
    ],
}

#: Symptom forms used to seed two confirmed appointments — one deliberately
#: red-flagged so the doctor portal shows a High-urgency triage card on load.
DEMO_SYMPTOMS = [
    {
        "symptoms": "Tightness in my chest for the last two days, worse when I climb stairs. "
                    "It comes with shortness of breath and I broke into a cold sweat this morning.",
        "duration_days": 2, "severity": 8,
        "existing_conditions": "High blood pressure, diagnosed 2021",
        "current_medications": "Telmisartan 40mg once daily",
        "allergies": "None known",
    },
    {
        "symptoms": "Dry cough and a low fever for about five days. Sleeping badly because of the cough. "
                    "No breathlessness, appetite is normal.",
        "duration_days": 5, "severity": 4,
        "existing_conditions": "None",
        "current_medications": "Paracetamol when the fever spikes",
        "allergies": "Penicillin — rash as a child",
    },
]


def seed_if_empty() -> bool:
    """Populate the database if it has no users. Returns True if it seeded."""
    db = SessionLocal()
    try:
        if db.scalar(select(User.id).limit(1)) is not None:
            return False

        logger.info("Empty database detected — seeding demo clinic")

        admin = User(
            email=settings.admin_email.lower(),
            password_hash=hash_password(settings.admin_password),
            role=Role.ADMIN,
            full_name="Clinic Administrator",
            phone="+91 98000 00000",
        )
        db.add(admin)

        profiles: list[DoctorProfile] = []
        for spec in DOCTORS:
            user = User(
                email=spec["email"],
                password_hash=hash_password(DEMO_PASSWORD),
                role=Role.DOCTOR,
                full_name=spec["full_name"],
                phone=spec["phone"],
            )
            db.add(user)
            db.flush()

            profile = DoctorProfile(
                user_id=user.id,
                specialisation=spec["specialisation"],
                qualifications=spec["qualifications"],
                bio=spec["bio"],
                experience_years=spec["experience_years"],
                consultation_fee=spec["consultation_fee"],
                slot_duration_minutes=spec["slot_duration_minutes"],
                buffer_minutes=spec["buffer_minutes"],
                room=spec["room"],
            )
            db.add(profile)
            db.flush()
            profiles.append(profile)

            for weekday in _WEEKDAYS:
                for start, end in spec["hours"]:
                    db.add(DoctorWorkingHour(doctor_id=profile.id, weekday=weekday, start_time=start, end_time=end))

        patients: list[User] = []
        for spec in PATIENTS:
            user = User(
                email=spec["email"],
                password_hash=hash_password(DEMO_PASSWORD),
                role=Role.PATIENT,
                full_name=spec["full_name"],
                phone=spec["phone"],
                date_of_birth=spec["date_of_birth"],
                gender=spec["gender"],
            )
            db.add(user)
            patients.append(user)

        db.commit()
        logger.info("Seeded %s doctors and %s patients", len(profiles), len(patients))

        _seed_appointments(db, profiles, patients)
        _seed_completed_visit(db, profiles[1], patients[2])
        return True
    finally:
        db.close()


def _seed_appointments(db, profiles, patients) -> None:
    """Book two appointments through the real hold/confirm path."""
    from app.routers.appointments import persist_previsit_summary
    from app.services import notifications as notify
    from app.services.slots import confirm_hold, hold_slot
    from app.models import SymptomReport

    pairs = [(profiles[0], patients[0], DEMO_SYMPTOMS[0]), (profiles[1], patients[1], DEMO_SYMPTOMS[1])]

    for profile, patient, form in pairs:
        db.refresh(profile)
        booked = False
        # Look a few days ahead: the doctor may not work tomorrow, and today's
        # remaining slots may already be inside the minimum-lead window.
        for offset in range(1, 8):
            if booked:
                break
            target = to_local(utcnow()).date() + timedelta(days=offset)
            grid = day_availability(db, profile, target)
            for slot in grid["slots"]:
                if not slot["available"]:
                    continue
                try:
                    appointment = hold_slot(
                        db,
                        doctor=profile,
                        patient=patient,
                        start_at=slot["start_at"],
                        reason_for_visit=form["symptoms"][:60] + "…",
                    )
                except Exception as exc:
                    logger.debug("Seed hold skipped (%s)", exc)
                    continue

                report = SymptomReport(appointment_id=appointment.id, **form)
                db.add(report)
                db.flush()
                appointment.symptom_report = report

                confirm_hold(db, appointment)
                db.refresh(appointment)
                persist_previsit_summary(db, appointment)
                notify.on_appointment_confirmed(db, appointment)
                db.commit()

                logger.info(
                    "Seeded appointment %s for %s with %s (%s urgency)",
                    appointment.reference,
                    patient.full_name,
                    profile.user.full_name,
                    appointment.previsit_summary.urgency,
                )
                booked = True
                break


def _seed_completed_visit(db, profile, patient) -> None:
    """Seed one finished consultation: notes, prescription, reminders, summaries.

    The appointment is written directly rather than booked through
    ``hold_slot``, because it is deliberately in the *past* and the booking
    rules (correctly) refuse that. Everything after the appointment row goes
    through the real services, so the seeded summary and reminders are produced
    by exactly the code path a live visit uses.
    """
    from datetime import datetime, time, timedelta

    from app.models import (
        Appointment, AppointmentStatus, Consultation, Prescription,
        PrescriptionItem, SymptomReport,
    )
    from app.routers.appointments import persist_previsit_summary
    from app.routers.consultations import _persist_postvisit_summary
    from app.services import notifications as notify
    from app.services.slots import generate_reference

    db.refresh(profile)
    yesterday = to_local(utcnow()).date() - timedelta(days=1)
    start_local = datetime.combine(yesterday, time(11, 0), tzinfo=settings.tz)
    start_at = start_local.astimezone(utcnow().tzinfo)

    appointment = Appointment(
        reference=generate_reference(),
        doctor_id=profile.id,
        patient_id=patient.id,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=profile.slot_duration_minutes),
        status=AppointmentStatus.CONFIRMED,
        confirmed_at=start_at - timedelta(days=1),
        reason_for_visit="Sore throat and fever",
    )
    db.add(appointment)
    db.flush()

    report = SymptomReport(
        appointment_id=appointment.id,
        symptoms=DEMO_VISIT["symptoms"],
        duration_days=DEMO_VISIT["duration_days"],
        severity=DEMO_VISIT["severity"],
        existing_conditions=DEMO_VISIT["existing_conditions"],
        current_medications=DEMO_VISIT["current_medications"],
        allergies=DEMO_VISIT["allergies"],
    )
    db.add(report)
    db.flush()
    appointment.symptom_report = report
    persist_previsit_summary(db, appointment)

    consultation = Consultation(
        appointment_id=appointment.id,
        doctor_id=profile.id,
        clinical_notes=DEMO_VISIT["clinical_notes"],
        diagnosis=DEMO_VISIT["diagnosis"],
        follow_up_date=yesterday + timedelta(days=14),
    )
    db.add(consultation)
    db.flush()

    prescription = Prescription(consultation_id=consultation.id, patient_id=patient.id)
    db.add(prescription)
    db.flush()

    today = to_local(utcnow()).date()
    items = []
    for spec in DEMO_VISIT["prescription"]:
        item = PrescriptionItem(prescription_id=prescription.id, start_date=today, **spec)
        db.add(item)
        db.flush()
        notify.schedule_medication_reminders(db, item, patient.id)
        items.append(spec)

    appointment.status = AppointmentStatus.COMPLETED
    db.commit()

    db.refresh(consultation)
    _persist_postvisit_summary(db, consultation, items)
    notify.on_postvisit_summary_ready(db, consultation)
    db.commit()

    from app.models import MedicationReminder

    doses = db.query(MedicationReminder).filter(MedicationReminder.patient_id == patient.id).count()
    logger.info(
        "Seeded completed visit %s for %s (%s medicines, %s upcoming doses)",
        appointment.reference, patient.full_name, len(items), doses,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from app.database import init_db

    init_db()
    print("Seeded." if seed_if_empty() else "Database already has data — nothing to do.")
