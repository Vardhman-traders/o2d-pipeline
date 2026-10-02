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

// A link may be an external https address or one of this app's own pages (a path such as /sales/).
function safeHttps(url) {
  if (typeof url === 'string' && url.startsWith('/') && !url.startsWith('//')) return url;
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
  S.token = null; S.user = null; S.view = null;
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
      rows.length ? tableFor(cols, rows, (r) => openOrderDetail(r.sl_no)) : h('div', { class: 'empty', text: 'No matching orders.' }));
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

// Own pages share this browser session (the sign-in lives in sessionStorage), so opening one is a plain
// navigation: no hand-off ticket, and the page picks the sign-in up by itself.
function openAppLink(url) {
  window.location.href = url;
}


function topbar(onHome) {
  const brand = h('div', { class: 'brand' }, h('div', { class: 'brand-mark', text: 'VT' }),
    h('div', { class: 'brand-text' }, h('span', { class: 'name', text: 'Vardhman Traders' }), h('span', { class: 'tag', text: 'Sales & Operations Portal' })));
  return h('header', { class: 'topbar' },
    onHome ? h('button', { class: 'brand brand-btn', title: 'Back to home', 'aria-label': 'Back to home', onclick: onHome }, ...brand.childNodes) : brand,
    onHome ? h('button', { class: 'btn', id: 'homeBtn', text: '⌂ Home', onclick: onHome }) : null,
    h('span', { class: 'who' }, S.user.display_name, h('span', { class: 'role-chip', text: S.user.role.replace('_', ' ') })),
    h('button', { class: 'btn', text: 'Change password', onclick: () => showChangePassword(false) }),
    h('button', { class: 'btn', text: 'Sign out', onclick: () => signOut() }));
}

// Home screen of big tiles, then modules, each with its own sub-tabs. A section may name the page that
// switches it on (4th item); admins have every page, everyone else only the pages they were given.
const can = (page) => !!S.user && (S.user.role === 'admin' || (S.user.pages || []).includes(page));
const canAny = (pages) => pages.some(can);
const DASHBOARD_PAGE_KEYS = ['dashboard_overview', 'dashboard_orders'];
const O2D_PAGE_KEYS = ['o2d_overview', 'o2d_shop', 'o2d_godown', 'o2d_shop_dispatch', 'o2d_godown_dispatch', 'o2d_receiving'];

const MODULES = {
  dashboard: { title: 'Dashboard', icon: '📊', stateKey: 'dashTab', hideTitle: true,
    sections: [['overview', 'Overview', (p) => renderDashboard(p), 'dashboard_overview'],
      ['orders', 'All orders', (p) => renderOrders(p), 'dashboard_orders']] },
  setup: { title: 'Setup', icon: '⚙️', stateKey: 'setupTab',
    sections: [['members', 'Members', (p) => renderMembers(p)], ['import', 'Import', (p) => renderImport(p)],
      ['lists', 'Dropdown values', (p) => renderReconcile(p)], ['access', 'Access & permissions', (p) => renderAccessHub(p)],
      ['schedule', 'Weekly off', (p) => renderWeeklyOff(p)]] },
};
const sectionsOf = (mod) => mod.sections.filter((sec) => !sec[3] || can(sec[3]));

