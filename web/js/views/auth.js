/** Sign-in and patient registration. */

import { api, auth } from '../api.js';
import { el, esc, toast } from '../ui.js';
import { state, setUser, render } from '../app.js';

const DEMO = [
  ['Admin', 'admin@clinix.health', 'Admin@12345'],
  ['Doctor', 'meera.iyer@clinix.health', 'Password@123'],
  ['Patient', 'aarav.sharma@example.com', 'Password@123'],
];

function hero() {
  return `
    <div class="auth-hero">
      <div class="brand" style="color:#fff;margin-bottom:26px">
        <span class="brand-mark" style="background:rgba(255,255,255,.16)">✚</span> Clinix
      </div>
      <h1>Care that starts before the appointment.</h1>
      <p>Patients share symptoms in advance, doctors walk in already briefed, and everyone gets
         confirmations on email and their calendar.</p>
      <div class="mt3 stack" style="gap:14px">
        <div class="auth-feat"><span class="ico">🧠</span><div>
          <b>AI pre-visit triage</b><span>Urgency level, chief complaint and three questions, ready before the visit.</span></div></div>
        <div class="auth-feat"><span class="ico">🔒</span><div>
          <b>No double-booking, ever</b><span>Slots are held while you fill the form and guarded by a database constraint.</span></div></div>
        <div class="auth-feat"><span class="ico">📬</span><div>
          <b>Reliable reminders</b><span>Email and calendar events are queued and retried, never dropped.</span></div></div>
      </div>
    </div>`;
}

function demoBlock() {
  return `
    <div class="demo-creds">
      <b>Demo accounts — click to fill</b>
      ${DEMO.map(([role, email, password]) => `
        <div class="demo-row">
          <span><code>${esc(email)}</code></span>
          <button class="btn btn-sm btn-ghost" data-demo="${esc(email)}|${esc(password)}">${esc(role)}</button>
        </div>`).join('')}
    </div>`;
}

async function afterAuth(result) {
  auth.save(result.access_token, result.user);
  setUser(result.user);
  const home = { patient: '#/book', doctor: '#/schedule', admin: '#/dashboard' }[result.user.role];
  if (location.hash === home) await render(); else location.hash = home;
}

function wireDemo(root, form) {
  root.querySelectorAll('[data-demo]').forEach((button) => {
    button.onclick = () => {
      const [email, password] = button.dataset.demo.split('|');
      form.email.value = email;
      form.password.value = password;
      form.querySelector('button[type=submit]').focus();
    };
  });
}

function submitting(button, on, labelBusy, labelIdle) {
  button.disabled = on;
  button.innerHTML = on ? `<span class="spinner" style="border-top-color:#fff"></span> ${labelBusy}` : labelIdle;
}

export function login(root) {
  const node = el(`
    <div class="auth-wrap">
      ${hero()}
      <div class="auth-form"><div class="auth-box">
        <h1>Sign in</h1>
        <p class="muted">Welcome back to ${esc(state.config?.clinic_name || 'the clinic')}.</p>
        <form id="f" class="mt2" novalidate>
          <div class="field"><label for="email">Email</label>
            <input class="input" id="email" name="email" type="email" required autocomplete="email" placeholder="you@example.com"></div>
          <div class="field"><label for="password">Password</label>
            <input class="input" id="password" name="password" type="password" required autocomplete="current-password" placeholder="••••••••"></div>
          <div id="err"></div>
          <button class="btn btn-primary btn-block btn-lg mt1" type="submit">Sign in</button>
        </form>
        <p class="mt2 small center muted">New patient? <a href="#/register">Create an account</a></p>
        ${demoBlock()}
      </div></div>
    </div>`);

  const form = node.querySelector('#f');
  const errorBox = node.querySelector('#err');
  wireDemo(node, form);

  form.onsubmit = async (event) => {
    event.preventDefault();
    errorBox.innerHTML = '';
    const button = form.querySelector('button[type=submit]');
    submitting(button, true, 'Signing in…', 'Sign in');
    try {
      await afterAuth(await api.login({ email: form.email.value.trim(), password: form.password.value }));
    } catch (error) {
      errorBox.innerHTML = `<div class="banner banner-err mb2">⚠ ${esc(error.message)}</div>`;
      submitting(button, false, '', 'Sign in');
    }
  };

  root.appendChild(node);
}

export function register(root) {
  const node = el(`
    <div class="auth-wrap">
      ${hero()}
      <div class="auth-form"><div class="auth-box">
        <h1>Create your account</h1>
        <p class="muted">Patients register here. Doctor accounts are created by the clinic administrator.</p>
        <form id="f" class="mt2" novalidate>
          <div class="field"><label for="full_name">Full name</label>
            <input class="input" id="full_name" name="full_name" required minlength="2" placeholder="Aarav Sharma"></div>
          <div class="field"><label for="email">Email</label>
            <input class="input" id="email" name="email" type="email" required autocomplete="email" placeholder="you@example.com"></div>
          <div class="field-row">
            <div class="field"><label for="phone">Phone <span class="muted">(optional)</span></label>
              <input class="input" id="phone" name="phone" placeholder="+91 90000 00000"></div>
            <div class="field"><label for="date_of_birth">Date of birth <span class="muted">(optional)</span></label>
              <input class="input" id="date_of_birth" name="date_of_birth" type="date"></div>
          </div>
          <div class="field"><label for="gender">Sex <span class="muted">(optional)</span></label>
            <select class="select" id="gender" name="gender">
              <option value="">Prefer not to say</option><option>Female</option><option>Male</option><option>Other</option>
            </select>
            <span class="help">Shared with your doctor to help interpret your symptoms.</span></div>
          <div class="field"><label for="password">Password</label>
            <input class="input" id="password" name="password" type="password" required minlength="8" autocomplete="new-password" placeholder="At least 8 characters"></div>
          <div id="err"></div>
          <button class="btn btn-primary btn-block btn-lg mt1" type="submit">Create account</button>
        </form>
        <p class="mt2 small center muted">Already registered? <a href="#/login">Sign in</a></p>
      </div></div>
    </div>`);

  const form = node.querySelector('#f');
  const errorBox = node.querySelector('#err');

  form.onsubmit = async (event) => {
    event.preventDefault();
    errorBox.innerHTML = '';
    const button = form.querySelector('button[type=submit]');
    submitting(button, true, 'Creating…', 'Create account');
    const data = Object.fromEntries(new FormData(form));
    for (const key of ['phone', 'date_of_birth', 'gender']) if (!data[key]) delete data[key];
    try {
      await afterAuth(await api.register(data));
      toast('Welcome to Clinix', 'ok');
    } catch (error) {
      errorBox.innerHTML = `<div class="banner banner-err mb2">⚠ ${esc(error.message)}</div>`;
      submitting(button, false, '', 'Create account');
    }
  };

  root.appendChild(node);
}
