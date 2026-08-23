"""LLM integration (Google Gemini) with hard guarantees around failure.

Contract with the rest of the app
---------------------------------
``generate_previsit_summary`` and ``generate_postvisit_summary`` **never raise
and never block indefinitely.** They always return a fully-populated result
object. ``result.source`` says where it came from:

* ``llm``      — the model answered and the answer passed schema validation
* ``fallback`` — the model was unavailable/invalid, so a deterministic
  rule-based summary was produced instead

Layers of defence, outermost first:

1. **Structured decoding** — Gemini is called with ``responseMimeType:
   application/json`` plus an explicit ``responseSchema``, so the model is
   constrained to the shape we need rather than asked politely for JSON.
2. **Validation** — the decoded payload is still re-validated and coerced in
   :func:`_coerce_previsit` / :func:`_coerce_postvisit`. A model that returns
   ``urgency: "urgent"`` is mapped to ``High``, not stored raw.
3. **Retries** — transient failures (timeouts, 429, 5xx) are retried with
   exponential backoff + jitter. 4xx other than 429 is not retried: it will
   never succeed.
4. **Circuit breaker** — after ``LLM_BREAKER_THRESHOLD`` consecutive failures
   the client stops calling out for ``LLM_BREAKER_COOLDOWN_SECONDS``. A dead
   API therefore costs one timeout, not one timeout per booking.
5. **Fallback** — clinically-conservative keyword triage for pre-visit, and a
   template built from the structured prescription rows for post-visit.

Clinical safety: the prompts explicitly forbid diagnosis and prescribing. The
output is a *triage and communication aid* for a licensed clinician, and the
UI labels it as AI-generated.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.models import FREQUENCY_TIMES, Urgency

logger = logging.getLogger("clinix.llm")

PROMPT_VERSION = "v1.2"
_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


# ==========================================================================
# Results
# ==========================================================================
@dataclass
class PreVisitResult:
    urgency: str
    chief_complaint: str
    suggested_questions: list[str]
    red_flags: list[str] = field(default_factory=list)
    summary_note: str = ""
    source: str = "llm"
    model: str | None = None
    latency_ms: int = 0
    attempts: int = 1
    error: str | None = None
    raw_response: str | None = None


@dataclass
class PostVisitResult:
    patient_summary: str
    medication_schedule: list[dict]
    follow_up_steps: list[str]
    warning_signs: list[str] = field(default_factory=list)
    source: str = "llm"
    model: str | None = None
    latency_ms: int = 0
    attempts: int = 1
    error: str | None = None
    raw_response: str | None = None


# ==========================================================================
# Circuit breaker
# ==========================================================================
class _CircuitBreaker:
    def __init__(self, threshold: int, cooldown: int) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._open_until = time.monotonic() + self.cooldown
                logger.warning(
                    "LLM circuit breaker OPEN for %ss after %s consecutive failures",
                    self.cooldown,
                    self._failures,
                )

    def snapshot(self) -> dict:
        with self._lock:
            remaining = max(0.0, self._open_until - time.monotonic())
        return {"open": remaining > 0, "consecutive_failures": self._failures, "reopens_in_s": round(remaining, 1)}


breaker = _CircuitBreaker(settings.llm_breaker_threshold, settings.llm_breaker_cooldown_seconds)


# ==========================================================================
# Prompts
# ==========================================================================
PREVISIT_SYSTEM = """You are a clinical intake assistant supporting a licensed doctor in an outpatient clinic.
You read a patient's self-reported symptom form and produce a concise triage brief the doctor reads in the 30 seconds before the consultation.

Rules you must follow:
- You do NOT diagnose. You do NOT name conditions as conclusions, and you never suggest medication.
- You summarise what the patient reported, flag anything time-critical, and propose questions that help the doctor narrow things down.
- Urgency is about how soon a clinician should see this person, not how serious it might eventually be:
  - "High"   — red-flag features suggesting a possible emergency (e.g. chest pain with breathlessness or sweating, one-sided weakness or facial droop, slurred speech, severe difficulty breathing, uncontrolled bleeding, fainting, seizure, sudden worst-ever headache, stiff neck with high fever, suicidal thoughts, severe dehydration in the very young or very old).
  - "Medium" — persistent, worsening, or functionally limiting symptoms that need prompt review but show no red flags.
  - "Low"    — mild, stable, self-limiting, or routine follow-up.