function renderModule(body, key) {
  const mod = MODULES[key];
  const secs = sectionsOf(mod);
  if (!secs.length) return body.append(noAccessCard(mod.title));
  if (!secs.some(([id]) => id === S[mod.stateKey])) S[mod.stateKey] = secs[0][0];
  const bar = h('div', { class: 'subtabs module-tabs', role: 'tablist' });
  const inner = h('div');
  // Top-right of the tab row: each section may park its main action (Download / Export) here.
  const actions = h('div', { class: 'module-actions', id: 'moduleActions' });
  const openInner = () => {
    stopTimer(); hideTip();
    actions.replaceChildren(); S.actionsSlot = actions;
    // A fresh panel per visit: a slow response from a section you already left writes into a detached node.
    const panel = h('div', { id: 'panel' });
    inner.replaceChildren(panel);
    secs.find(([id]) => id === S[mod.stateKey])[2](panel);
  };
  const drawBar = () => bar.replaceChildren(...secs.map(([id, label]) => h('button', {
    class: 'subtab', role: 'tab', 'aria-selected': String(S[mod.stateKey] === id), text: label + (id === 'access' && S.pendingRequests ? ` (${S.pendingRequests})` : ''),
    onclick: () => { S[mod.stateKey] = id; drawBar(); openInner(); } })));
  S.redrawModuleBar = drawBar;
  document.documentElement.style.setProperty('--filters-h', '0px'); // only the orders list stacks filters under the tabs
  // Dashboard's tabs already say where you are, so its big title is dropped (kept for Setup).
  const head = h('div', { class: 'module-head' }, bar, actions);
  body.append(mod.hideTitle ? h('h1', { class: 'sr-only', text: mod.title }) : h('h1', { class: 'module-title', text: mod.icon + ' ' + mod.title }),
    head, inner);
  trackSticky(head, '--head-h');
  trackSticky(document.querySelector('.topbar'), '--topbar-h');
  drawBar(); openInner();
}

async function refreshPendingRequests() {
  if (S.user?.role !== 'admin') return 0;
  try { S.pendingRequests = (await api('/admin/access/requests/count')).pending; } catch { /* badge just stays as it was */ }
  return S.pendingRequests || 0;
}

async function showPortal() {
  let links = [];
  try { links = await api('/links'); } catch { /* shown as empty */ }
  const admin = S.user.role === 'admin';
  const go = async (view) => {
    S.view = view;
    if (view === 'home') { // pick up access granted (or taken away) since sign-in
      try { S.user = await api('/auth/me'); } catch { /* keep what we have */ }
      await refreshPendingRequests();
    }
    draw();
  };
  const draw = () => {
    stopTimer(); hideTip();
    const modules = { dashboard: canAny(DASHBOARD_PAGE_KEYS), setup: admin };
    if (!modules[S.view]) S.view = 'home';
    const main = h('main', { class: 'wrap' });
    mount(topbar(S.view === 'home' ? null : () => go('home')), main);
    if (S.view !== 'home') return renderModule(main, S.view);
    const pending = S.pendingRequests || 0;
    renderHome(main, links.filter((l) => l.url !== '/sales/'), (linkTiles) => [
      modules.dashboard && appTile(MODULES.dashboard.icon, 'Dashboard', 'Open', () => go('dashboard'), 'tileDashboard'),
      canAny(O2D_PAGE_KEYS) && appTile('📋', 'O2D Portal', 'Open ↗', () => openAppLink('/sales/'), 'tileO2d'),
      ...linkTiles,
      admin && appTile(MODULES.setup.icon, 'Setup', pending ? `${pending} access request${pending === 1 ? '' : 's'} waiting` : 'Open', () => go('setup'), 'tileSetup')]);
  };
  if (admin) await refreshPendingRequests();
  draw();
}

// Emoji chosen by keyword in the app's name, so a shop/godown/dispatch/receiving
// tracker gets a sensible icon without admin having to pick one manually.
function iconForApp(name) {
  const n = (name || '').toLowerCase();
  if (n.includes('dispatch')) return '🚚';
  if (n.includes('receiv')) return '📥';
  if (n.includes('godown') || n.includes('warehouse') || n.includes('stock')) return '📦';
  if (n.includes('shop')) return '🧾';
  if (n.includes('o2d') || n.includes('sales') || n.includes('order') || n.includes('track')) return '📋';
  if (n.includes('report') || n.includes('dashboard')) return '📊';
  return '🔗';
}

