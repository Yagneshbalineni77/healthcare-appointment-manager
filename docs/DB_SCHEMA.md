# Database Schema

16 tables. Portable across **SQLite** (development, tests) and **PostgreSQL** (production) — the
same DDL is emitted for both, and the two places the dialects genuinely differ are isolated in
`app/database.py`.

The schema is defined in **[`app/models.py`](../app/models.py)** and created automatically on first
boot (`Base.metadata.create_all`).

---

## Entity relationships

```
                    ┌───────────┐
                    │   users   │  role: patient | doctor | admin
                    └─────┬─────┘
          ┌───────────────┼────────────────────┬─────────────────────┐
          │               │                    │                     │
          ▼               ▼                    ▼                     ▼
  ┌────────────────┐  ┌──────────────┐  ┌──────────────────┐  ┌────────────┐
  │ doctor_profiles│  │ appointments │  │ calendar_accounts│  │ audit_logs │
  └───┬────────┬───┘  │  (patient_id)│  │   (OAuth grant)  │  └────────────┘
      │        │      └───────┬──────┘  └──────────────────┘
      │        │              │
      │        │      ┌───────┴────────┬──────────────────┬───────────────┐
      │        │      ▼                ▼                  ▼               ▼
      │        │ ┌──────────────┐ ┌──────────────────┐ ┌─────────────┐ ┌──────────────┐
      │        │ │symptom_report│ │previsit_summaries│ │consultations│ │calendar_tasks│
      │        │ │    (1:1)     │ │   (1:1, AI)      │ │   (1:1)     │ │  (outbox)    │
      │        │ └──────────────┘ └──────────────────┘ └──────┬──────┘ └──────────────┘
      │        │                                              │
      │        │                              ┌───────────────┴──────────┐
      │        │                              ▼                          ▼
      │        │                  ┌────────────────────┐      ┌────────────────────┐
      │        │                  │ postvisit_summaries│      │   prescriptions    │
      │        │                  │     (1:1, AI)      │      └─────────┬──────────┘
      │        │                  └────────────────────┘                ▼
      │        │                                              ┌────────────────────┐
      ▼        ▼                                              │ prescription_items │
┌──────────────────────┐  ┌───────────────┐                   └─────────┬──────────┘
│ doctor_working_hours │  │ doctor_leaves │                             ▼
└──────────────────────┘  └───────────────┘                   ┌────────────────────┐
                                                              │medication_reminders│
                                                              └────────────────────┘

                          ┌───────────────┐
                          │ notifications │  email outbox, referenced by user + appointment
                          └───────────────┘
```

---

## Design conventions

### All timestamps are UTC, enforced by the type

`UTCDateTime` is a `TypeDecorator` that converts to UTC on write and re-attaches UTC on read.

Without it: PostgreSQL's `timestamptz` returns *aware* datetimes while SQLite returns *naive* ones,
so every `dt <= utcnow()` comparison would raise
`can't compare offset-naive and offset-aware datetimes` on SQLite and work fine on PostgreSQL — the
worst class of bug, one that only appears on the environment you did not develop on. Normalising at
the type boundary makes the whole codebase dialect-agnostic.

Clinic-local time is a **presentation** concern, handled in `app/services/slots.py` using
`CLINIC_TIMEZONE`.

### Enum-like columns are `String` + `CheckConstraint`

Native PostgreSQL enums need a migration to add a value; strings do not. The `CheckConstraint` means
the database still rejects anything unexpected. Python-side, `enum.StrEnum` gives the same
type-safety at the application layer.

### Deletes are soft where history matters

Deactivating a doctor sets `users.is_active = false` and `is_accepting_patients = false`. Hard
deletion would orphan clinical records a clinic is obliged to keep. Cancelled appointments keep their
row — they simply drop out of the partial unique index.

### Reliability tables are outboxes, not logs

`notifications` and `calendar_tasks` are **work queues**, written inside business transactions and
drained by the worker. Both carry `status`, `attempts`, `max_attempts`, `next_attempt_at`,
`last_error` and a unique `idempotency_key`.

---

## The index that prevents double-booking

```sql
CREATE UNIQUE INDEX uq_active_doctor_slot
    ON appointments (doctor_id, start_at)
 WHERE status IN ('held', 'confirmed');
```

Declared once, portably:

```python
Index(
    "uq_active_doctor_slot", "doctor_id", "start_at", unique=True,
    sqlite_where=text("status IN ('held','confirmed')"),
    postgresql_where=text("status IN ('held','confirmed')"),
)
```

Three properties matter:

1. **It is the guarantee.** Two concurrent inserts for the same `(doctor_id, start_at)` cannot both
   commit, whatever the interleaving, across any number of processes. The application's pre-check is
   only there for a nicer error message.
