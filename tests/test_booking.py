"""Slot generation, double-booking prevention, holds, reschedule and cancel."""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.models import Appointment, AppointmentStatus, utcnow
from app.services.slots import as_utc, expire_stale_holds, slot_starts_for_day
from tests.conftest import bearer, make_doctor, next_free_slot


# ==========================================================================
# Slot grid
# ==========================================================================
def test_slot_grid_respects_duration_and_buffer(db):
    doctor = make_doctor(db, email="grid@clinix.health", slot_minutes=30, buffer_minutes=15,
                         start="09:00", end="12:00")
    starts = slot_starts_for_day(doctor, date.today() + timedelta(days=1))
    # 45-minute step from 09:00; the last slot must END by 12:00 -> 11:15 is the final start.
    assert len(starts) == 4
    gaps = {(b - a).total_seconds() / 60 for a, b in zip(starts, starts[1:])}
    assert gaps == {45}


def test_slot_never_overruns_the_working_window(db):
    """A 45-minute doctor working 10:00-12:00 gets 10:00 and 10:45 — never 11:30,
    which would run 15 minutes past closing."""
    from app.services.slots import to_local

    doctor = make_doctor(db, email="overrun@clinix.health", slot_minutes=45, buffer_minutes=0,
                         start="10:00", end="12:00")
    starts = slot_starts_for_day(doctor, date.today() + timedelta(days=1))

    assert [to_local(s).strftime("%H:%M") for s in starts] == ["10:00", "10:45"]
    assert to_local(starts[-1] + timedelta(minutes=45)).time() <= time(12, 0)


def test_no_slots_on_a_non_working_day(db):
    doctor = make_doctor(db, email="mononly@clinix.health", days=[0])  # Mondays only
    target = date.today() + timedelta(days=1)
    while target.weekday() == 0:
        target += timedelta(days=1)
    assert slot_starts_for_day(doctor, target) == []


# ==========================================================================
# Double-booking — the core guarantee
# ==========================================================================
def test_second_hold_on_same_slot_is_rejected(client, clinic):
    doctor_id = clinic["doctor"].id
    slot, _ = next_free_slot(client, clinic["p1"], doctor_id)

    first = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                        json={"doctor_id": doctor_id, "start_at": slot})
    assert first.status_code == 201

    second = client.post("/api/appointments/hold", headers=bearer(clinic["p2"]),
                         json={"doctor_id": doctor_id, "start_at": slot})
    assert second.status_code == 409
    assert second.json()["code"] == "SLOT_TAKEN"


def test_concurrent_holds_produce_exactly_one_winner(client, clinic, db):
    """The headline concurrency test.

    Twelve threads race for one slot behind a barrier. Correctness comes from
    the partial unique index, so exactly one INSERT can survive regardless of
    how the transactions interleave.
    """
    from tests.conftest import make_user, token_for

    doctor_id = clinic["doctor"].id
    slot, _ = next_free_slot(client, clinic["p1"], doctor_id)

    n = 12
    tokens = []
    for i in range(n):
        make_user(db, email=f"racer{i}@example.com", name=f"Racer {i}")
        tokens.append(token_for(client, f"racer{i}@example.com"))

    barrier = threading.Barrier(n)
    results: list[int] = []
    lock = threading.Lock()

    def attempt(token):
        barrier.wait()
        response = client.post("/api/appointments/hold", headers=bearer(token),
                               json={"doctor_id": doctor_id, "start_at": slot})
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=attempt, args=(t,)) for t in tokens]
    for t in threads: t.start()
    for t in threads: t.join()

    assert results.count(201) == 1, f"expected exactly one winner, got {results}"
    assert set(results) <= {201, 409}, f"unexpected statuses: {results}"

    active = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.start_at == as_utc(datetime.fromisoformat(slot)),
        Appointment.status.in_([AppointmentStatus.HELD, AppointmentStatus.CONFIRMED]),
    ).count()
    assert active == 1


def test_patient_cannot_double_book_themselves(client, clinic, db):
    other = make_doctor(db, email="other@clinix.health", name="Dr. Other")
    doctor_id = clinic["doctor"].id
    slot, _ = next_free_slot(client, clinic["p1"], doctor_id)

    assert client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": doctor_id, "start_at": slot}).status_code == 201

    clash = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                        json={"doctor_id": other.id, "start_at": slot})
    assert clash.status_code == 409
    assert clash.json()["code"] == "PATIENT_BUSY"