function appTile(icon, name, hint, onclick, id) {
  return h('button', { class: 'app-tile', id, onclick },
    h('span', { class: 'icon-circle', text: icon }), h('strong', { text: name }), h('span', { text: hint }));
}
function renderHome(body, links, extra) {
  const linkTiles = links.map((l) => {
    const url = safeHttps(l.url);
    return url && appTile(iconForApp(l.name), l.name, 'Open ↗', () => openAppLink(url));
  });
  const tiles = (extra ? extra(linkTiles) : linkTiles).filter(Boolean);
  if (!tiles.length) return body.append(h('div', { class: 'home-wrap' }, h('div', { class: 'home-inner' }, noAccessCard())));
  body.append(h('div', { class: 'home-wrap' }, h('div', { class: 'home-inner' },
    h('h1', { text: 'Welcome, ' + S.user.display_name }),
    h('p', { class: 'note', text: 'Choose where to go' }),
    h('div', { class: 'apps' }, ...tiles),
    S.user.role === 'admin' ? null : h('p', { class: 'note', style: 'margin-top:18px' },
      'Need something that is not here? ', h('button', { class: 'linklike', id: 'requestMoreBtn', text: 'Request access', onclick: () => openAccessRequest() })))));
}

// ------------------------------------------------------------------ no access / request access
const statusPill = (st) => h('span', { class: 'pill' + (st === 'rejected' ? ' off' : st === 'pending' ? ' warn' : ''), text: st });

// The message a person sees instead of a blank page, with the way to ask an admin for access.
function noAccessCard(what) {
  const list = h('div', { id: 'myRequests' });
  const draw = async () => {
    try {
      const d = await api('/access/pages');
      list.replaceChildren(d.requests.length ? h('div', { class: 'table-wrap', style: 'margin-top:16px;text-align:left' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Page', 'Asked on', 'Status', 'Admin note'].map((t) => h('th', { text: t })))),
        h('tbody', {}, d.requests.map((r) => h('tr', {}, h('td', { text: (d.all_pages.find((p) => p.key === r.page_key) || {}).label || r.page_key }),
          h('td', { text: fmtDateTime(r.created_at) }), h('td', {}, statusPill(r.status)), h('td', { text: r.decision_note || '' })))))) : null);
    } catch { /* the card still works without the history */ }
  };
  draw();
  return h('div', { class: 'card no-access', id: 'noAccess' },
    h('div', { class: 'big-icon', text: '🔒' }),
    h('h1', { text: what ? `Access not provided: ${what}` : 'Access not provided yet' }),
    h('p', { text: what ? 'You do not have access to this page.' : 'You have not been given access to any page yet.' }),
    h('p', { class: 'note', text: 'Ask the administrator for access. They will see your request, with the reason you give, and can approve it.' }),
    h('div', { class: 'row', style: 'justify-content:center;margin-top:14px' },
      h('button', { class: 'btn primary', id: 'requestAccessBtn', text: 'Request access', onclick: () => openAccessRequest(draw) }),
      h('button', { class: 'btn', text: 'Check again', onclick: async () => { try { S.user = await api('/auth/me'); } catch { /* ignore */ } showPortal(); } })),
    list);
}

async function openAccessRequest(after) {
  let d;
  try { d = await api('/access/pages'); } catch (ex) { return alert(ex.message); }
  const open = new Set(d.requests.filter((r) => r.status === 'pending').map((r) => r.page_key));
  const options = d.all_pages.filter((p) => !d.pages.includes(p.key) && !open.has(p.key)).map((p) => [p.key, p.label]);
  if (!options.length) return alert(open.size ? 'Your requests are with the administrator already.' : 'You already have every page.');
  formDialog({
    title: 'Request access', submitLabel: 'Send request',
    fields: [
      { name: 'page_key', label: 'Which page do you need?', type: 'select', options, value: options[0][0] },
      { name: 'reason', label: 'Why do you need it?', required: true, maxlength: 500, hint: 'A line or two is enough. The administrator sees this.' }],
    onSubmit: async (v) => {
      await api('/access/request', { method: 'POST', body: { page_key: v.page_key, reason: v.reason } });
      if (after) after(); else alert('Request sent. The administrator will review it.');
    } });
}

