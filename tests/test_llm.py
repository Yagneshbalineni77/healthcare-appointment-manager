"""LLM behaviour: graceful degradation, triage safety, and output coercion.

``GEMINI_API_KEY`` is empty in the test environment, so every call here takes
the fallback path — which is exactly the failure mode the brief asks us to
handle without breaking the system.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import llm
from tests.conftest import bearer, next_free_slot


# ==========================================================================
# Never raises, always returns
# ==========================================================================
def test_previsit_falls_back_when_the_model_is_unavailable():
    result = llm.generate_previsit_summary({"symptoms": "sore throat and mild fever for two days"})
    assert result.source == "fallback"
    assert result.urgency in {"Low", "Medium", "High"}
    assert len(result.suggested_questions) == 3  # the brief asks for three
    assert result.chief_complaint
    assert result.error  # the reason is recorded for the audit trail


def test_postvisit_falls_back_and_rebuilds_the_schedule_from_the_prescription():
    result = llm.generate_postvisit_summary(
        notes="Ac. pharyngitis. Rest, fluids, review if no better in 5 days.",
        diagnosis="Acute pharyngitis", follow_up_date="2030-01-01",
        prescription_items=[
            {"drug_name": "Amoxicillin", "dosage": "500 mg", "frequency": "TDS",
             "duration_days": 5, "instructions": "after food"}],
    )
    assert result.source == "fallback"
    assert len(result.medication_schedule) == 1

    entry = result.medication_schedule[0]
    assert entry["medicine"] == "Amoxicillin"
    assert entry["dose"] == "500 mg"           # dose is copied verbatim, never reworded
    assert "three times a day" in entry["when"]
    assert result.warning_signs


def test_a_transport_failure_still_returns_a_result(monkeypatch):
    """Even a hard network error must not propagate to the caller."""
    import dataclasses

    # Settings is a frozen dataclass, so swap the whole object rather than a field.
    monkeypatch.setattr(llm, "settings", dataclasses.replace(
        llm.settings, gemini_api_key="fake-key", llm_max_attempts=1))
    llm.breaker.record_success()

    def boom(*args, **kwargs):
        raise httpx.ConnectError("dns failure")

    monkeypatch.setattr(httpx.Client, "post", boom)
    result = llm.generate_previsit_summary({"symptoms": "headache for a week that is getting worse"})
    assert result.source == "fallback"
    assert "ConnectError" in result.error or "dns" in result.error


# ==========================================================================
# Triage safety
# ==========================================================================
@pytest.mark.parametrize(
    "symptoms,expected",
    [
        ("Crushing chest pain with sweating and shortness of breath", "High"),
        ("Sudden slurred speech and weakness down my right side", "High"),
        ("I have been having thoughts of self harm", "High"),
        ("Mild sore throat for two days, no fever, eating normally", "Low"),
        ("Routine follow-up for blood pressure, feeling well, nothing new to report", "Low"),
    ],
)
def test_rule_based_triage_levels(symptoms, expected):
    urgency, _ = llm.triage_without_llm({"symptoms": symptoms})
    assert urgency == expected


@pytest.mark.parametrize(
    "symptoms",
    [
        "Dry cough for five days. No breathlessness, appetite normal.",
        "Headache since Monday. Denies chest pain and no fever.",
        "Rash on arm. Patient denies any difficulty breathing.",
    ],
)
def test_negated_symptoms_do_not_raise_red_flags(symptoms):
    """'No breathlessness' must not trip the breathing red flag."""
    assert llm._detect_red_flags(symptoms) == []


def test_negation_does_not_leak_across_a_clause_boundary():
    """'No fever, chest pain since morning' -> the chest pain IS a red flag."""
    flags = llm._detect_red_flags("No fever, chest pain since morning")
    assert any("Chest pain" in f for f in flags)


def test_vague_input_is_never_triaged_low():
    """Under-triaging is the dangerous direction of error."""
    urgency, _ = llm.triage_without_llm({"symptoms": "not well"})
    assert urgency == "Medium"


# ==========================================================================
# Coercion of model output
# ==========================================================================
@pytest.mark.parametrize(
    "raw,expected",
    [("urgent", "High"), ("EMERGENCY", "High"), ("moderate", "Medium"),
     ("low", "Low"), ("banana", "Medium"), (None, "Medium"), ("", "Medium")],
)
def test_unknown_urgency_values_default_to_medium(raw, expected):
    assert llm._coerce_urgency(raw) == expected


def test_model_output_is_escalated_when_it_misses_a_red_flag():
    """Our own keyword scan can raise the model's urgency, never lower it."""
    payload = {"urgency": "Low", "chief_complaint": "chest discomfort",
               "suggested_questions": ["a", "b", "c"], "red_flags": [], "summary_note": ""}
    form = {"symptoms": "Crushing chest pain radiating to my left arm since this morning"}

    urgency, _, questions, flags, _ = llm._coerce_previsit(payload, form)
    assert urgency == "High"
    assert flags


def test_missing_questions_are_topped_up_to_three():
    payload = {"urgency": "Low", "chief_complaint": "cough",
               "suggested_questions": ["only one"], "red_flags": [], "summary_note": ""}
    _, _, questions, _, _ = llm._coerce_previsit(payload, {"symptoms": "cough for a week"})
    assert len(questions) == 3


def test_schedule_is_rebuilt_when_the_model_drops_a_medicine():
    """The prescription is the source of truth, not the model."""
    items = [
        {"drug_name": "Amoxicillin", "dosage": "500 mg", "frequency": "TDS", "duration_days": 5, "instructions": ""},
        {"drug_name": "Paracetamol", "dosage": "650 mg", "frequency": "BD", "duration_days": 3, "instructions": ""},
    ]
    payload = {"patient_summary": "ok", "follow_up_steps": [], "warning_signs": [],
               "medication_schedule": [{"medicine": "Amoxicillin", "dose": "500 mg", "when": "thrice", "duration": "5 days"}]}

    _, schedule, _, _ = llm._coerce_postvisit(payload, items)
    assert len(schedule) == 2
    assert {e["medicine"] for e in schedule} == {"Amoxicillin", "Paracetamol"}


# ==========================================================================
# Circuit breaker
# ==========================================================================
def test_circuit_breaker_opens_and_recovers():
    breaker = llm._CircuitBreaker(threshold=3, cooldown=60)
    assert breaker.is_open is False
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_open is True
    assert breaker.snapshot()["consecutive_failures"] == 3

    breaker.record_success()
    assert breaker.is_open is False


# ==========================================================================
# End to end through the API
# ==========================================================================
def test_booking_still_succeeds_with_the_llm_down(client, clinic):
    """The whole point: a dead model must not cost the patient their booking."""
    doctor_id = clinic["doctor"].id
    slot, _ = next_free_slot(client, clinic["p1"], doctor_id)
    held = client.post("/api/appointments/hold", headers=bearer(clinic["p1"]),
                       json={"doctor_id": doctor_id, "start_at": slot}).json()

    response = client.post(f"/api/appointments/{held['id']}/confirm", headers=bearer(clinic["p1"]),
                           json={"symptom_form": {
                               "symptoms": "Chest tightness with shortness of breath since this morning",
                               "severity": 8, "duration_days": 1}})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"

    summary = body["previsit_summary"]
    assert summary["source"] == "fallback"   # honest provenance
    assert summary["urgency"] == "High"      # and still clinically safe