- If the report is vague or too short to judge, choose "Medium" and say so in the summary note. Never guess "Low" from missing information.
- Write in plain clinical English. No markdown, no preamble."""

PREVISIT_USER_TEMPLATE = """Analyse these symptoms and return: urgency level (Low / Medium / High), chief complaint, and three suggested questions for the doctor.

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
- summary_note: 2-3 sentences of context for the doctor"""

POSTVISIT_SYSTEM = """You rewrite a doctor's clinical notes into a summary the patient can actually understand and act on.

Rules you must follow:
- Reading level: a worried adult with no medical training. Short sentences. Expand every abbreviation (BD -> twice a day, PO -> by mouth, HTN -> high blood pressure).
- Never add, remove, or change a medication, a dose, or a duration. Copy them exactly as the doctor wrote them. If the notes are silent on something, stay silent.
- Never introduce a diagnosis, test result, or instruction that is not in the notes.
- Be warm and calm, not alarming. Do not tell the patient to "not worry" — tell them what to do.
- warning_signs must be concrete, observable things that mean "come back / go to emergency".
- No markdown, no preamble."""

POSTVISIT_USER_TEMPLATE = """Convert these clinical notes into a patient-friendly summary with medication schedule and follow-up steps.

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
- warning_signs: symptoms that mean they should seek care sooner"""


_PREVISIT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "urgency": {"type": "STRING", "enum": ["Low", "Medium", "High"]},
        "chief_complaint": {"type": "STRING"},
        "suggested_questions": {"type": "ARRAY", "items": {"type": "STRING"}},
        "red_flags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "summary_note": {"type": "STRING"},
    },
    "required": ["urgency", "chief_complaint", "suggested_questions", "red_flags", "summary_note"],
    "propertyOrdering": ["urgency", "chief_complaint", "suggested_questions", "red_flags", "summary_note"],
}

_POSTVISIT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "patient_summary": {"type": "STRING"},
        "medication_schedule": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "medicine": {"type": "STRING"},
                    "dose": {"type": "STRING"},
                    "when": {"type": "STRING"},
                    "duration": {"type": "STRING"},
                    "notes": {"type": "STRING"},
                },
                "required": ["medicine", "dose", "when", "duration"],
                "propertyOrdering": ["medicine", "dose", "when", "duration", "notes"],
            },
        },
        "follow_up_steps": {"type": "ARRAY", "items": {"type": "STRING"}},
        "warning_signs": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["patient_summary", "medication_schedule", "follow_up_steps", "warning_signs"],
    "propertyOrdering": ["patient_summary", "medication_schedule", "follow_up_steps", "warning_signs"],
}


# ==========================================================================
# Transport
# ==========================================================================
class LLMUnavailable(RuntimeError):
    """Raised internally; callers only ever see a fallback result."""


def _call_gemini(system: str, user: str, schema: dict) -> tuple[dict, str, int]:
    """One structured Gemini call. Returns ``(payload, raw_text, attempts)``."""
    if not settings.llm_enabled:
        raise LLMUnavailable("GEMINI_API_KEY is not configured")
    if breaker.is_open:
        raise LLMUnavailable("LLM circuit breaker is open")

    url = f"{_API_ROOT}/{settings.gemini_model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
        # Medical text trips over-eager safety filters; DANGEROUS_CONTENT in
        # particular fires on symptom descriptions. Keep the ceiling high so a
        # legitimate clinical form is not silently blocked.
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
            for c in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ],
    }

    last_error: Exception | None = None
    attempts = 0

    for attempt in range(1, settings.llm_max_attempts + 1):
        attempts = attempt
        try:
            with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
                response = client.post(
                    url,
                    params={"key": settings.gemini_api_key},
                    json=body,
                    headers={"Content-Type": "application/json"},
                )

            if response.status_code == 200:
                text = _extract_text(response.json())
                payload = _loads_lenient(text)
                breaker.record_success()
                return payload, text, attempts

            retryable = response.status_code == 429 or response.status_code >= 500
            last_error = LLMUnavailable(f"HTTP {response.status_code}: {response.text[:300]}")
            if not retryable:
                break  # 400/401/403 will not fix themselves

        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            last_error = exc  # malformed body — worth one more shot

        if attempt < settings.llm_max_attempts:
            backoff = (2 ** (attempt - 1)) * 0.75
            time.sleep(backoff + random.uniform(0, 0.4))  # jitter avoids thundering herd

    breaker.record_failure()
    raise LLMUnavailable(str(last_error) if last_error else "unknown LLM error")


def _extract_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
        raise LLMUnavailable(f"Gemini returned no candidates ({reason})")

    candidate = candidates[0]
    if candidate.get("finishReason") in {"SAFETY", "RECITATION", "BLOCKLIST"}:
        raise LLMUnavailable(f"Gemini blocked the response ({candidate['finishReason']})")

    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise LLMUnavailable("Gemini returned an empty response")
    return text


def _loads_lenient(text: str) -> dict:
    """Parse JSON, tolerating a stray code fence or leading prose."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM returned a non-object JSON value")
    return parsed


