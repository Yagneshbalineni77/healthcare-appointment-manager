# API Reference

Base URL: `http://localhost:8000` (or your deployed origin).
Interactive, always-current reference: **`/docs`** (Swagger UI) and **`/redoc`**.
Machine-readable spec: **`/openapi.json`**.

---

## Authentication

JWT bearer tokens. Sign in, then send the token on every subsequent request.

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"aarav.sharma@example.com","password":"Password@123"}' | jq -r .access_token)

curl -s localhost:8000/api/auth/me -H "Authorization: Bearer $TOKEN"
```

In Swagger UI: run `POST /api/auth/login`, copy `access_token`, click **Authorize**, paste it.

Tokens are HS256, valid for `ACCESS_TOKEN_TTL_MINUTES` (default 12 hours), and carry
`sub`, `email`, `role`, `name`, `iat`, `exp`, `jti`.

### Roles

| Role | Created by | Can do |
|---|---|---|
| `patient` | self-registration (`POST /api/auth/register`) | search doctors, book/reschedule/cancel own appointments, submit symptom forms, read own summaries and reminders |
| `doctor` | an admin | see own schedule, read triage briefs for own patients, file consultation notes and prescriptions |
| `admin` | seeded on first boot | manage doctors and leave, see all appointments, inspect the outboxes and audit trail |

**Self-registration always creates a `patient`.** A `role` field in the request body is ignored —
there is no path for a client to escalate its own privileges.

---

## Error format

Every error returns the same shape, with a stable machine-readable `code`:

```json
{ "detail": "That slot was just taken. Please choose another time.", "code": "SLOT_TAKEN" }
```

Validation errors add a per-field breakdown:

```json
{
  "detail": "password: String should have at least 8 characters",
  "code": "VALIDATION_ERROR",
  "errors": [{ "field": "password", "message": "String should have at least 8 characters" }]
}
```

### Codes

| Code | HTTP | Meaning |
|---|---|---|
| `VALIDATION_ERROR` | 422 | Request body or query failed validation |
| `BAD_CREDENTIALS` | 401 | Wrong email or password (identical message for both, to avoid email enumeration) |
| `ACCOUNT_DISABLED` | 403 | The account was deactivated |
| `FORBIDDEN` | 403 | Authenticated, but not allowed to touch this resource |
| `NOT_FOUND` | 404 | No such record, or not visible to you |
| `EMAIL_TAKEN` | 409 | An account with that email exists |
| `SLOT_TAKEN` | 409 | Another patient holds or has confirmed this slot |
| `PATIENT_BUSY` | 409 | The patient already has an overlapping appointment |
| `LEAVE_EXISTS` | 409 | That doctor is already on leave that day |
| `CONSULTATION_EXISTS` | 409 | Notes have already been filed for this appointment |
| `INVALID_STATE` | 409 | The action is not legal from the record's current status |
| `HOLD_EXPIRED` | 410 | The reservation timed out — pick the slot again |
| `SLOT_NOT_BOOKABLE` | 422 | Generic slot rejection; see the specific codes below |
| `OFF_GRID` | 422 | The time is not one of the doctor's slot starts |
| `TOO_LATE` | 422 | Inside `MIN_LEAD_MINUTES` of the start time |
| `TOO_FAR` | 422 | Beyond `BOOKING_HORIZON_DAYS` |
| `DOCTOR_ON_LEAVE` | 422 | The doctor is on leave that day |
| `DOCTOR_UNAVAILABLE` | 422 | The doctor has paused new bookings or is deactivated |
| `INTEGRATION_DISABLED` | 503 | The feature is not configured on this deployment |
| `DB_UNAVAILABLE` | 503 | Database error |

Every response also carries `X-Request-ID` and `X-Response-Time-ms` headers.

---

## The booking flow

Booking is **two-phase**. This is the part worth reading carefully.

```
  ┌────────────────────────────┐
  │ GET /api/doctors           │  search by specialisation or free text
  └────────────┬───────────────┘
               ▼
  ┌────────────────────────────┐
  │ GET /api/doctors/{id}/     │  every slot, each marked available with a
  │     availability?date=...  │  reason when it is not: booked/held/past/leave
  └────────────┬───────────────┘
               ▼
  ┌────────────────────────────┐
  │ POST /api/appointments/    │  ← PHASE 1. status = "held",
  │      hold                  │    hold_expires_at = now + SLOT_HOLD_MINUTES
  └────────────┬───────────────┘    409 SLOT_TAKEN if someone beat you to it
               ▼
       (patient fills the symptom form — the slot is already theirs)
               ▼
  ┌────────────────────────────┐
  │ POST /api/appointments/    │  ← PHASE 2. Saves the form, confirms the
  │      {id}/confirm          │    booking, THEN runs the LLM, then queues
  └────────────┬───────────────┘    emails + calendar events
               ▼                     410 HOLD_EXPIRED if the timer ran out
         status = "confirmed"