2. **It is partial.** `cancelled`, `expired`, `completed` and `no_show` rows are *not* in the index,
   so a released slot is immediately bookable again — no cleanup job, no tombstones.
3. **It covers holds.** `held` rows occupy the index exactly like `confirmed` ones, which is what
   makes the two-phase booking flow safe.

Supporting indexes on `appointments`: `(doctor_id, start_at)`, `(patient_id, start_at)`,
`(status, start_at)`, and `hold_expires_at` — matching the four query shapes the app actually issues
(a doctor's day, a patient's list, the reminder sweep, the hold-expiry sweep).

---

## Reference

Generated from the SQLAlchemy metadata, so it cannot drift from the code.

### `users`

Every human in the system. One table for all three roles — `role` discriminates.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `email` | VARCHAR(255) | NOT NULL, UNIQUE, indexed |
| `password_hash` | VARCHAR(255) | NOT NULL |
| `role` | VARCHAR(16) | NOT NULL, indexed |
| `full_name` | VARCHAR(160) | NOT NULL |
| `phone` | VARCHAR(32) | — |
| `date_of_birth` | DATE | — |
| `gender` | VARCHAR(24) | — |
| `is_active` | BOOLEAN | NOT NULL |
| `created_at` | TIMESTAMPTZ | NOT NULL |
| `updated_at` | TIMESTAMPTZ | NOT NULL |

**Indexes & checks**

- `ix_users_email` on (email) — UNIQUE
- CHECK `role IN ('patient','doctor','admin')`

### `doctor_profiles`

Clinical configuration for a doctor. 1:1 with a `users` row of role `doctor`.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `user_id` | INTEGER | NOT NULL, UNIQUE, → `users.id` |
| `specialisation` | VARCHAR(120) | NOT NULL, indexed |
| `qualifications` | VARCHAR(255) | — |
| `bio` | TEXT | — |
| `experience_years` | INTEGER | NOT NULL |
| `consultation_fee` | FLOAT | NOT NULL |
| `slot_duration_minutes` | INTEGER | NOT NULL |
| `buffer_minutes` | INTEGER | NOT NULL |
| `room` | VARCHAR(64) | — |
| `is_accepting_patients` | BOOLEAN | NOT NULL |
| `created_at` | TIMESTAMPTZ | NOT NULL |
| `updated_at` | TIMESTAMPTZ | NOT NULL |

**Indexes & checks**

- CHECK `buffer_minutes BETWEEN 0 AND 120`
- CHECK `slot_duration_minutes BETWEEN 5 AND 240`

### `doctor_working_hours`

One contiguous availability window on one weekday. Multiple rows per weekday give split shifts (10:00–13:00 and 17:00–20:00).

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `doctor_id` | INTEGER | NOT NULL, → `doctor_profiles.id`, indexed |
| `weekday` | INTEGER | NOT NULL |
| `start_time` | TIME | NOT NULL |
| `end_time` | TIME | NOT NULL |

**Indexes & checks**

- CHECK `weekday BETWEEN 0 AND 6`

### `doctor_leaves`

A full-day leave. `affected_appointment_count` records what applying it cancelled.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `doctor_id` | INTEGER | NOT NULL, → `doctor_profiles.id`, indexed |
| `leave_date` | DATE | NOT NULL, indexed |
| `reason` | VARCHAR(255) | — |
| `created_by_user_id` | INTEGER | → `users.id` |
| `affected_appointment_count` | INTEGER | NOT NULL |
| `created_at` | TIMESTAMPTZ | NOT NULL |

### `appointments`

A booked slot. **Carries the partial unique index that prevents double-booking.**

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `reference` | VARCHAR(16) | NOT NULL, UNIQUE, indexed |
| `doctor_id` | INTEGER | NOT NULL, → `doctor_profiles.id` |
| `patient_id` | INTEGER | NOT NULL, → `users.id` |
| `start_at` | TIMESTAMPTZ | NOT NULL |
| `end_at` | TIMESTAMPTZ | NOT NULL |
| `status` | VARCHAR(16) | NOT NULL |
| `hold_expires_at` | TIMESTAMPTZ | indexed |
| `confirmed_at` | TIMESTAMPTZ | — |
| `reason_for_visit` | VARCHAR(255) | — |
| `mode` | VARCHAR(16) | NOT NULL |
| `cancelled_at` | TIMESTAMPTZ | — |
| `cancelled_by` | VARCHAR(24) | — |
| `cancellation_reason` | VARCHAR(255) | — |
| `rescheduled_from_at` | TIMESTAMPTZ | — |
| `reschedule_count` | INTEGER | NOT NULL |
| `patient_calendar_event_id` | VARCHAR(255) | — |
| `doctor_calendar_event_id` | VARCHAR(255) | — |
| `reminder_sent_at` | TIMESTAMPTZ | — |
| `created_at` | TIMESTAMPTZ | NOT NULL |
| `updated_at` | TIMESTAMPTZ | NOT NULL |