# ==========================================================================
# Holds
# ==========================================================================
def test_expired_hold_releases_the_slot(client, clinic, db):
    doctor_id = clinic["doctor"].id
    slot, day = next_free_slot(client, clinic["p1"], doctor_id)

    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": doctor_id, "start_at": slot}).json()

    appointment = db.get(Appointment, held["id"])
    appointment.hold_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()

    assert expire_stale_holds(db) == 1
    db.refresh(appointment)
    assert appointment.status == AppointmentStatus.EXPIRED

    # The slot is bookable again, and the old row does not block the index.
    again = client.post("/api/appointments/hold", headers=bearer(clinic["p2"]),
                        json={"doctor_id": doctor_id, "start_at": slot})
    assert again.status_code == 201


def test_confirming_an_expired_hold_is_rejected(client, clinic, db):
    doctor_id = clinic["doctor"].id
    slot, _ = next_free_slot(client, clinic["p1"], doctor_id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": doctor_id, "start_at": slot}).json()

    appointment = db.get(Appointment, held["id"])
    appointment.hold_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()

    response = client.post(f"/api/appointments/{held['id']}/confirm", headers=bearer(clinic["p1"]),
                           json={"symptom_form": {"symptoms": "a persistent cough for three days"}})
    assert response.status_code == 410
    assert response.json()["code"] == "HOLD_EXPIRED"


def test_confirm_is_idempotent(client, clinic):
    doctor_id = clinic["doctor"].id
    slot, _ = next_free_slot(client, clinic["p1"], doctor_id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": doctor_id, "start_at": slot}).json()
    form = {"symptom_form": {"symptoms": "a persistent cough for three days, no fever"}}

    first = client.post(f"/api/appointments/{held['id']}/confirm", headers=bearer(clinic["p1"]), json=form)
    second = client.post(f"/api/appointments/{held['id']}/confirm", headers=bearer(clinic["p1"]), json=form)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["status"] == "confirmed"


# ==========================================================================
# Validation
# ==========================================================================
@pytest.mark.parametrize(
    "offset_days,hhmm,expected_code",
    [
        (-1, "10:00", "TOO_LATE"),     # in the past
        (400, "10:00", "TOO_FAR"),     # beyond the booking horizon
        (2, "10:07", "OFF_GRID"),      # not on the slot grid
        (2, "23:00", "OFF_GRID"),      # outside working hours
    ],
)
def test_unbookable_times_are_rejected(client, clinic, offset_days, hhmm, expected_code):
    target = (date.today() + timedelta(days=offset_days)).isoformat()
    response = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                           json={"doctor_id": clinic["doctor"].id, "start_at": f"{target}T{hhmm}:00+05:30"})
    assert response.status_code == 422
    assert response.json()["code"] == expected_code


# ==========================================================================
# Reschedule / cancel
# ==========================================================================
def test_reschedule_frees_the_old_slot_and_takes_the_new_one(client, clinic):
    doctor_id = clinic["doctor"].id
    slot, day = next_free_slot(client, clinic["p1"], doctor_id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": doctor_id, "start_at": slot}).json()
    client.post(f"/api/appointments/{held['id']}/confirm", headers=bearer(clinic["p1"]),
                json={"symptom_form": {"symptoms": "mild headache for two days"}})

    grid = client.get(f"/api/doctors/{doctor_id}/availability", params={"date": day},
                      headers=bearer(clinic["p1"])).json()
    target = [s for s in grid["slots"] if s["available"]][0]["start_at"]

    moved = client.post(f"/api/appointments/{held['id']}/reschedule", headers=bearer(clinic["p1"]),
                        json={"start_at": target, "reason": "work clash"})
    assert moved.status_code == 200
    assert moved.json()["reschedule_count"] == 1

    grid = client.get(f"/api/doctors/{doctor_id}/availability", params={"date": day},
                      headers=bearer(clinic["p1"])).json()
    by_start = {s["start_at"]: s for s in grid["slots"]}
    assert by_start[slot]["available"] is True
    assert by_start[target]["available"] is False


def test_cancelled_slot_is_immediately_rebookable(client, clinic):
    doctor_id = clinic["doctor"].id
    slot, _ = next_free_slot(client, clinic["p1"], doctor_id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": doctor_id, "start_at": slot}).json()
    client.post(f"/api/appointments/{held['id']}/confirm", headers=bearer(clinic["p1"]),
                json={"symptom_form": {"symptoms": "sore throat since yesterday"}})

    client.post(f"/api/appointments/{held['id']}/cancel", headers=bearer(clinic["p1"]),
                json={"reason": "feeling better"})

    retaken = client.post("/api/appointments/hold", headers=bearer(clinic["p2"]),
                          json={"doctor_id": doctor_id, "start_at": slot})
    assert retaken.status_code == 201
