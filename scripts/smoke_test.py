#!/usr/bin/env python3
"""End-to-end smoke test against a *running* server.

Complements the pytest suite: pytest exercises the code in-process, this drives
the real HTTP API exactly as a browser would, and is the quickest way to prove a
fresh deployment actually works.

    # terminal 1
    uvicorn app.main:app

    # terminal 2
    python scripts/smoke_test.py                       # localhost:8000
    python scripts/smoke_test.py https://your.app      # a deployment

Requires the demo data (SEED_DEMO_DATA=true, the default).
"""

from __future__ import annotations

import sys
import uuid
from collections import Counter
from datetime import date, timedelta

import httpx

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SFX = uuid.uuid4().hex[:8]

GREEN, RED, BOLD, OFF = "\033[32m", "\033[31m", "\033[1m", "\033[0m"


def ok(msg): print(f"  {GREEN}✔{OFF} {msg}")
def head(msg): print(f"\n{BOLD}{msg}{OFF}")
def die(msg): print(f"  {RED}✘ {msg}{OFF}"); sys.exit(1)
def auth(token): return {"Authorization": f"Bearer {token}"}


client = httpx.Client(base_url=BASE, timeout=60)

head("0. SYSTEM")
health = client.get("/api/health").json()
if health["status"] != "ok":
    die(f"health is {health['status']}")
ok(f"health ok · db={health['database']} · tz={health['timezone']} · worker={health['worker_enabled']}")
for i in health["integrations"]:
    ok(f"integration {i['name']}: {'LIVE' if i['configured'] else 'fallback'} ({i['mode']})")

head("1. AUTH & ROLE-BASED ACCESS")
patient = client.post("/api/auth/register", json={
    "email": f"smoke-{SFX}@example.com", "password": "Smoke@12345",
    "full_name": "Smoke Test Patient", "date_of_birth": "1994-02-11", "gender": "Male"})
if patient.status_code != 201:
    die(f"register failed: {patient.text}")
pt = patient.json()["access_token"]
ok(f"registered a patient (role={patient.json()['user']['role']})")

if client.post("/api/auth/register", json={
        "email": f"smoke-{SFX}@example.com", "password": "Smoke@12345",
        "full_name": "Dup"}).status_code != 409:
    die("duplicate email was not rejected")
ok("duplicate email -> 409")

admin = client.post("/api/auth/login", json={"email": "admin@clinix.health", "password": "Admin@12345"})
if admin.status_code != 200:
    die("admin login failed — is the demo data seeded, and ADMIN_PASSWORD unchanged?")
at = admin.json()["access_token"]
ok("admin login")

if client.get("/api/admin/stats", headers=auth(pt)).status_code != 403:
    die("a patient could reach an admin endpoint")
ok("patient blocked from admin endpoints -> 403")
if client.get("/api/admin/stats").status_code != 401:
    die("unauthenticated request was not rejected")
ok("no token -> 401")

head("2. DOCTOR SEARCH")
specs = client.get("/api/doctors/specialisations").json()
ok(f"{len(specs)} specialisations: {', '.join(specs[:4])}…")
doctors = client.get("/api/doctors", params={"specialisation": specs[0]}).json()
doctor = doctors[0]
ok(f"filtered to {doctor['full_name']} ({doctor['slot_duration_minutes']} min slots)")

head("3. AVAILABILITY & TWO-PHASE BOOKING")
slot = day = None
for offset in range(1, 9):
    day = (date.today() + timedelta(days=offset)).isoformat()
    grid = client.get(f"/api/doctors/{doctor['id']}/availability",
                      params={"date": day}, headers=auth(pt)).json()
    free = [s for s in grid["slots"] if s["available"]]
    if free:
        slot = free[0]["start_at"]
        ok(f"{day}: {len(free)}/{len(grid['slots'])} slots free")
        break
if not slot:
    die("no free slots in the next 8 days")

held = client.post("/api/appointments/hold", headers=auth(pt),
                   json={"doctor_id": doctor["id"], "start_at": slot})
if held.status_code != 201:
    die(f"hold failed: {held.text}")
appointment = held.json()
ok(f"PHASE 1 hold {appointment['reference']} · expires in {appointment['hold_seconds_remaining']}s")

grid = client.get(f"/api/doctors/{doctor['id']}/availability",
                  params={"date": day}, headers=auth(pt)).json()
row = next(s for s in grid["slots"] if s["start_at"] == slot)
if row["available"] or row["reason"] != "held":
    die("a held slot still shows as available")
ok("the held slot now reports reason='held' to everyone else")

confirmed = client.post(f"/api/appointments/{appointment['id']}/confirm", headers=auth(pt),
                        json={"symptom_form": {
                            "symptoms": "Persistent dry cough for six days with a mild fever at night. "
                                        "No breathlessness and no chest pain.",
                            "duration_days": 6, "severity": 5,
                            "existing_conditions": "None", "allergies": "None known"}})
if confirmed.status_code != 200:
    die(f"confirm failed: {confirmed.text}")
body = confirmed.json()
summary = body["previsit_summary"]
ok(f"PHASE 2 confirmed · status={body['status']}")
ok(f"AI triage: urgency={summary['urgency']} source={summary['source']} "
   f"questions={len(summary['suggested_questions'])} latency={summary['latency_ms']}ms")
if summary["source"] == "fallback":
    ok("  (LLM unavailable — the rule-based fallback was used, exactly as designed)")
if summary["red_flags"]:
    die(f"negated symptoms produced red flags: {summary['red_flags']}")