**Indexes & checks**

- `ix_appointments_doctor_window` on (doctor_id, start_at)
- `ix_appointments_patient_window` on (patient_id, start_at)
- `ix_appointments_reference` on (reference) — UNIQUE
- `ix_appointments_status_start` on (status, start_at)
- `uq_active_doctor_slot` on (doctor_id, start_at) — UNIQUE
- CHECK `end_at > start_at`
- CHECK `status IN ('held','confirmed','completed','cancelled','expired','no_show')`

### `symptom_reports`

The structured intake form the patient completes before confirming. 1:1 with an appointment.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `appointment_id` | INTEGER | NOT NULL, UNIQUE, → `appointments.id` |
| `symptoms` | TEXT | NOT NULL |
| `duration_days` | INTEGER | — |
| `severity` | INTEGER | — |
| `existing_conditions` | TEXT | — |
| `current_medications` | TEXT | — |
| `allergies` | TEXT | — |
| `created_at` | TIMESTAMPTZ | NOT NULL |

**Indexes & checks**

- CHECK `severity IS NULL OR severity BETWEEN 1 AND 10`

### `previsit_summaries`

LLM triage brief shown to the doctor, with full provenance. 1:1 with an appointment.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `appointment_id` | INTEGER | NOT NULL, UNIQUE, → `appointments.id` |
| `urgency` | VARCHAR(8) | NOT NULL |
| `chief_complaint` | VARCHAR(500) | NOT NULL |
| `suggested_questions` | JSON | NOT NULL |
| `red_flags` | JSON | NOT NULL |
| `summary_note` | TEXT | — |
| `created_at` | TIMESTAMPTZ | NOT NULL |
| `source` | VARCHAR(16) | NOT NULL |
| `model` | VARCHAR(80) | — |
| `prompt_version` | VARCHAR(24) | — |
| `latency_ms` | INTEGER | — |
| `attempts` | INTEGER | NOT NULL |
| `error` | TEXT | — |
| `raw_response` | TEXT | — |

**Indexes & checks**

- CHECK `urgency IN ('Low','Medium','High')`

### `consultations`

The doctor's record of the visit. 1:1 with an appointment.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `appointment_id` | INTEGER | NOT NULL, UNIQUE, → `appointments.id` |
| `doctor_id` | INTEGER | NOT NULL, → `doctor_profiles.id` |
| `clinical_notes` | TEXT | NOT NULL |
| `diagnosis` | VARCHAR(255) | — |
| `follow_up_date` | DATE | — |
| `created_at` | TIMESTAMPTZ | NOT NULL |
| `updated_at` | TIMESTAMPTZ | NOT NULL |

### `postvisit_summaries`

Plain-language rewrite of the clinical notes for the patient, with provenance. 1:1 with a consultation.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `consultation_id` | INTEGER | NOT NULL, UNIQUE, → `consultations.id` |
| `patient_summary` | TEXT | NOT NULL |
| `medication_schedule` | JSON | NOT NULL |
| `follow_up_steps` | JSON | NOT NULL |
| `warning_signs` | JSON | NOT NULL |
| `created_at` | TIMESTAMPTZ | NOT NULL |
| `source` | VARCHAR(16) | NOT NULL |
| `model` | VARCHAR(80) | — |
| `prompt_version` | VARCHAR(24) | — |
| `latency_ms` | INTEGER | — |
| `attempts` | INTEGER | NOT NULL |
| `error` | TEXT | — |
| `raw_response` | TEXT | — |

### `prescriptions`

Container for what was prescribed at one consultation.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `consultation_id` | INTEGER | NOT NULL, UNIQUE, → `consultations.id` |
| `patient_id` | INTEGER | NOT NULL, → `users.id`, indexed |
| `notes` | TEXT | — |
| `created_at` | TIMESTAMPTZ | NOT NULL |

### `prescription_items`

One prescribed medicine, with frequency and duration.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `prescription_id` | INTEGER | NOT NULL, → `prescriptions.id`, indexed |
| `drug_name` | VARCHAR(160) | NOT NULL |
| `dosage` | VARCHAR(80) | NOT NULL |
| `frequency` | VARCHAR(8) | NOT NULL |
| `duration_days` | INTEGER | NOT NULL |
| `instructions` | VARCHAR(255) | — |
| `start_date` | DATE | — |

**Indexes & checks**

