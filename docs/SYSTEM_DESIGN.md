# System Design Write-up

*Covering double-booking prevention, doctor leave conflict handling, the slot hold mechanism, and
notification failure handling.*

## The organising principle

Correctness belongs in the database; latency and third parties belong outside the request. Every
decision below follows from those two rules.

## Slot hold mechanism

The brief requires a symptom form and an LLM call *between* choosing a time and owning it. That gap
is a race: two patients can both be typing into a form for the same 10:30 slot and only discover the
clash on submit. So booking is two-phase.

`POST /appointments/hold` writes an appointment with `status='held'` and
`hold_expires_at = now + 5 min`. `POST /appointments/{id}/confirm` submits the form and promotes it
to `confirmed`. Held rows occupy the uniqueness index exactly like confirmed ones, and the availability API reports
them as `held` rather than merely absent, so the UI can explain itself.

Expiry is swept twice: lazily at the top of every availability and booking request, so a stale hold
never makes a slot *look* busy, and periodically by the worker, so slots free themselves on an idle
system. `confirm` re-reads the row inside its transaction, so a hold that lapsed a millisecond
earlier cannot slip through.

Ordering inside `confirm` matters too: the appointment is committed **before** the LLM is called, so
a slow or dead model can never cost a patient the slot they reserved. The summary and emails are
written in a second transaction; if the process dies between the two, a worker backfills them.

## Double-booking prevention

Application-level "check then insert" is a race by construction, so the guarantee is a database
constraint:

```sql
CREATE UNIQUE INDEX uq_active_doctor_slot
    ON appointments (doctor_id, start_at)
 WHERE status IN ('held', 'confirmed');
```

Two concurrent transactions inserting the same slot cannot both commit, however they interleave,
across any number of processes. The loser receives an `IntegrityError`, which the router
translates into `409 SLOT_TAKEN`. The `SELECT` before the insert only produces a friendlier
message in the uncontended case; it is never the guarantee.

Making the index **partial** does real work: cancelled and expired rows drop out of it, so a released
slot is bookable again immediately, with no cleanup job.

On PostgreSQL, `SELECT … FOR UPDATE` on the doctor row serialises same-doctor attempts, converting a
burst of rollbacks into an orderly queue. It is skipped on SQLite, which has no row locking —
acceptable precisely because correctness never depended on it. A separate overlap check stops a
patient double-booking *themselves* across two doctors, whose slot grids may differ.

This is verified, not asserted: twelve threads released from a barrier onto one slot yield exactly
one `201` and eleven `409`s.

## Doctor leave conflict handling

Cancelling someone's medical appointment should not be optimistic, so it is two-step.
`confirm: false` — the default — returns an impact report naming every affected appointment, patient
and email, and changes nothing. The admin sees the blast radius first.

`confirm: true` applies it, and the leave row, every cancellation, every apology email and every
calendar deletion commit in a **single transaction**. There is no state where the doctor is on leave
but three patients still believe they have appointments. Affected patients get a distinct,
apologetic template naming the doctor and date with a rebooking link; the doctor gets the plain
cancellation notice.

Removing a leave reopens the slots but deliberately does **not** resurrect cancelled bookings: those
patients have already been told, and their slot may since have gone to someone else.

## Notification failure handling

The failure to design against is not "SendGrid returned 500" — it is *"the booking committed but the
email never went"*, and its twin, *"the email went and the transaction rolled back"*.

So nothing is ever sent inline. Each message is a `notifications` row committed **in the same
transaction as the business change**. Either both land or neither does. A unique `idempotency_key`
makes a retried request a no-op rather than a duplicate email.

A background worker drains the table with exponential backoff — 1, 2, 4, 8 minutes, capped at 30.
After five attempts a message is **dead-lettered**: it stops consuming the queue but remains visible
in the admin portal with its error, requeueable in one click. Google Calendar writes use an
identical `calendar_tasks` outbox, so an API outage delays an invitation rather than failing a
booking.

The same philosophy covers the LLM. Timeouts, 429s and 5xxs are retried with jitter; 4xxs are not,
because they will not fix themselves; four consecutive failures open a circuit breaker so a dead API
costs one timeout rather than one per booking. Beneath that sits a deterministic keyword triage that
deliberately errs upward — unknown urgency becomes `Medium`, never `Low`. Every summary stores its
`source`, so the degradation is auditable rather than invisible.
