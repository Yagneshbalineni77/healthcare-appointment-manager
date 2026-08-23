# Deployment

The app is a single process serving both the API and the frontend, so one web service is all it
needs. `GET /api/health` is the health-check endpoint.

---

## Option A — Render (recommended, free)

`render.yaml` is a Blueprint that creates the web service *and* a free PostgreSQL database, wires
`DATABASE_URL`, and generates a strong `JWT_SECRET`.

1. Push to GitHub — branch **`main`**, repository **public**.
2. [Render Dashboard](https://dashboard.render.com/) → **New → Blueprint** → select the repo →
   **Apply**.
3. Render prompts for the `sync: false` variables. At minimum set:

   | Variable | Value |
   |---|---|
   | `PUBLIC_BASE_URL` | `https://<your-service>.onrender.com` |
   | `GOOGLE_REDIRECT_URI` | `https://<your-service>.onrender.com/api/calendar/callback` |
   | `ADMIN_EMAIL` | your email |
   | `ADMIN_PASSWORD` | a strong password |

   Optional: `GEMINI_API_KEY`, `SENDGRID_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.

4. First deploy takes ~3 minutes. Then open the URL — the schema is created and the demo clinic
   seeded automatically.

> You do not know your Render URL until the service exists. Deploy once, copy the URL, set
> `PUBLIC_BASE_URL` and `GOOGLE_REDIRECT_URI`, and redeploy. Everything works before that except
> absolute links in emails and the Google redirect.

**Free-tier caveat:** the instance sleeps after 15 minutes idle, so the first request afterwards
takes ~30 seconds and the background worker is not running while asleep. Reminders catch up on the
next wake because they are queued in the database rather than held in memory. For a demo this is
fine; if you want it always-on, use a paid instance or ping `/api/health` on a schedule.

---

## Option B — Railway

1. [Railway](https://railway.app/) → **New Project → Deploy from GitHub repo**.
2. **+ New → Database → PostgreSQL**. Railway injects `DATABASE_URL` automatically.
3. Variables → set `JWT_SECRET` (`python -c "import secrets;print(secrets.token_hex(32))"`),
   `ENVIRONMENT=production`, `PUBLIC_BASE_URL`, plus any optional keys.
4. Railway uses the `Procfile`. Generate a domain under **Settings → Networking**.

---

## Option C — Docker (anywhere)

```bash
docker build -t clinix .
docker run -p 8000:8000 --env-file .env clinix
```

The image is Python 3.12-slim, runs as a non-root user, and has a built-in `HEALTHCHECK`.
It works unchanged on Fly.io, Google Cloud Run, Azure Container Apps and any Kubernetes cluster.

For Cloud Run, remember the container must listen on `$PORT` — the `CMD` already does.

---

## Option D — A plain VPS

```bash
git clone <repo> && cd healthcare_appointment_manager
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && $EDITOR .env

uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

With multiple workers, run the background worker **once**, separately:

```bash
# web dynos
WORKER_ENABLED=false uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

# one worker process
python -m app.workers.scheduler
```

Every job is idempotent and DB-driven, so this split needs no code change. Even if two workers did
run, unique idempotency keys stop duplicate emails.

Put nginx or Caddy in front for TLS.

---

## Production checklist

- [ ] `ENVIRONMENT=production` — the app then **refuses to boot** with the built-in dev `JWT_SECRET`
- [ ] `JWT_SECRET` set to a fresh random value (`python -c "import secrets;print(secrets.token_hex(32))"`)
- [ ] `DEBUG=false`
- [ ] `DATABASE_URL` pointing at PostgreSQL, not SQLite
- [ ] `ADMIN_PASSWORD` changed from the default
- [ ] `PUBLIC_BASE_URL` set to the real origin (used in every email link)
- [ ] `CORS_ORIGINS` narrowed from `*` to your frontend origin
- [ ] `CLINIC_TIMEZONE` correct for the clinic
- [ ] `SEED_DEMO_DATA=false` for a real clinic (leave `true` for an assignment demo)
- [ ] `GOOGLE_REDIRECT_URI` added to the Google Console's authorised redirect URIs

On boot with `ENVIRONMENT=production`, the app logs a warning for each of: `DEBUG=true`, SQLite in
use, wildcard CORS, and every unconfigured integration — so the logs tell you what is not set up.

### Why SQLite is fine for the demo but not for production

The partial unique index works identically on both, so **double-booking is prevented either way**.
What SQLite lacks is `SELECT … FOR UPDATE` (so contention resolves through rollback-and-retry rather
than queueing) and safe multi-process writes over a network filesystem. On one instance with a
clinic's traffic it is genuinely fine; across several it is not.

---

## Verifying a deployment

```bash
BASE=https://your-app.onrender.com

curl -s $BASE/api/health | jq            # status, database, which integrations are live
curl -s $BASE/api/config | jq            # public bootstrap config

curl -s -X POST $BASE/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"admin@clinix.health","password":"Admin@12345"}' | jq -r .access_token
```

Then in the browser: sign in as a patient, book a slot, and check
**Admin → Operations** that the confirmation emails were queued and delivered.

---

## After deploying, update the README

Put the live URL at the top of [`README.md`](../README.md) so a reviewer finds it immediately:

```markdown
| **Live demo** | https://your-app.onrender.com |
```
