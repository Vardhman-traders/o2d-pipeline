'use strict';
/* Vardhman Traders portal. No frameworks, no inline HTML from data: every value goes in via textContent. */

// ------------------------------------------------------------------ helpers
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'value') el.value = v;
    else if (k === 'style') el.style.cssText = v; // CSSOM, not the style attribute (blocked by our CSP)
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const SVGNS = 'http://www.w3.org/2000/svg';
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
const $ = (sel, root = document) => root.querySelector(sel);

const nInt = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
const nDec = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 });
const fmtInt = (v) => (v == null ? '–' : nInt.format(v));
const fmtMoney = (v) => (v == null ? '–' : '₹' + nInt.format(Math.round(v)));
const fmtPct = (v) => (v == null ? '–' : nDec.format(v * 100) + '%');
function fmtHours(v) {
  if (v == null) return '–';
  if (v < 1) return Math.round(v * 60) + ' min';
  if (v < 48) return nDec.format(v) + ' h';
  return nDec.format(v / 24) + ' days';
}
const fmtDate = (iso) => {
  if (!iso) return '';
  const [y, m, d] = String(iso).slice(0, 10).split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', timeZone: 'UTC' });
};
const fmtDateTime = (iso) => new Date(iso).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });

function todayIST() { return new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' }); }
function shiftDate(iso, days) {
  const d = new Date(iso + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

// ------------------------------------------------------------------ state + API
const S = { token: sessionStorage.getItem('vt_token'), user: null, tab: 'dashboard', timer: null, tableView: {} };

function safeHttps(url) {
  try { return new URL(url).protocol === 'https:' ? url : null; } catch { return null; }
}

async function api(path, { method = 'GET', body, raw = false } = {}) {
  const headers = {};
  if (S.token) headers.Authorization = 'Bearer ' + S.token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (res.status === 401 && S.token && !path.startsWith('/auth/login')) {
    signOut('Your session ended. Please sign in again.');
    throw new Error('Session ended');
  }
  if (!res.ok) {
    let msg = res.status === 429 ? 'Too many attempts. Please wait a few minutes.' : res.statusText;
    try {
      const j = await res.json();
      if (typeof j.detail === 'string') msg = j.detail;
      else if (Array.isArray(j.detail)) msg = j.detail.map((d) => `${(d.loc || []).slice(-1)[0] || ''}: ${d.msg}`).join('; ');
    } catch { /* keep default */ }
    throw new Error(msg);
  }
  return raw ? res : res.json();
}

const app = () => $('#app');
function mount(...nodes) { app().replaceChildren(...nodes); }
function stopTimer() { if (S.timer) { clearInterval(S.timer); S.timer = null; } }

function signOut(message) {
  stopTimer();
  S.token = null; S.user = null;
  sessionStorage.removeItem('vt_token');
  showLogin(message);
}

// ------------------------------------------------------------------ tooltip
const tipEl = () => $('#tip');
function showTip(evt, head, lines) {
  const t = tipEl();
  t.replaceChildren(
    head ? h('div', { class: 't-head', text: head }) : null,
    ...lines.map(([value, name]) => h('div', { class: 't-line' }, h('b', { text: value }), h('span', { text: name || '' }))));
  t.hidden = false;
  const r = t.getBoundingClientRect();
  let x, y;
  if (evt.clientX != null && evt.type !== 'focus') { x = evt.clientX + 14; y = evt.clientY + 14; }
  else { const b = evt.target.getBoundingClientRect(); x = b.left + b.width / 2; y = b.top - r.height - 8; }
  x = Math.max(8, Math.min(x, innerWidth - r.width - 8));
  y = Math.max(8, Math.min(y, innerHeight - r.height - 8));
  t.style.left = x + 'px'; t.style.top = y + 'px';
}
function hideTip() { tipEl().hidden = true; }

// ------------------------------------------------------------------ dialogs
function dialog(title, bodyNodes, actionNodes, extraClass) {
  const dlg = h('dialog', { class: extraClass }, h('h2', { text: title }), ...bodyNodes, h('div', { class: 'actions' }, ...actionNodes));
  dlg.addEventListener('close', () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

// Click-through from any dashboard bar/segment: opens a wide modal listing the
// matching orders, via the same params the chart segment represents (a date, a
// stage, an hour range - see admin_list_orders in app/admin.py).
async function openDrilldown(title, params) {
  const body = h('div', {}, h('p', { class: 'note', text: 'Loading…' }));
  const close = h('button', { class: 'btn primary', text: 'Close' });
  const dlg = dialog(title, [body], [close], 'wide');
  close.addEventListener('click', () => dlg.close());
  try {
    const qs = new URLSearchParams(params).toString();
    const rows = await api('/admin/orders?' + qs);
    const cols = [
      { head: 'Sl No', get: (r) => r.sl_no },
      { head: 'DC / Inv', get: (r) => r.dc_inv_no || '' },
      { head: 'Order date', get: (r) => fmtDate(r.order_date) },
      { head: 'Stage', get: (r) => r.stage },
      { head: 'Delivery status', get: (r) => r.delivery_status || '–' },
      { head: 'Channel', get: (r) => r.channel || '' },
      { head: 'Location', get: (r) => r.shipping_location || '' },
      { head: 'Amount', num: 1, get: (r) => fmtMoney(r.amount_received) },
      { head: 'Hrs to deliver', num: 1, get: (r) => (r.hours_to_deliver == null ? '–' : fmtHours(Number(r.hours_to_deliver))) },
    ];
    body.replaceChildren(
      h('p', { class: 'note', style: 'margin-bottom:10px', text: rows.length + ' matching order' + (rows.length === 1 ? '' : 's') + (rows.length === 200 ? ' (showing first 200)' : '') }),
      rows.length ? tableFor(cols, rows) : h('div', { class: 'empty', text: 'No matching orders.' }));
  } catch (ex) {
    body.replaceChildren(h('div', { class: 'msg error', text: ex.message }));
  }
}

function randomPassword() {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
  const bytes = crypto.getRandomValues(new Uint32Array(14));
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join('');
}

/* fields: [{name,label,type:'text'|'password'|'number'|'url'|'select'|'checks'|'bool',options,value,hint,required}] */
function formDialog({ title, fields, submitLabel = 'Save', onSubmit }) {
  const inputs = {};
  const err = h('div', { class: 'msg error' });
  const form = h('form', { novalidate: true });
  for (const f of fields) {
    let input;
    if (f.type === 'select') {
      input = h('select', {}, f.options.map(([v, l]) => h('option', { value: v, text: l, selected: v === f.value })));
    } else if (f.type === 'checks') {
      input = h('div', { class: 'checks' }, f.options.map(([v, l]) =>
        h('label', {}, h('input', { type: 'checkbox', value: v, checked: (f.value || []).includes(v) }), l)));
    } else if (f.type === 'bool') {
      input = h('input', { type: 'checkbox', checked: !!f.value });
    } else if (f.type === 'password') {
      input = h('input', { type: 'text', autocomplete: 'off', value: f.value || '' });
    } else {
      input = h('input', { type: f.type || 'text', value: f.value ?? '', autocomplete: 'off', maxlength: f.maxlength });
    }
    inputs[f.name] = input;
    const label = h('label', {}, f.label);
    const extra = f.type === 'password'
      ? h('div', { class: 'row', style: 'margin-top:6px' },
          h('button', { type: 'button', class: 'btn small', text: 'Generate', onclick: () => { input.value = randomPassword(); } }),
          h('span', { class: 'note', text: 'Min 10 characters. They must change it at first sign-in.' }))
      : (f.hint ? h('div', { class: 'hint', text: f.hint }) : null);
    form.append(h('div', { class: 'field' }, f.type === 'checks' ? h('span', { class: 'lbl', text: f.label }) : label, input, extra));
  }
  const submit = h('button', { type: 'submit', class: 'btn primary', text: submitLabel });
  const cancel = h('button', { type: 'button', class: 'btn', text: 'Cancel' });
  form.append(err);
  const dlg = dialog(title, [form], [cancel, submit]);
  cancel.addEventListener('click', () => dlg.close());
  submit.addEventListener('click', () => form.requestSubmit());
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const values = {};
    for (const f of fields) {
      const el = inputs[f.name];
      if (f.type === 'checks') values[f.name] = Array.from(el.querySelectorAll('input:checked'), (c) => c.value);
      else if (f.type === 'bool') values[f.name] = el.checked;
      else if (f.type === 'number') values[f.name] = Number(el.value || 0);
      else values[f.name] = el.value.trim();
      if (f.required && (values[f.name] === '' || values[f.name] == null)) { err.textContent = `${f.label} is required.`; return; }
    }
    submit.disabled = true; err.textContent = '';
    try { await onSubmit(values); dlg.close(); }
    catch (ex) { err.textContent = ex.message; submit.disabled = false; }
  });
  const first = form.querySelector('input,select'); if (first) first.focus();
}

function confirmDialog(title, text, confirmLabel, danger) {
  return new Promise((resolve) => {
    const yes = h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), text: confirmLabel });
    const no = h('button', { class: 'btn', text: 'Cancel' });
    const dlg = dialog(title, [h('p', { text })], [no, yes]);
    let answer = false;
    yes.addEventListener('click', () => { answer = true; dlg.close(); });
    no.addEventListener('click', () => dlg.close());
    dlg.addEventListener('close', () => resolve(answer));
  });
}

