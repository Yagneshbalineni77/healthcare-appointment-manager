/** Admin portal: dashboard, doctor & leave management, operational queues. */

import { api } from '../api.js';
import {
  confirmDialog, el, empty, esc, fmtDate, fmtDateTime, isoDate, jobBadge,
  loading, modal, statusBadge, toast, urgencyBadge,
} from '../ui.js';

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

/* ==========================================================================
   Dashboard
   ========================================================================== */
export async function dashboard(root) {
  root.innerHTML = `<div class="page-head"><div>
      <h1>Clinic dashboard</h1><div class="sub">Live view of bookings, notifications and AI health.</div>
    </div><div class="page-head-actions">
      <button class="btn btn-ghost btn-sm" id="tick">▶ Run background worker</button>
    </div></div><div id="body">${loading()}</div>`;

  const body = root.querySelector('#body');

  root.querySelector('#tick').onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = 'Running…';
    try {
      const { report } = await api.runWorker();
      const sent = report.email?.sent ?? 0;
      toast(`Worker ran — ${sent} email(s) delivered, ${report.holds_expired ?? 0} hold(s) expired`, 'ok');
      load();
    } catch (err) { toast(err.message, 'err'); }
    e.target.disabled = false; e.target.textContent = '▶ Run background worker';
  };

  async function load() {
    body.innerHTML = loading();
    const [stats, health, appointments] = await Promise.all([
      api.stats(), api.health(), api.adminAppointments({ limit: 12 }),
    ]);

    const integration = (name) => health.integrations.find((i) => i.name === name) || {};
    const chip = (i) => `<span class="badge ${i.configured ? 'badge-green' : 'badge-amber'}">${i.configured ? 'live' : 'fallback'}</span>`;

    body.innerHTML = `
      <div class="grid grid-4 mb2">
        ${stat('Patients', stats.patients, 'registered')}
        ${stat('Doctors', stats.doctors, 'on the roster')}
        ${stat('Upcoming', stats.appointments_upcoming, 'confirmed appointments')}
        ${stat('Today', stats.appointments_today, 'in the clinic today')}
      </div>
      <div class="grid grid-4 mb3">
        ${stat('Total booked', stats.appointments_total, 'all time')}
        ${stat('Cancellations', stats.cancellations, 'all time')}
        ${stat('Emails queued', stats.notifications_pending, stats.notifications_dead ? `${stats.notifications_dead} need attention` : 'nothing stuck', stats.notifications_dead ? 'var(--red-600)' : '')}
        ${stat('AI fallback rate', `${Math.round(stats.llm_fallback_rate * 100)}%`, 'summaries from rules, not the model', stats.llm_fallback_rate > 0.5 ? 'var(--amber-600)' : '')}
      </div>

      <div class="grid grid-2">
        <div class="card">
          <div class="card-head"><h3>Integrations</h3><div class="right small muted">v${esc(health.version)} · ${esc(health.environment)}</div></div>
          <div class="card-pad stack" style="gap:12px">
            ${health.integrations.map((i) => `
              <div>
                <div class="row" style="justify-content:space-between">
                  <span class="strong">${esc({ llm: 'AI summaries', email: 'Email delivery', google_calendar: 'Google Calendar' }[i.name] || i.name)}</span>
                  ${chip(i)}
                </div>
                <div class="small muted">${esc(i.detail)}</div>
              </div>`).join('')}
            <div class="row" style="justify-content:space-between">
              <span class="strong">Background worker</span>
              <span class="badge ${health.worker_enabled ? 'badge-green' : 'badge-red'}">${health.worker_enabled ? 'running' : 'off'}</span>
            </div>
            <div class="row" style="justify-content:space-between">
              <span class="strong">Database</span><span class="badge badge-gray">${esc(health.database)}</span>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h3>Latest appointments</h3></div>
          <div class="table-wrap"><table class="data">
            <thead><tr><th>When</th><th>Patient</th><th>Doctor</th><th>Status</th></tr></thead>
            <tbody>${appointments.length ? appointments.map((a) => `
              <tr><td class="small nowrap">${esc(a.start_at_local)}</td>
                  <td class="small">${esc(a.patient.full_name)}</td>
                  <td class="small muted">${esc(a.doctor.full_name)}</td>
                  <td>${statusBadge(a.status)}</td></tr>`).join('')
              : '<tr><td colspan="4" class="muted center">No appointments yet</td></tr>'}
            </tbody></table></div>
        </div>
      </div>`;
  }

  await load();
}

