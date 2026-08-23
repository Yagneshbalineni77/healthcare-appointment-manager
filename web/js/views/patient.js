/** Patient portal: find a doctor -> hold a slot -> symptom form -> confirm. */

import { api } from '../api.js';
import {
  addDays, confirmDialog, el, empty, esc, fmtDate, fmtDateTime, isoDate, loading,
  modal, relative, sourceBadge, statusBadge, toast, triageCard, urgencyBadge,
} from '../ui.js';
import { state, navigate } from '../app.js';

/* ==========================================================================
   Booking wizard
   ========================================================================== */
const wizard = { step: 1, doctor: null, day: null, slot: null, appointment: null, timer: null };

function steps(current) {
  const labels = ['Choose a doctor', 'Pick a time', 'Describe your symptoms', 'Confirmed'];
  return `<div class="steps">${labels.map((label, i) => {
    const n = i + 1;
    const cls = n < current ? 'done' : n === current ? 'active' : '';
    return `${i ? '<span class="step-sep"></span>' : ''}
      <div class="step ${cls}"><span class="n">${n < current ? '✓' : n}</span> ${esc(label)}</div>`;
  }).join('')}</div>`;
}

export async function book(root) {
  root.innerHTML = `<div class="page-head"><div>
      <h1>Book an appointment</h1>
      <div class="sub">Search by specialisation, pick a free slot, and tell your doctor what is going on.</div>
    </div></div><div id="wiz"></div>`;
  wizard.step = 1; wizard.doctor = null; wizard.slot = null; wizard.appointment = null;
  clearInterval(wizard.timer);
  await stepDoctors(root.querySelector('#wiz'));
}

/* ---------- step 1: choose a doctor ---------- */
async function stepDoctors(host) {
  host.innerHTML = steps(1) + loading('Loading doctors…');
  const specs = await api.specialisations();

  host.innerHTML = `
    ${steps(1)}
    <div class="card card-pad mb2">
      <div class="row">
        <div class="field" style="flex:1;min-width:200px;margin:0">
          <label for="q">Search</label>
          <input class="input" id="q" placeholder="Name, specialisation or condition — e.g. skin, heart">
        </div>
        <div class="field" style="min-width:200px;margin:0">
          <label for="spec">Specialisation</label>
          <select class="select" id="spec">
            <option value="">All specialisations</option>
            ${specs.map((s) => `<option>${esc(s)}</option>`).join('')}
          </select>
        </div>
      </div>
    </div>
    <div id="list">${loading()}</div>`;

  const list = host.querySelector('#list');
  const queryInput = host.querySelector('#q');
  const specSelect = host.querySelector('#spec');

  async function load() {
    list.innerHTML = loading();
    const doctors = await api.doctors({ q: queryInput.value.trim(), specialisation: specSelect.value });
    if (!doctors.length) {
      list.innerHTML = empty('🔍', 'No doctors match', 'Try a different specialisation or clear the search.');
      return;
    }
    list.innerHTML = `<div class="grid grid-2">${doctors.map(doctorCard).join('')}</div>`;
    list.querySelectorAll('[data-doc]').forEach((button) => {
      button.onclick = () => {
        wizard.doctor = doctors.find((d) => d.id === Number(button.dataset.doc));
        stepSlots(host);
      };
    });
  }

  let debounce;
  queryInput.oninput = () => { clearTimeout(debounce); debounce = setTimeout(load, 260); };
  specSelect.onchange = load;
  await load();
}