function secretDialog(title, intro, secret) {
  const copy = h('button', { class: 'btn', text: 'Copy' });
  const done = h('button', { class: 'btn primary', text: 'Done' });
  const dlg = dialog(title, [h('p', { class: 'note', text: intro }), h('div', { class: 'secret', text: secret, style: 'margin-top:10px' })], [copy, done]);
  copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(secret); copy.textContent = 'Copied'; } catch { copy.textContent = 'Select and copy manually'; } });
  done.addEventListener('click', () => dlg.close());
}

// ------------------------------------------------------------------ login + password
function showLogin(message) {
  const err = h('div', { class: 'msg error', role: 'alert', text: message || '' });
  const user = h('input', { type: 'text', autocomplete: 'username', required: true, autofocus: true });
  const pass = h('input', { type: 'password', autocomplete: 'current-password', required: true });
  const btn = h('button', { type: 'submit', class: 'btn primary', style: 'width:100%', text: 'Sign in' });
  const form = h('form', {},
    h('div', { class: 'field' }, h('label', { text: 'Username' }), user),
    h('div', { class: 'field' }, h('label', { text: 'Password' }), pass),
    btn, err);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    btn.disabled = true; err.textContent = '';
    try {
      S.token = null;
      const r = await api('/auth/login', { method: 'POST', body: { username: user.value, password: pass.value } });
      S.token = r.access_token;
      sessionStorage.setItem('vt_token', S.token);
      S.user = null; // route() loads the full profile from /auth/me
      route();
    } catch (ex) { err.textContent = ex.message; btn.disabled = false; pass.value = ''; }
  });
  mount(h('div', { class: 'login-screen' }, h('div', { class: 'card center-card' }, h('div', { class: 'logo', text: 'VT' }),
    h('h1', { text: 'Vardhman Traders' }), h('p', { class: 'sub', text: 'Sign in to your sales & operations portal' }), form)));
}

function showChangePassword(forced) {
  const cur = h('input', { type: 'password', autocomplete: 'current-password', required: true });
  const nw = h('input', { type: 'password', autocomplete: 'new-password', required: true, minlength: 10 });
  const again = h('input', { type: 'password', autocomplete: 'new-password', required: true });
  const err = h('div', { class: 'msg error', role: 'alert' });
  const btn = h('button', { type: 'submit', class: 'btn primary', text: 'Save new password' });
  const form = h('form', {},
    h('div', { class: 'field' }, h('label', { text: 'Current password' }), cur),
    h('div', { class: 'field' }, h('label', { text: 'New password' }), nw, h('div', { class: 'hint', text: 'At least 10 characters.' })),
    h('div', { class: 'field' }, h('label', { text: 'Repeat new password' }), again),
    h('div', { class: 'row' }, btn,
      forced ? h('button', { type: 'button', class: 'btn', text: 'Sign out', onclick: () => signOut() })
             : h('button', { type: 'button', class: 'btn', text: 'Cancel', onclick: route })), err);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (nw.value !== again.value) { err.textContent = 'The new passwords do not match.'; return; }
    btn.disabled = true; err.textContent = '';
    try {
      await api('/auth/change-password', { method: 'POST', body: { current_password: cur.value, new_password: nw.value } });
      S.user.must_change_password = false;
      route();
    } catch (ex) { err.textContent = ex.message; btn.disabled = false; }
  });
  mount(h('div', { class: 'login-screen' }, h('div', { class: 'card center-card' }, h('div', { class: 'logo', text: 'VT' }),
    h('h1', { text: 'Change your password' }),
    h('p', { class: 'sub', text: forced ? 'You must choose a new password before continuing.' : 'Choose a new password.' }), form)));
}

// ------------------------------------------------------------------ shell
async function route() {
  stopTimer();
  try {
    if (!S.user) S.user = await api('/auth/me');
  } catch { return showLogin(); }
  if (S.user.must_change_password) return showChangePassword(true);
  showPortal();
}

// Opens an app link already signed in: mints a 90-second one-time ticket and appends it
// to the URL, instead of sending the real (12-hour) bearer token anywhere. The tab is
// opened synchronously (on the click) so browsers don't treat it as a blocked popup;
// its location is filled in once the ticket comes back.
async function openAppLink(url) {
  // No intermediate blank tab: navigate straight there. Simpler and no popup-blocker
  // risk (it's not a new window), at the cost of leaving the portal page.
  try {
    const { ticket } = await api('/auth/sso-ticket', { method: 'POST' });
    const joined = url + (url.includes('?') ? '&' : '?') + 'ssoTicket=' + encodeURIComponent(ticket);
    window.location.href = joined;
  } catch (ex) {
    alert('Could not open this app: ' + ex.message);
  }
}

