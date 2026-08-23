# Google Calendar Setup (OAuth 2.0)

Roughly 10 minutes, entirely on the free tier.

**You can skip this.** Without Google credentials the app runs normally — calendar tasks are marked
`cancelled` with the reason "Calendar integration disabled" in the admin Operations queue, and
booking, email and AI summaries are unaffected.

---

## How the integration works

Each **user** connects their **own** Google account, and we create a separate event on each
connected calendar — rather than creating one event and adding the other party as an attendee.

Two reasons:

1. Adding attendees requires the organiser's calendar to be permitted to invite them, which is a
   Google Workspace domain concern we cannot assume for a clinic whose patients are on personal
   Gmail accounts.
2. If a patient later disconnects, only *their* copy stops syncing. The doctor's calendar is
   untouched.

We request exactly one scope — `https://www.googleapis.com/auth/calendar.events` — the narrowest
that can create and delete our own events. We never ask to *read* the user's calendar.

---

## Step 1 — Create a project

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Project dropdown (top bar) → **New Project**.
3. Name it `clinix` → **Create**, then make sure it is selected.

## Step 2 — Enable the Calendar API

1. **APIs & Services → Library**.
2. Search **Google Calendar API** → open it → **Enable**.

## Step 3 — Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. User type **External** → **Create**.
3. Fill in:
   - App name: `Clinix`
   - User support email: your email
   - Developer contact email: your email
4. **Save and Continue**.
5. **Scopes** → *Add or remove scopes* → filter for `calendar.events` → tick
   `https://www.googleapis.com/auth/calendar.events` → **Update** → **Save and Continue**.
6. **Test users** → **+ Add users** → add every Google account you will demo with (your own, and any
   reviewer's). → **Save and Continue**.

> **This step is the one people miss.** While the app is in *Testing*, only listed test users can
> complete the flow. Anyone else gets `403: access_denied`. You do **not** need to publish or submit
> for verification for an assignment demo — just add the accounts as test users.

## Step 4 — Create the OAuth client

1. **APIs & Services → Credentials → + Create Credentials → OAuth client ID**.
2. Application type: **Web application**. Name: `Clinix web`.
3. Under **Authorised redirect URIs**, add every origin you will run from:

   ```
   http://localhost:8000/api/calendar/callback
   https://your-app.onrender.com/api/calendar/callback
   ```

   The path must be **exactly** `/api/calendar/callback`, and the URI must match
   `GOOGLE_REDIRECT_URI` character for character — including `http` vs `https` and any trailing
   segment. This is the most common cause of `redirect_uri_mismatch`.
4. **Create**, then copy the **Client ID** and **Client secret**.

## Step 5 — Configure the app

In `.env`:

```bash
GOOGLE_CLIENT_ID=1234567890-abcdefg.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxx
GOOGLE_REDIRECT_URI=http://localhost:8000/api/calendar/callback
PUBLIC_BASE_URL=http://localhost:8000
```

Restart the server. `GET /api/health` should now report:

```json
{ "name": "google_calendar", "configured": true, "mode": "oauth2" }
```

## Step 6 — Connect and verify

1. Sign in to any portal → **Settings** → **Connect Google Calendar**.
2. Approve the Google consent screen. You are redirected back with a success banner.
3. Book an appointment.
4. Admin → **Operations → Calendar queue**: the task shows `sent` with the Google event id.
5. Check your Google Calendar — the appointment is there, with 24-hour email and 1-hour popup
   reminders.

---

## What happens on each action

| Action | Calendar effect |
|---|---|
| Booking confirmed | `create` queued for **both** patient and doctor |
| Rescheduled | `update` on both events — a fresh idempotency key per reschedule, so every move syncs |
| Cancelled | `delete` on both events |
| Doctor marked on leave | `delete` on every affected appointment's events |
| User not connected | Task marked `cancelled` with a reason. Not an error |

Event ids are stored on the appointment (`patient_calendar_event_id`, `doctor_calendar_event_id`).
If an event was deleted directly in Google, an `update` recreates it rather than failing.

---

## Reliability

Calendar writes never happen inside a request. They are rows in `calendar_tasks`, committed with the
booking and executed by the background worker with exponential backoff (1, 2, 4, 8 minutes, capped
at 30) up to `NOTIFICATION_MAX_ATTEMPTS`.

Token handling:
- `access_type=offline` + `prompt=consent` guarantees a refresh token.
- Access tokens are refreshed automatically 60 seconds before expiry.
- An `invalid_grant` (the user revoked access or changed their password) marks the account revoked
  and dead-letters the task — retrying cannot help until they reconnect.
- **Disconnect** revokes the token with Google and clears it locally. Events already on the calendar
  are left alone.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `redirect_uri_mismatch` | The URI in the console differs from `GOOGLE_REDIRECT_URI` | Make them byte-identical, including scheme and trailing path. Changes take a minute to propagate |
| `403: access_denied` | Your Google account is not a **Test user** | Add it under OAuth consent screen → Test users |
| Connect button returns `503 INTEGRATION_DISABLED` | One of the three env vars is missing | Set all of `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` and restart |
| Callback says `invalid_state` | The signed state token expired (15 min) or `JWT_SECRET` changed mid-flow | Start the connect flow again |
| Tasks stuck `pending` | The worker is not running | Check `WORKER_ENABLED=true`, or force a pass with `POST /api/admin/worker/run-once` |
| Tasks show `cancelled` | That user has not connected their calendar | Expected. Connect from Settings |
| Task `dead` with "Google access was revoked" | The user revoked the grant in their Google account | They reconnect from Settings |

Useful checks:

```bash
curl -s localhost:8000/api/health | jq '.integrations[] | select(.name=="google_calendar")'
curl -s localhost:8000/api/calendar/status -H "Authorization: Bearer $TOKEN"
curl -s localhost:8000/api/admin/calendar-tasks -H "Authorization: Bearer $ADMIN_TOKEN"
```
