'use strict';
/* Admin tools: the Orders tab (filters, saved filters, Excel export) and the Import tab (bulk upload with template,
   validation, inline fixes, undo). Loaded after portal.js and uses its helpers (h, api, S, dialog, formDialog ...). */

const FILTER_GROUPS = [['stage', 'Stage'], ['channel', 'Order via'], ['submission_type', 'Submission type'],
  ['delivery_status', 'Delivery status'], ['payment_status', 'Payment status'], ['ready_by', 'Ready by'],
  ['colour_making_by', 'Colour making by'], ['delivered_by', 'Delivered by'], ['created_by', 'Logged by']];
const NUM_FILTERS = [['min_amount', 'Amount from'], ['max_amount', 'Amount to'], ['min_cartage', 'Cartage from'], ['max_cartage', 'Cartage to']];
const ORDER_COLS = [['sl_no', 'Sl'], ['order_date', 'Order date'], ['dc_inv_no', 'DC/Inv'], ['stage', 'Stage'], ['channel', 'Via'],
  ['delivery_status', 'Delivery'], ['payment_status', 'Payment'], ['amount_received', 'Amount'], ['cartage', 'Cartage'], ['hours_to_deliver', 'Hours']];

function queryFrom(filter, extra) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(filter)) {
    if (Array.isArray(v)) v.forEach((x) => qs.append(k, x)); else if (v !== '' && v != null && v !== false) qs.append(k, v);
  }
  for (const [k, v] of Object.entries(extra || {})) qs.set(k, v);
  return qs.toString();
}