ok("negated symptoms ('no breathlessness', 'no chest pain') correctly raised no red flags")

head("4. RACE CONDITION — 10 SIMULTANEOUS ATTEMPTS ON ONE SLOT")
import threading

slot2 = None
for offset in range(1, 9):
    day2 = (date.today() + timedelta(days=offset)).isoformat()
    grid = client.get(f"/api/doctors/{doctor['id']}/availability",
                      params={"date": day2}, headers=auth(pt)).json()
    free = [s for s in grid["slots"] if s["available"]]
    if free:
        slot2 = free[0]["start_at"]
        break

n = 10
tokens = [client.post("/api/auth/register", json={
    "email": f"race-{SFX}-{i}@example.com", "password": "Race@12345",
    "full_name": f"Racer {i}"}).json()["access_token"] for i in range(n)]

barrier, results, lock = threading.Barrier(n), [], threading.Lock()

def attempt(token):
    with httpx.Client(base_url=BASE, timeout=60) as c:
        barrier.wait()
        r = c.post("/api/appointments/hold", headers=auth(token),
                   json={"doctor_id": doctor["id"], "start_at": slot2})
    with lock:
        results.append((r.status_code, (r.json() or {}).get("code")))

threads = [threading.Thread(target=attempt, args=(t,)) for t in tokens]
for t in threads: t.start()
for t in threads: t.join()

winners = [s for s, _ in results if s == 201]
counts = Counter(f"{s} {c or 'OK'}" for s, c in results)
if len(winners) != 1:
    die(f"expected exactly 1 winner, got {len(winners)}: {dict(counts)}")
ok(f"{n} simultaneous attempts -> {dict(counts)}")
ok("exactly ONE booking succeeded — the partial unique index held")

head("5. POST-VISIT")
doctor_login = client.post("/api/auth/login", json={
    "email": doctor["email"], "password": "Password@123"})
if doctor_login.status_code == 200:
    dt = doctor_login.json()["access_token"]
    consultation = client.post(f"/api/appointments/{appointment['id']}/consultation", headers=auth(dt),
                               json={"clinical_notes": "O/E: throat mildly injected, chest clear. "
                                                       "Imp: viral URTI. Advised rest and fluids.",
                                     "diagnosis": "Viral upper respiratory tract infection",
                                     "follow_up_date": (date.today() + timedelta(days=7)).isoformat(),
                                     "prescription_items": [
                                         {"drug_name": "Paracetamol", "dosage": "650 mg",
                                          "frequency": "TDS", "duration_days": 3,
                                          "instructions": "after food"}]})
    if consultation.status_code != 201:
        die(f"consultation failed: {consultation.text}")
    c_body = consultation.json()
    pv = c_body["postvisit_summary"]
    ok(f"notes filed · reminders scheduled = {c_body['prescription_items'][0]['reminder_count']}")
    ok(f"patient summary: source={pv['source']} meds={len(pv['medication_schedule'])} "
       f"steps={len(pv['follow_up_steps'])} warnings={len(pv['warning_signs'])}")
    med = pv["medication_schedule"][0]
    if med["dose"] != "650 mg":
        die(f"the dose was altered: {med['dose']!r} (must be exactly '650 mg')")
    ok(f"dose preserved verbatim: {med['medicine']} {med['dose']} — {med['when']}")

    reminders = client.get("/api/me/medication-reminders", headers=auth(pt)).json()
    ok(f"patient sees {len(reminders)} upcoming doses")
else:
    ok("skipped (demo doctor password differs on this deployment)")

head("6. LEAVE CONFLICT HANDLING")
dry = client.post(f"/api/admin/doctors/{doctor['id']}/leave", headers=auth(at),
                  json={"leave_date": day, "reason": "Smoke test", "confirm": False}).json()
if dry["applied"]:
    die("a dry run applied the leave")
ok(f"DRY RUN: affected={dry['affected_count']}, nothing changed")

applied = client.post(f"/api/admin/doctors/{doctor['id']}/leave", headers=auth(at),
                      json={"leave_date": day, "reason": "Smoke test", "confirm": True}).json()
ok(f"APPLIED: {applied['affected_count']} cancelled, {applied['notifications_queued']} patient(s) emailed")

grid = client.get(f"/api/doctors/{doctor['id']}/availability",
                  params={"date": day}, headers=auth(pt)).json()
if not grid["is_leave"] or any(s["available"] for s in grid["slots"]):
    die("the leave day still has bookable slots")
ok(f"leave day closed: is_leave=true, 0/{len(grid['slots'])} bookable")

head("7. NOTIFICATIONS & BACKGROUND WORKER")
report = client.post("/api/admin/worker/run-once", headers=auth(at)).json()["report"]
ok(f"worker pass: {  {k: v for k, v in report.items() if v} }")
notes = client.get("/api/admin/notifications", headers=auth(at), params={"limit": 200}).json()
statuses = Counter(n["status"] for n in notes)
templates = Counter(n["template"] for n in notes)
ok(f"outbox: {len(notes)} messages · {dict(statuses)}")
ok(f"templates: {dict(templates)}")
if statuses.get("dead"):
    die(f"{statuses['dead']} notification(s) dead-lettered — check the provider credentials")

stats = client.get("/api/admin/stats", headers=auth(at)).json()
ok(f"stats: patients={stats['patients']} doctors={stats['doctors']} "
   f"upcoming={stats['appointments_upcoming']} ai_fallback_rate={stats['llm_fallback_rate']}")

print(f"\n{BOLD}{GREEN} ALL SMOKE TESTS PASSED — {BASE} {OFF}\n")