const TAB_ICON = { dashboard: '📊', members: '👥', setup: '⚙️' };

function topbar(links) {
  return h('header', { class: 'topbar' },
    h('div', { class: 'brand' }, h('div', { class: 'brand-mark', text: 'VT' }),
      h('div', { class: 'brand-text' }, h('span', { class: 'name', text: 'Vardhman Traders' }), h('span', { class: 'tag', text: 'Sales & Operations Portal' }))),
    ...links.map((l) => safeHttps(l.url) && h('button', { class: 'btn open-app', text: 'Open ' + l.name + ' ↗', onclick: () => openAppLink(l.url) })),
    h('span', { class: 'who' }, S.user.display_name, h('span', { class: 'role-chip', text: S.user.role.replace('_', ' ') })),
    h('button', { class: 'btn', text: 'Change password', onclick: () => showChangePassword(false) }),
    h('button', { class: 'btn', text: 'Sign out', onclick: () => signOut() }));
}

async function showPortal() {
  let links = [];
  try { links = await api('/links'); } catch { /* shown as empty */ }
  const isAdmin = S.user.role === 'admin';
  const body = h('main', { class: 'wrap' });
  mount(topbar(isAdmin ? links : []), body);
  if (!isAdmin) return renderHome(body, links);

  const holder = h('div');
  const tabs = [['dashboard', 'Dashboard'], ['members', 'Members'], ['setup', 'Setup']];
  const bar = h('div', { class: 'tabs', role: 'tablist' });
  const draw = () => {
    bar.replaceChildren(...tabs.map(([id, label]) => h('button', {
      class: 'tab', role: 'tab', 'aria-selected': String(S.tab === id),
      onclick: () => { S.tab = id; draw(); openTab(); } }, TAB_ICON[id] + ' ' + label)));
  };
  const openTab = () => {
    stopTimer(); hideTip();
    // A fresh panel per visit: a slow response from a tab you already left writes into a detached node.
    const panel = h('div', { id: 'panel' });
    holder.replaceChildren(panel);
    ({ dashboard: renderDashboard, members: renderMembers, setup: renderSetup })[S.tab](panel);
  };
  body.append(bar, holder);
  draw(); openTab();
}

// Emoji chosen by keyword in the app's name, so a shop/godown/dispatch/receiving
// tracker gets a sensible icon without admin having to pick one manually.
function iconForApp(name) {
  const n = (name || '').toLowerCase();
  if (n.includes('dispatch')) return '🚚';
  if (n.includes('receiv')) return '📥';
  if (n.includes('godown') || n.includes('warehouse') || n.includes('stock')) return '📦';
  if (n.includes('shop')) return '🧾';
  if (n.includes('order') || n.includes('track')) return '📋';
  if (n.includes('report') || n.includes('dashboard')) return '📊';
  return '🔗';
}

function renderHome(body, links) {
  const tiles = links.map((l) => {
    const url = safeHttps(l.url);
    return url && h('button', { class: 'app-tile', onclick: () => openAppLink(url) },
      h('span', { class: 'icon-circle', text: iconForApp(l.name) }),
      h('strong', { text: l.name }), h('span', { text: 'Open ↗' }));
  });
  body.append(h('div', { class: 'home-wrap' }, h('div', { class: 'home-inner' },
    h('h1', { text: 'Welcome, ' + S.user.display_name }),
    h('p', { class: 'note', text: links.length ? 'Choose an app to continue' : 'No apps have been assigned to you yet. Please contact the administrator.' }),
    h('div', { class: 'apps' }, ...tiles))));
}

// ------------------------------------------------------------------ setup (apps + activity)
async function renderSetup(panel) {
  const sub = [['apps', 'Apps'], ['lists', 'Dropdown values'], ['permissions', 'Permissions'], ['security', 'Security'], ['activity', 'Activity log']];
  const renderers = { apps: renderApps, lists: renderLists, permissions: renderPermissions, security: renderSecurity, activity: renderActivity };
  if (!S.setupTab) S.setupTab = 'apps';
  const bar = h('div', { class: 'subtabs', role: 'tablist' });
  const inner = h('div');
  const drawBar = () => {
    bar.replaceChildren(...sub.map(([id, label]) => h('button', {
      class: 'subtab', role: 'tab', 'aria-selected': String(S.setupTab === id), text: label,
      onclick: () => { S.setupTab = id; drawBar(); openInner(); } })));
  };
  const openInner = () => {
    const p = h('div');
    inner.replaceChildren(p);
    renderers[S.setupTab](p);
  };
  panel.replaceChildren(bar, inner);
  drawBar(); openInner();
}

// ------------------------------------------------------------------ charts
function tableFor(cols, rows) {
  return h('div', { class: 'table-wrap' }, h('table', {},
    h('thead', {}, h('tr', {}, cols.map((c) => h('th', { class: c.num ? 'num' : '', text: c.head })))),
    h('tbody', {}, rows.map((r) => h('tr', {}, cols.map((c) => h('td', { class: c.num ? 'num' : '', text: c.get(r) })))))));
}

/* A card with a Chart/Table twin. build() returns the chart node; cols/rows describe the table view. */
function chartCard(id, title, sub, build, cols, rows, extra) {
  const body = h('div');
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'View' });
  const draw = () => {
    const tv = !!S.tableView[id];
    body.replaceChildren(!rows.length ? h('div', { class: 'empty', text: 'No data for this period.' }) : (tv ? tableFor(cols, rows) : build()));
    seg.replaceChildren(
      h('button', { 'aria-pressed': String(!tv), text: 'Chart', onclick: () => { S.tableView[id] = false; draw(); } }),
      h('button', { 'aria-pressed': String(tv), text: 'Table', onclick: () => { S.tableView[id] = true; draw(); } }));
  };
  draw();
  return h('section', { class: 'chart-card' },
    h('div', { class: 'chart-head' }, h('div', {}, h('h3', { text: title }), sub && h('div', { class: 'sub', text: sub })), rows.length ? seg : null),
    body, extra);
}

function barList(rows, { valueKey = 'orders', fmt = fmtInt, tip, onClick }) {
  const max = Math.max(...rows.map((r) => Number(r[valueKey]) || 0), 1);
  return h('div', { class: 'bars' }, rows.map((r) => {
    const v = Number(r[valueKey]) || 0;
    const el = h('div', { class: 'bar-row' + (onClick ? ' clickable' : ''), tabindex: '0' },
      h('div', { class: 'bar-label', title: r.label, text: r.label }),
      h('div', { class: 'bar-track' }, h('div', { class: 'bar-fill', style: `width:${(v / max) * 100}%` })),
      h('div', { class: 'bar-val', text: fmt(v) }));
    const show = (e) => showTip(e, r.label, tip ? tip(r) : [[fmt(v), valueKey]]);
    el.addEventListener('pointermove', show); el.addEventListener('focus', show);
    el.addEventListener('pointerleave', hideTip); el.addEventListener('blur', hideTip);
    if (onClick) {
      el.addEventListener('click', () => onClick(r));
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(r); } });
    }
    return el;
  }));
}