- CHECK `duration_days BETWEEN 1 AND 180`
- CHECK `frequency IN ('OD','BD','TDS','QID','QHS','SOS')`

### `medication_reminders`

One scheduled dose. Materialised when the prescription is written.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `prescription_item_id` | INTEGER | NOT NULL, → `prescription_items.id` |
| `patient_id` | INTEGER | NOT NULL, → `users.id`, indexed |
| `due_at` | TIMESTAMPTZ | NOT NULL |
| `status` | VARCHAR(16) | NOT NULL |
| `sent_at` | TIMESTAMPTZ | — |
| `created_at` | TIMESTAMPTZ | NOT NULL |

**Indexes & checks**

- `ix_med_reminder_due` on (status, due_at)

### `notifications`

**Email outbox.** Written in the same transaction as the business change, delivered by the worker with backoff.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `idempotency_key` | VARCHAR(180) | NOT NULL, UNIQUE |
| `template` | VARCHAR(64) | NOT NULL, indexed |
| `to_email` | VARCHAR(255) | NOT NULL |
| `to_name` | VARCHAR(160) | — |
| `subject` | VARCHAR(255) | NOT NULL |
| `body_text` | TEXT | NOT NULL |
| `body_html` | TEXT | — |
| `user_id` | INTEGER | → `users.id`, indexed |
| `appointment_id` | INTEGER | → `appointments.id`, indexed |
| `status` | VARCHAR(16) | NOT NULL |
| `attempts` | INTEGER | NOT NULL |
| `max_attempts` | INTEGER | NOT NULL |
| `next_attempt_at` | TIMESTAMPTZ | NOT NULL |
| `last_error` | TEXT | — |
| `provider` | VARCHAR(24) | — |
| `sent_at` | TIMESTAMPTZ | — |
| `created_at` | TIMESTAMPTZ | NOT NULL |

**Indexes & checks**

- `ix_notifications_dispatch` on (status, next_attempt_at)
- CHECK `status IN ('pending','sent','failed','dead','cancelled')`

### `calendar_tasks`

**Google Calendar outbox.** Same reliability contract as `notifications`.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `idempotency_key` | VARCHAR(180) | NOT NULL, UNIQUE |
| `action` | VARCHAR(12) | NOT NULL |
| `appointment_id` | INTEGER | NOT NULL, → `appointments.id`, indexed |
| `user_id` | INTEGER | NOT NULL, → `users.id` |
| `role` | VARCHAR(12) | NOT NULL |
| `payload` | JSON | NOT NULL |
| `status` | VARCHAR(16) | NOT NULL |
| `attempts` | INTEGER | NOT NULL |
| `max_attempts` | INTEGER | NOT NULL |
| `next_attempt_at` | TIMESTAMPTZ | NOT NULL |
| `last_error` | TEXT | — |
| `external_event_id` | VARCHAR(255) | — |
| `completed_at` | TIMESTAMPTZ | — |
| `created_at` | TIMESTAMPTZ | NOT NULL |

**Indexes & checks**

- `ix_calendar_tasks_dispatch` on (status, next_attempt_at)
- CHECK `action IN ('create','update','delete')`

### `calendar_accounts`

A stored Google OAuth 2.0 grant for one user.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `user_id` | INTEGER | NOT NULL, UNIQUE, → `users.id` |
| `provider` | VARCHAR(24) | NOT NULL |
| `google_email` | VARCHAR(255) | — |
| `calendar_id` | VARCHAR(255) | NOT NULL |
| `access_token` | TEXT | — |
| `refresh_token` | TEXT | — |
| `token_expires_at` | TIMESTAMPTZ | — |
| `scope` | TEXT | — |
| `connected_at` | TIMESTAMPTZ | NOT NULL |
| `revoked_at` | TIMESTAMPTZ | — |

### `audit_logs`

Append-only trail of admin-visible actions.

| Column | Type | Constraints |
|---|---|---|
| `id` | INTEGER | PK |
| `actor_user_id` | INTEGER | → `users.id`, indexed |
| `action` | VARCHAR(64) | NOT NULL, indexed |
| `entity_type` | VARCHAR(48) | NOT NULL |
| `entity_id` | VARCHAR(48) | — |
| `meta` | JSON | NOT NULL |
| `created_at` | TIMESTAMPTZ | NOT NULL, indexed |

---

## Migrations

The app calls `Base.metadata.create_all()` on startup, which is sufficient for this project: it
creates anything missing and is safe to run repeatedly.

For a clinic in production you would add Alembic:

```bash
pip install alembic
alembic init migrations          # point env.py at app.database:Base.metadata
alembic revision --autogenerate -m "initial"
alembic upgrade head
```

The schema was written with that in mind — string-based enums and additive nullable columns keep
migrations cheap.