# ==========================================================================
# Coercion / validation
# ==========================================================================
def _as_str_list(value, limit: int, max_len: int = 300) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item.get("text", item) if isinstance(item, dict) else item).strip()
        if text:
            out.append(text[:max_len])
        if len(out) >= limit:
            break
    return out


_URGENCY_ALIASES = {
    "low": Urgency.LOW, "routine": Urgency.LOW, "mild": Urgency.LOW, "non-urgent": Urgency.LOW,
    "medium": Urgency.MEDIUM, "moderate": Urgency.MEDIUM, "soon": Urgency.MEDIUM, "prompt": Urgency.MEDIUM,
    "high": Urgency.HIGH, "urgent": Urgency.HIGH, "emergency": Urgency.HIGH,
    "critical": Urgency.HIGH, "severe": Urgency.HIGH, "immediate": Urgency.HIGH,
}


def _coerce_urgency(value) -> str:
    """Map anything the model says onto our three levels.

    Unknown values become ``Medium``, never ``Low`` — under-triaging is the
    dangerous direction of error.
    """
    return _URGENCY_ALIASES.get(str(value or "").strip().lower(), Urgency.MEDIUM)


def _coerce_previsit(payload: dict, form: dict) -> tuple[str, str, list[str], list[str], str]:
    urgency = _coerce_urgency(payload.get("urgency"))
    complaint = str(payload.get("chief_complaint") or "").strip()[:480]
    if not complaint:
        complaint = _first_sentence(form.get("symptoms", "")) or "Symptoms reported by patient"

    questions = _as_str_list(payload.get("suggested_questions"), limit=5)
    while len(questions) < 3:  # the brief asks for three; guarantee three
        questions.append(_DEFAULT_QUESTIONS[len(questions) % len(_DEFAULT_QUESTIONS)])

    red_flags = _as_str_list(payload.get("red_flags"), limit=6)
    # Safety net: if our own keyword scan sees a red flag the model missed,
    # escalate. We never de-escalate the model's answer.
    detected = _detect_red_flags(form.get("symptoms", ""))
    if detected and urgency != Urgency.HIGH:
        urgency = Urgency.HIGH
        for flag in detected:
            if flag not in red_flags:
                red_flags.append(flag)

    note = str(payload.get("summary_note") or "").strip()[:1500]
    return urgency, complaint, questions[:3], red_flags, note


def _coerce_postvisit(payload: dict, items: list[dict]) -> tuple[str, list[dict], list[str], list[str]]:
    summary = str(payload.get("patient_summary") or "").strip()[:4000]

    schedule: list[dict] = []
    for entry in payload.get("medication_schedule") or []:
        if not isinstance(entry, dict):
            continue
        schedule.append(
            {
                "medicine": str(entry.get("medicine") or "").strip()[:160],
                "dose": str(entry.get("dose") or "").strip()[:80],
                "when": str(entry.get("when") or "").strip()[:160],
                "duration": str(entry.get("duration") or "").strip()[:80],
                "notes": str(entry.get("notes") or "").strip()[:240],
            }
        )

    # The prescription is the source of truth, not the model. If the model
    # dropped or invented a medicine, rebuild the schedule from the DB rows.
    if items and len(schedule) != len(items):
        logger.warning("LLM schedule had %s entries for %s prescribed items — rebuilding", len(schedule), len(items))
        schedule = _schedule_from_items(items)

    steps = _as_str_list(payload.get("follow_up_steps"), limit=8)
    warnings = _as_str_list(payload.get("warning_signs"), limit=8)
    return summary, schedule, steps, warnings


def _first_sentence(text: str, limit: int = 160) -> str:
    clean = " ".join((text or "").split())
    if not clean:
        return ""
    sentence = re.split(r"(?<=[.!?])\s", clean)[0]
    return sentence[:limit].rstrip(" ,;")