// ------------------------------------------------------------------ charts
function tableFor(cols, rows, onRow) {
  return h('div', { class: 'table-wrap' }, h('table', {},
    h('thead', {}, h('tr', {}, cols.map((c) => h('th', { class: c.num ? 'num' : '', text: c.head })))),
    h('tbody', {}, rows.map((r) => h('tr', onRow ? { class: 'clickable-row', tabindex: '0', title: 'Open order details', onclick: () => onRow(r),
      onkeydown: (e) => { if (e.key === 'Enter') onRow(r); } } : {},
      cols.map((c) => h('td', { class: c.num ? 'num' : '', text: c.get(r) })))))));
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

// ------------------------------------------------------------------ multi-select
// A compact "Label (n)" button that opens a checkbox list with a search box, instead of a long
// always-open checkbox list. `selected` is a Set the caller owns; onChange fires after every toggle.
function multiSelect(label, options, selected, onChange) {
  const btn = h('button', { type: 'button', class: 'btn multiselect-btn', 'aria-haspopup': 'listbox', 'aria-expanded': 'false' },
    h('span', { class: 'ms-label', text: label }), h('span', { class: 'ms-count' }), h('span', { class: 'ms-caret', 'aria-hidden': 'true', text: '▾' }));
  const search = h('input', { type: 'text', placeholder: 'Search…', class: 'ms-search', 'aria-label': `Search ${label}` });
  const list = h('div', { class: 'ms-list' });
  const done = h('button', { type: 'button', class: 'btn small primary ms-done', text: 'Done', onclick: () => setOpen(false) });
  const panel = h('div', { class: 'ms-panel' }, options.length > 8 ? search : null, list, h('div', { class: 'ms-foot' }, done));
  const wrap = h('div', { class: 'multiselect' }, btn, panel);
  const setOpen = (open) => { wrap.classList.toggle('open', open); btn.setAttribute('aria-expanded', String(open)); };

  // The button shows what is chosen ("Stage: Closed", or "Stage: 3 selected"), so an active filter is visible at a glance.
  const refresh = () => {
    const n = selected.size;
    btn.querySelector('.ms-label').textContent = n ? label + ':' : label;
    btn.querySelector('.ms-count').textContent = !n ? '' : n === 1 ? ' ' + [...selected][0] : ` ${n} selected`;
    btn.classList.toggle('active', n > 0);
  };
  const draw = (q) => {
    const needle = (q || '').trim().toLowerCase();
    list.replaceChildren(...options.filter((v) => !needle || v.toLowerCase().includes(needle)).map((v) => {
      const box = h('input', { type: 'checkbox', checked: selected.has(v) || null });
      box.addEventListener('change', () => {
        if (box.checked) selected.add(v); else selected.delete(v);
        refresh(); onChange();  // applied straight away; the list stays open so several values can be ticked
      });
      return h('label', { class: 'check ms-item' }, box, v);
    }));
    if (!options.length) list.replaceChildren(h('p', { class: 'note', text: 'No values yet.' }));
    if (selected.size) list.prepend(h('button', { type: 'button', class: 'linklike ms-clear', text: 'Clear selection',
      onclick: () => { selected.clear(); refresh(); draw(search.value); onChange(); } }));
  };
  search.addEventListener('input', () => draw(search.value));
  draw('');
  refresh();

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = !wrap.classList.contains('open');
    document.querySelectorAll('.multiselect.open').forEach((m) => { if (m !== wrap) m.classList.remove('open'); }); // never two lists at once
    if (open) draw(search.value);
    setOpen(open);
    if (open) search.focus();
  });
  panel.addEventListener('click', (e) => e.stopPropagation());
  wrap.addEventListener('keydown', (e) => { if (e.key === 'Escape') { setOpen(false); btn.focus(); } });
  document.addEventListener('click', () => setOpen(false));
  wrap.refresh = refresh;
  return wrap;
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

