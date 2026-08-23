/** Small DOM/formatting helpers shared by every view. No framework. */

/** Escape untrusted text before it goes into innerHTML. */
export function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

export function el(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

export function toast(message, kind = '') {
  const node = el(`<div class="toast ${kind}"><span>${esc(message)}</span></div>`);
  document.getElementById('toasts').appendChild(node);
  setTimeout(() => {
    node.style.transition = 'opacity .25s, transform .25s';
    node.style.opacity = '0';
    node.style.transform = 'translateX(18px)';
    setTimeout(() => node.remove(), 250);
  }, kind === 'err' ? 6000 : 3800);
}

/** Promise-based modal. Resolves with the form data, or null if dismissed. */
export function modal({ title, body, confirmLabel = 'Confirm', cancelLabel = 'Cancel', danger = false, wide = false }) {
  return new Promise((resolve) => {
    const backdrop = el(`
      <div class="modal-backdrop">
        <div class="modal" style="${wide ? 'width:min(760px,100%)' : ''}" role="dialog" aria-modal="true">
          <div class="modal-head"><h2>${esc(title)}</h2></div>
          <div class="modal-body"></div>
          <div class="modal-foot">
            <button class="btn btn-ghost" data-act="cancel">${esc(cancelLabel)}</button>
            <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-act="ok">${esc(confirmLabel)}</button>
          </div>
        </div>
      </div>`);

    const bodyEl = backdrop.querySelector('.modal-body');
    if (typeof body === 'string') bodyEl.innerHTML = body; else bodyEl.appendChild(body);

    const close = (value) => { backdrop.remove(); document.removeEventListener('keydown', onKey); resolve(value); };
    const onKey = (e) => { if (e.key === 'Escape') close(null); };

    backdrop.querySelector('[data-act=cancel]').onclick = () => close(null);
    backdrop.querySelector('[data-act=ok]').onclick = () => {
      const form = bodyEl.querySelector('form');
      close(form ? Object.fromEntries(new FormData(form)) : true);
    };
    backdrop.onclick = (e) => { if (e.target === backdrop) close(null); };
    document.addEventListener('keydown', onKey);

    document.getElementById('modal-root').appendChild(backdrop);
    setTimeout(() => bodyEl.querySelector('input,textarea,select')?.focus(), 40);
  });
}

export const confirmDialog = (title, message, { confirmLabel = 'Confirm', danger = true } = {}) =>
  modal({ title, body: `<p class="muted">${esc(message)}</p>`, confirmLabel, danger }).then(Boolean);

/* ---------------- formatting ---------------- */

export function fmtDateTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString(undefined, {
    weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit',
  });
}
export function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}
export function fmtTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}
export function relative(iso) {
  const diff = new Date(iso) - new Date();
  const mins = Math.round(diff / 60000);
  const abs = Math.abs(mins);
  if (abs < 1) return 'now';
  if (abs < 60) return mins > 0 ? `in ${abs} min` : `${abs} min ago`;
  if (abs < 1440) { const h = Math.round(abs / 60); return mins > 0 ? `in ${h} h` : `${h} h ago`; }
  const d = Math.round(abs / 1440);
  return mins > 0 ? `in ${d} day${d > 1 ? 's' : ''}` : `${d} day${d > 1 ? 's' : ''} ago`;
}
export const isoDate = (d) => {
  const dt = d instanceof Date ? d : new Date(d);
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
};
export const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };

/* ---------------- shared fragments ---------------- */

const STATUS_STYLE = {
  held: ['badge-amber', 'On hold'],
  confirmed: ['badge-green', 'Confirmed'],
  completed: ['badge-blue', 'Completed'],
  cancelled: ['badge-gray', 'Cancelled'],
  expired: ['badge-gray', 'Expired'],
  no_show: ['badge-red', 'No show'],
};
export function statusBadge(status) {
  const [cls, label] = STATUS_STYLE[status] || ['badge-gray', status];
  return `<span class="badge ${cls} badge-dot">${esc(label)}</span>`;
}

const URGENCY_STYLE = { High: 'badge-red', Medium: 'badge-amber', Low: 'badge-green' };
export function urgencyBadge(urgency) {
  return `<span class="badge ${URGENCY_STYLE[urgency] || 'badge-gray'}">${esc(urgency)} urgency</span>`;
}

export function jobBadge(status) {
  const map = { sent: 'badge-green', pending: 'badge-amber', failed: 'badge-amber', dead: 'badge-red', cancelled: 'badge-gray' };
  return `<span class="badge ${map[status] || 'badge-gray'}">${esc(status)}</span>`;
}

/** Provenance chip: was this text written by the model or by the fallback? */
export function sourceBadge(source, model, latency) {
  if (source === 'llm') {
    return `<span class="badge badge-brand" title="${esc(model || '')}${latency ? ` · ${latency}ms` : ''}">✨ AI generated</span>`;
  }
  return `<span class="badge badge-amber" title="The language model was unavailable, so the clinic's rule-based summary was used instead.">⚙ Rule-based fallback</span>`;
}

export function empty(icon, title, message, actionHtml = '') {
  return `<div class="empty"><div class="ico">${icon}</div><h3>${esc(title)}</h3><p>${esc(message)}</p>${actionHtml}</div>`;
}

export const loading = (label = 'Loading…') => `<div class="loading"><span class="spinner"></span> ${esc(label)}</div>`;

/** Render an AI pre-visit triage brief. */
export function triageCard(summary, { compact = false } = {}) {
  if (!summary) return '';
  const flags = (summary.red_flags || []).length
    ? `<div class="mt2"><div class="ai-label">Red flags</div>
       <ul class="flag-list">${summary.red_flags.map((f) => `<li>⚠ ${esc(f)}</li>`).join('')}</ul></div>`
    : '';
  const questions = (summary.suggested_questions || []).length
    ? `<div class="mt2"><div class="ai-label">Suggested questions</div>
       <ol class="qlist mt1">${summary.suggested_questions.map((q) => `<li>${esc(q)}</li>`).join('')}</ol></div>`
    : '';
  const note = summary.summary_note && !compact
    ? `<p class="small muted mt2 mb0">${esc(summary.summary_note)}</p>` : '';

  return `
    <div class="card ai-card urgency-${esc(summary.urgency)} card-pad">
      <div class="row" style="justify-content:space-between">
        <div class="ai-label">Pre-visit triage</div>
        ${sourceBadge(summary.source, summary.model, summary.latency_ms)}
      </div>
      <div class="row mt1">${urgencyBadge(summary.urgency)}</div>
      <p class="strong mt1 mb0">${esc(summary.chief_complaint)}</p>
      ${flags}${questions}${note}
    </div>`;
}
