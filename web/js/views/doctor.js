/** Doctor portal: today's clinic list with AI triage, and post-visit notes. */

import { api } from '../api.js';
import {
  addDays, el, empty, esc, fmtDate, isoDate, loading, modal, relative,
  sourceBadge, statusBadge, toast, triageCard, urgencyBadge,
} from '../ui.js';
import { showVisitSummary } from './patient.js';

const URGENCY_RANK = { High: 0, Medium: 1, Low: 2 };

export async function schedule(root) {
  root.innerHTML = `<div class="page-head"><div>
      <h1>My schedule</h1>
      <div class="sub">Your clinic list, sorted so the most urgent patients surface first.</div>
    </div><div class="page-head-actions">
      <input class="input" type="date" id="day" style="width:auto" value="${isoDate(new Date())}">
    </div></div>
    <div id="list">${loading()}</div>`;

  const list = root.querySelector('#list');
  const dayInput = root.querySelector('#day');

  async function load() {
    list.innerHTML = loading();
    const rows = await api.appointments({ on: dayInput.value, limit: 200 });
    const active = rows.filter((a) => ['confirmed', 'held', 'completed'].includes(a.status));

    if (!active.length) {
      list.innerHTML = empty('🩺', 'No patients booked',
        `Nothing scheduled for ${fmtDate(dayInput.value)}. Enjoy the quiet.`);
      return;
    }

    active.sort((a, b) => {
      const ua = URGENCY_RANK[a.previsit_summary?.urgency] ?? 3;
      const ub = URGENCY_RANK[b.previsit_summary?.urgency] ?? 3;
      return ua - ub || new Date(a.start_at) - new Date(b.start_at);
    });

    const counts = active.reduce((acc, a) => {
      const u = a.previsit_summary?.urgency || 'Unknown';
      acc[u] = (acc[u] || 0) + 1; return acc;
    }, {});

    list.innerHTML = `
      <div class="grid grid-4 mb2">
        <div class="card stat"><div class="label">Patients</div><div class="value">${active.length}</div></div>
        <div class="card stat"><div class="label">High urgency</div>
          <div class="value" style="color:var(--red-600)">${counts.High || 0}</div></div>
        <div class="card stat"><div class="label">Medium</div>
          <div class="value" style="color:var(--amber-600)">${counts.Medium || 0}</div></div>
        <div class="card stat"><div class="label">Notes filed</div>
          <div class="value">${active.filter((a) => a.has_consultation).length}</div></div>
      </div>
      <div class="stack">${active.map(clinicRow).join('')}</div>`;

    wireDoctorActions(list, load);
  }

  dayInput.onchange = load;
  await load();
}

function clinicRow(a) {
  const s = a.previsit_summary;
  return `
    <div class="card">
      <div class="card-pad">
        <div class="row" style="align-items:flex-start;gap:16px">
          <div style="text-align:center;min-width:76px">
            <div class="strong" style="font-size:19px;font-variant-numeric:tabular-nums">
              ${esc(new Date(a.start_at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }))}</div>
            <div class="tiny muted">${esc(relative(a.start_at))}</div>
          </div>
          <div style="flex:1;min-width:0">
            <div class="row" style="gap:7px">
              ${statusBadge(a.status)}
              ${s ? urgencyBadge(s.urgency) : '<span class="badge badge-gray">No form yet</span>'}
              ${s ? sourceBadge(s.source, s.model, s.latency_ms) : ''}
            </div>
            <h3 class="mt1">${esc(a.patient.full_name)}</h3>
            <div class="small muted">${esc(a.patient.email)}${a.patient.phone ? ` · ${esc(a.patient.phone)}` : ''} · Ref <span class="mono">${esc(a.reference)}</span></div>
            ${s ? `<p class="strong small mt1 mb0">${esc(s.chief_complaint)}</p>` : ''}
            ${s?.red_flags?.length ? `<div class="row mt1" style="gap:5px">${s.red_flags.map((f) => `<span class="badge badge-red">⚠ ${esc(f)}</span>`).join('')}</div>` : ''}
          </div>
          <div class="stack" style="gap:6px;min-width:150px">
            <button class="btn btn-sm btn-ghost" data-brief="${a.id}">Full briefing</button>
            ${a.has_consultation
              ? `<button class="btn btn-sm btn-ghost" data-view="${a.id}">View notes</button>`
              : `<button class="btn btn-sm btn-primary" data-file="${a.id}" data-name="${esc(a.patient.full_name)}">File notes</button>`}
          </div>
        </div>
      </div>
    </div>`;
}

