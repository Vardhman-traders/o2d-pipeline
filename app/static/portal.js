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
const S = { token: sessionStorage.getItem('vt_token'), user: null, tab: 'dashboard', timer: null,
            range: { preset: '30d', from: null, to: null }, tableView: {} };

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
function dialog(title, bodyNodes, actionNodes) {
  const dlg = h('dialog', {}, h('h2', { text: title }), ...bodyNodes, h('div', { class: 'actions' }, ...actionNodes));
  dlg.addEventListener('close', () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
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
  mount(h('div', { class: 'card center-card' }, h('div', { class: 'logo', text: 'VT' }),
    h('h1', { text: 'Vardhman Traders' }), h('p', { class: 'sub', text: 'Sign in to continue' }), form));
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
  mount(h('div', { class: 'card center-card' }, h('div', { class: 'logo', text: 'VT' }),
    h('h1', { text: 'Change your password' }),
    h('p', { class: 'sub', text: forced ? 'You must choose a new password before continuing.' : 'Choose a new password.' }), form));
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
  const win = window.open('', '_blank', 'noopener,noreferrer');
  try {
    const { ticket } = await api('/auth/sso-ticket', { method: 'POST' });
    const joined = url + (url.includes('?') ? '&' : '?') + 'ssoTicket=' + encodeURIComponent(ticket);
    if (win) win.location = joined; else window.open(joined, '_blank', 'noopener,noreferrer');
  } catch (ex) {
    if (win) win.close();
    alert('Could not open this app: ' + ex.message);
  }
}

function topbar(links) {
  return h('header', { class: 'topbar' },
    h('div', { class: 'brand' }, h('span', { class: 'badge', text: 'VT' }), 'Vardhman Traders'),
    ...links.map((l) => safeHttps(l.url) && h('button', { class: 'btn', text: 'Open ' + l.name + ' ↗', onclick: () => openAppLink(l.url) })),
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
  const tabs = [['dashboard', 'Dashboard'], ['members', 'Members'], ['apps', 'Apps'], ['activity', 'Activity log']];
  const bar = h('div', { class: 'tabs', role: 'tablist' });
  const draw = () => {
    bar.replaceChildren(...tabs.map(([id, label]) => h('button', {
      class: 'tab', role: 'tab', 'aria-selected': String(S.tab === id), text: label,
      onclick: () => { S.tab = id; draw(); openTab(); } })));
  };
  const openTab = () => {
    stopTimer(); hideTip();
    // A fresh panel per visit: a slow response from a tab you already left writes into a detached node.
    const panel = h('div', { id: 'panel' });
    holder.replaceChildren(panel);
    ({ dashboard: renderDashboard, members: renderMembers, apps: renderApps, activity: renderActivity })[S.tab](panel);
  };
  body.append(bar, holder);
  draw(); openTab();
}

function renderHome(body, links) {
  const tiles = links.map((l) => {
    const url = safeHttps(l.url);
    return url && h('button', { class: 'app-tile', onclick: () => openAppLink(url) },
      h('strong', { text: l.name }), h('span', { text: 'Open ↗' }));
  });
  body.append(h('h1', { text: 'Welcome, ' + S.user.display_name, style: 'font-size:20px' }),
    links.length ? h('p', { class: 'note', text: 'Your apps' }) : h('p', { class: 'note', style: 'margin-top:8px', text: 'No apps have been assigned to you yet. Please contact the administrator.' }),
    h('div', { class: 'apps' }, ...tiles));
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

function barList(rows, { valueKey = 'orders', fmt = fmtInt, tip }) {
  const max = Math.max(...rows.map((r) => Number(r[valueKey]) || 0), 1);
  return h('div', { class: 'bars' }, rows.map((r) => {
    const v = Number(r[valueKey]) || 0;
    const el = h('div', { class: 'bar-row', tabindex: '0' },
      h('div', { class: 'bar-label', title: r.label, text: r.label }),
      h('div', { class: 'bar-track' }, h('div', { class: 'bar-fill', style: `width:${(v / max) * 100}%` })),
      h('div', { class: 'bar-val', text: fmt(v) }));
    const show = (e) => showTip(e, r.label, tip ? tip(r) : [[fmt(v), valueKey]]);
    el.addEventListener('pointermove', show); el.addEventListener('focus', show);
    el.addEventListener('pointerleave', hideTip); el.addEventListener('blur', hideTip);
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

function columnSvg(rows, { fmt = fmtInt, tip, labelEvery, height = 210 }, W) {
  const H = height, m = { l: 46, r: 8, t: 16, b: 24 };
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
    const hit = s('rect', { class: 'hit', x: cx - slot / 2, y: m.t, width: slot, height: ph, tabindex: '0', 'aria-label': `${r.label}: ${fmt(r.value)}` });
    const show = (e) => { if (bar) bar.classList.add('hot'); showTip(e, r.label, tip ? tip(r) : [[fmt(r.value), '']]); };
    const hide = () => { if (bar) bar.classList.remove('hot'); hideTip(); };
    hit.addEventListener('pointermove', show); hit.addEventListener('focus', show);
    hit.addEventListener('pointerleave', hide); hit.addEventListener('blur', hide);
    svg.append(hit);
  });
  return svg;
}

// ------------------------------------------------------------------ dashboard
function rangeFromPreset() {
  const t = todayIST(), r = S.range;
  if (r.preset === 'custom') return { from: r.from || t, to: r.to || t };
  if (r.preset === 'today') return { from: t, to: t };
  if (r.preset === '7d') return { from: shiftDate(t, -6), to: t };
  if (r.preset === 'month') return { from: t.slice(0, 8) + '01', to: t };
  if (r.preset === '90d') return { from: shiftDate(t, -89), to: t };
  return { from: shiftDate(t, -29), to: t };
}

function delta(cur, prev, goodWhenUp = true, label = 'previous period') {
  if (!delta.comparable) return null; // earlier period is only partly covered by the data: a % would mislead
  if (prev == null || cur == null || prev === 0) return h('span', { class: 'delta flat', text: prev === 0 && cur > 0 ? `new vs ${label}` : `no ${label} data` });
  const pct = (cur - prev) / prev;
  if (Math.abs(pct) < 0.005) return h('span', { class: 'delta flat', text: `▬ flat vs ${label}` });
  const up = pct > 0, good = up === goodWhenUp;
  return h('span', { class: `delta ${up ? 'up' : 'down'}-${good ? 'good' : 'bad'}`, text: `${up ? '▲' : '▼'} ${nDec.format(Math.abs(pct) * 100)}% vs ${label}` });
}

function tile(label, value, sub, hero) {
  return h('div', { class: 'tile' + (hero ? ' hero' : '') }, h('div', { class: 'label', text: label }), h('div', { class: 'value', text: value }), h('div', { class: 'sub' }, sub));
}

async function renderDashboard(panel) {
  const dateFrom = h('input', { type: 'date', 'aria-label': 'From date' });
  const dateTo = h('input', { type: 'date', 'aria-label': 'To date' });
  const stamp = h('span', { text: 'Loading…' });
  const body = h('div', { id: 'dash-body' });
  const dl = h('button', { class: 'btn primary', text: '⬇ Download Excel' });
  const presets = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['month', 'This month'], ['90d', '90 days']];
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Period' });
  let busy = false, last = null;

  const syncControls = () => {
    const r = rangeFromPreset();
    dateFrom.value = r.from; dateTo.value = r.to;
    seg.replaceChildren(...presets.map(([id, label]) => h('button', { 'aria-pressed': String(S.range.preset === id), text: label,
      onclick: () => { S.range = { preset: id }; syncControls(); load(); } })));
  };
  const custom = () => { S.range = { preset: 'custom', from: dateFrom.value, to: dateTo.value }; seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', 'false')); if (dateFrom.value && dateTo.value && dateFrom.value <= dateTo.value) load(); };
  dateFrom.addEventListener('change', custom); dateTo.addEventListener('change', custom);

  async function load() {
    if (busy) return;
    busy = true; body.classList.add('loading');
    const r = rangeFromPreset();
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
    const r = rangeFromPreset();
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
    h('div', { class: 'filters' }, seg, dateFrom, h('span', { class: 'note', text: 'to' }), dateTo, dl,
      h('span', { class: 'live' }, h('span', { class: 'dot' }), 'Live · refreshes every 30 s · ', stamp)),
    body);
  syncControls();
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
  const pct = (n) => (hd.orders ? fmtPct(n / hd.orders) + ' of orders' : '');
  const periodLabel = `${fmtDate(d.range.from)} – ${fmtDate(d.range.to)} · ${days} day${days > 1 ? 's' : ''}`;
  const prevLabel = `prior ${days} day${days > 1 ? 's' : ''}`;

  const kpi1 = h('div', { class: 'kpis' },
    tile('Orders received', fmtInt(hd.orders), [periodLabel, h('br'), delta(hd.orders, pv.orders, true, prevLabel)], true),
    tile('Delivered', fmtInt(hd.delivered), [pct(hd.delivered), h('br'), delta(hd.delivered, pv.delivered, true, prevLabel)]),
    tile('Fully closed', fmtInt(hd.closed), pct(hd.closed)),
    tile('Cancelled', fmtInt(hd.cancelled), [pct(hd.cancelled), h('br'), delta(hd.cancelled, pv.cancelled, false, prevLabel)]));
  const kpi2 = h('div', { class: 'kpis second' },
    tile('Typical order-to-delivery time', fmtHours(hd.median_hours), (() => { const dm = delta(hd.median_hours, pv.median_hours, false, prevLabel); return dm ? ['median · ', dm] : 'median'; })()),
    tile('Delivered within 24 h', fmtPct(hd.within_24h), 'of delivered orders'),
    tile('Amount received', fmtMoney(hd.amount_received), delta(Number(hd.amount_received), Number(pv.amount_received), true, prevLabel)),
    tile('Cartage', fmtMoney(hd.cartage), delta(Number(hd.cartage), Number(pv.cartage), false, prevLabel)));

  // orders per day
  const daily = d.daily.map((x) => ({ ...x, label: fmtDate(x.date), value: x.orders }));
  const perDay = chartCard('daily', 'Orders per day', periodLabel,
    () => columnChart(daily, { tip: (r) => [[fmtInt(r.orders), 'orders'], [fmtInt(r.cancelled), 'cancelled'], [fmtHours(r.avg_hours), 'avg to deliver']].concat(r.holiday ? [['Monday', 'holiday']] : []) }),
    [{ head: 'Date', get: (r) => fmtDate(r.date) }, { head: 'Orders', num: 1, get: (r) => fmtInt(r.orders) }, { head: 'Cancelled', num: 1, get: (r) => fmtInt(r.cancelled) }, { head: 'Avg to deliver', num: 1, get: (r) => fmtHours(r.avg_hours) }],
    d.daily.filter((x) => x.orders > 0 || !x.holiday), null);

  // pipeline
  const pipe = d.pipeline;
  const pipeCard = chartCard('pipeline', 'Where open orders are right now', 'All dates, not just the selected period',
    () => barList(pipe, { tip: (r) => [[fmtInt(r.orders), 'open orders'], [r.oldest ? 'since ' + fmtDate(r.oldest) : '', 'oldest']] }),
    [{ head: 'Stage', get: (r) => r.label }, { head: 'Orders', num: 1, get: (r) => fmtInt(r.orders) }, { head: 'Oldest', get: (r) => fmtDate(r.oldest) }], pipe,
    h('p', { class: 'note', style: 'margin-top:10px', text: 'Awaiting godown = no delivery status yet. Awaiting dispatch = not delivered. Awaiting receiving = delivered but date received or payment status missing.' }));

  // delivery time
  const buckets = d.delivery_time_buckets.map((b) => ({ label: b.bucket, value: b.n }));
  const timeCard = chartCard('o2d', 'Order-to-delivery time', `Median ${fmtHours(hd.median_hours)} · 9 in 10 within ${fmtHours(hd.p90_hours)} · average ${fmtHours(hd.avg_hours)}`,
    () => columnChart(buckets, { labelEvery: 1, tip: (r) => [[fmtInt(r.value), 'orders']] }),
    [{ head: 'Time from logging to delivery', get: (r) => r.label }, { head: 'Orders', num: 1, get: (r) => fmtInt(r.value) }], buckets, null);

  // money by day
  const money = d.daily.map((x) => ({ ...x, label: fmtDate(x.date), value: Number(x.amount_received) }));
  const moneyCard = chartCard('money', 'Amount received per day', 'Rupees, by order date',
    () => columnChart(money, { fmt: fmtMoney, tip: (r) => [[fmtMoney(r.value), 'received'], [fmtMoney(r.cartage), 'cartage']] }),
    [{ head: 'Date', get: (r) => fmtDate(r.date) }, { head: 'Received', num: 1, get: (r) => fmtMoney(r.amount_received) }, { head: 'Cartage', num: 1, get: (r) => fmtMoney(r.cartage) }],
    d.daily.filter((x) => Number(x.amount_received) > 0 || Number(x.cartage) > 0), null);

  const simple = (id, title, sub, rows, valueKey = 'orders', headName = 'Orders') => chartCard(id, title, sub,
    () => barList(rows, { valueKey }), [{ head: title.replace(/^By /, ''), get: (r) => r.label }, { head: headName, num: 1, get: (r) => fmtInt(r[valueKey]) }], rows, null);

  const channel = chartCard('channel', 'By order channel', 'Orders in period',
    () => barList(d.by_channel, { tip: (r) => [[fmtInt(r.orders), 'orders'], [fmtMoney(r.amount_received), 'received']] }),
    [{ head: 'Channel', get: (r) => r.label }, { head: 'Orders', num: 1, get: (r) => fmtInt(r.orders) }, { head: 'Received', num: 1, get: (r) => fmtMoney(r.amount_received) }], d.by_channel, null);
  const payment = chartCard('payment', 'By payment status', 'Orders in period',
    () => barList(d.by_payment_status, { tip: (r) => [[fmtInt(r.orders), 'orders'], [fmtMoney(r.amount_received), 'received']] }),
    [{ head: 'Payment status', get: (r) => r.label }, { head: 'Orders', num: 1, get: (r) => fmtInt(r.orders) }, { head: 'Received', num: 1, get: (r) => fmtMoney(r.amount_received) }], d.by_payment_status, null);

  const hours = d.by_hour.map((x) => ({ label: String(x.hour).padStart(2, '0'), value: x.orders }));
  const hourCard = chartCard('hours', 'When orders are logged', 'By hour of day (India time)',
    () => columnChart(hours, { labelEvery: 3, tip: (r) => [[fmtInt(r.value), `orders at ${r.label}:00`]] }),
    [{ head: 'Hour', get: (r) => r.label + ':00' }, { head: 'Orders', num: 1, get: (r) => fmtInt(r.value) }], hours.some((x) => x.value) ? hours : [], null);

  const delivered = chartCard('deliveredby', 'By delivery person', 'Orders delivered in period',
    () => barList(d.delivered_by, { tip: (r) => [[fmtInt(r.orders), 'orders'], [fmtHours(r.avg_hours), 'avg to deliver'], [fmtMoney(r.cartage), 'cartage']] }),
    [{ head: 'Person', get: (r) => r.label }, { head: 'Orders', num: 1, get: (r) => fmtInt(r.orders) }, { head: 'Avg time', num: 1, get: (r) => fmtHours(r.avg_hours) }, { head: 'Cartage', num: 1, get: (r) => fmtMoney(r.cartage) }], d.delivered_by, null);

  const oldest = h('section', { class: 'chart-card wide' },
    h('div', { class: 'chart-head' }, h('div', {}, h('h3', { text: 'Longest-waiting open orders' }), h('div', { class: 'sub', text: 'Oldest 10 orders that are not closed or cancelled (any date)' }))),
    d.oldest_open.length ? tableFor([
      { head: 'Sl No', get: (r) => r.sl_no }, { head: 'DC / Inv', get: (r) => r.dc_inv_no || '' }, { head: 'Order date', get: (r) => fmtDate(r.order_date) },
      { head: 'Stage', get: (r) => r.stage }, { head: 'Delivery status', get: (r) => r.delivery_status || '–' },
      { head: 'Waiting', num: 1, get: (r) => fmtHours(Number(r.age_hours)) }, { head: 'Location', get: (r) => r.shipping_location || '' }], d.oldest_open)
      : h('div', { class: 'empty', text: 'Nothing is waiting. 🎉' }));

  return [kpi1, kpi2,
    h('div', { class: 'grid' }, h('div', { class: 'wide' }, perDay)),
    h('div', { class: 'grid' }, pipeCard, timeCard),
    h('div', { class: 'grid' }, channel, simple('subtype', 'By submission type', 'Orders in period', d.by_submission_type),
      simple('dstatus', 'By delivery status', 'Orders in period', d.by_delivery_status), payment),
    h('div', { class: 'grid' }, moneyCard, hourCard),
    h('div', { class: 'grid three' }, simple('readyby', 'By ready-by person', 'Orders prepared', d.ready_by), delivered,
      h('div', {}, simple('colour', 'By colour-making person', 'Orders', d.colour_making_by), h('div', { style: 'height:14px' }), simple('createdby', 'Data entry by staff', 'Orders logged', d.created_by))),
    h('div', { class: 'grid' }, oldest)];
}

// ------------------------------------------------------------------ members
const ROLE_HELP = { shop: 'Shop', godown: 'Godown', shop_dispatch: 'Shop dispatch', godown_dispatch: 'Godown dispatch', receiving: 'Receiving', admin: 'Admin', cashier: 'Cashier', accounts: 'Accounts', cartage: 'Cartage' };
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
          !me && !u.disabled && h('button', { class: 'btn small danger', text: 'Disable', onclick: () => disable(u) }))));
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