function stat(label, value, hint, colour = '') {
  return `<div class="card stat"><div class="label">${esc(label)}</div>
    <div class="value" ${colour ? `style="color:${colour}"` : ''}>${esc(value)}</div>
    <div class="hint">${esc(hint)}</div></div>`;
}

/* ==========================================================================
   Doctors & leave
   ========================================================================== */
export async function doctors(root) {
  root.innerHTML = `<div class="page-head"><div>
      <h1>Doctors &amp; leave</h1>
      <div class="sub">Create profiles, set working hours, and mark leave days safely.</div>
    </div><div class="page-head-actions">
      <button class="btn btn-primary btn-sm" id="add">+ Add doctor</button>
    </div></div><div id="list">${loading()}</div>`;

  const list = root.querySelector('#list');
  root.querySelector('#add').onclick = () => doctorForm(null, load);

  async function load() {
    list.innerHTML = loading();
    const rows = await api.adminDoctors();
    if (!rows.length) { list.innerHTML = empty('👩‍⚕️', 'No doctors yet', 'Add your first doctor to start taking bookings.'); return; }

    list.innerHTML = `<div class="stack">${rows.map((d) => {
      const hours = groupHours(d.working_hours);
      return `
      <div class="card card-pad">
        <div class="row" style="align-items:flex-start;gap:14px">
          <div class="brand-mark" style="width:42px;height:42px;border-radius:12px;font-size:16px;${d.is_active ? '' : 'background:var(--slate-400)'}">
            ${esc((d.full_name || '').replace(/^Dr\.?\s*/i, '').charAt(0))}</div>
          <div style="flex:1;min-width:0">
            <div class="row" style="gap:7px">
              <h3>${esc(d.full_name)}</h3>
              ${d.is_active ? '' : '<span class="badge badge-gray">deactivated</span>'}
              ${d.is_accepting_patients ? '' : '<span class="badge badge-amber">not accepting</span>'}
              ${d.calendar_connected ? '<span class="badge badge-blue">📆 calendar</span>' : ''}
            </div>
            <div class="small muted">${esc(d.specialisation)}${d.qualifications ? ` · ${esc(d.qualifications)}` : ''} · ${esc(d.email)}</div>
            <div class="small muted mt1">
              ${esc(d.slot_duration_minutes)} min slots${d.buffer_minutes ? ` + ${esc(d.buffer_minutes)} min buffer` : ''}
              ${d.room ? ` · Room ${esc(d.room)}` : ''} · ₹${(d.consultation_fee || 0).toLocaleString('en-IN')}
            </div>
            <div class="mt1 small">${hours || '<span class="muted">No working hours set — this doctor has no bookable slots.</span>'}</div>
            ${d.upcoming_leaves?.length ? `<div class="row mt1" style="gap:5px">
              ${d.upcoming_leaves.slice(0, 6).map((l) => `<span class="badge badge-red">🏖 ${esc(fmtDate(l))}</span>`).join('')}</div>` : ''}
          </div>
          <div class="stack" style="gap:6px;min-width:140px">
            <button class="btn btn-sm btn-ghost" data-edit="${d.id}">Edit profile</button>
            <button class="btn btn-sm btn-ghost" data-leave="${d.id}" data-name="${esc(d.full_name)}">Mark leave</button>
            <button class="btn btn-sm btn-ghost" data-leaves="${d.id}" data-name="${esc(d.full_name)}">Leave history</button>
            ${d.is_active ? `<button class="btn btn-sm btn-danger" data-off="${d.id}" data-name="${esc(d.full_name)}">Deactivate</button>` : ''}
          </div>
        </div>
      </div>`;
    }).join('')}</div>`;

    list.querySelectorAll('[data-edit]').forEach((b) => {
      b.onclick = () => doctorForm(rows.find((d) => d.id === Number(b.dataset.edit)), load);
    });
    list.querySelectorAll('[data-leave]').forEach((b) => {
      b.onclick = () => leaveFlow(Number(b.dataset.leave), b.dataset.name, load);
    });
    list.querySelectorAll('[data-leaves]').forEach((b) => {
      b.onclick = () => leaveHistory(Number(b.dataset.leaves), b.dataset.name, load);
    });
    list.querySelectorAll('[data-off]').forEach((b) => {
      b.onclick = async () => {
        if (!await confirmDialog(`Deactivate ${b.dataset.name}?`,
          'They disappear from search and take no new bookings. Existing appointments and history are kept.',
          { confirmLabel: 'Deactivate' })) return;
        await api.deactivateDoctor(b.dataset.off);
        toast('Doctor deactivated', 'ok'); load();
      };
    });
  }

  await load();
}

