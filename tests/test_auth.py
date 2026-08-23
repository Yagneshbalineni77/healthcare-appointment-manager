"""Authentication, role-based access control, and data isolation between users."""

from __future__ import annotations

import pytest

from app.models import Role
from app.security import hash_password, verify_password
from tests.conftest import bearer, make_doctor, make_user, next_free_slot, token_for


# ==========================================================================
# Passwords
# ==========================================================================
def test_password_hash_roundtrip():
    digest = hash_password("Password@123")
    assert digest != "Password@123"
    assert verify_password("Password@123", digest)
    assert not verify_password("Password@124", digest)


def test_long_passwords_stay_distinct_past_bcrypts_72_byte_limit():
    """Raw bcrypt would treat these as the same password."""
    a = "x" * 200 + "A"
    b = "x" * 200 + "B"
    assert not verify_password(b, hash_password(a))


def test_hashes_are_salted():
    assert hash_password("same") != hash_password("same")


# ==========================================================================
# Registration & login
# ==========================================================================
def test_self_registration_always_creates_a_patient(client):
    """No client-supplied field may escalate a role."""
    response = client.post("/api/auth/register", json={
        "email": "sneaky@example.com", "password": "Password@123",
        "full_name": "Sneaky User", "role": "admin"})
    assert response.status_code == 201
    assert response.json()["user"]["role"] == Role.PATIENT


def test_duplicate_email_is_rejected(client):
    payload = {"email": "dup@example.com", "password": "Password@123", "full_name": "Dup"}
    assert client.post("/api/auth/register", json=payload).status_code == 201
    second = client.post("/api/auth/register", json=payload)
    assert second.status_code == 409 and second.json()["code"] == "EMAIL_TAKEN"


def test_login_error_is_identical_for_unknown_user_and_wrong_password(client, db):
    make_user(db, email="real@example.com")
    unknown = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "x"})
    wrong = client.post("/api/auth/login", json={"email": "real@example.com", "password": "wrong"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"], "must not leak which emails exist"


def test_deactivated_account_cannot_sign_in(client, db):
    user = make_user(db, email="off@example.com")
    user.is_active = False
    db.commit()
    response = client.post("/api/auth/login", json={"email": "off@example.com", "password": "Password@123"})
    assert response.status_code == 403 and response.json()["code"] == "ACCOUNT_DISABLED"


@pytest.mark.parametrize("password", ["short", "1234567"])
def test_weak_passwords_are_rejected(client, password):
    response = client.post("/api/auth/register", json={
        "email": "weak@example.com", "password": password, "full_name": "Weak"})
    assert response.status_code == 422


def test_invalid_email_is_rejected(client):
    response = client.post("/api/auth/register", json={
        "email": "not-an-email", "password": "Password@123", "full_name": "Bad"})
    assert response.status_code == 422


# ==========================================================================
# Tokens
# ==========================================================================
def test_protected_route_requires_a_token(client):
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("header", ["Bearer garbage", "Bearer ", "Basic abc"])
def test_malformed_tokens_are_rejected(client, header):
    assert client.get("/api/auth/me", headers={"Authorization": header}).status_code == 401


def test_token_signed_with_another_secret_is_rejected(client, db):
    import jwt as pyjwt
    from datetime import datetime, timedelta, timezone

    user = make_user(db, email="victim@example.com")
    forged = pyjwt.encode(
        {"sub": str(user.id), "role": "admin",
         "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())},
        "the-wrong-secret", algorithm="HS256")
    assert client.get("/api/auth/me", headers=bearer(forged)).status_code == 401


def test_expired_token_is_rejected(client, db):
    import jwt as pyjwt
    from datetime import datetime, timedelta, timezone

    from app.config import settings

    user = make_user(db, email="expired@example.com")
    stale = pyjwt.encode(
        {"sub": str(user.id), "role": "patient",
         "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())},
        settings.jwt_secret, algorithm=settings.jwt_algorithm)

    response = client.get("/api/auth/me", headers=bearer(stale))
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


# ==========================================================================
# Role-based access
# ==========================================================================
@pytest.mark.parametrize("path", ["/api/admin/stats", "/api/admin/doctors", "/api/admin/notifications", "/api/admin/audit"])
def test_admin_routes_reject_patients_and_doctors(client, clinic, path):
    for token in (clinic["p1"], clinic["doctor_token"]):
        assert client.get(path, headers=bearer(token)).status_code == 403


def test_admin_routes_accept_admins(client, clinic):
    assert client.get("/api/admin/stats", headers=bearer(clinic["admin"])).status_code == 200


def test_only_patients_can_book(client, clinic):
    slot, _ = next_free_slot(client, clinic["p1"], clinic["doctor"].id)
    for token in (clinic["doctor_token"], clinic["admin"]):
        response = client.post("/api/appointments/hold", headers=bearer(token),
                               json={"doctor_id": clinic["doctor"].id, "start_at": slot})
        assert response.status_code == 403


def test_only_the_treating_doctor_can_file_notes(client, clinic, db):
    slot, _ = next_free_slot(client, clinic["p1"], clinic["doctor"].id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": clinic["doctor"].id, "start_at": slot}).json()
    client.post(f"/api/appointments/{held['id']}/confirm", headers=bearer(clinic["p1"]),
                json={"symptom_form": {"symptoms": "cough for three days"}})

    make_doctor(db, email="intruder@clinix.health", name="Dr. Intruder")
    intruder = token_for(client, "intruder@clinix.health")

    payload = {"clinical_notes": "Attempting to file notes for someone else's patient."}
    assert client.post(f"/api/appointments/{held['id']}/consultation",
                       headers=bearer(intruder), json=payload).status_code == 403
    assert client.post(f"/api/appointments/{held['id']}/consultation",
                       headers=bearer(clinic["p1"]), json=payload).status_code == 403


# ==========================================================================
# Data isolation
# ==========================================================================
def test_a_patient_cannot_read_another_patients_appointment(client, clinic):
    slot, _ = next_free_slot(client, clinic["p1"], clinic["doctor"].id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": clinic["doctor"].id, "start_at": slot}).json()

    assert client.get(f"/api/appointments/{held['id']}", headers=bearer(clinic["p2"])).status_code == 403
    assert client.get(f"/api/appointments/{held['id']}", headers=bearer(clinic["p1"])).status_code == 200


def test_a_patient_cannot_cancel_another_patients_appointment(client, clinic):
    slot, _ = next_free_slot(client, clinic["p1"], clinic["doctor"].id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": clinic["doctor"].id, "start_at": slot}).json()
    assert client.post(f"/api/appointments/{held['id']}/cancel",
                       headers=bearer(clinic["p2"]), json={}).status_code == 403


def test_appointment_lists_are_scoped_to_the_caller(client, clinic, db):
    slot, _ = next_free_slot(client, clinic["p1"], clinic["doctor"].id)
    client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                json={"doctor_id": clinic["doctor"].id, "start_at": slot})

    assert len(client.get("/api/appointments", headers=bearer(clinic["p1"])).json()) == 1
    assert client.get("/api/appointments", headers=bearer(clinic["p2"])).json() == []
    assert len(client.get("/api/appointments", headers=bearer(clinic["doctor_token"])).json()) == 1