_DEFAULT_QUESTIONS = [
    "When exactly did the symptoms start, and have they got better, worse, or stayed the same?",
    "Is there anything that clearly makes it better or worse?",
    "Any fever, weight loss, or night sweats alongside this?",
    "Have you had this before, and what helped last time?",
]


# ==========================================================================
# Deterministic fallbacks
# ==========================================================================
#: Conservative red-flag lexicon. Ordered most-specific-first so the phrase that
#: matches is the one reported back to the doctor.
_RED_FLAG_RULES: list[tuple[tuple[str, ...], str]] = [
    (("chest pain", "chest tightness", "chest pressure"), "Chest pain reported — rule out cardiac cause"),
    (("shortness of breath", "breathlessness", "can't breathe", "cannot breathe", "difficulty breathing", "gasping"), "Breathing difficulty reported"),
    (("slurred speech", "face droop", "facial droop", "one side weak", "one-sided weakness", "arm weakness", "cannot move", "can't move"), "Possible stroke features — FAST assessment indicated"),
    (("unconscious", "fainted", "faint", "passed out", "blackout", "collapsed"), "Loss of consciousness reported"),
    (("seizure", "fits", "convulsion"), "Seizure activity reported"),
    (("heavy bleeding", "uncontrolled bleeding", "blood in vomit", "vomiting blood", "coughing blood", "blood in stool", "black stool"), "Significant bleeding reported"),
    (("worst headache", "thunderclap", "sudden severe headache"), "Sudden severe headache — rule out subarachnoid haemorrhage"),
    (("stiff neck", "neck stiffness"), "Neck stiffness — consider meningism"),
    (("suicidal", "kill myself", "end my life", "self harm", "self-harm"), "Self-harm risk disclosed — safeguarding required"),
    (("severe abdominal pain", "rigid abdomen"), "Severe abdominal pain — rule out acute abdomen"),
    (("not passing urine", "no urine", "cannot urinate", "can't urinate"), "Urinary retention / anuria reported"),
    (("blue lips", "turning blue", "cyanosis"), "Cyanosis reported"),
    (("high fever", "104", "105", "very high temperature"), "Very high fever reported"),
    (("dehydrated", "severe dehydration", "not drinking", "no fluids"), "Possible severe dehydration"),
    (("allergic reaction", "swollen tongue", "swollen throat", "anaphylaxis", "throat closing"), "Possible anaphylaxis"),
]

_MEDIUM_RULES: tuple[str, ...] = (
    "fever", "vomiting", "diarrhoea", "diarrhea", "persistent", "worsening", "getting worse",
    "cannot sleep", "can't sleep", "weight loss", "night sweats", "rash", "swelling",
    "infection", "pain for", "recurring", "dizzy", "dizziness", "palpitations",
)


#: Words that flip the meaning of a symptom phrase. "No breathlessness" must
#: not raise the breathing red flag — patients routinely write what they do NOT
#: have, and a triage tool that cannot read a negation is worse than useless.
_NEGATORS = (
    "no ", "not ", "n't ", "without ", "denies ", "denied ", "never ",
    "free of ", "absent ", "negative for ", "ruled out ", "nil ",
)

#: How far back to look for a negator. Long enough for "she denies any chest
#: pain", short enough that a negation in the previous clause does not leak.
_NEGATION_WINDOW = 26


def _is_negated(text: str, index: int) -> bool:
    """True if the phrase starting at ``index`` is preceded by a negator."""
    window = text[max(0, index - _NEGATION_WINDOW) : index]
    # Do not let a clause boundary carry the negation across:
    # "no fever, chest pain since morning" -> chest pain is NOT negated.
    for boundary in (",", ";", ".", " but ", " however ", " though "):
        cut = window.rfind(boundary)
        if cut != -1:
            window = window[cut + len(boundary) :]
    return any(negator in window for negator in _NEGATORS)


def _mentions(text: str, phrase: str) -> bool:
    """True if ``phrase`` appears in ``text`` in a non-negated position."""
    start = 0
    while (index := text.find(phrase, start)) != -1:
        if not _is_negated(text, index):
            return True
        start = index + len(phrase)
    return False


def _detect_red_flags(symptoms: str) -> list[str]:
    """Scan free text for time-critical features, ignoring negated mentions."""
    text = " ".join((symptoms or "").lower().split())
    found: list[str] = []

    for phrases, label in _RED_FLAG_RULES:
        if label in found:
            continue
        if any(_mentions(text, phrase) for phrase in phrases):
            found.append(label)

    return found


