/**
 * Application shell + hash router.
 *
 * Hash routing (`#/book`) rather than the History API, so the whole SPA can be
 * served by a single static mount with no server-side rewrite rule — which is
 * what keeps deployment to one process on a free tier.
 */

import { api, auth, ApiError, setUnauthorizedHandler } from './api.js';
import { el, esc, toast } from './ui.js';
import * as authView from './views/auth.js';
import * as patientView from './views/patient.js';
import * as doctorView from './views/doctor.js';
import * as adminView from './views/admin.js';
import * as settingsView from './views/settings.js';

export const state = { config: null, user: null };

const NAV = {
  patient: [
    { hash: '#/book', icon: '🔍', label: 'Find a doctor' },
    { hash: '#/appointments', icon: '📅', label: 'My appointments' },
    { hash: '#/medications', icon: '💊', label: 'My medicines' },
    { hash: '#/settings', icon: '⚙️', label: 'Settings' },
  ],
  doctor: [
    { hash: '#/schedule', icon: '🩺', label: 'My schedule' },
    { hash: '#/appointments', icon: '📅', label: 'All appointments' },
    { hash: '#/settings', icon: '⚙️', label: 'Settings' },
  ],
  admin: [
    { hash: '#/dashboard', icon: '📊', label: 'Dashboard' },
    { hash: '#/doctors', icon: '👩‍⚕️', label: 'Doctors & leave' },
    { hash: '#/operations', icon: '📮', label: 'Operations' },
    { hash: '#/settings', icon: '⚙️', label: 'Settings' },
  ],
};

const ROUTES = {
  patient: {
    '/book': patientView.book,
    '/appointments': patientView.appointments,
    '/medications': patientView.medications,
    '/settings': settingsView.render,
  },
  doctor: {
    '/schedule': doctorView.schedule,
    '/appointments': doctorView.appointments,
    '/settings': settingsView.render,
  },
  admin: {
    '/dashboard': adminView.dashboard,
    '/doctors': adminView.doctors,
    '/operations': adminView.operations,
    '/settings': settingsView.render,
  },
};

const HOME = { patient: '#/book', doctor: '#/schedule', admin: '#/dashboard' };

export function navigate(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

export async function logout() {
  auth.clear();
  state.user = null;
  location.hash = '#/login';
}

function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [path, queryString] = raw.split('?');
  return { path: path || '/', params: new URLSearchParams(queryString || '') };
}

function shell(user, contentHost) {
  const items = NAV[user.role] || [];
  const { path } = parseHash();
  const initials = (user.full_name || '?').split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();

  const node = el(`
    <div>
      <header class="topbar">
        <button class="btn btn-ghost btn-sm menu-btn" id="menu-btn" aria-label="Toggle navigation">☰</button>
        <div class="brand"><span class="brand-mark">✚</span> <span>Clinix</span></div>
        <div class="topbar-spacer"></div>
        <span class="badge badge-brand">${esc(user.role)}</span>
        <div class="row" style="gap:8px">
          <div class="brand-mark" style="background:var(--slate-700);font-size:12px;font-weight:700">${esc(initials)}</div>
          <div class="tiny" style="line-height:1.3">
            <div class="strong">${esc(user.full_name)}</div>
            <div class="muted truncate" style="max-width:170px">${esc(user.email)}</div>
          </div>
        </div>
        <button class="btn btn-ghost btn-sm" id="logout-btn">Sign out</button>
      </header>
      <div class="shell">
        <nav class="sidebar" id="sidebar">
          <div class="nav-section">${esc(user.role === 'admin' ? 'Administration' : user.role === 'doctor' ? 'Clinical' : 'My care')}</div>
          ${items.map((i) => `
            <a class="nav-item ${path === i.hash.slice(1) ? 'active' : ''}" href="${i.hash}">
              <span class="ico">${i.icon}</span> ${esc(i.label)}
            </a>`).join('')}
          <div style="flex:1"></div>
          <a class="nav-item" href="/docs" target="_blank" rel="noopener"><span class="ico">📖</span> API docs</a>
        </nav>
        <main><div class="container" id="view"></div></main>
      </div>
    </div>`);

  node.querySelector('#logout-btn').onclick = logout;
  node.querySelector('#menu-btn').onclick = () => node.querySelector('#sidebar').classList.toggle('open');
  node.querySelector('#sidebar').addEventListener('click', (e) => {
    if (e.target.closest('.nav-item')) node.querySelector('#sidebar').classList.remove('open');
  });
  node.querySelector('#view').appendChild(contentHost);
  return node;
}

export async function render() {
  const app = document.getElementById('app');
  const { path, params } = parseHash();
  const user = state.user;

  // ---- unauthenticated ------------------------------------------------
  if (!user) {
    app.innerHTML = '';
    const host = document.createElement('div');
    app.appendChild(host);
    if (path === '/register') authView.register(host);
    else authView.login(host);
    return;
  }

  // ---- authenticated --------------------------------------------------
  const routes = ROUTES[user.role] || {};
  if (!routes[path]) { location.hash = HOME[user.role] || '#/settings'; return; }

  const host = document.createElement('div');
  app.innerHTML = '';
  app.appendChild(shell(user, host));

  try {
    await routes[path](host, params);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return;
    console.error(error);
    host.innerHTML = `<div class="banner banner-err">⚠ ${esc(error.message || 'Something went wrong.')}</div>`;
    toast(error.message || 'Something went wrong', 'err');
  }
}

async function boot() {
  // A 401 means the token expired or was signed with a previous JWT_SECRET.
  // Without this the failing view would sit on its spinner forever, because
  // render() deliberately swallows 401s (the session is already gone, so a
  // red error banner would be noise). Bounce to sign-in and say why.
  setUnauthorizedHandler(() => {
    const wasSignedIn = state.user !== null;
    state.user = null;
    if (wasSignedIn) toast('Your session has expired — please sign in again.', 'err');
    if (location.hash.startsWith('#/login')) render();
    else location.hash = '#/login';
  });

  try {
    state.config = await api.config();
    document.title = state.config.app_name || 'Clinix';
  } catch {
    document.getElementById('app').innerHTML =
      '<div class="loading">⚠ Cannot reach the Clinix API. Is the server running?</div>';
    return;
  }

  // Re-validate a stored token so a stale session cannot render a broken shell.
  if (auth.token) {
    try { state.user = await api.me(); }
    catch { auth.clear(); state.user = null; }
  }

  // Google Calendar bounces back here with ?calendar=connected|error
  const flag = new URLSearchParams(location.hash.split('?')[1] || '').get('calendar');
  if (flag === 'connected') toast('Google Calendar connected', 'ok');
  else if (flag === 'error') toast('Could not connect Google Calendar. Please try again.', 'err');

  window.addEventListener('hashchange', render);
  await render();
}

export function setUser(user) { state.user = user; }

boot();
