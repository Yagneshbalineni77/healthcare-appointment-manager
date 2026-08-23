"""Test fixtures.

Environment is configured **before** importing the app, because
``app.config.Settings`` is read once at import time and ``app.database.engine``
is built from it. Each test session gets its own throwaway SQLite file (not
``:memory:``) so the concurrency tests can use real threads and real
connections.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="clinix-tests-"))

os.environ.update(
    DATABASE_URL=f"sqlite:///{TMP / 'test.db'}",
    SEED_DEMO_DATA="false",       # tests build exactly the data they need
    WORKER_ENABLED="false",       # the worker is driven explicitly, never in the background
    EMAIL_PROVIDER="outbox",
    OUTBOX_DIR=str(TMP / "outbox"),
    GEMINI_API_KEY="",            # force the deterministic fallback path
    JWT_SECRET="test-secret-" + "a" * 40,
    ENVIRONMENT="test",
    SLOT_HOLD_MINUTES="5",
    MIN_LEAD_MINUTES="30",
    CLINIC_TIMEZONE="Asia/Kolkata",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import DoctorProfile, DoctorWorkingHour, Role, User  # noqa: E402
from app.security import hash_password  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean():
    """Truncate between tests so each one starts from a known state."""
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.exec_driver_sql(f"DELETE FROM {table.name}")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------
# Data builders
# --------------------------------------------------------------------------
def make_user(db, *, email, role=Role.PATIENT, password="Password@123", name="Test User", **kw) -> User:
    user = User(email=email, password_hash=hash_password(password), role=role, full_name=name, **kw)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_doctor(db, *, email="doc@clinix.health", name="Dr. Test", spec="General Medicine",
                slot_minutes=30, buffer_minutes=0, days=range(7),
                start="09:00", end="17:00") -> DoctorProfile:
    from datetime import time as _time

    user = make_user(db, email=email, role=Role.DOCTOR, name=name)
    profile = DoctorProfile(
        user_id=user.id, specialisation=spec,
        slot_duration_minutes=slot_minutes, buffer_minutes=buffer_minutes,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)

    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    for weekday in days:
        db.add(DoctorWorkingHour(doctor_id=profile.id, weekday=weekday,
                                 start_time=_time(sh, sm), end_time=_time(eh, em)))
    db.commit()
    db.refresh(profile)
    return profile


def token_for(client, email, password="Password@123") -> str:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def bearer(token) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def clinic(db, client):
    """A doctor, two patients and an admin, all logged in."""
    doctor = make_doctor(db)
    make_user(db, email="p1@example.com", name="Patient One")
    make_user(db, email="p2@example.com", name="Patient Two")
    make_user(db, email="admin@clinix.health", role=Role.ADMIN, name="Admin", password="Admin@12345")

    return {
        "doctor": doctor,
        "doctor_token": token_for(client, "doc@clinix.health"),
        "p1": token_for(client, "p1@example.com"),
        "p2": token_for(client, "p2@example.com"),
        "admin": token_for(client, "admin@clinix.health", "Admin@12345"),
    }


def next_free_slot(client, token, doctor_id, day_offset=1):
    """First bookable slot at least ``day_offset`` days out."""
    from datetime import date, timedelta

    for offset in range(day_offset, day_offset + 8):
        target = (date.today() + timedelta(days=offset)).isoformat()
        data = client.get(f"/api/doctors/{doctor_id}/availability",
                          params={"date": target}, headers=bearer(token)).json()
        free = [s for s in data["slots"] if s["available"]]
        if free:
            return free[0]["start_at"], target
    raise AssertionError("no free slot found")