// Downloads need the sign-in header, so fetch the file and hand the browser a blob.
async function downloadFile(path, fallbackName) {
  const res = await api(path, { raw: true });
  const blob = await res.blob();
  const name = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '')?.[1] || fallbackName;
  const a = h('a', { href: URL.createObjectURL(blob), download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

// ------------------------------------------------------------------ Orders
async function renderOrders(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let options, saved;
  try { [options, saved] = await Promise.all([api('/admin/orders/filter-options'), api('/admin/saved-filters')]); }
  catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }

  const st = { filter: {}, sort: 'order_date', dir: 'desc', offset: 0, limit: 50 };
  const inputs = {};
  const results = h('div');
  const savedSel = h('select', { id: 'savedFilters', 'aria-label': 'Saved filters' });

  const field = (label, el) => h('div', { class: 'field' }, h('label', {}, label), el);
  inputs.q = h('input', { type: 'text', placeholder: 'DC/Inv no, address, remarks', 'aria-label': 'Search', id: 'f_q' });
  inputs.date_from = h('input', { type: 'date', id: 'f_from' });
  inputs.date_to = h('input', { type: 'date', id: 'f_to' });
  inputs.cancelled = h('select', { id: 'f_cancelled' }, [['', 'Include cancelled'], ['exclude', 'Hide cancelled'], ['only', 'Only cancelled']].map(([v, l]) => h('option', { value: v, text: l })));
  inputs.include_archived = h('input', { type: 'checkbox', id: 'f_archived' });
  NUM_FILTERS.forEach(([k]) => { inputs[k] = h('input', { type: 'number', min: '0', step: 'any', id: 'f_' + k }); });
  const groups = FILTER_GROUPS.map(([key, label]) => {
    const boxes = (options[key] || []).map((v) => h('label', { class: 'check' }, h('input', { type: 'checkbox', value: v, 'data-group': key }), v));
    inputs[key] = boxes;
    return h('details', { class: 'fgroup', 'data-key': key }, h('summary', { text: label }), h('div', { class: 'checks' }, boxes));
  });

  const collect = () => {
    const f = {};
    for (const k of ['q', 'date_from', 'date_to', 'cancelled', ...NUM_FILTERS.map((n) => n[0])]) { const v = inputs[k].value.trim(); if (v) f[k] = v; }
    if (inputs.include_archived.checked) f.include_archived = true;
    for (const [key] of FILTER_GROUPS) {
      const on = inputs[key].map((b) => b.firstChild).filter((c) => c.checked).map((c) => c.value);
      if (on.length) f[key] = on;
    }
    return f;
  };
  const apply = (f) => {
    for (const k of ['q', 'date_from', 'date_to', 'cancelled', ...NUM_FILTERS.map((n) => n[0])]) inputs[k].value = f[k] ?? '';
    inputs.include_archived.checked = !!f.include_archived;
    for (const [key] of FILTER_GROUPS) inputs[key].forEach((b) => { b.firstChild.checked = (f[key] || []).includes(b.firstChild.value); });
    groups.forEach((g) => { g.open = !!(f[g.dataset.key] || []).length; });
  };

  const load = async () => {
    results.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
    try {
      const data = await api('/admin/orders/search?' + queryFrom(st.filter, { sort: st.sort, dir: st.dir, limit: st.limit, offset: st.offset }));
      const head = h('tr', {}, ORDER_COLS.map(([k, l]) => h('th', { class: 'sortable', 'aria-sort': st.sort === k ? (st.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
        h('button', { class: 'linklike', text: l + (st.sort === k ? (st.dir === 'asc' ? ' ▲' : ' ▼') : ''),
          onclick: () => { st.dir = st.sort === k && st.dir === 'desc' ? 'asc' : 'desc'; st.sort = k; st.offset = 0; load(); } }))));
      const money = (v) => (v == null ? '' : fmtInt(v));
      const body = data.rows.map((r) => h('tr', {},
        h('td', { text: r.sl_no }), h('td', { text: fmtDate(r.order_date) }), h('td', { text: r.dc_inv_no }),
        h('td', {}, h('span', { class: 'pill' + (r.is_cancelled ? ' off' : ''), text: r.stage }), r.archived ? h('span', { class: 'pill warn', text: 'Archived' }) : null),
        h('td', { text: r.channel || '' }), h('td', { text: r.delivery_status || '' }), h('td', { text: r.payment_status || '' }),
        h('td', { class: 'num', text: money(r.amount_received) }), h('td', { class: 'num', text: money(r.cartage) }),
        h('td', { class: 'num', text: r.hours_to_deliver == null ? '' : fmtHours(r.hours_to_deliver) })));
      const last = Math.min(st.offset + st.limit, data.total);
      const prev = h('button', { class: 'btn small', text: '‹ Previous', disabled: st.offset === 0 || null, onclick: () => { st.offset = Math.max(0, st.offset - st.limit); load(); } });
      const next = h('button', { class: 'btn small', text: 'Next ›', disabled: last >= data.total || null, onclick: () => { st.offset += st.limit; load(); } });
      results.replaceChildren(
        h('p', { class: 'note', id: 'orderCount', text: data.total ? `Showing ${st.offset + 1}–${last} of ${fmtInt(data.total)} orders` : 'No orders match these filters.' }),
        data.rows.length ? h('div', { class: 'table-wrap' }, h('table', { id: 'ordersTable' }, h('thead', {}, head), h('tbody', {}, body))) : null,
        h('div', { class: 'row', style: 'margin-top:10px' }, prev, next));
    } catch (ex) { results.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  };
  const run = () => { st.filter = collect(); st.offset = 0; load(); };

  const drawSaved = () => {
    savedSel.replaceChildren(h('option', { value: '', text: 'Saved filters…' }),
      ...saved.map((s) => h('option', { value: String(s.filter_key), text: s.name + (s.shared && !s.mine ? ` (shared by ${s.owner})` : s.shared ? ' (shared)' : '') })));
  };
  savedSel.addEventListener('change', () => {
    const s = saved.find((x) => String(x.filter_key) === savedSel.value);
    if (s) { apply(s.definition); run(); }
  });
  const saveCurrent = () => {
    const def = collect();
    if (!Object.keys(def).length) return alert('Set at least one filter first.');
    formDialog({ title: 'Save this filter', submitLabel: 'Save',
      fields: [{ name: 'name', label: 'Name', required: true, maxlength: 80 }, { name: 'shared', label: 'Let other admins use it', type: 'bool' }],
      onSubmit: async (v) => { await api('/admin/saved-filters', { method: 'POST', body: { name: v.name, shared: v.shared, definition: def } });
        saved = await api('/admin/saved-filters'); drawSaved(); } });
  };
  const deleteSaved = async () => {
    const s = saved.find((x) => String(x.filter_key) === savedSel.value);
    if (!s) return;
    if (!s.mine) return alert('You can only delete filters you saved.');
    if (!(await confirmDialog('Delete “' + s.name + '”?', 'The saved filter is removed. Orders are not affected.', 'Delete', true))) return;
    try { await api('/admin/saved-filters/' + s.filter_key, { method: 'DELETE' }); saved = saved.filter((x) => x !== s); drawSaved(); } catch (ex) { alert(ex.message); }
  };
  const exportXlsx = async (btn) => {
    btn.disabled = true;
    try { await downloadFile('/admin/orders/search.xlsx?' + queryFrom(collect(), { sort: st.sort, dir: st.dir }), 'orders.xlsx'); }
    catch (ex) { alert(ex.message); }
    btn.disabled = false;
  };

  drawSaved();
  const exportBtn = h('button', { class: 'btn', id: 'exportBtn', text: 'Export to Excel', onclick: () => exportXlsx(exportBtn) });
  panel.replaceChildren(
    h('div', { class: 'card filters' },
      h('div', { class: 'filter-grid' },
        field('Search', inputs.q), field('Order date from', inputs.date_from), field('Order date to', inputs.date_to), field('Cancelled', inputs.cancelled),
        ...NUM_FILTERS.map(([k, l]) => field(l, inputs[k]))),
      h('div', { class: 'fgroups' }, groups),
      h('label', { class: 'check' }, inputs.include_archived, ' Include archived orders'),
      h('div', { class: 'row', style: 'margin-top:12px' },
        h('button', { class: 'btn primary', id: 'applyBtn', text: 'Apply filters', onclick: run }),
        h('button', { class: 'btn', text: 'Reset', onclick: () => { apply({}); run(); } }),
        h('span', { class: 'grow' }), savedSel,
        h('button', { class: 'btn small', text: 'Save current…', onclick: saveCurrent }),
        h('button', { class: 'btn small danger', text: 'Delete saved', onclick: deleteSaved }), exportBtn)),
    results);
  inputs.q.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
  run();
}

// ------------------------------------------------------------------ Import
const IMPORT_KINDS = [['orders', 'Orders'], ['users', 'Members']];

async function renderImport(panel, kind) {
  kind = kind || S.importKind || 'orders';
  S.importKind = kind;
  const holder = h('div');
  const bar = h('div', { class: 'subtabs', role: 'tablist' }, IMPORT_KINDS.map(([id, label]) => h('button', {
    class: 'subtab', role: 'tab', 'aria-selected': String(kind === id), text: label, onclick: () => renderImport(panel, id) })));
  panel.replaceChildren(bar, holder);
  importFlow(holder, kind, () => renderImport(panel, kind));
}

function importFlow(root, kind, restart) {
  const label = kind === 'orders' ? 'orders' : 'members';
  const status = h('div', { id: 'importStatus' });
  const work = h('div');
  const history = h('div');
  const file = h('input', { type: 'file', accept: '.csv,.xlsx', id: 'importFile', 'aria-label': 'Choose a file to upload' });
  let filename = '';

  root.replaceChildren(
    h('div', { class: 'card' },
      h('h3', { text: `Upload ${label} in bulk` }),
      h('ol', { class: 'steps' },
        h('li', {}, 'Download the template and fill it in (rows marked EXAMPLE are ignored).'),
        h('li', {}, 'Upload the CSV or Excel file. Every row is checked and problems are explained.'),
        h('li', {}, 'Fix problems right here, then import. Nothing is saved until you press Import, and an import can be undone.')),
      h('div', { class: 'row' },
        h('button', { class: 'btn', id: 'templateBtn', text: 'Download template', onclick: () => downloadFile(`/admin/bulk/${kind}/template.csv`, kind + '_template.csv').catch((ex) => alert(ex.message)) }),
        file), status),
    work, h('h3', { style: 'margin-top:24px', text: 'Recent imports' }), history);

  const setStatus = (text, cls) => status.replaceChildren(h('div', { class: 'msg ' + (cls || ''), role: 'status', text }));
  loadHistory(history, restart);

  file.addEventListener('change', async () => {
    const f = file.files[0];
    if (!f) return;
    filename = f.name; work.replaceChildren(); setStatus('Checking ' + f.name + '…');
    try {
      const res = await fetch(`/admin/bulk/${kind}/validate?filename=${encodeURIComponent(f.name)}`, {
        method: 'POST', headers: { Authorization: 'Bearer ' + S.token, 'Content-Type': 'application/octet-stream' }, body: await f.arrayBuffer() });
      if (res.status === 401) return signOut('Your session ended. Please sign in again.');
      const rep = await res.json();
      if (!res.ok) throw new Error(typeof rep.detail === 'string' ? rep.detail : 'Upload failed');
      status.replaceChildren();
      showReport(rep);
    } catch (ex) { setStatus(ex.message, 'error'); }
  });

  function showReport(rep) {
    if (rep.format_error) { setStatus(rep.format_error, 'error'); return work.replaceChildren(); }
    const columns = rep.columns;
    // every row we are holding, keyed by its row number in the file, so fixes can be re-checked together
    const held = new Map();
    const hold = (r) => held.set(r._row, { ...r });
    rep.rows.forEach(hold); rep.errors.forEach((e) => hold(e.data));
    let errors = rep.errors, validCount = rep.valid;
    const draw = () => {
      const problems = errors.length;
      const importBtn = h('button', { class: 'btn primary', id: 'importBtn', disabled: validCount ? null : true,
        text: validCount ? `Import ${fmtInt(validCount)} ${label}` : 'Nothing to import', onclick: () => doImport(importBtn) });
      const inputsByRow = new Map();
      const rowsFor = errors.map((e) => {
        const cells = columns.map((c) => {
          const input = h('input', { type: 'text', value: held.get(e.row)[c] ?? '', 'aria-label': `${c} (row ${e.row})` });
          input.addEventListener('input', () => { held.get(e.row)[c] = input.value; });
          return h('td', {}, input);
        });
        inputsByRow.set(e.row, cells);
        return h('tr', { 'data-row': e.row }, h('td', { text: e.row }), h('td', { class: 'err-text', text: e.error }), cells);
      });
      work.replaceChildren(
        h('div', { class: 'summary' },
          h('span', { class: 'pill', text: `${fmtInt(rep.total)} rows read` }),
          h('span', { class: 'pill', text: `${fmtInt(validCount)} ready` }),
          h('span', { class: 'pill ' + (problems ? 'off' : ''), text: `${fmtInt(problems)} with problems` })),
        problems ? h('div', {},
          h('p', { class: 'note', text: 'Correct the cells below and press Re-check. Rows you leave with problems are skipped.' }),
          h('div', { class: 'table-wrap' }, h('table', { id: 'errorTable' },
            h('thead', {}, h('tr', {}, h('th', { text: 'Row' }), h('th', { text: 'Problem' }), columns.map((c) => h('th', { text: c })))),
            h('tbody', {}, rowsFor))),
          h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn', id: 'recheckBtn', text: 'Re-check', onclick: recheck }))) : h('p', { class: 'note', text: 'Everything looks good.' }),
        h('div', { class: 'row', style: 'margin-top:14px' }, importBtn),
        rep.help ? h('details', { class: 'fgroup', style: 'margin-top:14px' }, h('summary', { text: 'What each column means' }), h('p', { class: 'note', text: rep.help })) : null);
    };
    const recheck = async () => {
      try {
        const out = await api(`/admin/bulk/${kind}/revalidate`, { method: 'POST', body: { rows: Array.from(held.values()) } });
        errors = out.errors; validCount = out.valid; rep = { ...rep, total: out.total, help: rep.help }; draw();
      } catch (ex) { setStatus(ex.message, 'error'); }
    };
    const doImport = async (btn) => {
      const ok = errors.length
        ? await confirmDialog('Import with problems?', `${errors.length} row(s) still have problems and will be skipped. Import the other ${validCount}?`, 'Import', false)
        : true;
      if (!ok) return;
      btn.disabled = true;
      try {
        const good = new Set(errors.map((e) => e.row));
        const rows = Array.from(held.values()).filter((r) => !good.has(r._row));
        const res = await api(`/admin/bulk/${kind}/confirm`, { method: 'POST', body: { rows, filename } });
        if (!res.created) { setStatus(res.message || 'Nothing was imported.', 'error'); errors = res.errors || errors; return draw(); }
        setStatus(`Imported ${res.created} ${label}.`, 'ok');
        work.replaceChildren();
        if (res.credentials) showCredentials(res.credentials);
        loadHistory(history, restart);
      } catch (ex) { setStatus(ex.message, 'error'); btn.disabled = false; }
    };
    draw();
  }

  function showCredentials(creds) {
    const text = ['Name,Username,Temporary password', ...creds.map((c) => [c.display_name, c.username, c.temporary_password].join(','))].join('\n');
    work.replaceChildren(h('div', { class: 'card' },
      h('h3', { text: 'Temporary passwords' }),
      h('p', { class: 'msg error', role: 'alert', text: 'These passwords are shown only now. Copy them and share each with its owner; they must change it at first sign-in.' }),
      h('div', { class: 'table-wrap' }, h('table', { id: 'credTable' },
        h('thead', {}, h('tr', {}, ['Name', 'Username', 'Temporary password'].map((t) => h('th', { text: t })))),
        h('tbody', {}, creds.map((c) => h('tr', {}, h('td', { text: c.display_name }), h('td', { text: c.username }), h('td', { class: 'secret-cell', text: c.temporary_password })))))),
      h('div', { class: 'row', style: 'margin-top:10px' },
        h('button', { class: 'btn', text: 'Copy all', onclick: async (e) => { try { await navigator.clipboard.writeText(text); e.target.textContent = 'Copied'; } catch { e.target.textContent = 'Select and copy manually'; } } }))));
  }
}

async function loadHistory(box, restart) {
  let list;
  try { list = await api('/admin/bulk/batches'); } catch (ex) { return box.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  if (!list.length) return box.replaceChildren(h('p', { class: 'note', text: 'No imports yet.' }));
  const undo = async (b) => {
    if (!(await confirmDialog('Undo this import?', `Removes the ${b.row_count} ${b.entity === 'orders' ? 'orders' : 'members'} from “${b.filename || 'import'}”. This only works if none of them has been used or edited since.`, 'Undo import', true))) return;
    try { await api(`/admin/bulk/batches/${b.batch_id}/undo`, { method: 'POST' }); restart(); } catch (ex) { alert(ex.message); }
  };
  box.replaceChildren(h('div', { class: 'table-wrap' }, h('table', { id: 'batchTable' },
    h('thead', {}, h('tr', {}, ['#', 'What', 'File', 'Rows', 'By', 'When', ''].map((t) => h('th', { text: t })))),
    h('tbody', {}, list.map((b) => h('tr', {},
      h('td', { text: b.batch_id }), h('td', { text: b.entity === 'orders' ? 'Orders' : 'Members' }), h('td', { text: b.filename || '' }),
      h('td', { class: 'num', text: b.row_count }), h('td', { text: b.created_by || '' }), h('td', { text: fmtDateTime(b.created_at) }),
      h('td', {}, b.undone_at ? h('span', { class: 'pill off', text: 'Undone' }) : h('button', { class: 'btn small danger', text: 'Undo', onclick: () => undo(b) }))))))));
}