function doctorCard(d) {
  const fee = d.consultation_fee ? `₹${d.consultation_fee.toLocaleString('en-IN')}` : 'Free';
  const days = [...new Set((d.working_hours || []).map((w) => w.weekday))].length;
  return `
    <div class="card card-pad">
      <div class="row" style="align-items:flex-start">
        <div class="brand-mark" style="width:42px;height:42px;font-size:17px;border-radius:12px">
          ${esc((d.full_name || '').replace(/^Dr\.?\s*/i, '').charAt(0) || '?')}
        </div>
        <div style="flex:1;min-width:0">
          <h3>${esc(d.full_name)}</h3>
          <div class="small muted">${esc(d.specialisation)}${d.qualifications ? ` · ${esc(d.qualifications)}` : ''}</div>
        </div>
      </div>
      ${d.bio ? `<p class="small muted mt1 mb0">${esc(d.bio)}</p>` : ''}
      <div class="row mt2" style="gap:6px">
        <span class="badge badge-gray">${esc(d.experience_years)} yrs exp</span>
        <span class="badge badge-gray">${esc(d.slot_duration_minutes)} min slots</span>
        <span class="badge badge-gray">${fee}</span>
        ${d.room ? `<span class="badge badge-gray">Room ${esc(d.room)}</span>` : ''}
        ${days ? `<span class="badge badge-gray">${days} days/week</span>` : ''}
      </div>
      <button class="btn btn-primary btn-block mt2" data-doc="${d.id}">See available slots →</button>
    </div>`;
}

/* ---------- step 2: pick a slot ---------- */
async function stepSlots(host) {
  const doctor = wizard.doctor;
  host.innerHTML = `
    ${steps(2)}
    <div class="card card-pad mb2">
      <div class="row">
        <div style="flex:1">
          <h2>${esc(doctor.full_name)}</h2>
          <div class="small muted">${esc(doctor.specialisation)} · ${esc(doctor.slot_duration_minutes)} minute consultations</div>
        </div>
        <button class="btn btn-ghost btn-sm" id="back">← Change doctor</button>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>Choose a day</h3>
        <div class="right small muted">Times shown in ${esc(state.config?.timezone || 'clinic time')}</div>
      </div>
      <div class="card-pad">
        <div class="daystrip" id="days">${loading()}</div>
        <div id="slots" class="mt3"></div>
      </div>
    </div>`;

  host.querySelector('#back').onclick = () => stepDoctors(host);

  const range = await api.availabilityRange(doctor.id, isoDate(new Date()), 14);
  const daysHost = host.querySelector('#days');
  const slotsHost = host.querySelector('#slots');

  daysHost.innerHTML = range.map((day, index) => {
    const date = new Date(`${day.date}T00:00:00`);
    const free = day.slots.filter((s) => s.available).length;
    return `<button class="day ${day.is_leave ? 'leave' : ''} ${index === 0 ? 'selected' : ''}" data-i="${index}">
        <div class="dow">${date.toLocaleDateString(undefined, { weekday: 'short' })}</div>
        <div class="num">${date.getDate()}</div>
        <div class="mon">${day.is_leave ? 'on leave' : free ? `${free} free` : 'full'}</div>
      </button>`;
  }).join('');

  function showDay(index) {
    const day = range[index];
    daysHost.querySelectorAll('.day').forEach((b, i) => b.classList.toggle('selected', i === index));
    wizard.day = day;

    if (day.is_leave) {
      slotsHost.innerHTML = `<div class="banner banner-warn">🏖 ${esc(doctor.full_name)} is on leave on ${esc(fmtDate(day.date))}. Please pick another day.</div>`;
      return;
    }
    if (!day.slots.length) {
      slotsHost.innerHTML = `<div class="banner banner-info">${esc(doctor.full_name)} does not hold clinic on ${esc(fmtDate(day.date))}.</div>`;
      return;
    }

    const free = day.slots.filter((s) => s.available).length;
    slotsHost.innerHTML = `
      <div class="row mb1" style="justify-content:space-between">
        <div class="strong">${esc(fmtDate(day.date))}</div>
        <div class="small muted">${free} of ${day.slots.length} slots available</div>
      </div>
      <div class="slot-grid">
        ${day.slots.map((s) => `
          <button class="slot" data-start="${esc(s.start_at)}" ${s.available ? '' : 'disabled'}
            title="${s.available ? 'Available' : `Unavailable — ${esc(s.reason)}`}">${esc(s.label)}</button>`).join('')}
      </div>
      <div class="row mt2" style="justify-content:flex-end">
        <button class="btn btn-primary" id="go" disabled>Continue →</button>
      </div>`;

    const goButton = slotsHost.querySelector('#go');
    slotsHost.querySelectorAll('.slot:not(:disabled)').forEach((button) => {
      button.onclick = () => {
        slotsHost.querySelectorAll('.slot').forEach((b) => b.classList.remove('selected'));
        button.classList.add('selected');
        wizard.slot = button.dataset.start;
        goButton.disabled = false;
      };
    });
    goButton.onclick = () => holdAndContinue(host, goButton);
  }

  daysHost.querySelectorAll('.day').forEach((b) => { b.onclick = () => showDay(Number(b.dataset.i)); });
  showDay(0);
}