export async function appointments(root) {
  root.innerHTML = `<div class="page-head"><div>
      <h1>All appointments</h1><div class="sub">Everything booked with you.</div>
    </div></div>
    <div class="tabs">
      <button class="tab active" data-scope="upcoming">Upcoming</button>
      <button class="tab" data-scope="past">Past</button>
      <button class="tab" data-scope="all">All</button>
    </div>
    <div id="list">${loading()}</div>`;

  const list = root.querySelector('#list');

  async function load(scope) {
    list.innerHTML = loading();
    const rows = await api.appointments({ scope, limit: 200 });
    if (!rows.length) { list.innerHTML = empty('📅', 'Nothing to show', 'No appointments in this view.'); return; }

    list.innerHTML = `<div class="card table-wrap"><table class="data">
      <thead><tr><th>When</th><th>Patient</th><th>Status</th><th>Triage</th><th>Chief complaint</th><th></th></tr></thead>
      <tbody>${rows.map((a) => `
        <tr>
          <td class="nowrap small">${esc(a.start_at_local)}</td>
          <td><div class="strong">${esc(a.patient.full_name)}</div><div class="tiny muted mono">${esc(a.reference)}</div></td>
          <td>${statusBadge(a.status)}</td>
          <td>${a.previsit_summary ? urgencyBadge(a.previsit_summary.urgency) : '<span class="muted tiny">—</span>'}</td>
          <td class="small muted">${esc(a.previsit_summary?.chief_complaint || a.reason_for_visit || '—')}</td>
          <td style="text-align:right" class="nowrap">
            <button class="btn btn-sm btn-ghost" data-brief="${a.id}">Brief</button>
            ${a.has_consultation
              ? `<button class="btn btn-sm btn-ghost" data-view="${a.id}">Notes</button>`
              : (['confirmed', 'held', 'completed'].includes(a.status)
                  ? `<button class="btn btn-sm btn-primary" data-file="${a.id}" data-name="${esc(a.patient.full_name)}">File</button>` : '')}
          </td>
        </tr>`).join('')}</tbody></table></div>`;

    wireDoctorActions(list, () => load(scope));
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

function wireDoctorActions(scope, reload) {
  scope.querySelectorAll('[data-brief]').forEach((b) => {
    b.onclick = async () => {
      const a = await api.appointment(b.dataset.brief);
      const r = a.symptom_report;
      modal({
        title: `Pre-visit briefing — ${a.patient.full_name}`, wide: true, confirmLabel: 'Close', cancelLabel: '',
        body: `
          ${a.previsit_summary ? triageCard(a.previsit_summary) : '<div class="banner banner-warn">The patient has not submitted a symptom form.</div>'}
          ${r ? `<div class="card card-pad mt2">
            <div class="ai-label">Patient's own words</div>
            <p class="mt1" style="white-space:pre-wrap">${esc(r.symptoms)}</p>
            <dl class="kv mt2">
              ${r.duration_days != null ? `<dt>Duration</dt><dd>${esc(r.duration_days)} day(s)</dd>` : ''}
              ${r.severity != null ? `<dt>Severity</dt><dd>${esc(r.severity)}/10</dd>` : ''}
              <dt>Known conditions</dt><dd>${esc(r.existing_conditions || 'none reported')}</dd>
              <dt>Current medication</dt><dd>${esc(r.current_medications || 'none reported')}</dd>
              <dt>Allergies</dt><dd>${esc(r.allergies || 'none reported')}</dd>
            </dl></div>` : ''}
          ${a.previsit_summary?.source === 'fallback'
            ? `<div class="banner banner-warn mt2">⚙ This brief came from the rule-based fallback because the AI service was unavailable
                 ${a.previsit_summary.error ? `(${esc(a.previsit_summary.error.slice(0, 120))})` : ''}.
                 <button class="btn btn-sm btn-ghost" data-regen="${a.id}" style="margin-left:8px">Retry AI</button></div>` : ''}`,
      });

      document.querySelectorAll('[data-regen]').forEach((rb) => {
        rb.onclick = async () => {
          rb.disabled = true; rb.textContent = 'Retrying…';
          try {
            const s = await api.regenerateTriage(rb.dataset.regen);
            toast(s.source === 'llm' ? 'AI summary regenerated' : 'AI still unavailable — fallback kept', s.source === 'llm' ? 'ok' : 'warn');
            document.querySelector('.modal-backdrop')?.remove();
            reload();
          } catch (e) { toast(e.message, 'err'); rb.disabled = false; rb.textContent = 'Retry AI'; }
        };
      });
    };
  });

  scope.querySelectorAll('[data-view]').forEach((b) => {
    b.onclick = async () => {
      try { showVisitSummary(await api.consultation(b.dataset.view)); }
      catch (e) { toast(e.message, 'err'); }
    };
  });

  scope.querySelectorAll('[data-file]').forEach((b) => {
    b.onclick = () => fileNotes(Number(b.dataset.file), b.dataset.name, reload);
  });
}

/* ---------- post-visit notes + prescription builder ---------- */
function medRow(index) {
  return `
    <tr data-med>
      <td><input class="input" name="drug_name" placeholder="Amoxicillin" required></td>
      <td><input class="input" name="dosage" placeholder="500 mg" required style="width:100px"></td>
      <td><select class="select" name="frequency" style="width:82px">
            <option value="OD">OD</option><option value="BD" selected>BD</option><option value="TDS">TDS</option>
            <option value="QID">QID</option><option value="QHS">QHS</option><option value="SOS">SOS</option>
          </select></td>
      <td><input class="input" name="duration_days" type="number" min="1" max="180" value="5" style="width:70px"></td>
      <td><input class="input" name="instructions" placeholder="after food"></td>
      <td><button type="button" class="btn btn-sm btn-ghost" data-rm>✕</button></td>
    </tr>`;
}

async function fileNotes(appointmentId, patientName, reload) {
  const form = el(`
    <form>
      <div class="field">
        <label>Clinical notes <span style="color:var(--red-600)">*</span></label>
        <textarea class="textarea" name="clinical_notes" required minlength="10" style="min-height:120px"
          placeholder="O/E findings, impression, advice. Shorthand is fine — the patient gets a plain-language version."></textarea>
        <span class="help">Written for the record. An AI rewrite is generated for the patient automatically.</span>
      </div>
      <div class="field-row">
        <div class="field"><label>Diagnosis</label><input class="input" name="diagnosis" placeholder="Acute pharyngitis"></div>
        <div class="field"><label>Follow-up date</label><input class="input" name="follow_up_date" type="date"></div>
      </div>
      <div class="field">
        <label>Prescription</label>
        <div class="card table-wrap"><table class="data">
          <thead><tr><th>Medicine</th><th>Dose</th><th>Freq</th><th>Days</th><th>Instructions</th><th></th></tr></thead>
          <tbody id="meds">${medRow(0)}</tbody>
        </table></div>
        <button type="button" class="btn btn-sm btn-ghost mt1" id="add-med">+ Add medicine</button>
        <span class="help">A reminder email is scheduled for every dose. OD=once, BD=twice, TDS=3×, QID=4×, QHS=bedtime, SOS=as needed (no reminders).</span>
      </div>
    </form>`);

  const tbody = form.querySelector('#meds');
  const wireRemove = () => form.querySelectorAll('[data-rm]').forEach((b) => {
    b.onclick = () => { if (tbody.querySelectorAll('[data-med]').length > 1) b.closest('tr').remove(); else b.closest('tr').querySelectorAll('input').forEach((i) => (i.value = '')); };
  });
  form.querySelector('#add-med').onclick = () => { tbody.insertAdjacentHTML('beforeend', medRow(0)); wireRemove(); };
  wireRemove();

  const result = await modal({
    title: `Post-visit notes — ${patientName}`, wide: true,
    body: form, confirmLabel: 'File notes & send summary',
  });
  if (!result) return;

  const notes = form.querySelector('[name=clinical_notes]').value.trim();
  if (notes.length < 10) { toast('Clinical notes must be at least 10 characters.', 'err'); return; }

  const items = [...tbody.querySelectorAll('[data-med]')].map((tr) => {
    const get = (n) => tr.querySelector(`[name=${n}]`).value.trim();
    if (!get('drug_name') || !get('dosage')) return null;
    return {
      drug_name: get('drug_name'), dosage: get('dosage'), frequency: get('frequency'),
      duration_days: Number(get('duration_days') || 5), instructions: get('instructions') || null,
    };
  }).filter(Boolean);

  const payload = {
    clinical_notes: notes,
    diagnosis: form.querySelector('[name=diagnosis]').value.trim() || null,
    follow_up_date: form.querySelector('[name=follow_up_date]').value || null,
    prescription_items: items,
  };

  toast('Filing notes and generating the patient summary…');
  try {
    const consultation = await api.createConsultation(appointmentId, payload);
    const source = consultation.postvisit_summary?.source;
    toast(source === 'llm' ? 'Notes filed — AI summary emailed to the patient' : 'Notes filed — summary used the rule-based fallback', source === 'llm' ? 'ok' : 'warn');
    reload();
    showVisitSummary(consultation);
  } catch (e) {
    toast(e.message, 'err');
  }
}