```

### 1. Find a doctor

```bash
curl -s "localhost:8000/api/doctors?specialisation=Dermatology"
curl -s "localhost:8000/api/doctors?q=skin"          # free text over name, spec, bio, qualifications
curl -s "localhost:8000/api/doctors/specialisations"
```

### 2. Read availability

```bash
curl -s "localhost:8000/api/doctors/3/availability?date=2026-08-25" -H "Authorization: Bearer $TOKEN"
```

```json
{
  "doctor_id": 3, "date": "2026-08-25", "is_leave": false, "is_working_day": true,
  "timezone": "Asia/Kolkata", "slot_duration_minutes": 20,
  "slots": [
    { "start_at": "2026-08-25T05:30:00Z", "end_at": "2026-08-25T05:50:00Z",
      "label": "11:00 AM", "available": true,  "reason": null },
    { "start_at": "2026-08-25T05:55:00Z", "end_at": "2026-08-25T06:15:00Z",
      "label": "11:25 AM", "available": false, "reason": "held" }
  ]
}
```

`reason` is one of `booked`, `held`, `past`, `leave`, `not_accepting`. Expired holds are swept
*before* the grid is built, so an abandoned booking never keeps a slot looking busy.

Use `/availability-range?from=2026-08-25&days=7` to build a week view in one request.

### 3. Hold the slot

```bash
curl -s -X POST localhost:8000/api/appointments/hold \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"doctor_id": 3, "start_at": "2026-08-25T05:30:00Z", "reason_for_visit": "Persistent rash"}'
```

Returns `201` with `status: "held"`, `hold_expires_at`, and `hold_seconds_remaining` for a UI
countdown. Returns `409 SLOT_TAKEN` if another patient won the race.

### 4. Submit the symptom form and confirm

```bash
curl -s -X POST localhost:8000/api/appointments/12/confirm \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"symptom_form": {
        "symptoms": "Itchy red patches on both forearms for ten days, spreading slowly.",
        "duration_days": 10, "severity": 5,
        "existing_conditions": "Mild asthma", "current_medications": "None", "allergies": "Dust"}}'
```

The response includes the stored triage brief:

```json
{
  "status": "confirmed", "reference": "APT-PUYY7N",
  "start_at_local": "Tue, 25 Aug 2026 · 11:00 AM IST",
  "previsit_summary": {
    "urgency": "Medium",
    "chief_complaint": "Itchy erythematous patches b/l forearms, 10 days, slowly spreading.",
    "suggested_questions": ["…", "…", "…"],
    "red_flags": [],
    "source": "llm", "model": "gemini-2.5-flash", "latency_ms": 4156, "error": null
  }
}
```

**`source` is the field to watch.** `"llm"` means the model produced it; `"fallback"` means the model
was unavailable and the deterministic rule-based summary was stored instead, with the reason in
`error`. The booking succeeds either way.

### 5. Reschedule / cancel

```bash
curl -s -X POST localhost:8000/api/appointments/12/reschedule \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"start_at": "2026-08-26T05:55:00Z", "reason": "work clash"}'