async function holdAndContinue(host, button) {
  button.disabled = true;
  button.innerHTML = '<span class="spinner" style="border-top-color:#fff"></span> Reserving…';
  try {
    wizard.appointment = await api.hold({ doctor_id: wizard.doctor.id, start_at: wizard.slot });
    stepSymptoms(host);
  } catch (error) {
    button.disabled = false;
    button.textContent = 'Continue →';
    if (error.code === 'SLOT_TAKEN') {
      toast('Someone just booked that slot. Refreshing availability…', 'warn');
      stepSlots(host);
    } else {
      toast(error.message, 'err');
    }
  }
}

/* ---------- step 3: symptom form ---------- */
function stepSymptoms(host) {
  const appointment = wizard.appointment;

  host.innerHTML = `
    ${steps(3)}
    <div class="grid" style="grid-template-columns:minmax(0,2fr) minmax(260px,1fr)">
      <div class="card">
        <div class="card-head"><h3>Tell your doctor what is going on</h3></div>
        <div class="card-pad">
          <div class="banner banner-info mb2">
            🔒 Your slot is reserved. Complete this form within
            <b class="countdown" id="cd">--:--</b> or the slot is released for other patients.
          </div>
          <form id="f" novalidate>
            <div class="field">
              <label for="symptoms">What are your symptoms? <span style="color:var(--red-600)">*</span></label>
              <textarea class="textarea" id="symptoms" name="symptoms" required minlength="5" maxlength="4000"
                placeholder="Describe what you are feeling, when it started, and anything that makes it better or worse."></textarea>
              <span class="help">Write it as you would say it. Your doctor sees this, plus an AI summary of it.</span>
            </div>
            <div class="field-row">
              <div class="field"><label for="duration_days">How many days has this lasted?</label>
                <input class="input" id="duration_days" name="duration_days" type="number" min="0" max="3650" placeholder="e.g. 3"></div>
              <div class="field"><label for="severity">How bad is it, 1–10?</label>
                <input class="input" id="severity" name="severity" type="number" min="1" max="10" placeholder="e.g. 6"></div>
            </div>
            <div class="field"><label for="existing_conditions">Existing conditions</label>
              <input class="input" id="existing_conditions" name="existing_conditions" maxlength="2000" placeholder="Diabetes, asthma, high blood pressure…"></div>
            <div class="field"><label for="current_medications">Medicines you already take</label>
              <input class="input" id="current_medications" name="current_medications" maxlength="2000" placeholder="Name and dose, if you know it"></div>
            <div class="field"><label for="allergies">Allergies</label>
              <input class="input" id="allergies" name="allergies" maxlength="1000" placeholder="Penicillin, sulfa, none…"></div>
            <div id="err"></div>
            <div class="row mt2" style="justify-content:space-between">
              <button class="btn btn-ghost" type="button" id="abandon">Release slot</button>
              <button class="btn btn-primary btn-lg" type="submit">Confirm appointment</button>
            </div>
          </form>
        </div>
      </div>

      <div class="stack">
        <div class="card card-pad">
          <div class="ai-label">Your appointment</div>
          <h3 class="mt1">${esc(wizard.doctor.full_name)}</h3>
          <div class="small muted">${esc(wizard.doctor.specialisation)}</div>
          <dl class="kv mt2">
            <dt>When</dt><dd>${esc(appointment.start_at_local)}</dd>
            <dt>Reference</dt><dd class="mono">${esc(appointment.reference)}</dd>
            ${wizard.doctor.room ? `<dt>Room</dt><dd>${esc(wizard.doctor.room)}</dd>` : ''}
            ${wizard.doctor.consultation_fee ? `<dt>Fee</dt><dd>₹${wizard.doctor.consultation_fee.toLocaleString('en-IN')}</dd>` : ''}
          </dl>
        </div>
        <div class="card card-pad">
          <div class="ai-label">What happens next</div>
          <ol class="qlist small mt1" style="color:var(--slate-600)">
            <li>An AI triage brief is prepared for your doctor.</li>
            <li>You and your doctor both get a confirmation email.</li>
            <li>A calendar invite is created if you have connected Google Calendar.</li>
            <li>We remind you ${esc(state.config?.reminder_lead_hours || 24)} hours before.</li>
          </ol>
        </div>
      </div>
    </div>`;

  startCountdown(host.querySelector('#cd'), appointment.hold_expires_at, host);

  host.querySelector('#abandon').onclick = async () => {
    if (!await confirmDialog('Release this slot?', 'The time will be offered to other patients and you will need to start again.', { confirmLabel: 'Release slot' })) return;
    clearInterval(wizard.timer);
    try { await api.cancel(appointment.id, { reason: 'Abandoned before confirming' }); } catch {}
    stepSlots(host);
  };

  const form = host.querySelector('#f');
  form.onsubmit = async (event) => {
    event.preventDefault();
    const errorBox = host.querySelector('#err');
    errorBox.innerHTML = '';
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    button.innerHTML = '<span class="spinner" style="border-top-color:#fff"></span> Confirming…';

    const raw = Object.fromEntries(new FormData(form));
    const payload = { symptoms: (raw.symptoms || '').trim() };
    if (raw.duration_days !== '') payload.duration_days = Number(raw.duration_days);
    if (raw.severity !== '') payload.severity = Number(raw.severity);
    for (const key of ['existing_conditions', 'current_medications', 'allergies']) {
      if (raw[key]?.trim()) payload[key] = raw[key].trim();
    }

    try {
      const confirmed = await api.confirm(appointment.id, { symptom_form: payload });
      clearInterval(wizard.timer);
      stepDone(host, confirmed);
    } catch (error) {
      button.disabled = false;
      button.textContent = 'Confirm appointment';
      errorBox.innerHTML = `<div class="banner banner-err mb2">⚠ ${esc(error.message)}</div>`;
      if (error.code === 'HOLD_EXPIRED') {
        clearInterval(wizard.timer);
        toast('Your reservation expired. Please pick a slot again.', 'warn');
        setTimeout(() => stepSlots(host), 1400);
      }
    }
  };
}

