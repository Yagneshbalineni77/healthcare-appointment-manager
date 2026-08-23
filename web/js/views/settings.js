/** Shared settings page: profile, Google Calendar connection, deployment info. */

import { api } from '../api.js';
import { esc, fmtDateTime, loading, toast, confirmDialog } from '../ui.js';
import { state } from '../app.js';

export async function render(root) {
  const user = state.user;
  root.innerHTML = `<div class="page-head"><div>
      <h1>Settings</h1><div class="sub">Your profile and connected services.</div>
    </div></div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-head"><h3>Profile</h3></div>
        <div class="card-pad">
          <dl class="kv">
            <dt>Name</dt><dd>${esc(user.full_name)}</dd>
            <dt>Email</dt><dd>${esc(user.email)}</dd>
            <dt>Role</dt><dd><span class="badge badge-brand">${esc(user.role)}</span></dd>
            <dt>Phone</dt><dd>${esc(user.phone || '—')}</dd>
            <dt>Member since</dt><dd>${esc(fmtDateTime(user.created_at))}</dd>
          </dl>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>Google Calendar</h3></div>
        <div class="card-pad" id="cal">${loading()}</div>
      </div>

      <div class="card" style="grid-column:1/-1">
        <div class="card-head"><h3>How this deployment is configured</h3></div>
        <div class="card-pad" id="health">${loading()}</div>
      </div>
    </div>`;

  await renderCalendar(root.querySelector('#cal'));

  const health = await api.health();
  root.querySelector('#health').innerHTML = `
    <div class="grid grid-3">
      ${health.integrations.map((i) => `
        <div>
          <div class="row" style="justify-content:space-between">
            <span class="strong">${esc({ llm: 'AI summaries', email: 'Email', google_calendar: 'Google Calendar' }[i.name] || i.name)}</span>
            <span class="badge ${i.configured ? 'badge-green' : 'badge-amber'}">${i.configured ? 'live' : 'fallback'}</span>
          </div>
          <div class="small muted mt1">${esc(i.detail)}</div>
        </div>`).join('')}
    </div>
    <p class="small muted mt2 mb0">
      Environment <b>${esc(health.environment)}</b> · database ${esc(health.database)} ·
      clinic timezone ${esc(health.timezone)} · background worker ${health.worker_enabled ? 'running' : 'off'} ·
      API version ${esc(health.version)}. Full API reference at <a href="/docs" target="_blank" rel="noopener">/docs</a>.
    </p>`;
}

async function renderCalendar(host) {
  let status;
  try { status = await api.calendarStatus(); }
  catch (e) { host.innerHTML = `<div class="banner banner-err">${esc(e.message)}</div>`; return; }

  if (!status.integration_configured) {
    host.innerHTML = `
      <div class="banner banner-warn">
        📆 Google Calendar is not configured on this deployment.
      </div>
      <p class="small muted mt2 mb0">
        Set <code>GOOGLE_CLIENT_ID</code>, <code>GOOGLE_CLIENT_SECRET</code> and <code>GOOGLE_REDIRECT_URI</code>
        to enable it. Booking, email and AI summaries all work without it — calendar tasks are simply skipped,
        which you can see in the admin Operations queue.
      </p>`;
    return;
  }

  if (status.connected) {
    host.innerHTML = `
      <div class="banner banner-ok">✓ Connected${status.google_email ? ` as <b>${esc(status.google_email)}</b>` : ''}</div>
      <p class="small muted mt2">Appointments are added to your calendar automatically, and updated or removed when
        they are rescheduled or cancelled.</p>
      <button class="btn btn-ghost" id="disconnect">Disconnect</button>`;

    host.querySelector('#disconnect').onclick = async () => {
      if (!await confirmDialog('Disconnect Google Calendar?',
        'New appointments will stop syncing. Events already on your calendar stay there.',
        { confirmLabel: 'Disconnect' })) return;
      await api.calendarDisconnect();
      toast('Google Calendar disconnected', 'ok');
      renderCalendar(host);
    };
    return;
  }

  host.innerHTML = `
    <p class="small muted">Connect your Google account and every appointment is added to your calendar,
      then updated or deleted automatically when it changes.</p>
    <p class="tiny muted">We request only <code>calendar.events</code> — permission to manage the events we create,
      and nothing else. You can disconnect at any time.</p>
    <button class="btn btn-primary mt1" id="connect">Connect Google Calendar</button>`;

  host.querySelector('#connect').onclick = async (e) => {
    e.target.disabled = true;
    try { location.href = (await api.calendarConnect()).authorization_url; }
    catch (err) { toast(err.message, 'err'); e.target.disabled = false; }
  };
}