curl -s -X POST localhost:8000/api/appointments/12/cancel \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"reason": "feeling better"}'
```

Both release the previous slot immediately, email both parties, and update or delete the calendar
events. Cancelling a `held` (not yet confirmed) appointment sends no email — there is nothing to
apologise for.

---

## Post-visit flow

```bash
curl -s -X POST localhost:8000/api/appointments/12/consultation \
  -H "Authorization: Bearer $DOCTOR_TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "clinical_notes": "O/E: erythematous papular rash b/l forearms. Imp: atopic dermatitis flare.",
    "diagnosis": "Atopic dermatitis (flare)",
    "follow_up_date": "2026-09-08",
    "prescription_items": [
      {"drug_name":"Cetirizine","dosage":"10 mg","frequency":"QHS","duration_days":7,
       "instructions":"at bedtime, may cause drowsiness"}
    ]}'
```

This one call: marks the appointment `completed`, stores the notes and prescription, **materialises
one `medication_reminders` row per scheduled dose**, generates the patient-friendly summary, and
queues the summary email.

### Prescription frequencies

| Code | Meaning | Reminder times (clinic-local) |
|---|---|---|
| `OD` | once daily | 09:00 |
| `BD` | twice daily | 09:00, 21:00 |
| `TDS` | three times daily | 08:00, 14:00, 20:00 |
| `QID` | four times daily | 08:00, 12:00, 16:00, 20:00 |
| `QHS` | at bedtime | 22:00 |
| `SOS` | as needed | none — no reminders are scheduled |

Doses already in the past are skipped, so writing a prescription at 3pm does not immediately fire
the 8am reminder.

---

## Doctor leave — the two-step call

```bash
# Step 1: dry run. Changes NOTHING. Returns the impact report.
curl -s -X POST localhost:8000/api/admin/doctors/1/leave \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"leave_date": "2026-08-24", "reason": "Conference", "confirm": false}'
```

```json
{
  "applied": false, "affected_count": 1,
  "affected": [{ "reference": "APT-PKRQQK", "patient_name": "Aarav Sharma",
                 "patient_email": "aarav.sharma@example.com",
                 "start_at_local": "Mon, 24 Aug 2026 · 10:00 AM IST" }],
  "message": "Dry run — nothing changed. Applying this leave will cancel 1 appointment(s) and email 1 patient(s)."
}
```

```bash
# Step 2: apply. Leave + cancellations + emails + calendar deletions, in ONE transaction.
curl -s -X POST localhost:8000/api/admin/doctors/1/leave \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"leave_date": "2026-08-24", "reason": "Conference", "confirm": true}'
```

---

## Operational endpoints

```bash
curl -s localhost:8000/api/health                                            # public
curl -s localhost:8000/api/admin/notifications?status=dead -H "Authorization: Bearer $ADMIN_TOKEN"
curl -s -X POST localhost:8000/api/admin/notifications/7/requeue -H "Authorization: Bearer $ADMIN_TOKEN"
curl -s -X POST localhost:8000/api/admin/worker/run-once -H "Authorization: Bearer $ADMIN_TOKEN"
```

`/api/admin/worker/run-once` forces a worker pass instead of waiting for the interval — useful for
demos and integration tests. It returns the counters from the pass:

```json
{"ran": true, "report": {
  "holds_expired": 0, "appointment_reminders_queued": 2, "medication_reminders_queued": 0,
  "summaries_backfilled": 0, "no_shows": 0,
  "email": {"picked": 12, "sent": 12, "retrying": 0, "dead": 0},
  "calendar": {"picked": 10, "completed": 0, "retrying": 0, "dead": 0, "skipped": 10}}}