function startCountdown(node, expiresAt, host) {
  clearInterval(wizard.timer);
  const deadline = new Date(expiresAt).getTime();
  const tick = () => {
    const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    node.textContent = `${String(Math.floor(left / 60)).padStart(2, '0')}:${String(left % 60).padStart(2, '0')}`;
    node.style.color = left < 60 ? 'var(--red-600)' : '';
    if (left <= 0) {
      clearInterval(wizard.timer);
      toast('Your slot reservation expired.', 'warn');
      stepSlots(host);
    }
  };
  tick();
  wizard.timer = setInterval(tick, 1000);
}

/* ---------- step 4: done ---------- */
function stepDone(host, appointment) {
  const summary = appointment.previsit_summary;
  host.innerHTML = `
    ${steps(4)}
    <div class="card card-pad center mb2" style="border-color:var(--green-500);background:var(--green-50)">
      <div style="font-size:40px">✅</div>
      <h2 class="mt1">Appointment confirmed</h2>
      <p class="muted mb0">Reference <b class="mono">${esc(appointment.reference)}</b> ·
        ${esc(appointment.start_at_local)} with ${esc(appointment.doctor.full_name)}</p>
      <p class="small muted">A confirmation email is on its way to you and your doctor.</p>
    </div>
    <div class="grid grid-2">
      ${summary ? `<div>${triageCard(summary)}
        <p class="tiny muted mt1">This brief was prepared for your doctor. It is a summary aid, not a diagnosis.</p></div>` : ''}
      <div class="card card-pad">
        <h3>What to do now</h3>
        <ul class="qlist small mt1" style="color:var(--slate-600)">
          <li>Arrive 10 minutes early.</li>
          <li>Bring any previous prescriptions or reports.</li>
          <li>Connect Google Calendar in Settings to get an invite automatically.</li>
          <li>Need to change it? You can reschedule or cancel from My appointments.</li>
        </ul>
        <div class="row mt2">
          <button class="btn btn-primary" id="mine">View my appointments</button>
          <button class="btn btn-ghost" id="again">Book another</button>
        </div>
      </div>
    </div>`;

  host.querySelector('#mine').onclick = () => navigate('#/appointments');
  host.querySelector('#again').onclick = () => book(host.parentElement);
}