def triage_without_llm(form: dict) -> tuple[str, list[str]]:
    """Rule-based urgency. Deliberately errs upward.

    Exposed separately so it can be unit-tested and reused by the model-output
    safety net in :func:`_coerce_previsit`.
    """
    symptoms = str(form.get("symptoms") or "")
    text = " ".join(symptoms.lower().split())
    red_flags = _detect_red_flags(symptoms)
    if red_flags:
        return Urgency.HIGH, red_flags

    severity = form.get("severity")
    duration = form.get("duration_days")

    if isinstance(severity, int) and severity >= 8:
        return Urgency.HIGH, ["Patient self-rated severity 8+/10"]
    if isinstance(severity, int) and severity >= 5:
        return Urgency.MEDIUM, []
    if isinstance(duration, int) and duration >= 14:
        return Urgency.MEDIUM, []
    # Negation-aware here too: "no fever" must not read as a fever.
    if any(_mentions(text, word) for word in _MEDIUM_RULES):
        return Urgency.MEDIUM, []
    if len(text.split()) < 6:
        # Too little information to call it low — see the doctor sooner.
        return Urgency.MEDIUM, []
    return Urgency.LOW, []


def _fallback_previsit(form: dict, error: str) -> PreVisitResult:
    urgency, red_flags = triage_without_llm(form)
    symptoms = str(form.get("symptoms") or "").strip()
    complaint = _first_sentence(symptoms) or "Symptoms reported by patient"

    bits = [f"Patient reports: {symptoms[:600]}"]
    if form.get("duration_days") is not None:
        bits.append(f"Duration: {form['duration_days']} day(s).")
    if form.get("severity") is not None:
        bits.append(f"Self-rated severity: {form['severity']}/10.")
    if form.get("existing_conditions"):
        bits.append(f"Known conditions: {form['existing_conditions']}.")
    if form.get("current_medications"):
        bits.append(f"Current medication: {form['current_medications']}.")
    if form.get("allergies"):
        bits.append(f"Allergies: {form['allergies']}.")
    bits.append("AI summarisation was unavailable — this brief was generated by the clinic's rule-based triage. Please read the full symptom form below.")

    return PreVisitResult(
        urgency=urgency,
        chief_complaint=complaint,
        suggested_questions=list(_DEFAULT_QUESTIONS[:3]),
        red_flags=red_flags,
        summary_note=" ".join(bits),
        source="fallback",
        model=None,
        error=error[:500],
    )


_FREQUENCY_WORDS = {
    "OD": "once a day",
    "BD": "twice a day",
    "TDS": "three times a day",
    "QID": "four times a day",
    "QHS": "once at bedtime",
    "SOS": "only when you need it",
}


def _schedule_from_items(items: list[dict]) -> list[dict]:
    """Build the medication table straight from the prescription rows.

    Used both as the pure fallback and as a repair when the model's schedule
    does not line up with what was actually prescribed.
    """
    schedule = []
    for item in items:
        freq = str(item.get("frequency") or "OD").upper()
        times = FREQUENCY_TIMES.get(freq, ())
        when = _FREQUENCY_WORDS.get(freq, freq)
        if times:
            pretty = ", ".join(_pretty_time(t) for t in times)
            when = f"{when} (around {pretty})"
        schedule.append(
            {
                "medicine": item.get("drug_name", ""),
                "dose": item.get("dosage", ""),
                "when": when,
                "duration": f"{item.get('duration_days', 0)} day(s)",
                "notes": item.get("instructions") or "",
            }
        )
    return schedule