// Default window is the last 30 days; the From / To date pickers on the page change it.
const DASHBOARD_WINDOW_DAYS = 30;
function defaultDashboardRange() {
  const to = todayIST();
  return { from: shiftDate(to, -(DASHBOARD_WINDOW_DAYS - 1)), to };
}

// Same attribute list the Orders page filters by; the dashboard filters by the same values.
const DASH_FILTER_GROUPS = [['stage', 'Stage'], ['channel', 'Order via'], ['submission_type', 'Submission type'],
  ['delivery_status', 'Delivery status'], ['payment_status', 'Payment status'], ['ready_by', 'Ready by'],
  ['colour_making_by', 'Colour making by'], ['delivered_by', 'Delivered by'], ['created_by', 'Logged by']];

async function renderDashboard(panel) {
  const slot = S.actionsSlot || h('div'); // top-right of the tab row (see renderModule)
  const stamp = h('span', { text: 'Loading…' });
  const body = h('div', { id: 'dash-body' });
  const dl = h('button', { class: 'btn primary', text: '⬇ Download Excel' });
  let busy = false, last = null;
  let options = {};
  try { options = await api('/admin/orders/filter-options'); } catch { /* filters just won't have choices */ }
  const dashSelections = {};
  const dflt = defaultDashboardRange();
  const fromEl = h('input', { type: 'date', id: 'dash_from', value: dflt.from, 'aria-label': 'From date' });
  const toEl = h('input', { type: 'date', id: 'dash_to', value: dflt.to, 'aria-label': 'To date' });
  const rangeMsg = h('span', { class: 'msg error', id: 'dash_range_msg' });
  const currentRange = () => ({ from: fromEl.value || dflt.from, to: toEl.value || dflt.to });
  const rangeOk = () => {
    const r = currentRange();
    const bad = r.from > r.to;
    rangeMsg.textContent = bad ? '"From" date must be on or before "To" date.' : '';
    return !bad;
  };
  const onRangeChange = () => { if (rangeOk()) load(); };
  fromEl.addEventListener('change', onRangeChange);
  toEl.addEventListener('change', onRangeChange);
  const resetBtn = h('button', { type: 'button', class: 'btn', id: 'dash_reset', text: 'Last 30 days',
    onclick: () => { const d = defaultDashboardRange(); fromEl.value = d.from; toEl.value = d.to; onRangeChange(); } });
  const dateBar = h('div', { class: 'filter-row' },
    h('span', { class: 'filter-label', text: 'Period' }),
    h('label', { class: 'date-field' }, 'From ', fromEl), h('label', { class: 'date-field' }, 'To ', toEl), resetBtn, rangeMsg);
  const filterBar = h('div', { class: 'filter-row' },
    h('span', { class: 'filter-label', text: 'Filter by' }),
    ...DASH_FILTER_GROUPS.map(([key, label]) => {
      dashSelections[key] = new Set();
      return multiSelect(label, options[key] || [], dashSelections[key], () => load());
    }));
  const archiveNote = h('div', { class: 'archive-note', id: 'archiveNote' },
    h('strong', { text: 'Archived orders are not included on this page. ' }),
    'An order is archived when it is at least 7 days old (by order date) and fully finished: ' +
    'either Closed (delivered, received and payment recorded) or Cancelled. ' +
    'Everything still open stays here, whatever its age. ' +
    'To see archived orders, go to the "All orders" tab and tick "Include archived orders".');
  const filterQuery = () => {
    const qs = new URLSearchParams();
    for (const [key] of DASH_FILTER_GROUPS) dashSelections[key].forEach((v) => qs.append(key, v));
    return qs.toString();
  };

  let pending = false;
  async function load() {
    if (busy) { pending = true; return; } // a filter/date change during a refresh is re-run right after
    busy = true; body.classList.add('loading');
    const r = currentRange();
    try {
      const fq = filterQuery();
      const d = await api(`/admin/dashboard?date_from=${r.from}&date_to=${r.to}` + (fq ? `&${fq}` : ''));
      last = d;
      const y = window.scrollY;
      body.replaceChildren(...dashboardNodes(d));
      window.scrollTo(0, y); // keep the reader's place across the 30 s refresh
      stamp.textContent = 'Updated ' + new Date().toLocaleTimeString('en-IN', { hour12: false });
    } catch (ex) {
      if (ex.message !== 'Session ended') body.replaceChildren(h('div', { class: 'card' }, h('div', { class: 'msg error', text: ex.message })));
    } finally {
      busy = false; body.classList.remove('loading');
      if (pending && panel.isConnected) { pending = false; load(); }
    }
  }

  dl.addEventListener('click', async () => {
    if (!rangeOk()) return;
    const r = currentRange();
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
    dateBar,
    filterBar,
    archiveNote,
    body);
  if (panel.isConnected) slot.replaceChildren(h('span', { class: 'live' }, h('span', { class: 'dot' }), 'Live · every 30 s · ', stamp), dl);
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
    topFunnel,
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
    h('div', { class: 'row', style: 'margin-bottom:14px' }, search, h('span', { class: 'grow' }), h('button', { class: 'btn', id: 'bulkMembersBtn', text: 'Add many (upload)', onclick: () => { S.setupTab = 'import'; S.importKind = 'users'; showPortal(); } }), h('button', { class: 'btn primary', text: '+ Add member', onclick: add })),
    h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Name', 'Username', 'Role', 'Status'].map((t) => h('th', { text: t })), h('th', { class: 'num', text: 'Orders logged' }), h('th', { text: '' }))), rowsBody)),
    h('p', { class: 'note', style: 'margin-top:10px', text: 'Operating roles (shop, godown, dispatch, receiving) work in the order tracker. Cashier, accounts and cartage can view all orders only if switched on under Setup.' }));
  draw();
}