function niceStep(max, ticks = 4) {
  const raw = max / ticks, p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

/* Draws at the card's real pixel width (so text stays 10px) and redraws when the card resizes. */
function columnChart(rows, opts) {
  const wrap = h('div', { class: 'col-wrap' });
  let lastW = 0;
  const ro = new ResizeObserver(() => {
    const w = Math.round(wrap.clientWidth);
    if (w > 40 && Math.abs(w - lastW) > 1) { lastW = w; wrap.replaceChildren(columnSvg(rows, opts, w)); }
  });
  ro.observe(wrap);
  return wrap;
}

function columnSvg(rows, { fmt = fmtInt, tip, labelEvery, height = 210, onClick, cumulative }, W) {
  const H = height, m = { l: 46, r: cumulative ? 40 : 8, t: 16, b: 24 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const maxV = Math.max(...rows.map((r) => r.value), 0);
  const step = niceStep(maxV || 1), top = Math.max(step * Math.ceil((maxV || 1) / step), step);
  const y = (v) => m.t + ph - (v / top) * ph;
  const slot = pw / rows.length, bw = Math.min(24, slot * 0.72);
  const svg = s('svg', { class: 'col-chart', width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Column chart, see the Table view for values' });
  for (let v = 0; v <= top + 1e-9; v += step) {
    svg.append(s('line', { class: v === 0 ? 'base-line' : 'grid-line', x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }),
      s('text', { x: m.l - 6, y: y(v) + 3, 'text-anchor': 'end' }, fmt === fmtMoney ? nInt.format(v) : nInt.format(v)));
  }
  const every = labelEvery || Math.max(1, Math.ceil(rows.length / Math.max(3, Math.floor(pw / 80))));
  const peakIdx = rows.findIndex((r) => r.value === maxV && maxV > 0);

  // Cumulative % overlay - right axis, so "when have 80% of deliveries happened" reads at a glance.
  let cumPoints = null;
  if (cumulative) {
    const total = rows.reduce((a, r) => a + r.value, 0) || 1;
    let running = 0;
    cumPoints = rows.map((r, i) => { running += r.value; return { x: m.l + slot * i + slot / 2, pct: running / total }; });
    const yc = (pct) => m.t + ph - pct * ph;
    [0, 0.25, 0.5, 0.75, 1].forEach((pct) => svg.append(
      s('text', { x: W - m.r + 6, y: yc(pct) + 3, 'text-anchor': 'start' }, Math.round(pct * 100) + '%')));
    const path = cumPoints.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${yc(p.pct)}`).join(' ');
    svg.append(s('path', { class: 'cum-line', d: path, fill: 'none' }));
    cumPoints.forEach((p) => svg.append(s('circle', { class: 'cum-dot', cx: p.x, cy: yc(p.pct), r: 2.5 })));
  }

  rows.forEach((r, i) => {
    const cx = m.l + slot * i + slot / 2, x = cx - bw / 2, yy = y(r.value), rad = Math.min(4, bw / 2);
    let bar = null;
    if (r.value > 0) {
      const d = `M${x},${m.t + ph} V${yy + rad} a${rad},${rad} 0 0 1 ${rad},${-rad} h${bw - 2 * rad} a${rad},${rad} 0 0 1 ${rad},${rad} V${m.t + ph} Z`;
      bar = s('path', { class: 'bar', d });
      svg.append(bar);
    }
    if (i % every === 0) svg.append(s('text', { x: cx, y: H - 8, 'text-anchor': 'middle' }, r.label));
    if (i === peakIdx) svg.append(s('text', { class: 'peak', x: cx, y: yy - 5, 'text-anchor': 'middle' }, fmt(r.value)));
    const hit = s('rect', { class: 'hit' + (onClick ? ' clickable' : ''), x: cx - slot / 2, y: m.t, width: slot, height: ph, tabindex: '0', 'aria-label': `${r.label}: ${fmt(r.value)}` });
    const show = (e) => { if (bar) bar.classList.add('hot'); showTip(e, r.label, tip ? tip(r) : [[fmt(r.value), '']]); };
    const hide = () => { if (bar) bar.classList.remove('hot'); hideTip(); };
    hit.addEventListener('pointermove', show); hit.addEventListener('focus', show);
    hit.addEventListener('pointerleave', hide); hit.addEventListener('blur', hide);
    if (onClick) {
      hit.addEventListener('click', () => onClick(r));
      hit.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(r); } });
    }
    svg.append(hit);
  });
  return svg;
}

// ------------------------------------------------------------------ dashboard
function delta(cur, prev, goodWhenUp = true, label = 'previous period') {
  if (!delta.comparable) return null; // earlier period is only partly covered by the data: a % would mislead
  if (prev == null || cur == null || prev === 0) return h('span', { class: 'delta flat', text: prev === 0 && cur > 0 ? `new vs ${label}` : `no ${label} data` });
  const pct = (cur - prev) / prev;
  if (Math.abs(pct) < 0.005) return h('span', { class: 'delta flat', text: `▬ flat vs ${label}` });
  const up = pct > 0, good = up === goodWhenUp;
  return h('span', { class: `delta ${up ? 'up' : 'down'}-${good ? 'good' : 'bad'}`, text: `${up ? '▲' : '▼'} ${nDec.format(Math.abs(pct) * 100)}% vs ${label}` });
}

function tile(label, value, sub, hero, icon) {
  return h('div', { class: 'tile' + (hero ? ' hero' : '') }, icon && h('span', { class: 'icon', text: icon }),
    h('div', { class: 'label', text: label }), h('div', { class: 'value', text: value }), h('div', { class: 'sub' }, sub));
}

/* A funnel stage, as a row of horizontal KPI scorecards - clickable through to the
   matching orders. Segments in one funnel() call are meant to be mutually exclusive
   and sum to the total. */
function funnel(segments, total) {
  return h('div', { class: 'funnel' }, segments.map((seg) => {
    const pct = total ? seg.value / total : 0;
    const el = h('div', { class: 'funnel-card' + (seg.onClick ? ' clickable' : ''), style: `--seg-color:${seg.color}`, tabindex: seg.onClick ? '0' : undefined },
      h('div', { class: 'funnel-value', text: fmtInt(seg.value) }),
      h('div', { class: 'funnel-label', text: seg.label }),
      h('div', { class: 'funnel-pct', text: total ? fmtPct(pct) + ' of total' : '' }));
    if (seg.onClick) {
      el.addEventListener('click', seg.onClick);
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); seg.onClick(); } });
    }
    return el;
  }));
}

// With archiving on, "active" data is naturally always a recent + still-open window,
// so a user-facing period picker no longer adds much - the dashboard just always shows
// this fixed trailing window.
const DASHBOARD_WINDOW_DAYS = 30;
function fixedDashboardRange() {
  const to = todayIST();
  return { from: shiftDate(to, -(DASHBOARD_WINDOW_DAYS - 1)), to };
}

async function renderDashboard(panel) {
  const stamp = h('span', { text: 'Loading…' });
  const body = h('div', { id: 'dash-body' });
  const dl = h('button', { class: 'btn primary', text: '⬇ Download Excel' });
  let busy = false, last = null;

  async function load() {
    if (busy) return;
    busy = true; body.classList.add('loading');
    const r = fixedDashboardRange();
    try {
      const d = await api(`/admin/dashboard?date_from=${r.from}&date_to=${r.to}`);
      last = d;
      const y = window.scrollY;
      body.replaceChildren(...dashboardNodes(d));
      window.scrollTo(0, y); // keep the reader's place across the 30 s refresh
      stamp.textContent = 'Updated ' + new Date().toLocaleTimeString('en-IN', { hour12: false });
    } catch (ex) {
      if (ex.message !== 'Session ended') body.replaceChildren(h('div', { class: 'card' }, h('div', { class: 'msg error', text: ex.message })));
    } finally { busy = false; body.classList.remove('loading'); }
  }

  dl.addEventListener('click', async () => {
    const r = fixedDashboardRange();
    dl.disabled = true; dl.textContent = 'Preparing…';
    try {
      const res = await api(`/admin/export.xlsx?date_from=${r.from}&date_to=${r.to}`, { raw: true });
      const url = URL.createObjectURL(await res.blob());
      const a = h('a', { href: url, download: `vardhman_orders_${r.from}_to_${r.to}.xlsx` });
      document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (ex) { alert(ex.message); }
    dl.disabled = false; dl.textContent = '⬇ Download Excel';
  });

  panel.replaceChildren(
    h('div', { class: 'filters' }, dl,
      h('span', { class: 'live' }, h('span', { class: 'dot' }), 'Live · refreshes every 30 s · ', stamp)),
    body);
  await load();
  if (!panel.isConnected) return; // user already left this tab
  S.timer = setInterval(() => {
    if (!panel.isConnected) return stopTimer();
    if (!document.hidden) load();
  }, 30000);
}

function dashboardNodes(d) {
  const hd = d.headline, pv = d.previous, days = d.range.days;
  delta.comparable = !!d.data_start && d.range.previous_from >= d.data_start;
  const periodLabel = `${fmtDate(d.range.from)} – ${fmtDate(d.range.to)} · ${days} day${days > 1 ? 's' : ''}`;
  const prevLabel = `prior ${days} day${days > 1 ? 's' : ''}`;
  const drill = (extra) => openDrilldown(extra.title, { date_from: d.range.from, date_to: d.range.to, ...extra.params });

  // ---- the story: total -> cancelled / open / delivered, delivered -> awaiting receiving / closed
  const cancelled = hd.cancelled, delivered = hd.delivered, closed = hd.closed;
  const open = hd.orders - cancelled - delivered;
  const awaitingReceiving = delivered - closed;

  const story = h('p', { class: 'story' },
    `Of `, h('strong', { text: fmtInt(hd.orders) }), ` orders received ${periodLabel}, `,
    h('strong', { text: fmtInt(cancelled) }), ` were cancelled and `,
    h('strong', { text: fmtInt(open) }), ` are still open. `,
    h('strong', { text: fmtInt(delivered) }), ` have been delivered, of which `,
    h('strong', { text: fmtInt(closed) }), ` are fully closed and `,
    h('strong', { text: fmtInt(awaitingReceiving) }), ` are awaiting receiving or payment.`);

  const topFunnel = funnel([
    { label: 'Cancelled', value: cancelled, color: 'var(--bad)', onClick: () => drill({ title: 'Cancelled orders', params: { stage: 'Cancelled' } }) },
    { label: 'Still open (awaiting godown / dispatch)', value: open, color: 'var(--warn)', onClick: () => drill({ title: 'Open orders', params: { stage: 'Awaiting godown,Awaiting dispatch' } }) },
    { label: 'Delivered', value: delivered, color: 'var(--good)', onClick: () => drill({ title: 'Delivered orders', params: { stage: 'Awaiting receiving,Closed' } }) },
  ], hd.orders);
  const subFunnel = funnel([
    { label: 'Awaiting receiving / payment', value: awaitingReceiving, color: 'var(--warn)', onClick: () => drill({ title: 'Awaiting receiving or payment', params: { stage: 'Awaiting receiving' } }) },
    { label: 'Fully closed', value: closed, color: 'var(--good)', onClick: () => drill({ title: 'Fully closed orders', params: { stage: 'Closed' } }) },
  ], delivered);

  const storyCard = h('div', { class: 'card story-card' },
    h('div', { class: 'section-head' }, h('div', {}, h('h1', { text: fmtInt(hd.orders) + ' orders' }), h('div', { class: 'sub', text: periodLabel })),
      delta(hd.orders, pv.orders, true, prevLabel)),
    story, topFunnel,
    h('div', { class: 'funnel-sub-label', text: 'Of the delivered orders:' }), subFunnel);

  const kpi2 = h('div', { class: 'kpis second' },
    tile('Typical order-to-delivery time', fmtHours(hd.median_hours), (() => { const dm = delta(hd.median_hours, pv.median_hours, false, prevLabel); return dm ? ['median · ', dm] : 'median'; })(), false, '⏱️'),
    tile('Delivered within 24 h', fmtPct(hd.within_24h), 'of delivered orders', false, '⚡'),
    tile('Amount received', fmtMoney(hd.amount_received), delta(Number(hd.amount_received), Number(pv.amount_received), true, prevLabel), false, '💰'),
    tile('Cartage', fmtMoney(hd.cartage), delta(Number(hd.cartage), Number(pv.cartage), false, prevLabel), false, '🧮'));

  // orders per day - click a day to see that day's orders
  const daily = d.daily.map((x) => ({ ...x, label: fmtDate(x.date), value: x.orders }));
  const perDay = chartCard('daily', 'Orders per day', periodLabel,
    () => columnChart(daily, {
      tip: (r) => [[fmtInt(r.orders), 'orders'], [fmtInt(r.cancelled), 'cancelled'], [fmtHours(r.avg_hours), 'avg to deliver']].concat(r.holiday ? [['Monday', 'holiday']] : []),
      onClick: (r) => openDrilldown('Orders on ' + fmtDate(r.date), { date_from: r.date, date_to: r.date }),
    }),
    [{ head: 'Date', get: (r) => fmtDate(r.date) }, { head: 'Orders', num: 1, get: (r) => fmtInt(r.orders) }, { head: 'Cancelled', num: 1, get: (r) => fmtInt(r.cancelled) }, { head: 'Avg to deliver', num: 1, get: (r) => fmtHours(r.avg_hours) }],
    d.daily.filter((x) => x.orders > 0 || !x.holiday), null);

  // pipeline - click a stage to see those orders (always "right now", not period-scoped)
  const pipe = d.pipeline;
  const pipeCard = chartCard('pipeline', 'Where open orders are right now', 'All dates, not just the selected period',
    () => barList(pipe, {
      tip: (r) => [[fmtInt(r.orders), 'open orders'], [r.oldest ? 'since ' + fmtDate(r.oldest) : '', 'oldest']],
      onClick: (r) => openDrilldown(r.label, { stage: r.label }),
    }),
    [{ head: 'Stage', get: (r) => r.label }, { head: 'Orders', num: 1, get: (r) => fmtInt(r.orders) }, { head: 'Oldest', get: (r) => fmtDate(r.oldest) }], pipe,
    h('p', { class: 'note', style: 'margin-top:10px', text: 'Awaiting godown = no delivery status yet. Awaiting dispatch = not delivered. Awaiting receiving = delivered but date received or payment status missing.' }));

  // delivery time, Pareto-style: biggest bucket first, with a cumulative-% line
  // on the right axis - click a bucket to see those orders
  const buckets = d.delivery_time_buckets
    .map((b) => ({ label: b.bucket, value: b.n, min_hours: b.min_hours, max_hours: b.max_hours }))
    .sort((a, b) => b.value - a.value);
  const timeCard = chartCard('o2d', 'Order-to-delivery time', `Median ${fmtHours(hd.median_hours)} · 9 in 10 within ${fmtHours(hd.p90_hours)} · average ${fmtHours(hd.avg_hours)}`,
    () => columnChart(buckets, {
      labelEvery: 1, cumulative: true, tip: (r) => [[fmtInt(r.value), 'orders']],
      onClick: (r) => openDrilldown(r.label + ' to deliver', { min_hours: r.min_hours, ...(r.max_hours != null ? { max_hours: r.max_hours } : {}) }),
    }),
    [{ head: 'Time from logging to delivery', get: (r) => r.label }, { head: 'Orders', num: 1, get: (r) => fmtInt(r.value) }], buckets, null);

  const oldest = h('section', { class: 'chart-card wide' },
    h('div', { class: 'chart-head' }, h('div', {}, h('h3', { text: 'Longest-waiting open orders' }), h('div', { class: 'sub', text: 'Oldest 10 orders that are not closed or cancelled (any date)' }))),
    d.oldest_open.length ? tableFor([
      { head: 'Sl No', get: (r) => r.sl_no }, { head: 'DC / Inv', get: (r) => r.dc_inv_no || '' }, { head: 'Order date', get: (r) => fmtDate(r.order_date) },
      { head: 'Stage', get: (r) => r.stage }, { head: 'Delivery status', get: (r) => r.delivery_status || '–' },
      { head: 'Waiting', num: 1, get: (r) => fmtHours(Number(r.age_hours)) }, { head: 'Location', get: (r) => r.shipping_location || '' }], d.oldest_open)
      : h('div', { class: 'empty', text: 'Nothing is waiting. 🎉' }));

  return [storyCard, kpi2,
    h('div', { class: 'grid' }, h('div', { class: 'wide' }, perDay)),
    h('div', { class: 'grid' }, pipeCard, timeCard),
    h('div', { class: 'grid' }, oldest)];
}

// ------------------------------------------------------------------ members
const ROLE_HELP = { shop: 'Shop', godown: 'Godown', shop_dispatch: 'Shop dispatch', godown_dispatch: 'Godown dispatch', receiving: 'Receiving', admin: 'Admin', cashier: 'Cashier', accounts: 'Accounts', cartage: 'Cartage', legacy: 'Legacy (login disabled)' };
const roleLabel = (r) => ROLE_HELP[r] || r;

async function renderMembers(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let users, meta;
  try { [users, meta] = await Promise.all([api('/admin/users'), api('/admin/roles')]); }
  catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  const roleOptions = meta.roles.map((r) => [r, roleLabel(r)]);
  const reload = () => renderMembers(panel);
  const search = h('input', { type: 'text', placeholder: 'Search name or username', 'aria-label': 'Search members', style: 'max-width:280px' });

  const rowsBody = h('tbody');
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    rowsBody.replaceChildren(...users.filter((u) => !q || u.username.includes(q) || u.display_name.toLowerCase().includes(q)).map((u) => {
      const me = u.user_key === S.user.user_key;
      const status = u.disabled ? h('span', { class: 'pill off', text: 'Disabled' })
        : u.must_change_password ? h('span', { class: 'pill warn', text: 'Must change password' }) : h('span', { class: 'pill', text: 'Active' });
      return h('tr', {},
        h('td', { text: u.display_name }), h('td', { text: u.username }), h('td', { text: roleLabel(u.role) }), h('td', {}, status),
        h('td', { class: 'num', text: fmtInt(u.orders_created) }),
        h('td', {}, h('div', { class: 'row' },
          h('button', { class: 'btn small', text: 'Edit', onclick: () => editUser(u) }),
          h('button', { class: 'btn small', text: 'Reset password', onclick: () => resetPw(u) }),
          !me && !u.disabled && h('button', { class: 'btn small danger', text: 'Disable', onclick: () => disable(u) }),
          !me && h('button', { class: 'btn small danger', text: 'Delete', onclick: () => remove(u) }))));
    }));
  };
  search.addEventListener('input', draw);

  const editUser = (u) => formDialog({ title: 'Edit ' + u.username, submitLabel: 'Save',
    fields: [{ name: 'display_name', label: 'Display name', value: u.display_name, required: true, maxlength: 150 },
      { name: 'role', label: 'Role', type: 'select', options: roleOptions, value: u.role, hint: u.user_key === S.user.user_key ? 'You cannot change your own role.' : '' }],
    onSubmit: async (v) => { const body = { display_name: v.display_name }; if (u.user_key !== S.user.user_key) body.role = v.role;
      await api('/admin/users/' + u.user_key, { method: 'PATCH', body }); reload(); } });

  const resetPw = (u) => formDialog({ title: 'Reset password for ' + u.username, submitLabel: 'Reset',
    fields: [{ name: 'password', label: 'Temporary password', type: 'password', required: true }],
    onSubmit: async (v) => { await api(`/admin/users/${u.user_key}/reset-password`, { method: 'POST', body: { password: v.password } });
      reload(); secretDialog('Password reset', `Give this temporary password to ${u.display_name}. They must change it when they sign in.`, v.password); } });

  const disable = async (u) => {
    if (!(await confirmDialog('Disable ' + u.username + '?', `${u.display_name} will be signed out and unable to log in until you reset their password.`, 'Disable', true))) return;
    try { await api(`/admin/users/${u.user_key}/disable`, { method: 'POST' }); reload(); } catch (ex) { alert(ex.message); }
  };

  const remove = async (u) => {
    if (!(await confirmDialog('Delete ' + u.username + '?', `Permanently removes ${u.display_name}'s account. This only works if they have no order or admin-action history - otherwise use Disable instead. Cannot be undone.`, 'Delete', true))) return;
    try { await api(`/admin/users/${u.user_key}`, { method: 'DELETE' }); reload(); } catch (ex) { alert(ex.message); }
  };

  const add = () => formDialog({ title: 'Add member', submitLabel: 'Create',
    fields: [{ name: 'display_name', label: 'Display name', required: true, maxlength: 150 },
      { name: 'username', label: 'Username', required: true, hint: 'Lowercase letters, digits, dot, dash, underscore. Min 3.', maxlength: 40 },
      { name: 'role', label: 'Role', type: 'select', options: roleOptions, value: 'shop' },
      { name: 'password', label: 'Temporary password', type: 'password', required: true }],
    onSubmit: async (v) => { await api('/admin/users', { method: 'POST', body: v });
      reload(); secretDialog('Member created', `Give ${v.display_name} the username “${v.username}” and this temporary password. They must change it at first sign-in.`, v.password); } });

  panel.replaceChildren(
    h('div', { class: 'row', style: 'margin-bottom:14px' }, search, h('span', { class: 'grow' }), h('button', { class: 'btn primary', text: '+ Add member', onclick: add })),
    h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Name', 'Username', 'Role', 'Status'].map((t) => h('th', { text: t })), h('th', { class: 'num', text: 'Orders logged' }), h('th', { text: '' }))), rowsBody)),
    h('p', { class: 'note', style: 'margin-top:10px', text: 'Cashier, accounts and cartage roles currently have no order access. Only the four operating roles can use the order tracker.' }));
  draw();
}

// ------------------------------------------------------------------ apps
async function renderApps(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let links, meta;
  try { [links, meta] = await Promise.all([api('/admin/links'), api('/admin/roles')]); }
  catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  const reload = () => renderApps(panel);
  const roleChoices = meta.roles.filter((r) => r !== 'admin').map((r) => [r, roleLabel(r)]);
  const fields = (l) => [
    { name: 'name', label: 'Name shown on the button', value: l?.name || '', required: true, maxlength: 100 },
    { name: 'url', label: 'Web address (must start with https://)', type: 'url', value: l?.url || '', required: true },
    { name: 'roles', label: 'Who can open it', type: 'checks', options: roleChoices, value: l?.roles || [], hint: 'Admins always see every app.' },
    { name: 'sort_order', label: 'Sort order', type: 'number', value: l?.sort_order ?? 0 },
    { name: 'active', label: 'Active', type: 'bool', value: l ? l.active : true }];
  const add = () => formDialog({ title: 'Add app', fields: fields(null), submitLabel: 'Add', onSubmit: async (v) => { await api('/admin/links', { method: 'POST', body: v }); reload(); } });
  const edit = (l) => formDialog({ title: 'Edit app', fields: fields(l), onSubmit: async (v) => { await api('/admin/links/' + l.link_key, { method: 'PUT', body: v }); reload(); } });
  const del = async (l) => { if (await confirmDialog('Delete “' + l.name + '”?', 'It will disappear from everyone’s home page.', 'Delete', true)) { try { await api('/admin/links/' + l.link_key, { method: 'DELETE' }); reload(); } catch (ex) { alert(ex.message); } } };

  panel.replaceChildren(
    h('p', { class: 'note', style: 'margin-bottom:12px', text: 'Apps appear as buttons after sign-in. Add each Apps Script web app here and choose which roles may open it.' }),
    h('div', { class: 'row', style: 'margin-bottom:14px' }, h('span', { class: 'grow' }), h('button', { class: 'btn primary', text: '+ Add app', onclick: add })),
    links.length ? h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Name', 'Address', 'Roles', 'Status', ''].map((t) => h('th', { text: t })))),
      h('tbody', {}, links.map((l) => h('tr', {}, h('td', { text: l.name }), h('td', { text: l.url, style: 'word-break:break-all;max-width:340px' }),
        h('td', { text: l.roles.map(roleLabel).join(', ') || 'Admins only' }),
        h('td', {}, h('span', { class: 'pill' + (l.active ? '' : ' off'), text: l.active ? 'Active' : 'Hidden' })),
        h('td', {}, h('div', { class: 'row' }, h('button', { class: 'btn small', text: 'Edit', onclick: () => edit(l) }), h('button', { class: 'btn small danger', text: 'Delete', onclick: () => del(l) }))))))))
      : h('div', { class: 'card empty', text: 'No apps yet. Click “Add app” and paste your Apps Script web address.' }));
}

// ------------------------------------------------------------------ security (client key)
async function renderSecurity(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let sec;
  try { sec = await api('/admin/security'); } catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  const sourceLabel = { database: 'Set from this dashboard', environment: 'Set via Render environment variable (not yet rotated here)', none: 'Not configured - order endpoints are currently open to any caller' }[sec.client_key_source];
  const rotate = async () => {
    if (!(await confirmDialog('Rotate the client key?', 'Every Apps Script deployment using the old key will immediately stop being able to read or write orders, until you update its CLIENT_KEY Script Property with the new value.', 'Rotate', true))) return;
    try {
      const res = await api('/admin/security/client-key/rotate', { method: 'POST' });
      secretDialog('New client key', 'Copy this into the ONE authorized Apps Script project: Project Settings → Script Properties → CLIENT_KEY. It will not be shown again.', res.client_key);
      renderSecurity(panel);
    } catch (ex) { alert(ex.message); }
  };
  panel.replaceChildren(
    h('div', { class: 'card' },
      h('h2', { style: 'font-size:15px;margin-bottom:10px', text: 'Apps Script client key' }),
      h('p', { class: 'note', style: 'margin-bottom:14px', text: 'Only the Apps Script deployment holding the current key may read or write orders through the API. Rotating it invalidates any other copy - including any extra Google Sheet you don’t want connected.' }),
      h('p', {}, h('span', { class: 'pill' + (sec.client_key_configured ? '' : ' off'), text: sec.client_key_configured ? 'Configured' : 'Not configured' }), ' ', h('span', { class: 'note', text: sourceLabel })),
      h('div', { class: 'actions', style: 'justify-content:flex-start;margin-top:14px' }, h('button', { class: 'btn primary', text: sec.client_key_configured ? 'Rotate key' : 'Generate key', onclick: rotate }))));
}

// ------------------------------------------------------------------ permissions (field access per role)
async function renderPermissions(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let data, viewData;
  try { [data, viewData] = await Promise.all([api('/admin/permissions'), api('/admin/view-access')]); }
  catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  const byKey = {};
  data.editable.forEach((e) => { byKey[e.role + '|' + e.field_name] = e.editable; });

  const toggle = async (role, field, checked, cb) => {
    cb.disabled = true;
    try { await api('/admin/permissions', { method: 'PUT', body: { role, field_name: field, editable: checked } }); }
    catch (ex) { cb.checked = !checked; alert(ex.message); }
    cb.disabled = false;
  };

  const head = h('tr', {}, h('th', { text: 'Field' }), ...data.roles.map((r) => h('th', { class: 'num', text: roleLabel(r) })));
  const body = data.fields.map((f) => h('tr', {}, h('td', { text: f }),
    ...data.roles.map((r) => {
      const cb = h('input', { type: 'checkbox' });
      cb.checked = !!byKey[r + '|' + f];
      cb.addEventListener('change', () => toggle(r, f, cb.checked, cb));
      return h('td', { class: 'num' }, cb);
    })));

  // View access: roles with no built-in order workflow (cashier, accounts, cartage) -
  // whether they can see orders at all is a plain on/off switch, off by default.
  const viewByRole = {};
  viewData.access.forEach((v) => { viewByRole[v.role] = v.can_view; });
  const toggleView = async (role, checked, cb) => {
    cb.disabled = true;
    try { await api('/admin/view-access', { method: 'PUT', body: { role, can_view: checked } }); }
    catch (ex) { cb.checked = !checked; alert(ex.message); }
    cb.disabled = false;
  };
  const viewRows = viewData.roles.map((r) => {
    const cb = h('input', { type: 'checkbox' });
    cb.checked = !!viewByRole[r];
    cb.addEventListener('change', () => toggleView(r, cb.checked, cb));
    return h('tr', {}, h('td', { text: roleLabel(r) }), h('td', { class: 'num' }, cb));
  });

  panel.replaceChildren(
    h('div', { class: 'card', style: 'margin-bottom:16px' },
      h('h2', { style: 'font-size:15px;margin-bottom:8px', text: 'View access' }),
      h('p', { class: 'note', style: 'margin-bottom:12px', text: 'Roles with no order-entry role of their own (cashier, accounts, cartage) have no visibility into orders until you turn it on here. Read-only either way - this never grants editing.' }),
      h('div', { class: 'table-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', { text: 'Role' }), h('th', { class: 'num', text: 'Can view all orders' }))), h('tbody', {}, viewRows)))),
    h('p', { class: 'note', style: 'margin-bottom:12px', text: 'Which order fields each operating role may set. Unchecking a field a role currently relies on will start rejecting their saves immediately - change with care.' }),
    h('div', { class: 'table-wrap' }, h('table', {}, h('thead', {}, head), h('tbody', {}, body))));
}

