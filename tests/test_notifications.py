"""Outbox reliability: transactional queueing, idempotency, backoff, dead-lettering."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import JobStatus, Notification, utcnow
from app.services import email as email_service
from tests.conftest import bearer, next_free_slot


def _confirm_booking(client, clinic, symptoms="a persistent cough for three days"):
    doctor_id = clinic["doctor"].id
    slot, day = next_free_slot(client, clinic["p1"], doctor_id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": doctor_id, "start_at": slot}).json()
    return client.post(f"/api/appointments/{held['id']}/confirm", headers=bearer(clinic["p1"]),
                       json={"symptom_form": {"symptoms": symptoms}}).json(), day


# ==========================================================================
# Queueing
# ==========================================================================
def test_confirming_queues_email_for_both_parties(client, clinic, db):
    _confirm_booking(client, clinic)
    templates = {n.template for n in db.query(Notification).all()}
    assert "booking_confirmed_patient" in templates
    assert "booking_confirmed_doctor" in templates


def test_idempotency_key_prevents_duplicate_sends(db):
    kwargs = dict(template="welcome", to_email="dup@example.com", to_name="Dup",
                  ctx={"patient_name": "Dup"}, idempotency_key="welcome:1")

    first = email_service.queue(db, **kwargs)
    db.commit()
    second = email_service.queue(db, **kwargs)
    db.commit()

    assert first is not None
    assert second is None, "the same message must not be queued twice"
    assert db.query(Notification).filter(Notification.idempotency_key == "welcome:1").count() == 1


def test_cancelling_a_confirmed_booking_emails_both_parties(client, clinic, db):
    appointment, _ = _confirm_booking(client, clinic)
    client.post(f"/api/appointments/{appointment['id']}/cancel", headers=bearer(clinic["p1"]),
                json={"reason": "feeling better"})

    cancels = db.query(Notification).filter(Notification.template == "appointment_cancelled").all()
    assert {n.to_email for n in cancels} == {"p1@example.com", "doc@clinix.health"}


def test_abandoning_an_unconfirmed_hold_sends_no_apology(client, clinic, db):
    """A hold the patient walks away from is not an event worth emailing about."""
    doctor_id = clinic["doctor"].id
    slot, _ = next_free_slot(client, clinic["p1"], doctor_id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": doctor_id, "start_at": slot}).json()

    client.post(f"/api/appointments/{held['id']}/cancel", headers=bearer(clinic["p1"]), json={})
    assert db.query(Notification).filter(Notification.template == "appointment_cancelled").count() == 0


# ==========================================================================
# Delivery, retry, dead-letter
# ==========================================================================
def test_worker_delivers_pending_notifications(client, clinic, db):
    _confirm_booking(client, clinic)
    assert db.query(Notification).filter(Notification.status == JobStatus.PENDING).count() > 0

    report = client.post("/api/admin/worker/run-once", headers=bearer(clinic["admin"])).json()["report"]
    assert report["email"]["sent"] > 0
    assert db.query(Notification).filter(Notification.status == JobStatus.PENDING).count() == 0


def test_failures_retry_with_exponential_backoff_then_dead_letter(db, monkeypatch):
    note = email_service.queue(db, template="welcome", to_email="fail@example.com", to_name="F",
                               ctx={"patient_name": "F"}, idempotency_key="fail:1")
    note.max_attempts = 3
    db.commit()

    def always_fail(_note):
        raise email_service.EmailSendError("provider is down")

    monkeypatch.setattr(email_service, "deliver", always_fail)

    # Attempt 1 -> FAILED, scheduled ~60s out.
    email_service.dispatch_pending(db)
    db.refresh(note)
    assert note.status == JobStatus.FAILED and note.attempts == 1
    first_delay = note.next_attempt_at - utcnow()
    assert timedelta(seconds=30) < first_delay <= timedelta(seconds=61)

    # Attempt 2 -> still FAILED, but the wait has doubled.
    note.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()
    email_service.dispatch_pending(db)
    db.refresh(note)
    assert note.attempts == 2
    second_delay = note.next_attempt_at - utcnow()
    assert second_delay > first_delay

    # Attempt 3 exhausts max_attempts -> DEAD, and it stops being picked up.
    note.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()
    email_service.dispatch_pending(db)
    db.refresh(note)
    assert note.status == JobStatus.DEAD
    assert "provider is down" in note.last_error

    assert email_service.dispatch_pending(db)["picked"] == 0


def test_admin_can_requeue_a_dead_notification(client, clinic, db):
    note = email_service.queue(db, template="welcome", to_email="dead@example.com", to_name="D",
                               ctx={"patient_name": "D"}, idempotency_key="dead:1")
    note.status = JobStatus.DEAD
    note.attempts = 5
    note.last_error = "smtp timeout"
    db.commit()

    response = client.post(f"/api/admin/notifications/{note.id}/requeue", headers=bearer(clinic["admin"]))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending" and body["attempts"] == 0 and body["last_error"] is None


def test_delivery_failure_never_rolls_back_the_booking(client, clinic, db, monkeypatch):
    """The whole reason for the outbox."""
    def always_fail(_note):
        raise email_service.EmailSendError("provider is down")

    monkeypatch.setattr(email_service, "deliver", always_fail)

    appointment, _ = _confirm_booking(client, clinic)
    email_service.dispatch_pending(db)

    from app.models import Appointment, AppointmentStatus
    assert db.get(Appointment, appointment["id"]).status == AppointmentStatus.CONFIRMED
    assert db.query(Notification).filter(Notification.status == JobStatus.FAILED).count() > 0


# ==========================================================================
# Medication reminders
# ==========================================================================
def test_prescription_materialises_one_reminder_per_dose(client, clinic, db):
    appointment, _ = _confirm_booking(client, clinic)

    response = client.post(f"/api/appointments/{appointment['id']}/consultation",
                           headers=bearer(clinic["doctor_token"]),
                           json={"clinical_notes": "Acute pharyngitis, advised rest and fluids.",
                                 "diagnosis": "Acute pharyngitis",
                                 "prescription_items": [
                                     {"drug_name": "Amoxicillin", "dosage": "500 mg",
                                      "frequency": "TDS", "duration_days": 5},
                                     {"drug_name": "Vitamin C", "dosage": "500 mg",
                                      "frequency": "SOS", "duration_days": 5}]})
    assert response.status_code == 201
    items = {i["drug_name"]: i for i in response.json()["prescription_items"]}

    # TDS = 3 doses/day x 5 days = 15, minus any already in the past today.
    assert 12 <= items["Amoxicillin"]["reminder_count"] <= 15
    assert items["Vitamin C"]["reminder_count"] == 0, "SOS (as-needed) drugs get no reminders"


def test_patient_sees_and_can_stop_a_reminder(client, clinic, db):
    appointment, _ = _confirm_booking(client, clinic)
    client.post(f"/api/appointments/{appointment['id']}/consultation",
                headers=bearer(clinic["doctor_token"]),
                json={"clinical_notes": "Acute pharyngitis, advised rest and fluids.",
                      "prescription_items": [{"drug_name": "Amoxicillin", "dosage": "500 mg",
                                              "frequency": "BD", "duration_days": 3}]})

    rows = client.get("/api/me/medication-reminders", headers=bearer(clinic["p1"])).json()
    assert rows and rows[0]["drug_name"] == "Amoxicillin"

    assert client.delete(f"/api/me/medication-reminders/{rows[0]['id']}",
                         headers=bearer(clinic["p1"])).status_code == 200
    remaining = client.get("/api/me/medication-reminders", headers=bearer(clinic["p1"])).json()
    assert len(remaining) == len(rows) - 1


def test_due_medication_reminders_become_emails(client, clinic, db):
    from app.models import MedicationReminder
    from app.services import notifications as notify

    appointment, _ = _confirm_booking(client, clinic)
    client.post(f"/api/appointments/{appointment['id']}/consultation",
                headers=bearer(clinic["doctor_token"]),
                json={"clinical_notes": "Acute pharyngitis, advised rest and fluids.",
                      "prescription_items": [{"drug_name": "Amoxicillin", "dosage": "500 mg",
                                              "frequency": "BD", "duration_days": 3}]})

    reminder = db.query(MedicationReminder).order_by(MedicationReminder.due_at).first()
    reminder.due_at = utcnow() - timedelta(minutes=1)
    db.commit()

    assert notify.queue_due_medication_reminders(db) == 1
    db.refresh(reminder)
    assert reminder.status == JobStatus.SENT
    assert db.query(Notification).filter(Notification.template == "medication_reminder").count() == 1


def test_appointment_reminder_is_queued_once_inside_the_window(client, clinic, db):
    from app.models import Appointment
    from app.services import notifications as notify

    appointment, _ = _confirm_booking(client, clinic)
    row = db.get(Appointment, appointment["id"])
    row.start_at = utcnow() + timedelta(hours=2)   # inside the 24h reminder window
    db.commit()

    assert notify.queue_due_appointment_reminders(db) == 1
    assert notify.queue_due_appointment_reminders(db) == 0, "must not re-send"

    reminders = db.query(Notification).filter(Notification.template == "appointment_reminder").all()
    assert {n.to_email for n in reminders} == {"p1@example.com", "doc@clinix.health"}