/* ==========================================================================
   My appointments
   ========================================================================== */
export async function appointments(root) {
  root.innerHTML = `<div class="page-head"><div>
      <h1>My appointments</h1><div class="sub">Everything you have booked, past and upcoming.</div>
    </div><div class="page-head-actions">
      <button class="btn btn-primary btn-sm" id="new">+ Book an appointment</button>
    </div></div>
    <div class="tabs">
      <button class="tab active" data-scope="upcoming">Upcoming</button>
      <button class="tab" data-scope="past">Past</button>
      <button class="tab" data-scope="all">All</button>
    </div>
    <div id="list">${loading()}</div>`;

  root.querySelector('#new').onclick = () => navigate('#/book');
  const list = root.querySelector('#list');

  async function load(scope) {
    list.innerHTML = loading();
    const rows = await api.appointments({ scope });
    if (!rows.length) {
      list.innerHTML = empty('📅', 'Nothing here yet',
        scope === 'upcoming' ? 'You have no upcoming appointments.' : 'No appointments to show.',
        '<button class="btn btn-primary mt2" onclick="location.hash=\'#/book\'">Book an appointment</button>');
      return;
    }
    list.innerHTML = `<div class="stack">${rows.map(patientAppointmentCard).join('')}</div>`;
    wireAppointmentActions(list, () => load(scope));
  }

  root.querySelectorAll('.tab').forEach((tab) => {
    tab.onclick = () => {
      root.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      load(tab.dataset.scope);
    };
  });
  await load('upcoming');
}

function patientAppointmentCard(a) {
  const active = a.status === 'confirmed' || a.status === 'held';
  const upcoming = new Date(a.start_at) > new Date();
  return `
    <div class="card">
      <div class="card-pad">
        <div class="row" style="align-items:flex-start">
          <div style="flex:1;min-width:0">
            <div class="row" style="gap:8px">
              ${statusBadge(a.status)}
              ${a.previsit_summary ? urgencyBadge(a.previsit_summary.urgency) : ''}
              ${a.calendar_synced ? '<span class="badge badge-blue">📆 In calendar</span>' : ''}
              ${a.reschedule_count ? `<span class="badge badge-gray">moved ${a.reschedule_count}×</span>` : ''}
            </div>
            <h3 class="mt1">${esc(a.doctor.full_name)} · <span class="muted" style="font-weight:500">${esc(a.doctor.specialisation)}</span></h3>
            <div class="small muted">${esc(a.start_at_local)} ${upcoming && active ? `· ${esc(relative(a.start_at))}` : ''}</div>
            <div class="tiny muted mt1">Ref <span class="mono">${esc(a.reference)}</span>${a.doctor.room ? ` · Room ${esc(a.doctor.room)}` : ''}</div>
            ${a.cancellation_reason ? `<div class="small mt1" style="color:var(--red-700)">Cancelled: ${esc(a.cancellation_reason)}</div>` : ''}
          </div>
          <div class="row" style="gap:6px">
            ${a.has_consultation ? `<button class="btn btn-sm btn-primary" data-summary="${a.id}">View visit summary</button>` : ''}
            ${a.previsit_summary ? `<button class="btn btn-sm btn-ghost" data-triage="${a.id}">My symptom brief</button>` : ''}
            ${active && upcoming ? `<button class="btn btn-sm btn-ghost" data-move="${a.id}" data-doc="${a.doctor.id}">Reschedule</button>
              <button class="btn btn-sm btn-danger" data-cancel="${a.id}">Cancel</button>` : ''}
          </div>
        </div>
      </div>
    </div>`;
}