// ------------------------------------------------------------------ permissions (field access per role)
async function renderWeeklyOff(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let data;
  try { data = await api('/admin/weekly-off-days'); }
  catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }

  const msg = h('div', { class: 'msg' });
  const boxes = data.day_names.map((name, i) => {
    const cb = h('input', { type: 'checkbox', checked: data.days.includes(i) || null });
    return h('label', { class: 'check' }, cb, name);
  });
  const save = async () => {
    const days = boxes.map((b, i) => (b.firstChild.checked ? i : null)).filter((d) => d !== null);
    msg.className = 'msg'; msg.textContent = 'Saving…';
    try {
      await api('/admin/weekly-off-days', { method: 'PUT', body: { days } });
      msg.className = 'msg ok'; msg.textContent = 'Saved.';
    } catch (ex) { msg.className = 'msg error'; msg.textContent = ex.message; }
  };
  panel.replaceChildren(h('div', { class: 'card', style: 'max-width:420px' },
    h('h2', { text: 'Weekly off' }),
    h('p', { class: 'note', text: 'Which day(s) the business is closed. The O2D dashboard uses this to skip to the right "previous working day" instead of a fixed Monday.' }),
    h('div', { class: 'checks', style: 'flex-direction:column;gap:8px;margin:12px 0' }, boxes),
    h('button', { class: 'btn primary', text: 'Save', onclick: save }), msg));
}

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

// ------------------------------------------------------------------ activity

// ------------------------------------------------------------------ start
document.addEventListener('visibilitychange', () => { if (document.hidden) hideTip(); });
if (S.token) route(); else showLogin();