// ------------------------------------------------------------------ dropdown values (lookups + people)
const LIST_KINDS = [
  { key: 'channels', label: 'Order Channels', kind: 'lookup' },
  { key: 'submission-types', label: 'Submission Types', kind: 'lookup' },
  { key: 'delivery-statuses', label: 'Delivery Statuses', kind: 'lookup' },
  { key: 'payment-statuses', label: 'Payment Statuses', kind: 'lookup' },
  { key: 'ready_by', label: 'Ready-By People', kind: 'person' },
  { key: 'colour_making', label: 'Colour-Making People', kind: 'person' },
  { key: 'delivery', label: 'Delivery People', kind: 'person' },
];

async function renderLists(panel) {
  panel.replaceChildren(
    h('p', { class: 'note', style: 'margin-bottom:12px', text: 'Values shown in every dropdown across the order-entry apps. Deleting is blocked while any order still uses that value.' }),
    ...LIST_KINDS.map((k) => h('div', { class: 'card', style: 'margin-bottom:16px', id: 'listcard_' + k.key })));
  LIST_KINDS.forEach((k) => renderOneList(document.getElementById('listcard_' + k.key), k));
}

async function renderOneList(container, k) {
  container.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let rows;
  try { rows = k.kind === 'lookup' ? await api('/admin/lookups/' + k.key) : await api('/admin/people?role=' + k.key); }
  catch (ex) { return container.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  const reload = () => renderOneList(container, k);
  const label = (r) => (k.kind === 'lookup' ? r.name : r.full_name);
  const path = (r) => (k.kind === 'lookup' ? '/admin/lookups/' + k.key + '/' + r.key : '/admin/people/' + r.key);

  const del = async (r) => {
    if (!(await confirmDialog('Delete "' + label(r) + '"?', 'Only works if no order currently uses this value.', 'Delete', true))) return;
    try { await api(path(r), { method: 'DELETE' }); reload(); } catch (ex) { alert(ex.message); }
  };

  const editRow = (r) => {
    if (k.kind === 'lookup') {
      formDialog({ title: 'Rename', fields: [{ name: 'name', label: 'Name', value: r.name, required: true, maxlength: 150 }],
        onSubmit: async (v) => { await api(path(r), { method: 'PUT', body: v }); reload(); } });
    } else {
      formDialog({ title: 'Edit person', fields: [
        { name: 'full_name', label: 'Name', value: r.full_name, required: true, maxlength: 150 },
        { name: 'phone_number', label: 'Phone (optional)', value: r.phone_number || '' }],
        onSubmit: async (v) => { await api(path(r), { method: 'PUT', body: { full_name: v.full_name, phone_number: v.phone_number || null } }); reload(); } });
    }
  };

  const addRow = () => {
    if (k.kind === 'lookup') {
      formDialog({ title: 'Add ' + k.label, submitLabel: 'Add', fields: [{ name: 'name', label: 'Name', required: true, maxlength: 150 }],
        onSubmit: async (v) => { await api('/admin/lookups/' + k.key, { method: 'POST', body: v }); reload(); } });
    } else {
      formDialog({ title: 'Add person', submitLabel: 'Add', fields: [
        { name: 'full_name', label: 'Name', required: true, maxlength: 150 },
        { name: 'phone_number', label: 'Phone (optional)' }],
        onSubmit: async (v) => { await api('/admin/people?role=' + k.key, { method: 'POST', body: { full_name: v.full_name, phone_number: v.phone_number || null } }); reload(); } });
    }
  };

  container.replaceChildren(
    h('div', { class: 'row', style: 'margin-bottom:10px' },
      h('h2', { style: 'font-size:14px;margin:0', text: k.label }),
      h('span', { class: 'grow' }),
      h('button', { class: 'btn small primary', text: '+ Add', onclick: addRow })),
    rows.length ? h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', { text: 'Name' }), k.kind === 'person' ? h('th', { text: 'Phone' }) : null, h('th', { text: '' }))),
      h('tbody', {}, rows.map((r) => h('tr', {},
        h('td', { text: label(r) }),
        k.kind === 'person' ? h('td', { text: r.phone_number || '' }) : null,
        h('td', {}, h('div', { class: 'row' },
          h('button', { class: 'btn small', text: 'Edit', onclick: () => editRow(r) }),
          h('button', { class: 'btn small danger', text: 'Delete', onclick: () => del(r) }))))))))
      : h('div', { class: 'empty', text: 'Nothing yet.' }));
}

// ------------------------------------------------------------------ activity
async function renderActivity(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let rows;
  try { rows = await api('/admin/audit?limit=200'); } catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  const detail = (d) => { if (!d) return ''; const t = JSON.stringify(d); return t.length > 140 ? t.slice(0, 137) + '…' : t; };
  panel.replaceChildren(
    h('p', { class: 'note', style: 'margin-bottom:12px', text: 'Every member, app and export action taken in this portal (latest 200).' }),
    rows.length ? tableFor([{ head: 'When', get: (r) => fmtDateTime(r.at) }, { head: 'Admin', get: (r) => r.admin_username }, { head: 'Action', get: (r) => r.action },
      { head: 'Target', get: (r) => r.target || '' }, { head: 'Details', get: (r) => detail(r.details) }], rows)
      : h('div', { class: 'card empty', text: 'Nothing recorded yet.' }));
}

// ------------------------------------------------------------------ start
document.addEventListener('visibilitychange', () => { if (document.hidden) hideTip(); });
if (S.token) route(); else showLogin();
