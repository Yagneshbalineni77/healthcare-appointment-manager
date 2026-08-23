# LLM Prompts & Failure Handling

Provider: **Google Gemini** (`gemini-2.5-flash` by default), called over plain HTTPS with `httpx` —
no vendor SDK. All of it lives in **[`app/services/llm.py`](../app/services/llm.py)**.

**The contract:** `generate_previsit_summary()` and `generate_postvisit_summary()` **never raise and
never block indefinitely.** They always return a fully-populated result. `result.source` says where
it came from — `"llm"` or `"fallback"`.

---

## Contents
- [What the brief asked for, and what we shipped](#what-the-brief-asked-for-and-what-we-shipped)
- [Pre-visit summary](#pre-visit-summary)
- [Post-visit summary](#post-visit-summary)
- [Structured output](#structured-output)
- [Validation and coercion](#validation-and-coercion)
- [Failure handling](#failure-handling)
- [The deterministic fallbacks](#the-deterministic-fallbacks)
- [Provenance](#provenance)

---

## What the brief asked for, and what we shipped

The brief supplies two prompts. Used verbatim they work in a demo and fail in production: free-text
output needs parsing, the model will happily invent a diagnosis, and a single timeout takes the
booking down with it. Each is kept as the **user prompt** and paired with a system instruction,
a response schema, and a validation layer.

| Brief | Added |
|---|---|
| *"Analyse these symptoms and return: urgency level (Low/Medium/High), chief complaint, and three suggested questions"* | System prompt with explicit triage definitions and red-flag examples · JSON response schema · guaranteed exactly 3 questions · red-flag list · patient context (age/sex, duration, severity, conditions, medications, allergies) |
| *"Convert these clinical notes into a patient-friendly summary with medication schedule and follow-up steps"* | System prompt forbidding any change to medication/dose/duration · JSON response schema · structured medication table · warning signs · schedule rebuilt from the DB if the model's does not match |

---

## Pre-visit summary

### System instruction

```
You are a clinical intake assistant supporting a licensed doctor in an outpatient clinic.
You read a patient's self-reported symptom form and produce a concise triage brief the doctor reads
in the 30 seconds before the consultation.

Rules you must follow:
- You do NOT diagnose. You do NOT name conditions as conclusions, and you never suggest medication.
- You summarise what the patient reported, flag anything time-critical, and propose questions that
  help the doctor narrow things down.
- Urgency is about how soon a clinician should see this person, not how serious it might eventually be:
  - "High"   — red-flag features suggesting a possible emergency (e.g. chest pain with breathlessness
               or sweating, one-sided weakness or facial droop, slurred speech, severe difficulty
               breathing, uncontrolled bleeding, fainting, seizure, sudden worst-ever headache,
               stiff neck with high fever, suicidal thoughts, severe dehydration in the very young
               or very old).
  - "Medium" — persistent, worsening, or functionally limiting symptoms that need prompt review but
               show no red flags.
  - "Low"    — mild, stable, self-limiting, or routine follow-up.
- If the report is vague or too short to judge, choose "Medium" and say so in the summary note.
  Never guess "Low" from missing information.
- Write in plain clinical English. No markdown, no preamble.
```

### User prompt

The brief's sentence, with the structured intake data appended:

```
Analyse these symptoms and return: urgency level (Low / Medium / High), chief complaint, and three
suggested questions for the doctor.

PATIENT CONTEXT
- Age/Sex: {age_sex}
- Symptom duration: {duration}
- Self-rated severity (1-10): {severity}
- Known conditions: {conditions}
- Current medications: {medications}
- Allergies: {allergies}

SYMPTOMS
{symptoms}

Return:
- urgency: one of Low, Medium, High
- chief_complaint: one sentence, under 25 words, in clinical shorthand
- suggested_questions: exactly 3 specific questions the doctor should ask
- red_flags: any time-critical features you noticed (empty list if none)
- summary_note: 2-3 sentences of context for the doctor
```

### Real output

Live call, `gemini-2.5-flash`, 4.2 s:

> **Input:** *"Tightness in my chest for the last two days, worse when I climb stairs. It comes with
> shortness of breath and I broke into a cold sweat this morning."* — 35y Male, severity 8/10,
> known hypertension on Telmisartan 40 mg.

```json
{
  "urgency": "High",
  "chief_complaint": "35M reports 2-day history of exertional chest tightness with dyspnea and diaphoresis.",
  "red_flags": ["Chest pain with breathlessness", "Cold sweats", "Exertion-related worsening of symptoms"],
  "suggested_questions": [
    "Can you describe the exact sensation of the chest tightness and its precise location?",
    "Does the tightness radiate to your arm, jaw, or back?",
    "Have you experienced any nausea, dizziness, or palpitations with these symptoms?"
  ],
  "summary_note": "35-year-old male with known hypertension presents with 2 days of chest tightness, dyspnea, and cold sweats, worsened by exertion. Symptoms are rated 8/10 in severity. These are red-flag features for acute cardiac events."
}
```

A low-acuity case (*"Mild sore throat for 2 days, no fever, eating and drinking normally"*) returns
`"Low"` — the prompt discriminates rather than defaulting high.

---

## Post-visit summary

### System instruction

```
You rewrite a doctor's clinical notes into a summary the patient can actually understand and act on.

Rules you must follow:
- Reading level: a worried adult with no medical training. Short sentences. Expand every
  abbreviation (BD -> twice a day, PO -> by mouth, HTN -> high blood pressure).
- Never add, remove, or change a medication, a dose, or a duration. Copy them exactly as the doctor
  wrote them. If the notes are silent on something, stay silent.
- Never introduce a diagnosis, test result, or instruction that is not in the notes.
- Be warm and calm, not alarming. Do not tell the patient to "not worry" — tell them what to do.
- warning_signs must be concrete, observable things that mean "come back / go to emergency".
- No markdown, no preamble.
```

### User prompt

```
Convert these clinical notes into a patient-friendly summary with medication schedule and follow-up steps.

DIAGNOSIS (as recorded): {diagnosis}
FOLLOW-UP DATE: {follow_up}

CLINICAL NOTES
{notes}

PRESCRIPTION AS WRITTEN (do not alter)
{prescription}

Return:
- patient_summary: 3-5 short sentences explaining what was found and what happens next
- medication_schedule: one entry per prescribed medicine, with plain-language timing
- follow_up_steps: concrete actions the patient should take
- warning_signs: symptoms that mean they should seek care sooner
```

### Real output

> **Input:** *"O/E: BP 148/92. Chest clear. ECG: NSR, no ST changes. Trop-I negative x2. Imp: stable
> angina, r/o ACS neg. Advised statin, continue ARB, cardiac stress test in 2 wks. Lifestyle:
> low-salt diet, 30min walk daily."*

> **patient_summary:** "You have stable angina, which means you have chest pain or discomfort when
> your heart muscle does not get enough blood. We checked your heart and it is stable. Your blood
> pressure was a bit high. We will do a heart stress test soon to learn more about your heart. You
> will also start new medicine and make some lifestyle changes."

| Medicine | Dose | When | Duration |
|---|---|---|---|
| Atorvastatin | 20 mg | once at bedtime | 30 days |
| Telmisartan | 40 mg | once a day (morning, after food) | 30 days |

> **follow_up_steps:** cardiac stress test in two weeks · start a low-salt diet · walk 30 minutes
> daily · follow-up appointment on 6 September 2026
> **warning_signs:** new or worsening chest pain · shortness of breath · dizziness or
> light-headedness · pain spreading to arm, back, neck, jaw or stomach

Note what it did *not* do: no invented dose, no reworded strength, no new diagnosis.

---

## Structured output

The model is **constrained** to the shape we need rather than asked politely for JSON:

```python
"generationConfig": {
    "temperature": 0.2,
    "responseMimeType": "application/json",
    "responseSchema": {
        "type": "OBJECT",
        "properties": {
            "urgency": {"type": "STRING", "enum": ["Low", "Medium", "High"]},
            "chief_complaint": {"type": "STRING"},
            "suggested_questions": {"type": "ARRAY", "items": {"type": "STRING"}},
            "red_flags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "summary_note": {"type": "STRING"},
        },
        "required": ["urgency", "chief_complaint", "suggested_questions", "red_flags", "summary_note"],
    },
}
```

Safety thresholds are set to `BLOCK_ONLY_HIGH`. `DANGEROUS_CONTENT` in particular fires on ordinary
symptom descriptions, and a blocked response on a legitimate clinical form is a failure, not a save.
If a response *is* blocked, `finishReason` is detected and the fallback runs.

---

## Validation and coercion

Structured output is not blind trust. Everything is re-validated:

| Rule | Why |
|---|---|
| Unknown urgency → `Medium`, never `Low` | Under-triage is the dangerous direction of error. `"urgent"`, `"EMERGENCY"`, `"banana"`, `null` all map safely |
| Our red-flag scan can **raise** the model's urgency, never lower it | Defence in depth. If the model says `Low` on *"crushing chest pain radiating to my left arm"*, it is escalated to `High` |
| Fewer than 3 questions → topped up from a default set | The brief asks for three |
| Medication schedule length ≠ prescribed items → rebuilt from the DB | **The prescription is the source of truth, not the model.** A dropped or invented medicine is corrected, not published |
| Empty `patient_summary` despite valid JSON → fallback | Valid but useless is still a failure |
| All strings length-capped | A runaway generation cannot bloat the row |

---

## Failure handling

Five layers, outermost first:

```
1. Structured decoding   responseSchema constrains the shape
2. Validation            coercion + safety net (above)
3. Retries               timeout / 429 / 5xx → exponential backoff + jitter (LLM_MAX_ATTEMPTS)
                         4xx other than 429 → NOT retried; it will not fix itself
4. Circuit breaker       LLM_BREAKER_THRESHOLD consecutive failures → stop calling for
                         LLM_BREAKER_COOLDOWN_SECONDS. A dead API costs ONE timeout, not one per booking
5. Fallback              deterministic rule-based summary, stored with source="fallback"
```

Crucially, **the appointment is committed before the model is called**. The worst case for a total
LLM outage is a confirmed booking whose triage brief came from the rule engine — never a lost slot,
never a 500.

Live circuit-breaker state: `GET /api/llm/status`.

---

## The deterministic fallbacks

### Pre-visit: keyword triage that errs upward

A curated red-flag lexicon (cardiac, respiratory, stroke/FAST, haemorrhage, neurological,
anaphylaxis, safeguarding, …), then severity and duration heuristics, then a floor of `Medium` for
anything too vague to judge. `Low` is only returned for text that is clearly mild, stable and
detailed enough to justify it.

**Negation handling.** Patients routinely write what they *don't* have. A triage tool that reads
`"no breathlessness"` as a breathing red flag is worse than useless:

| Input | Red flag? | Why |
|---|---|---|
| `"Dry cough for five days. No breathlessness."` | no | negated |
| `"Headache since Monday. Denies chest pain and no fever."` | no | negated |
| `"No fever, chest pain since morning"` | **yes** | the negation does not survive the comma — different clause |
| `"Patient denies chest pain but has severe abdominal pain"` | **yes** (abdominal) | `but` ends the negated clause |

A negator (`no`, `not`, `n't`, `without`, `denies`, `never`, `free of`, `negative for`, `ruled out`)
counts only inside a 26-character window that stops at the nearest clause boundary. All four cases
above are covered by tests.

### Post-visit: built from the prescription rows

The medication table is generated from `prescription_items`, so doses and durations are exactly what
the doctor wrote — the fallback literally cannot hallucinate a medicine. Frequency codes expand to
plain English with concrete times (`TDS` → *"three times a day (around 8:00 AM, 2:00 PM, 8:00 PM)"*),
matching the reminder schedule the patient will actually receive. The doctor's verbatim notes are
appended so nothing is hidden behind a degraded summary.

---

## Provenance

Every stored summary records how it was produced:

| Column | Example |
|---|---|
| `source` | `llm` \| `fallback` |
| `model` | `gemini-2.5-flash` |
| `prompt_version` | `v1.2` |
| `latency_ms` | `4156` |
| `attempts` | `1` |
| `error` | `null`, or e.g. `HTTP 429: quota exceeded` |
| `raw_response` | first 8 KB of the model's reply, for debugging |

Surfaced throughout the product, not buried in a table:

- **Doctor's UI** — an ✨ *AI generated* or ⚙ *Rule-based fallback* chip on every brief, with a
  **Retry AI** button on fallback ones (`POST /api/appointments/{id}/previsit-summary/regenerate`).
- **Patient's UI** — the same chip on the visit summary.
- **Admin dashboard** — a clinic-wide **AI fallback rate**, which turns amber above 50%.

A clinician can always tell whether a machine or a rule wrote what they are reading.