function groupHours(hours = []) {
  if (!hours.length) return '';
  const byDay = new Map();
  for (const h of hours) {
    if (!byDay.has(h.weekday)) byDay.set(h.weekday, []);
    byDay.get(h.weekday).push(`${h.start_time.slice(0, 5)}–${h.end_time.slice(0, 5)}`);
  }
  return [...byDay.entries()].sort((a, b) => a[0] - b[0])
    .map(([d, w]) => `<span class="badge badge-gray">${WEEKDAYS[d].slice(0, 3)} ${w.join(', ')}</span>`).join(' ');
}

/* ---------- create / edit doctor ---------- */
async function doctorForm(doctor, reload) {
  const editing = Boolean(doctor);
  const existing = new Map();
  for (const h of doctor?.working_hours || []) {
    if (!existing.has(h.weekday)) existing.set(h.weekday, []);
    existing.get(h.weekday).push(h);
  }

  const dayRow = (index) => {
    const windows = existing.get(index) || [];
    const on = windows.length > 0;
    const first = windows[0] || {};
    const second = windows[1] || {};
    return `
      <tr>
        <td><label class="checkbox"><input type="checkbox" name="on_${index}" ${on ? 'checked' : ''}>
          <span>${WEEKDAYS[index].slice(0, 3)}</span></label></td>
        <td><input class="input" type="time" name="s1_${index}" value="${esc((first.start_time || '09:00').slice(0, 5))}"></td>
        <td><input class="input" type="time" name="e1_${index}" value="${esc((first.end_time || '13:00').slice(0, 5))}"></td>
        <td><input class="input" type="time" name="s2_${index}" value="${esc(second.start_time ? second.start_time.slice(0, 5) : '')}"></td>
        <td><input class="input" type="time" name="e2_${index}" value="${esc(second.end_time ? second.end_time.slice(0, 5) : '')}"></td>
      </tr>`;
  };

  const form = el(`
    <form>
      <div class="field-row">
        <div class="field"><label>Full name *</label>
          <input class="input" name="full_name" required value="${esc(doctor?.full_name || '')}" placeholder="Dr. Meera Iyer"></div>
        <div class="field"><label>Specialisation *</label>
          <input class="input" name="specialisation" required value="${esc(doctor?.specialisation || '')}" placeholder="Cardiology"></div>
      </div>
      ${editing ? '' : `
      <div class="field-row">
        <div class="field"><label>Login email *</label><input class="input" type="email" name="email" required placeholder="doctor@clinix.health"></div>
        <div class="field"><label>Temporary password *</label><input class="input" name="password" required minlength="8" placeholder="At least 8 characters"></div>
      </div>`}
      <div class="field-row">
        <div class="field"><label>Phone</label><input class="input" name="phone" value="${esc(doctor?.phone || '')}"></div>
        <div class="field"><label>Qualifications</label><input class="input" name="qualifications" value="${esc(doctor?.qualifications || '')}" placeholder="MBBS, MD"></div>
      </div>
      <div class="field"><label>Bio</label>
        <textarea class="textarea" name="bio" style="min-height:64px" placeholder="Shown to patients when they search.">${esc(doctor?.bio || '')}</textarea></div>
      <div class="field-row">
        <div class="field"><label>Slot duration (min) *</label>
          <input class="input" type="number" name="slot_duration_minutes" min="5" max="240" value="${doctor?.slot_duration_minutes ?? 30}"></div>
        <div class="field"><label>Buffer between slots (min)</label>
          <input class="input" type="number" name="buffer_minutes" min="0" max="120" value="${doctor?.buffer_minutes ?? 0}"></div>
      </div>
      <div class="field-row">
        <div class="field"><label>Consultation fee (₹)</label>
          <input class="input" type="number" name="consultation_fee" min="0" value="${doctor?.consultation_fee ?? 0}"></div>
        <div class="field"><label>Experience (years)</label>
          <input class="input" type="number" name="experience_years" min="0" max="70" value="${doctor?.experience_years ?? 0}"></div>
      </div>
      <div class="field"><label>Room</label><input class="input" name="room" value="${esc(doctor?.room || '')}" placeholder="C-204"></div>
      ${editing ? `<label class="checkbox mb2"><input type="checkbox" name="is_accepting_patients" ${doctor.is_accepting_patients ? 'checked' : ''}>
        <span>Accepting new bookings</span></label>` : ''}
      <div class="field">
        <label>Weekly working hours</label>
        <div class="card table-wrap"><table class="data">
          <thead><tr><th>Day</th><th>Morning from</th><th>to</th><th>Evening from</th><th>to</th></tr></thead>
          <tbody>${WEEKDAYS.map((_, i) => dayRow(i)).join('')}</tbody>
        </table></div>
        <span class="help">Leave the evening pair blank for a single shift. Slots are generated from these windows.</span>
      </div>
    </form>`);

  const submitted = await modal({
    title: editing ? `Edit ${doctor.full_name}` : 'Add a doctor', wide: true,
    body: form, confirmLabel: editing ? 'Save changes' : 'Create doctor',
  });
  if (!submitted) return;

  const value = (name) => form.querySelector(`[name="${name}"]`)?.value?.trim() ?? '';
  const checked = (name) => form.querySelector(`[name="${name}"]`)?.checked ?? false;

  const working_hours = [];
  WEEKDAYS.forEach((_, i) => {
    if (!checked(`on_${i}`)) return;
    for (const [s, e] of [[`s1_${i}`, `e1_${i}`], [`s2_${i}`, `e2_${i}`]]) {
      const start = value(s); const end = value(e);
      if (start && end && start < end) working_hours.push({ weekday: i, start_time: `${start}:00`, end_time: `${end}:00` });
    }
  });

  const payload = {
    full_name: value('full_name'), specialisation: value('specialisation'),
    phone: value('phone') || null, qualifications: value('qualifications') || null, bio: value('bio') || null,
    slot_duration_minutes: Number(value('slot_duration_minutes')) || 30,
    buffer_minutes: Number(value('buffer_minutes')) || 0,
    consultation_fee: Number(value('consultation_fee')) || 0,
    experience_years: Number(value('experience_years')) || 0,
    room: value('room') || null, working_hours,
  };

  try {
    if (editing) {
      payload.is_accepting_patients = checked('is_accepting_patients');
      await api.updateDoctor(doctor.id, payload);
      toast('Doctor updated', 'ok');
    } else {
      payload.email = value('email'); payload.password = value('password');
      await api.createDoctor(payload);
      toast('Doctor created', 'ok');
    }
    reload();
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------- leave: dry run then apply ---------- */
async function leaveFlow(doctorId, name, reload) {
  const picked = await modal({
    title: `Mark ${name} on leave`,
    body: `<form>
        <div class="field"><label>Leave date *</label>
          <input class="input" type="date" name="leave_date" required min="${isoDate(new Date())}" value="${isoDate(new Date())}"></div>
        <div class="field"><label>Reason <span class="muted">(shown to affected patients)</span></label>
          <input class="input" name="reason" placeholder="Conference / personal leave"></div>
      </form>
      <p class="small muted">We will first show you exactly who is affected. Nothing changes until you confirm.</p>`,
    confirmLabel: 'Check impact',
  });
  if (!picked) return;

  let impact;
  try { impact = await api.markLeave(doctorId, { leave_date: picked.leave_date, reason: picked.reason || null, confirm: false }); }
  catch (e) { toast(e.message, 'err'); return; }

  const rows = impact.affected.map((a) => `
    <tr><td class="mono tiny">${esc(a.reference)}</td><td>${esc(a.patient_name)}</td>
        <td class="small muted">${esc(a.patient_email)}</td><td class="small nowrap">${esc(a.start_at_local)}</td></tr>`).join('');

  const confirmed = await modal({
    title: impact.affected_count ? `${impact.affected_count} appointment(s) will be cancelled` : 'No appointments affected',
    wide: Boolean(impact.affected_count),
    body: `
      <div class="banner ${impact.affected_count ? 'banner-warn' : 'banner-ok'} mb2">
        ${impact.affected_count ? '⚠' : '✓'} ${esc(impact.message)}
      </div>
      ${impact.affected_count ? `
        <div class="card table-wrap"><table class="data">
          <thead><tr><th>Reference</th><th>Patient</th><th>Email</th><th>Appointment</th></tr></thead>
          <tbody>${rows}</tbody></table></div>
        <p class="small muted mt2">Each patient gets an apology email with a link to rebook, and their calendar event is removed.
           The cancellations and the emails are committed in one transaction — all or nothing.</p>` : ''}`,
    confirmLabel: impact.affected_count ? `Cancel ${impact.affected_count} and apply leave` : 'Apply leave',
    cancelLabel: 'Back', danger: Boolean(impact.affected_count),
  });
  if (!confirmed) return;

  try {
    const applied = await api.markLeave(doctorId, { leave_date: picked.leave_date, reason: picked.reason || null, confirm: true });
    toast(applied.message, 'ok');
    reload();
  } catch (e) { toast(e.message, 'err'); }
}

async function leaveHistory(doctorId, name, reload) {
  const rows = await api.leaves(doctorId);
  await modal({
    title: `Leave history — ${name}`, wide: true, confirmLabel: 'Close', cancelLabel: '',
    body: rows.length ? `
      <div class="card table-wrap"><table class="data">
        <thead><tr><th>Date</th><th>Reason</th><th>Cancelled</th><th>Marked on</th><th></th></tr></thead>
        <tbody>${rows.map((l) => `
          <tr><td class="strong nowrap">${esc(fmtDate(l.leave_date))}</td>
              <td class="small">${esc(l.reason || '—')}</td>
              <td>${l.affected_appointment_count ? `<span class="badge badge-red">${l.affected_appointment_count}</span>` : '<span class="muted tiny">none</span>'}</td>
              <td class="small muted nowrap">${esc(fmtDate(l.created_at))}</td>
              <td><button class="btn btn-sm btn-ghost" data-del="${l.id}">Remove</button></td></tr>`).join('')}
        </tbody></table></div>
      <p class="small muted mt2">Removing a leave reopens those slots. Patients already told their appointment was cancelled are not restored — they rebook.</p>`
      : '<p class="muted">No leave has been recorded for this doctor.</p>',
  });

  document.querySelectorAll('[data-del]').forEach((b) => {
    b.onclick = async () => {
      await api.deleteLeave(b.dataset.del);
      toast('Leave removed — slots reopened', 'ok');
      document.querySelector('.modal-backdrop')?.remove();
      reload();
    };
  });
}

/* ==========================================================================
   Operations
   ========================================================================== */
export async function operations(root) {
  root.innerHTML = `<div class="page-head"><div>
      <h1>Operations</h1>
      <div class="sub">Email outbox, calendar queue and the audit trail — every side effect is inspectable.</div>
    </div><div class="page-head-actions">
      <button class="btn btn-ghost btn-sm" id="tick">▶ Run worker now</button>
    </div></div>
    <div class="tabs">
      <button class="tab active" data-t="email">Email outbox</button>
      <button class="tab" data-t="calendar">Calendar queue</button>
      <button class="tab" data-t="audit">Audit trail</button>
    </div>
    <div id="body">${loading()}</div>`;

  const body = root.querySelector('#body');
  let tab = 'email';

  root.querySelector('#tick').onclick = async (e) => {
    e.target.disabled = true;
    try { const { report } = await api.runWorker(); toast(`Worker ran: ${report.email?.sent ?? 0} sent, ${report.email?.retrying ?? 0} retrying`, 'ok'); load(); }
    catch (err) { toast(err.message, 'err'); }
    e.target.disabled = false;
  };

  async function load() {
    body.innerHTML = loading();
    if (tab === 'email') {
      const rows = await api.notifications({ limit: 150 });
      body.innerHTML = rows.length ? `
        <div class="banner banner-info mb2">
          📮 Emails are written to this outbox inside the same transaction as the booking, then delivered by the
          background worker with exponential backoff. A provider outage delays a message; it never fails a booking.
        </div>
        <div class="card table-wrap"><table class="data">
          <thead><tr><th>Template</th><th>To</th><th>Subject</th><th>Status</th><th>Attempts</th><th>Last error</th><th></th></tr></thead>
          <tbody>${rows.map((n) => `
            <tr>
              <td class="tiny mono">${esc(n.template)}</td>
              <td class="small">${esc(n.to_email)}</td>
              <td class="small muted truncate" style="max-width:260px">${esc(n.subject)}</td>
              <td>${jobBadge(n.status)}</td>
              <td class="small">${n.attempts}/${n.max_attempts}</td>
              <td class="tiny truncate" style="color:var(--red-600);max-width:200px">${esc((n.last_error || '').slice(0, 90))}</td>
              <td>${n.status === 'dead' ? `<button class="btn btn-sm btn-ghost" data-requeue="${n.id}">Retry</button>` : ''}</td>
            </tr>`).join('')}</tbody></table></div>`
        : empty('📮', 'Outbox is empty', 'No notifications have been queued yet.');

      body.querySelectorAll('[data-requeue]').forEach((b) => {
        b.onclick = async () => { await api.requeueNotification(b.dataset.requeue); toast('Requeued for delivery', 'ok'); load(); };
      });

    } else if (tab === 'calendar') {
      const rows = await api.calendarTasks();
      body.innerHTML = rows.length ? `
        <div class="banner banner-info mb2">
          📆 Calendar writes use the same outbox pattern. Tasks show <b>cancelled</b> when that user has not connected
          Google Calendar — expected, not an error.
        </div>
        <div class="card table-wrap"><table class="data">
          <thead><tr><th>Action</th><th>Appointment</th><th>Whose calendar</th><th>Status</th><th>Attempts</th><th>Event id / error</th></tr></thead>
          <tbody>${rows.map((t) => `
            <tr><td><span class="badge badge-gray">${esc(t.action)}</span></td>
                <td class="small">#${t.appointment_id}</td>
                <td class="small">${esc(t.role)}</td>
                <td>${jobBadge(t.status)}</td>
                <td class="small">${t.attempts}</td>
                <td class="tiny muted truncate" style="max-width:280px">${esc(t.external_event_id || t.last_error || '—')}</td></tr>`).join('')}
          </tbody></table></div>`
        : empty('📆', 'No calendar tasks', 'Calendar events are queued when appointments are booked.');

    } else {
      const rows = await api.audit();
      body.innerHTML = rows.length ? `
        <div class="card table-wrap"><table class="data">
          <thead><tr><th>When</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead>
          <tbody>${rows.map((a) => `
            <tr><td class="small nowrap muted">${esc(fmtDateTime(a.created_at))}</td>
                <td><span class="badge badge-gray">${esc(a.action)}</span></td>
                <td class="small">${esc(a.entity_type)} #${esc(a.entity_id)}</td>
                <td class="tiny mono muted truncate" style="max-width:340px">${esc(JSON.stringify(a.meta || {}))}</td></tr>`).join('')}
          </tbody></table></div>`
        : empty('📜', 'No audit entries', 'Administrative actions appear here.');
    }
  }

  root.querySelectorAll('.tab').forEach((t) => {
    t.onclick = () => {
      root.querySelectorAll('.tab').forEach((x) => x.classList.remove('active'));
      t.classList.add('active'); tab = t.dataset.t; load();
    };
  });
  await load();
}