export function wireAppointmentActions(scope, reload) {
  scope.querySelectorAll('[data-cancel]').forEach((b) => {
    b.onclick = async () => {
      const result = await modal({
        title: 'Cancel this appointment?',
        body: `<form><div class="field"><label>Reason <span class="muted">(optional, shared with the clinic)</span></label>
                 <input class="input" name="reason" placeholder="e.g. feeling better, travel"></div></form>
               <p class="small muted">The slot is released immediately and both of you get an email.</p>`,
        confirmLabel: 'Cancel appointment', cancelLabel: 'Keep it', danger: true,
      });
      if (!result) return;
      try { await api.cancel(b.dataset.cancel, { reason: result.reason || null }); toast('Appointment cancelled', 'ok'); reload(); }
      catch (e) { toast(e.message, 'err'); }
    };
  });

  scope.querySelectorAll('[data-move]').forEach((b) => {
    b.onclick = () => rescheduleDialog(Number(b.dataset.move), Number(b.dataset.doc), reload);
  });

  scope.querySelectorAll('[data-summary]').forEach((b) => {
    b.onclick = async () => {
      try { showVisitSummary(await api.consultation(b.dataset.summary)); }
      catch (e) { toast(e.message, 'err'); }
    };
  });

  scope.querySelectorAll('[data-triage]').forEach((b) => {
    b.onclick = async () => {
      const a = await api.appointment(b.dataset.triage);
      modal({
        title: 'Your pre-visit brief', wide: true, confirmLabel: 'Close', cancelLabel: '',
        body: `${triageCard(a.previsit_summary)}
          ${a.symptom_report ? `<div class="card card-pad mt2"><div class="ai-label">What you told us</div>
            <p class="small mt1 mb0">${esc(a.symptom_report.symptoms)}</p></div>` : ''}`,
      });
    };
  });
}

async function rescheduleDialog(appointmentId, doctorId, reload) {
  const range = await api.availabilityRange(doctorId, isoDate(new Date()), 10);
  const options = range.flatMap((day) =>
    day.slots.filter((s) => s.available).map((s) => ({ value: s.start_at, label: `${fmtDate(day.date)} · ${s.label}` })));

  if (!options.length) { toast('No free slots in the next 10 days for this doctor.', 'warn'); return; }

  const result = await modal({
    title: 'Move this appointment',
    body: `<form>
      <div class="field"><label>New time</label>
        <select class="select" name="start_at">${options.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('')}</select></div>
      <div class="field"><label>Reason <span class="muted">(optional)</span></label>
        <input class="input" name="reason" placeholder="e.g. work clash"></div>
    </form><p class="small muted">Your calendar invite and reminders update automatically.</p>`,
    confirmLabel: 'Move appointment', danger: false,
  });
  if (!result) return;
  try { await api.reschedule(appointmentId, { start_at: result.start_at, reason: result.reason || null }); toast('Appointment moved', 'ok'); reload(); }
  catch (e) { toast(e.message, 'err'); }
}

