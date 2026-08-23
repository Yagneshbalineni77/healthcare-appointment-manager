# Clinix — Healthcare Appointment & Follow-up Manager

A clinic platform with three portals — **patient**, **doctor**, **admin**. Patients book a slot and
describe their symptoms in advance; the doctor gets an AI triage brief before the visit; the patient
gets a plain-language summary and medication reminders after it. Both sides are kept informed by
email and Google Calendar.

| | |
|---|---|
| **Live demo** | **https://clinix-2xyl.onrender.com** · first load takes ~50s while the free Render instance wakes |
| **API reference** | `/docs` (Swagger UI) · `/redoc` |
| **Stack** | FastAPI · SQLAlchemy 2 · PostgreSQL/SQLite · vanilla ES-module frontend (no build step) |
| **Tests** | 85 passing — `pytest -q` |

---

## Contents

- [Why this design](#why-this-design)
- [Quick start](#quick-start-2-minutes)
- [Demo accounts](#demo-accounts)
- [Try the whole flow](#try-the-whole-flow-in-3-minutes)
- [Architecture](#architecture)
- [The four hard problems](#the-four-hard-problems)
- [Project layout](#project-layout)
- [Configuration](#configuration)
- [API overview](#api-overview)
- [Database schema](#database-schema)
- [LLM prompts](#llm-prompts)
- [Google Calendar setup](#google-calendar-setup)
- [Running the tests](#running-the-tests)
- [Deployment](#deployment)
- [Deliverables map](#deliverables-map)

---

## Why this design

Three decisions shape everything else.

**1. Correctness lives in the database, not in application code.**
Double-booking is prevented by a *partial unique index* on `(doctor_id, start_at)` restricted to
active statuses. No sequence of concurrent requests can defeat it, on any number of processes.
Application-level checks exist only to return a friendlier error in the uncontended case.

**2. Booking is two-phase, because the brief demands thinking time.**
A symptom form and an LLM call sit between "I want 10:30" and "I have 10:30". Without a hold, that
window is a race: two patients fill in forms for the same slot and only find out at submit.
`POST /appointments/hold` reserves the slot for five minutes; `POST /appointments/{id}/confirm`
completes it. Abandoned holds expire and the slot returns to the pool.

**3. Nothing that talks to a third party runs inside a request.**
Emails and calendar writes are committed as *outbox rows* in the same transaction as the booking,
then delivered by a background worker with exponential backoff. A SendGrid outage delays a
confirmation; it cannot roll back a confirmed appointment. The same discipline covers the LLM: the
appointment is committed **before** the model is called, so a slow or dead model never costs a
patient their slot.

---

## Quick start (2 minutes)

Requires **Python 3.11+**. No database server, no Node, no Docker.

```bash
git clone <your-repo-url>
cd healthcare_appointment_manager

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # works as-is; every integration has a safe fallback
uvicorn app.main:app --reload
```

Open **<http://localhost:8000>**.

On first boot the app creates its schema and seeds a demo clinic — 6 doctors with real working
hours, 3 patients, and 2 confirmed appointments complete with AI triage briefs. You land on a
working system, not three empty portals.

> **It runs with an empty `.env`.** Without `GEMINI_API_KEY` the summaries come from the rule-based
> fallback; without an email provider messages are written to `var/outbox/*.eml` and listed in the
> admin portal; without Google credentials calendar tasks are skipped. Nothing errors. Add each
> credential to light up the corresponding integration — see [Configuration](#configuration).

---

> **Note on email in the hosted demo.** Render's free tier blocks outbound SMTP
> (ports 25/465/587), so the deployed instance cannot reach Gmail. Notifications are
> therefore queued and retried rather than delivered — visible, with their error and
> retry count, under **Admin → Operations**. This is the transactional-outbox design
> behaving exactly as intended under a transport outage: nothing is lost, and setting
> `SENDGRID_API_KEY` (an HTTPS API, which is not blocked) flushes the whole queue.
> Email delivery is fully working when run locally over SMTP.

## Demo accounts

Password is pre-filled by the **Demo accounts** buttons on the sign-in screen.

| Role | Email | Password |
|---|---|---|
| Admin | `admin@clinix.health` | `Admin@12345` |
| Doctor | `meera.iyer@clinix.health` | `Password@123` |
| Doctor | `sana.qureshi@clinix.health` | `Password@123` |
| Patient | `aarav.sharma@example.com` | `Password@123` |
| Patient | `neha.gupta@example.com` | `Password@123` |

Change `ADMIN_EMAIL` / `ADMIN_PASSWORD` before deploying anywhere public.

---

## Try the whole flow in 3 minutes

1. **Book (patient).** Sign in as `aarav.sharma@example.com` → *Find a doctor* → search `skin` →
   pick a slot. Watch the **5-minute countdown** start — that slot is now yours and shows as `held`
   to everyone else. Fill in the symptom form and confirm. The AI triage brief appears immediately.
2. **See the race prevention.** In a second browser (or a private window), sign in as
   `neha.gupta@example.com` and try to take the same slot. You get `409 SLOT_TAKEN`.
3. **Triage (doctor).** Sign in as `sana.qureshi@clinix.health` → *My schedule*. Patients are sorted
   **most urgent first**, each with a chief complaint, red flags and three suggested questions.
   Click *Full briefing* to see the patient's own words next to the AI summary.
4. **Post-visit.** Click *File notes*, write clinical shorthand, add a medicine (`Cetirizine`,
   `10 mg`, `QHS`, 7 days). On submit you get a plain-language patient summary, and a reminder is
   scheduled for **every dose**.
5. **Medicines (patient).** Back as the patient → *My medicines* — every upcoming dose, grouped by day.
6. **Leave conflict (admin).** Sign in as `admin@clinix.health` → *Doctors & leave* → *Mark leave* on
   a day with a booking. You get a **dry-run impact report** naming every affected patient and
   nothing changes until you confirm. Confirm, and the appointments are cancelled, apology emails
   queued and calendar events removed — in one transaction.
7. **Watch the plumbing.** *Operations* → the email outbox with statuses, attempts and errors; the
   calendar queue; the audit trail. Hit **▶ Run worker now** to drain it on demand.

---

## Architecture

```
                        ┌──────────────────────────────────────────┐
  Browser               │  FastAPI  (app/main.py)                  │
  ─────────             │                                          │
  patient portal  ────► │  routers/   auth, doctors, appointments, │
  doctor portal   ────► │             consultations, admin,        │
  admin portal    ────► │             calendar, ops                │
  (vanilla ES modules)  │      │                                   │
                        │      ▼                                   │
                        │  services/  slots ── availability +      │
                        │             │        race-safe booking   │
                        │             llm ──── Gemini + fallback   │
                        │             email ── outbox + transports │
                        │             gcal ─── OAuth2 + outbox     │
                        │      │                                   │
                        │      ▼                                   │
                        │  SQLAlchemy ──► SQLite / PostgreSQL      │
                        │      ▲                                   │
                        │      │                                   │
                        │  workers/scheduler.py  (asyncio loop)    │
                        │    • expire stale holds                  │
                        │    • queue appointment reminders         │
                        │    • queue medication reminders          │
                        │    • backfill missing AI summaries       │
                        │    • drain email outbox (backoff)        │
                        │    • drain calendar outbox (backoff)     │
                        └──────────────────────────────────────────┘
                                   │                    │
                                   ▼                    ▼
                          SendGrid / SMTP        Google Calendar API
```

**Request path is synchronous and fast.** Everything slow or externally-dependent is a row in a
table that a worker picks up. The worker is an in-process asyncio task (no broker, no second dyno —
it fits the free tier), but every job is idempotent and DB-driven, so moving to a separate process
is a config change: set `WORKER_ENABLED=false` on the web dynos and run
`python -m app.workers.scheduler` elsewhere against the same database.

---

## The four hard problems

The brief calls out four. Here is the short version; the full write-up is in
**[docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md)**.

### 1. Double-booking prevention

```sql
CREATE UNIQUE INDEX uq_active_doctor_slot
    ON appointments (doctor_id, start_at)
 WHERE status IN ('held', 'confirmed');
```

The index is *partial*: cancelled and expired rows fall out of it, so a released slot is instantly
bookable again with no cleanup job. Two concurrent inserts cannot both succeed — the loser gets an
`IntegrityError`, which the router translates to `409 SLOT_TAKEN`. On PostgreSQL a
`SELECT … FOR UPDATE` on the doctor row turns a burst of rollbacks into an orderly queue, but
correctness never depends on it.

Verified by `test_concurrent_holds_produce_exactly_one_winner` — 12 threads released from a barrier
onto one slot. Result: exactly one `201`, eleven `409`s. The same test run against a live server with
16 concurrent clients gives the same answer.

### 2. Slot hold mechanism

`held` rows carry `hold_expires_at`. They occupy the unique index exactly like `confirmed` rows, so
a held slot is genuinely unavailable. Expiry is swept **twice**: lazily at the top of every
availability/booking request (so a stale hold never makes a slot *look* busy) and periodically by
the worker (so slots free themselves on an idle system). `confirm` re-reads the row inside the
transaction, so a hold that expired a millisecond ago cannot be confirmed.

### 3. Doctor leave conflict handling

Marking leave is **two-step**. `confirm: false` (the default) returns an impact report — every
affected appointment, patient name and email — and changes nothing. `confirm: true` applies it, and
the leave row, every cancellation, every apology email and every calendar deletion commit in **one
transaction**. Partial application is impossible.

### 4. Notification failure handling

Every email is a `notifications` row written *in the booking's transaction*, with a unique
`idempotency_key`. The worker drains it with backoff `1m → 2m → 4m → 8m → …` capped at 30 minutes.
After `NOTIFICATION_MAX_ATTEMPTS` a message is **dead-lettered** — it stops burning the queue but
stays visible in the admin portal with its error, and can be requeued with one click. Google
Calendar uses an identical `calendar_tasks` outbox.

---

## Project layout

```
healthcare_appointment_manager/
├── app/
│   ├── main.py              FastAPI factory, error handlers, SPA mount, lifespan
│   ├── config.py            all env config + production safety guard
│   ├── database.py          engine, session, dialect helpers, SQLite pragmas
│   ├── models.py            16 tables, incl. the partial unique index
│   ├── schemas.py           Pydantic request/response contracts (the API docs)
│   ├── security.py          bcrypt+SHA-256 hashing, JWT, role dependencies
│   ├── serializers.py       ORM → response models, with role-based redaction
│   ├── errors.py            domain exceptions → stable HTTP codes
│   ├── seed.py              demo clinic, booked through the real services
│   ├── routers/             auth · doctors · appointments · consultations
│   │                        admin · calendar · ops · meta
│   ├── services/
│   │   ├── slots.py         availability grid + race-safe hold/confirm/move
│   │   ├── llm.py           Gemini + retries + circuit breaker + fallbacks
│   │   ├── email.py         templates + outbox + SendGrid/SMTP/file transports
│   │   ├── gcal.py          OAuth 2.0 + Calendar REST + calendar outbox
│   │   └── notifications.py lifecycle orchestration (what gets sent when)
│   └── workers/scheduler.py background loop
├── web/                     3 portals — index.html, css/, js/ (ES modules, no build)
├── tests/                   85 tests: booking, leave, LLM, notifications, auth
├── docs/                    SYSTEM_DESIGN · API · DB_SCHEMA · LLM_PROMPTS
│                            GOOGLE_CALENDAR_SETUP · DEPLOYMENT
├── .env.example  requirements.txt  Dockerfile  render.yaml  Procfile
└── README.md
```

---

## Configuration

Full annotated list in **[.env.example](.env.example)**. The values that matter:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | SQLite file | `postgres://` URLs are rewritten to `postgresql+psycopg://` automatically |
| `JWT_SECRET` | dev key | **Required in production** — the app refuses to boot with the dev key when `ENVIRONMENT=production` |
| `CLINIC_TIMEZONE` | `Asia/Kolkata` | Any IANA zone. All timestamps are stored UTC and rendered in this zone |
| `SLOT_HOLD_MINUTES` | `5` | How long a slot is reserved during the symptom form |
| `MIN_LEAD_MINUTES` | `30` | Minimum notice for a booking |
| `BOOKING_HORIZON_DAYS` | `45` | How far ahead patients may book |
| `GEMINI_API_KEY` | — | Absent → rule-based fallback, `source: "fallback"` on every summary |
| `EMAIL_PROVIDER` | `auto` | `auto` picks SendGrid → SMTP → on-disk outbox |
| `GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI` | — | Absent → calendar tasks skipped, bookings unaffected |
| `WORKER_ENABLED` | `true` | Set `false` on web dynos when running a standalone worker |

### Graceful degradation, precisely

| Integration | Configured | Not configured |
|---|---|---|
| **LLM** | Gemini with structured JSON output, retries, circuit breaker | Deterministic keyword triage + template summary. `source: "fallback"`, reason stored in `error` |
| **Email** | SendGrid API or SMTP | `.eml` files in `OUTBOX_DIR`, marked `sent`, visible in admin → Operations |
| **Calendar** | Per-user OAuth 2.0, events created/updated/deleted | Tasks marked `cancelled` with a reason. No effect on booking |

---

## API overview

42 operations. Interactive reference at **`/docs`** — sign in via `POST /api/auth/login`, click
**Authorize**, paste the `access_token`. Full prose reference: **[docs/API.md](docs/API.md)**.

```
Authentication   POST   /api/auth/register              patient self-registration
                 POST   /api/auth/login                 any role
                 GET    /api/auth/me

Doctors          GET    /api/doctors                    search by specialisation / free text
                 GET    /api/doctors/specialisations
                 GET    /api/doctors/{id}/availability          slot grid for one day
                 GET    /api/doctors/{id}/availability-range    up to 14 days

Appointments     POST   /api/appointments/hold          phase 1 — reserve the slot
                 POST   /api/appointments/{id}/confirm  phase 2 — symptom form + AI + confirm
                 GET    /api/appointments               role-scoped list
                 POST   /api/appointments/{id}/reschedule
                 POST   /api/appointments/{id}/cancel
                 GET    /api/appointments/{id}/previsit-summary
                 POST   /api/appointments/{id}/previsit-summary/regenerate

Consultations    POST   /api/appointments/{id}/consultation      notes + prescription + AI summary
                 GET    /api/appointments/{id}/consultation
                 GET    /api/me/medication-reminders

Admin            POST   /api/admin/doctors              create login + profile + schedule
                 PATCH  /api/admin/doctors/{id}
                 POST   /api/admin/doctors/{id}/leave   dry-run, then apply
                 GET    /api/admin/stats
                 GET    /api/admin/notifications        email outbox
                 POST   /api/admin/notifications/{id}/requeue
                 GET    /api/admin/calendar-tasks
                 GET    /api/admin/audit
                 POST   /api/admin/worker/run-once

Calendar         GET    /api/calendar/connect           returns the Google consent URL
                 GET    /api/calendar/callback          OAuth 2.0 redirect target
                 POST   /api/calendar/disconnect

System           GET    /api/health                     liveness + integration status
                 GET    /api/config                     public bootstrap config
```

**Errors are machine-readable.** Every failure returns `{"detail": "...", "code": "..."}`:

| Code | HTTP | Meaning |
|---|---|---|
| `SLOT_TAKEN` | 409 | Another patient won the race |
| `PATIENT_BUSY` | 409 | The patient already has an overlapping appointment |
| `HOLD_EXPIRED` | 410 | The reservation timed out — pick the slot again |
| `SLOT_NOT_BOOKABLE` / `OFF_GRID` / `TOO_LATE` / `TOO_FAR` / `DOCTOR_ON_LEAVE` | 422 | Slot validation |
| `LEAVE_EXISTS`, `CONSULTATION_EXISTS`, `EMAIL_TAKEN` | 409 | Duplicate |
| `INTEGRATION_DISABLED` | 503 | Feature not configured on this deployment |

---

## Database schema

16 tables. Full column-by-column reference with rationale:
**[docs/DB_SCHEMA.md](docs/DB_SCHEMA.md)**.

```
users ──┬─< doctor_profiles ──┬─< doctor_working_hours
        │                     └─< doctor_leaves
        │
        └─< appointments >─────┬── symptom_reports        (1:1)
                               ├── previsit_summaries     (1:1, AI + provenance)
                               └── consultations ─┬── postvisit_summaries  (1:1, AI)
                                                  └── prescriptions ─< prescription_items
                                                                          └─< medication_reminders

  reliability:  notifications (email outbox) · calendar_tasks (calendar outbox)
                calendar_accounts (OAuth grants) · audit_logs
```

Conventions worth knowing:
- **All timestamps are UTC**, enforced by a `UTCDateTime` type decorator. SQLite returns naive
  datetimes and PostgreSQL returns aware ones; normalising at the type boundary means the same code
  behaves identically on both.
- **Enum-like columns are `String` + `CheckConstraint`**, not native enums — adding a value needs no
  migration, and the database still rejects garbage.
- **Deletes are soft** for doctors. Clinical history must be retained.

---

## LLM prompts

Both prompts, the JSON schemas, the failure ladder and the fallback rules:
**[docs/LLM_PROMPTS.md](docs/LLM_PROMPTS.md)**.

The brief's prompts are the starting point, hardened for production:

- **Structured output.** Gemini is called with `responseMimeType: application/json` plus an explicit
  `responseSchema`, so the model is *constrained* to the shape we need rather than asked for JSON.
- **Clinical guardrails.** The system prompt forbids diagnosis and prescribing; the post-visit
  prompt forbids adding, removing or altering any medication, dose or duration.
- **Never under-triage.** An unrecognised urgency value maps to `Medium`, never `Low`. Our own
  red-flag scan can *raise* the model's urgency but never lower it.
- **The prescription is the source of truth.** If the model's medication schedule does not match the
  prescribed rows, it is rebuilt from the database.
- **Failure ladder.** timeout/429/5xx → retry with exponential backoff + jitter (4xx is not retried) →
  circuit breaker after 4 consecutive failures → deterministic fallback. `generate_previsit_summary`
  and `generate_postvisit_summary` **never raise**.
- **Provenance is stored.** Every summary records `source`, `model`, `prompt_version`, `latency_ms`,
  `attempts` and `error`. The admin dashboard surfaces the fallback rate; the doctor's UI labels
  fallback briefs and offers a *Retry AI* button.

Negation handling deserves a mention: patients routinely write what they *don't* have.
`"No breathlessness"` must not raise the breathing red flag, while `"No fever, chest pain since
morning"` must still raise the chest-pain one. Both are tested.

---

## Google Calendar setup

Step-by-step with exact console screens: **[docs/GOOGLE_CALENDAR_SETUP.md](docs/GOOGLE_CALENDAR_SETUP.md)**.

The short version:

1. Google Cloud Console → new project → enable **Google Calendar API**.
2. **OAuth consent screen** → External → add your email as a *Test user*.
3. **Credentials → OAuth client ID → Web application**, with authorised redirect URI
   `http://localhost:8000/api/calendar/callback` (and your production URL).
4. Put the client id/secret/redirect into `.env`, restart, and connect from **Settings** in any portal.

We request only `calendar.events` — the narrowest scope that can create and delete our own events.
Each user connects their **own** calendar and gets their own copy of the event, so nothing depends on
Workspace-domain attendee permissions, and a patient disconnecting does not affect the doctor.

---

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest -q                       # 85 tests, ~2 minutes
pytest tests/test_booking.py -v # the concurrency ones
```

Tests run against a throwaway SQLite file with `GEMINI_API_KEY` deliberately empty, so the LLM
fallback path is exercised on every run.

| File | What it proves |
|---|---|
| `test_booking.py` | Slot grid maths, buffers, window overrun, **12-thread race → exactly one winner**, patient self-clash, hold expiry, idempotent confirm, reschedule/cancel releasing slots |
| `test_leave.py` | Dry run changes nothing, apply is transactional, leave days close slots, duplicate leave rejected, removing leave reopens slots without resurrecting bookings |
| `test_llm.py` | Never raises, transport failure still returns, triage levels, **negation handling**, vague input never `Low`, unknown urgency → `Medium`, model escalation, schedule rebuilt from prescription, circuit breaker, booking succeeds with the LLM down |
| `test_notifications.py` | Queued in-transaction, idempotency, **exponential backoff → dead-letter → requeue**, delivery failure never rolls back a booking, one reminder per dose, `SOS` gets none |
| `test_auth.py` | Hashing incl. bcrypt's 72-byte limit, no role escalation via registration, identical error for unknown-user/wrong-password, forged and expired tokens, RBAC on every admin route, cross-patient data isolation |

---

## Deployment

Full walkthrough: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**. Render is the quickest:

1. Push to GitHub (branch `main`, public).
2. Render → **New → Blueprint** → select the repo. `render.yaml` creates the web service *and* a free
   PostgreSQL instance, wires `DATABASE_URL`, and generates a strong `JWT_SECRET`.
3. Set `PUBLIC_BASE_URL` to your Render URL and `GOOGLE_REDIRECT_URI` to
   `<PUBLIC_BASE_URL>/api/calendar/callback`.
4. Optionally add `GEMINI_API_KEY`, `SENDGRID_API_KEY`, `GOOGLE_CLIENT_ID/SECRET`.

`Procfile` covers Railway/Heroku and `Dockerfile` covers anything else. `GET /api/health` is the
health-check path and reports which integrations are live.

**Production safety.** With `ENVIRONMENT=production` the app refuses to start if `JWT_SECRET` is
still the built-in development key, and logs warnings for `DEBUG=true`, SQLite, wildcard CORS, and
each unconfigured integration.

---

## Deliverables map

| Required | Where |
|---|---|
| Complete source code | this repository |
| README with setup guide | this file → [Quick start](#quick-start-2-minutes) |
| `.env.example` | [.env.example](.env.example) — every variable annotated |
| API docs | [docs/API.md](docs/API.md) + live OpenAPI at `/docs` |
| DB schema | [docs/DB_SCHEMA.md](docs/DB_SCHEMA.md) |
| LLM prompts | [docs/LLM_PROMPTS.md](docs/LLM_PROMPTS.md) |
| Google Calendar setup | [docs/GOOGLE_CALENDAR_SETUP.md](docs/GOOGLE_CALENDAR_SETUP.md) |
| Hosted URL | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — add your link at the top of this README |
| System design write-up (≤800 words) | [docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md) |

### Scope-of-work checklist

- [x] Admin creates and manages doctor profiles — specialisation, working hours, slot duration, leave days
- [x] Patient registers, logs in, searches by specialisation, books a slot
- [x] Double-booking prevented; simultaneous attempts handled safely (proven under 12-thread concurrency)
- [x] Doctor marked on leave → affected patients cancelled **and notified**, with a dry-run preview first
- [x] Symptom form before confirming → LLM pre-visit summary with urgency level for the doctor
- [x] Doctor files post-visit notes + prescription → LLM patient-friendly summary
- [x] Medication reminders derived from prescription frequency (`OD`/`BD`/`TDS`/`QID`/`QHS`/`SOS`)
- [x] Emails to patient and doctor: booking confirmation, reminder, cancellation (+ reschedule, welcome, post-visit)
- [x] Google Calendar event for both on booking; updated on reschedule, deleted on cancellation
- [x] Role-based auth — patient / doctor / admin
- [x] LLM outputs stored in the DB with full provenance
- [x] Background job for medication reminders and email retries
- [x] LLM failures handled gracefully — the system never breaks

---

## Licence

MIT.
