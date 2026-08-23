/**
 * Thin fetch wrapper around the Clinix JSON API.
 *
 * Responsibilities: attach the bearer token, parse the error envelope the
 * backend guarantees ({detail, code}), and expire the session on a 401 so a
 * stale token never leaves the UI in a half-signed-in state.
 */

const TOKEN_KEY = 'clinix.token';
const USER_KEY = 'clinix.user';

export const auth = {
  get token() { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } },
  get user() {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null'); } catch { return null; }
  },
  save(token, user) {
    try {
      localStorage.setItem(TOKEN_KEY, token);
      localStorage.setItem(USER_KEY, JSON.stringify(user));
    } catch { /* private mode — session lives in memory for this page only */ }
  },
  clear() {
    try { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); } catch {}
  },
  get isAuthed() { return Boolean(this.token && this.user); },
};

/** Error carrying the backend's machine-readable `code`. */
export class ApiError extends Error {
  constructor(message, status, code, payload) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

async function request(method, path, { body, query, auth: needsAuth = true } = {}) {
  const url = new URL(path, window.location.origin);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
    }
  }

  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (needsAuth && auth.token) headers['Authorization'] = `Bearer ${auth.token}`;

  let response;
  try {
    response = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError('Cannot reach the server. Check your connection.', 0, 'NETWORK');
  }

  if (response.status === 204) return null;

  const text = await response.text();
  let data = null;
  if (text) { try { data = JSON.parse(text); } catch { data = { detail: text }; } }

  if (!response.ok) {
    if (response.status === 401 && needsAuth) { auth.clear(); onUnauthorized(); }
    throw new ApiError(
      data?.detail || `Request failed (${response.status})`,
      response.status,
      data?.code || null,
      data,
    );
  }
  return data;
}

const get = (p, o) => request('GET', p, o);
const post = (p, body, o) => request('POST', p, { ...o, body });
const patch = (p, body, o) => request('PATCH', p, { ...o, body });
const del = (p, o) => request('DELETE', p, o);

export const api = {
  // system
  config: () => get('/api/config', { auth: false }),
  health: () => get('/api/health', { auth: false }),

  // auth
  register: (payload) => post('/api/auth/register', payload, { auth: false }),
  login: (payload) => post('/api/auth/login', payload, { auth: false }),
  me: () => get('/api/auth/me'),

  // doctors
  specialisations: () => get('/api/doctors/specialisations', { auth: false }),
  doctors: (query) => get('/api/doctors', { query, auth: false }),
  doctor: (id) => get(`/api/doctors/${id}`, { auth: false }),
  availability: (id, date) => get(`/api/doctors/${id}/availability`, { query: { date } }),
  availabilityRange: (id, from, days) => get(`/api/doctors/${id}/availability-range`, { query: { from, days } }),

  // appointments
  hold: (payload) => post('/api/appointments/hold', payload),
  confirm: (id, payload) => post(`/api/appointments/${id}/confirm`, payload),
  appointments: (query) => get('/api/appointments', { query }),
  appointment: (id) => get(`/api/appointments/${id}`),
  reschedule: (id, payload) => post(`/api/appointments/${id}/reschedule`, payload),
  cancel: (id, payload) => post(`/api/appointments/${id}/cancel`, payload),
  regenerateTriage: (id) => post(`/api/appointments/${id}/previsit-summary/regenerate`, {}),

  // consultations
  createConsultation: (id, payload) => post(`/api/appointments/${id}/consultation`, payload),
  consultation: (id) => get(`/api/appointments/${id}/consultation`),
  regeneratePostvisit: (id) => post(`/api/appointments/${id}/consultation/regenerate-summary`, {}),
  medicationReminders: (query) => get('/api/me/medication-reminders', { query }),
  cancelReminder: (id) => del(`/api/me/medication-reminders/${id}`),

  // admin
  stats: () => get('/api/admin/stats'),
  adminDoctors: () => get('/api/admin/doctors'),
  createDoctor: (payload) => post('/api/admin/doctors', payload),
  updateDoctor: (id, payload) => patch(`/api/admin/doctors/${id}`, payload),
  deactivateDoctor: (id) => del(`/api/admin/doctors/${id}`),
  markLeave: (id, payload) => post(`/api/admin/doctors/${id}/leave`, payload),
  leaves: (id) => get(`/api/admin/doctors/${id}/leaves`),
  deleteLeave: (id) => del(`/api/admin/leaves/${id}`),
  adminAppointments: (query) => get('/api/admin/appointments', { query }),
  notifications: (query) => get('/api/admin/notifications', { query }),
  requeueNotification: (id) => post(`/api/admin/notifications/${id}/requeue`, {}),
  calendarTasks: () => get('/api/admin/calendar-tasks'),
  audit: () => get('/api/admin/audit'),
  runWorker: () => post('/api/admin/worker/run-once', {}),

  // google calendar
  calendarStatus: () => get('/api/calendar/status'),
  calendarConnect: () => get('/api/calendar/connect'),
  calendarDisconnect: () => post('/api/calendar/disconnect', {}),
};