export function showVisitSummary(consultation) {
  const s = consultation.postvisit_summary;
  const meds = (s?.medication_schedule || []).map((m) => `
    <tr><td class="strong">${esc(m.medicine)}</td><td>${esc(m.dose)}</td><td>${esc(m.when)}</td>
        <td>${esc(m.duration)}</td><td class="small muted">${esc(m.notes || '')}</td></tr>`).join('');

  modal({
    title: 'Your visit summary', wide: true, confirmLabel: 'Close', cancelLabel: '',
    body: `
      ${s ? `
      <div class="row mb2" style="justify-content:space-between">
        <span class="ai-label">Written for you, in plain language</span>
        ${sourceBadge(s.source, s.model, s.latency_ms)}
      </div>
      <div class="card card-pad ai-card mb2"><p class="mb0" style="white-space:pre-wrap">${esc(s.patient_summary)}</p></div>
      ${meds ? `<h3 class="mb1">Your medicines</h3>
        <div class="card table-wrap mb2"><table class="data">
          <thead><tr><th>Medicine</th><th>Dose</th><th>When</th><th>For</th><th>Notes</th></tr></thead>
          <tbody>${meds}</tbody></table></div>` : ''}
      ${s.follow_up_steps?.length ? `<h3 class="mb1">Next steps</h3><ul class="qlist mb2">${s.follow_up_steps.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
      ${s.warning_signs?.length ? `<h3 class="mb1">Get help sooner if you notice</h3>
        <ul class="flag-list mb2">${s.warning_signs.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
      ` : '<p class="muted">No patient summary was generated for this visit.</p>'}
      <details class="mt2"><summary class="small muted" style="cursor:pointer">Doctor's clinical notes</summary>
        <div class="card card-pad mt1"><p class="small mb0" style="white-space:pre-wrap">${esc(consultation.clinical_notes)}</p>
        ${consultation.diagnosis ? `<p class="small mt1 mb0"><b>Diagnosis:</b> ${esc(consultation.diagnosis)}</p>` : ''}
        ${consultation.follow_up_date ? `<p class="small mb0"><b>Follow-up:</b> ${esc(fmtDate(consultation.follow_up_date))}</p>` : ''}</div>
      </details>`,
  });
}

/* ==========================================================================
   Medication schedule
   ========================================================================== */
export async function medications(root) {
  root.innerHTML = `<div class="page-head"><div>
      <h1>My medicines</h1>
      <div class="sub">Reminders are emailed automatically at each scheduled dose.</div>
    </div></div><div id="list">${loading()}</div>`;

  const list = root.querySelector('#list');

  async function load() {
    list.innerHTML = loading();
    const rows = await api.medicationReminders({ upcoming_only: true, limit: 300 });
    if (!rows.length) {
      list.innerHTML = empty('💊', 'No medication reminders',
        'When a doctor prescribes something, every dose appears here and you get an email at each one.');
      return;
    }

    const byDay = new Map();
    for (const r of rows) {
      const key = new Date(r.due_at).toDateString();
      if (!byDay.has(key)) byDay.set(key, []);
      byDay.get(key).push(r);
    }

    const drugs = [...new Set(rows.map((r) => r.drug_name))];
    list.innerHTML = `
      <div class="banner banner-info mb2">💊 ${rows.length} upcoming doses across ${drugs.length} medicine(s): ${esc(drugs.join(', '))}</div>
      <div class="stack">
        ${[...byDay.entries()].slice(0, 14).map(([day, items]) => `
          <div class="card">
            <div class="card-head"><h3>${esc(new Date(day).toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' }))}</h3>
              <div class="right small muted">${items.length} dose${items.length > 1 ? 's' : ''}</div></div>
            <div class="table-wrap"><table class="data"><tbody>
              ${items.map((r) => `<tr>
                <td class="nowrap strong">${esc(new Date(r.due_at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }))}</td>
                <td>${esc(r.drug_name)} <span class="muted">${esc(r.dosage)}</span></td>
                <td class="small muted">${esc(r.instructions || '')}</td>
                <td style="text-align:right"><button class="btn btn-sm btn-ghost" data-skip="${r.id}">Stop</button></td>
              </tr>`).join('')}
            </tbody></table></div>
          </div>`).join('')}
      </div>`;

    list.querySelectorAll('[data-skip]').forEach((b) => {
      b.onclick = async () => {
        if (!await confirmDialog('Stop this reminder?', 'You will not be emailed for this dose. Keep taking your medicine as prescribed.', { confirmLabel: 'Stop reminder' })) return;
        await api.cancelReminder(b.dataset.skip);
        toast('Reminder stopped', 'ok');
        load();
      };
    });
  }

  await load();
}