def _pretty_time(hhmm: str) -> str:
    hour, minute = (int(part) for part in hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {suffix}"


def _fallback_postvisit(notes: str, diagnosis: str | None, follow_up: str | None, items: list[dict], error: str) -> PostVisitResult:
    lines = ["Here is a plain-language record of your visit."]
    if diagnosis:
        lines.append(f"Your doctor recorded: {diagnosis}.")
    if items:
        lines.append(f"You have been prescribed {len(items)} medicine(s) — the schedule is listed below.")
    if follow_up:
        lines.append(f"Please come back for a follow-up on {follow_up}.")
    lines.append("Your doctor's full notes are included below. If anything is unclear, contact the clinic before changing what you take.")

    steps = ["Take every medicine exactly as listed in the schedule, for the full duration."]
    if follow_up:
        steps.append(f"Attend your follow-up appointment on {follow_up}.")
    steps.append("Finish the full course even if you start feeling better.")
    steps.append("Call the clinic if your symptoms get worse instead of better.")

    return PostVisitResult(
        patient_summary=" ".join(lines) + f"\n\nDoctor's notes (as written):\n{notes.strip()}",
        medication_schedule=_schedule_from_items(items),
        follow_up_steps=steps,
        warning_signs=[
            "Difficulty breathing, chest pain, or fainting",
            "A fever that will not come down, or that comes back",
            "Vomiting that stops you keeping medicine or fluids down",
            "Any new rash, swelling of the face or tongue, or difficulty swallowing",
            "Symptoms that are clearly getting worse rather than better",
        ],
        source="fallback",
        model=None,
        error=error[:500],
    )


# ==========================================================================
# Public API
# ==========================================================================
def generate_previsit_summary(form: dict) -> PreVisitResult:
    """Symptom form -> triage brief for the doctor. Never raises."""
    started = time.monotonic()
    prompt = PREVISIT_USER_TEMPLATE.format(
        age_sex=form.get("age_sex") or "not stated",
        duration=f"{form['duration_days']} day(s)" if form.get("duration_days") is not None else "not stated",
        severity=form.get("severity") if form.get("severity") is not None else "not stated",
        conditions=form.get("existing_conditions") or "none reported",
        medications=form.get("current_medications") or "none reported",
        allergies=form.get("allergies") or "none reported",
        symptoms=(form.get("symptoms") or "").strip(),
    )

    try:
        payload, raw, attempts = _call_gemini(PREVISIT_SYSTEM, prompt, _PREVISIT_SCHEMA)
    except Exception as exc:  # LLMUnavailable, and anything unforeseen
        logger.warning("Pre-visit summary falling back to rules: %s", exc)
        result = _fallback_previsit(form, str(exc))
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    urgency, complaint, questions, red_flags, note = _coerce_previsit(payload, form)
    return PreVisitResult(
        urgency=urgency,
        chief_complaint=complaint,
        suggested_questions=questions,
        red_flags=red_flags,
        summary_note=note,
        source="llm",
        model=settings.gemini_model,
        latency_ms=int((time.monotonic() - started) * 1000),
        attempts=attempts,
        raw_response=raw[:8000],
    )


def generate_postvisit_summary(
    *,
    notes: str,
    diagnosis: str | None,
    follow_up_date: str | None,
    prescription_items: list[dict],
) -> PostVisitResult:
    """Clinical notes -> patient-friendly summary. Never raises."""
    started = time.monotonic()

    if prescription_items:
        rendered = "\n".join(
            f"- {i.get('drug_name')} {i.get('dosage')}, {i.get('frequency')} "
            f"({_FREQUENCY_WORDS.get(str(i.get('frequency')).upper(), '')}), "
            f"for {i.get('duration_days')} day(s)"
            + (f", {i.get('instructions')}" if i.get("instructions") else "")
            for i in prescription_items
        )
    else:
        rendered = "No medication prescribed."

    prompt = POSTVISIT_USER_TEMPLATE.format(
        diagnosis=diagnosis or "not recorded",
        follow_up=follow_up_date or "none scheduled",
        notes=notes.strip(),
        prescription=rendered,
    )

    try:
        payload, raw, attempts = _call_gemini(POSTVISIT_SYSTEM, prompt, _POSTVISIT_SCHEMA)
    except Exception as exc:
        logger.warning("Post-visit summary falling back to template: %s", exc)
        result = _fallback_postvisit(notes, diagnosis, follow_up_date, prescription_items, str(exc))
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    summary, schedule, steps, warnings = _coerce_postvisit(payload, prescription_items)
    if not summary:  # model produced valid JSON but an empty summary
        result = _fallback_postvisit(notes, diagnosis, follow_up_date, prescription_items, "empty patient_summary")
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    return PostVisitResult(
        patient_summary=summary,
        medication_schedule=schedule or _schedule_from_items(prescription_items),
        follow_up_steps=steps,
        warning_signs=warnings,
        source="llm",
        model=settings.gemini_model,
        latency_ms=int((time.monotonic() - started) * 1000),
        attempts=attempts,
        raw_response=raw[:8000],
    )


def status() -> dict:
    return {
        "configured": settings.llm_enabled,
        "model": settings.gemini_model if settings.llm_enabled else None,
        "prompt_version": PROMPT_VERSION,
        "breaker": breaker.snapshot(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
