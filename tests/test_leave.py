"""Doctor leave: dry-run impact report, transactional apply, and slot closure."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models import Appointment, AppointmentStatus, CancelActor, JobStatus, Notification
from tests.conftest import bearer, next_free_slot


def _book(client, clinic, symptoms="a persistent cough for three days"):
    doctor_id = clinic["doctor"].id
    slot, day = next_free_slot(client, clinic["p1"], doctor_id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": doctor_id, "start_at": slot}).json()
    confirmed = client.post(f"/api/appointments/{held['id']}/confirm", headers=bearer(clinic["p1"]),
                            json={"symptom_form": {"symptoms": symptoms}}).json()
    return confirmed, day


def test_dry_run_reports_impact_without_changing_anything(client, clinic, db):
    appointment, day = _book(client, clinic)

    response = client.post(f"/api/admin/doctors/{clinic['doctor'].id}/leave",
                           headers=bearer(clinic["admin"]),
                           json={"leave_date": day, "reason": "Conference", "confirm": False})
    assert response.status_code == 200
    body = response.json()

    assert body["applied"] is False
    assert body["affected_count"] == 1
    assert body["affected"][0]["reference"] == appointment["reference"]
    assert body["affected"][0]["patient_email"] == "p1@example.com"

    # Nothing changed.
    assert db.get(Appointment, appointment["id"]).status == AppointmentStatus.CONFIRMED
    assert db.query(Notification).filter(Notification.template == "leave_cancellation").count() == 0


def test_applying_leave_cancels_and_notifies_affected_patients(client, clinic, db):
    appointment, day = _book(client, clinic)

    response = client.post(f"/api/admin/doctors/{clinic['doctor'].id}/leave",
                           headers=bearer(clinic["admin"]),
                           json={"leave_date": day, "reason": "Conference", "confirm": True})
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["affected_count"] == 1
    assert body["notifications_queued"] == 1

    row = db.get(Appointment, appointment["id"])
    db.refresh(row)
    assert row.status == AppointmentStatus.CANCELLED
    assert row.cancelled_by == CancelActor.SYSTEM_LEAVE

    note = db.query(Notification).filter(Notification.template == "leave_cancellation").one()
    assert note.to_email == "p1@example.com"
    assert day in note.subject or "leave" in note.subject.lower()


def test_leave_day_has_no_bookable_slots(client, clinic):
    _, day = _book(client, clinic)
    client.post(f"/api/admin/doctors/{clinic['doctor'].id}/leave", headers=bearer(clinic["admin"]),
                json={"leave_date": day, "confirm": True})

    grid = client.get(f"/api/doctors/{clinic['doctor'].id}/availability",
                      params={"date": day}, headers=bearer(clinic["p2"])).json()
    assert grid["is_leave"] is True
    assert all(s["available"] is False and s["reason"] == "leave" for s in grid["slots"])


def test_booking_on_a_leave_day_is_rejected(client, clinic):
    appointment, day = _book(client, clinic)
    slot = appointment["start_at"]
    client.post(f"/api/admin/doctors/{clinic['doctor'].id}/leave", headers=bearer(clinic["admin"]),
                json={"leave_date": day, "confirm": True})

    response = client.post("/api/appointments/hold", headers=bearer(clinic["p2"]),
                           json={"doctor_id": clinic["doctor"].id, "start_at": slot})
    assert response.status_code == 422
    assert response.json()["code"] == "DOCTOR_ON_LEAVE"


def test_leave_cannot_be_marked_twice(client, clinic):
    _, day = _book(client, clinic)
    first = client.post(f"/api/admin/doctors/{clinic['doctor'].id}/leave", headers=bearer(clinic["admin"]),
                        json={"leave_date": day, "confirm": True})
    assert first.status_code == 200

    second = client.post(f"/api/admin/doctors/{clinic['doctor'].id}/leave", headers=bearer(clinic["admin"]),
                         json={"leave_date": day, "confirm": True})
    assert second.status_code == 409
    assert second.json()["code"] == "LEAVE_EXISTS"


def test_removing_leave_reopens_slots_but_does_not_restore_bookings(client, clinic, db):
    appointment, day = _book(client, clinic)
    client.post(f"/api/admin/doctors/{clinic['doctor'].id}/leave", headers=bearer(clinic["admin"]),
                json={"leave_date": day, "confirm": True})

    leaves = client.get(f"/api/admin/doctors/{clinic['doctor'].id}/leaves", headers=bearer(clinic["admin"])).json()
    client.delete(f"/api/admin/leaves/{leaves[0]['id']}", headers=bearer(clinic["admin"]))

    grid = client.get(f"/api/doctors/{clinic['doctor'].id}/availability",
                      params={"date": day}, headers=bearer(clinic["p2"])).json()
    assert grid["is_leave"] is False
    assert any(s["available"] for s in grid["slots"])

    db.refresh(db.get(Appointment, appointment["id"]))
    assert db.get(Appointment, appointment["id"]).status == AppointmentStatus.CANCELLED


def test_only_admin_can_mark_leave(client, clinic):
    for token in (clinic["p1"], clinic["doctor_token"]):
        response = client.post(f"/api/admin/doctors/{clinic['doctor'].id}/leave", headers=bearer(token),
                               json={"leave_date": "2030-01-01", "confirm": True})
        assert response.status_code == 403