```

`calendar.skipped` counts tasks for users who have not connected Google Calendar — expected, not an
error.

---

## Complete endpoint list

Generated from the live OpenAPI specification.

### System

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/config` | Public bootstrap config for the frontend |
| `GET` | `/api/health` | Liveness + integration status |
| `GET` | `/api/llm/status` | LLM client health and circuit-breaker state |

### Authentication

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/login` | Sign in (any role) |
| `GET` | `/api/auth/me` | Current signed-in user |
| `POST` | `/api/auth/register` | Register a patient account |

### Doctors

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/doctors` | Search doctors by specialisation or name |
| `GET` | `/api/doctors/specialisations` | Distinct specialisations offered |
| `GET` | `/api/doctors/{doctor_id}` | One doctor's public profile |
| `GET` | `/api/doctors/{doctor_id}/availability` | Slot grid for one day |
| `GET` | `/api/doctors/{doctor_id}/availability-range` | Slot grid for several consecutive days |

### Appointments

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/appointments` | My appointments (role-aware) |
| `POST` | `/api/appointments/hold` | Phase 1 — reserve a slot while the patient fills the symptom form |
| `GET` | `/api/appointments/{appointment_id}` | One appointment |
| `POST` | `/api/appointments/{appointment_id}/cancel` | Cancel an appointment |
| `POST` | `/api/appointments/{appointment_id}/confirm` | Phase 2 — submit the symptom form and confirm the booking |
| `GET` | `/api/appointments/{appointment_id}/previsit-summary` | AI triage brief for the doctor |
| `POST` | `/api/appointments/{appointment_id}/previsit-summary/regenerate` | Re-run the AI triage (doctor/admin) |
| `POST` | `/api/appointments/{appointment_id}/reschedule` | Move to another slot |

### Consultations

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/appointments/{appointment_id}/consultation` | Read the consultation record and patient summary |
| `POST` | `/api/appointments/{appointment_id}/consultation` | File post-visit notes and prescription (doctor only) |
| `POST` | `/api/appointments/{appointment_id}/consultation/regenerate-summary` | Re-run the patient-friendly summary (doctor/admin) |
| `GET` | `/api/me/medication-reminders` | My upcoming medication reminders |
| `DELETE` | `/api/me/medication-reminders/{reminder_id}` | Stop one medication reminder |

### Admin

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/appointments` | All appointments |
| `GET` | `/api/admin/audit` | Recent admin actions |
| `GET` | `/api/admin/calendar-tasks` | Google Calendar outbox |
| `GET` | `/api/admin/doctors` | All doctors, including deactivated |
| `POST` | `/api/admin/doctors` | Create a doctor (login + clinical profile + weekly schedule) |
| `DELETE` | `/api/admin/doctors/{doctor_id}` | Deactivate a doctor (soft delete) |
| `PATCH` | `/api/admin/doctors/{doctor_id}` | Update a doctor profile or schedule |
| `POST` | `/api/admin/doctors/{doctor_id}/leave` | Mark a doctor on leave (dry run by default) |
| `GET` | `/api/admin/doctors/{doctor_id}/leaves` | A doctor's leave days |
| `DELETE` | `/api/admin/leaves/{leave_id}` | Remove a leave day |
| `GET` | `/api/admin/notifications` | Email outbox |
| `POST` | `/api/admin/notifications/{notification_id}/requeue` | Retry a dead notification |
| `GET` | `/api/admin/stats` | Dashboard counters |
| `POST` | `/api/admin/worker/run-once` | Run one background-worker pass immediately |

### Google Calendar

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/calendar/callback` | OAuth 2.0 redirect target (called by Google, not by the SPA) |
| `GET` | `/api/calendar/connect` | Get the Google consent URL |
| `POST` | `/api/calendar/disconnect` | Revoke and forget the Google grant |
| `GET` | `/api/calendar/status` | Is this user's Google Calendar connected? |

