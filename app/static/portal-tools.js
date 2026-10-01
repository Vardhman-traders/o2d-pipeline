'use strict';
/* Admin tools: the Orders tab (filters, saved filters, Excel export) and the Import tab (bulk upload with template,
   validation, inline fixes, undo). Loaded after portal.js and uses its helpers (h, api, S, dialog, formDialog ...). */

const FILTER_GROUPS = [['stage', 'Stage'], ['channel', 'Order via'], ['submission_type', 'Submission type'],
  ['delivery_status', 'Delivery status'], ['payment_status', 'Payment status'], ['ready_by', 'Ready by'],
  ['colour_making_by', 'Colour making by'], ['delivered_by', 'Delivered by'], ['created_by', 'Logged by']];
// Each pair is [min key, max key]; rendered as one "from – to" field instead of two separate boxes.
const RANGE_FILTERS = [['min_amount', 'max_amount', 'Amount (Rs)'], ['min_cartage', 'max_cartage', 'Cartage (Rs)']];
const ORDER_COLS = [['sl_no', 'Sl'], ['order_date', 'Order date'], ['dc_inv_no', 'DC/Inv'], ['stage', 'Stage'], ['channel', 'Via'],
  ['delivery_status', 'Delivery'], ['payment_status', 'Payment'], ['amount_received', 'Amount'], ['cartage', 'Cartage'], ['hours_to_deliver', 'Hours']];

// Admin can edit or permanently delete any order here, active or archived - no need to go through
// the O2D "view as" screens first. Edit reuses the same endpoint those screens' admin form uses.
function openOrderEdit(row, options, onSaved) {
  const sel = (key) => [['', '-- no change --'], ...(options[key] || []).map((v) => [v, v])];
  formDialog({
    title: `Edit order ${row.dc_inv_no || row.sl_no}`, submitLabel: 'Save',
    fields: [
      { name: 'orderRcvdDate', label: 'Order date', type: 'date', value: row.order_date },
      { name: 'dcNo', label: 'DC / Inv No', value: row.dc_inv_no || '' },
      { name: 'orderVia', label: 'Order via', type: 'select', options: sel('channel'), value: row.channel || '' },
      { name: 'typeOfSubmission', label: 'Submission type', type: 'select', options: sel('submission_type'), value: row.submission_type || '' },
      { name: 'deliveryStatus', label: 'Delivery status (set to Cancelled to cancel)', type: 'select', options: sel('delivery_status'), value: row.delivery_status || '' },
      { name: 'paymentStatus', label: 'Payment status', type: 'select', options: sel('payment_status'), value: row.payment_status || '' },
      { name: 'readyByWhom', label: 'Ready by', type: 'select', options: sel('ready_by'), value: row.ready_by || '' },
      { name: 'colourMakingBy', label: 'Colour making by', type: 'select', options: sel('colour_making_by'), value: row.colour_making_by || '' },
      { name: 'deliveredByWhom', label: 'Delivered by', type: 'select', options: sel('delivered_by'), value: row.delivered_by || '' },
      { name: 'amountReceived', label: 'Amount received', type: 'number', value: row.amount_received ?? '' },
      { name: 'cartage', label: 'Cartage', type: 'number', value: row.cartage ?? '' },
      { name: 'shippingLocation', label: 'Address', value: row.shipping_location || '' },
      { name: 'detailedRemarks', label: 'Remarks', value: row.detailed_remarks || '' },
    ],
    onSubmit: async (v) => {
      await api(`/o2d/orders/${row.sl_no}/admin?archived=${row.archived ? 'true' : 'false'}`, { method: 'PUT', body: v });
      onSaved();
    },
  });
}

async function deleteOrder(row, onDeleted) {
  if (!(await confirmDialog(`Permanently delete order ${row.dc_inv_no || row.sl_no}?`,
      'This removes it from the database entirely - not the same as cancelling. It cannot be undone.', 'Delete', true))) return;
  try {
    await api(`/orders/${row.sl_no}?archived=${row.archived ? 'true' : 'false'}`, { method: 'DELETE' });
    onDeleted();
  } catch (ex) { alert(ex.message); }
}

// A "from – to" pair of inputs sharing one label, used for amount/cartage/date ranges.
function rangeField(label, fromEl, toEl) {
  return h('div', { class: 'field' }, h('label', {}, label),
    h('div', { class: 'row', style: 'gap:6px;flex-wrap:nowrap' }, fromEl, h('span', { text: '–' }), toEl));
}

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
  let options;
  try { options = await api('/admin/orders/filter-options'); }
  catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }

  const st = { filter: {}, sort: 'order_date', dir: 'desc', offset: 0, limit: 50 };
  const inputs = {};
  const results = h('div');

  // Same top panel as the Dashboard (Period + Filter by); the only extra is the Sl / DC number search.
  const last30 = () => { const to = todayIST(); return { from: shiftDate(to, -29), to }; };
  const d0 = last30();
  inputs.date_from = h('input', { type: 'date', id: 'f_from', value: d0.from, 'aria-label': 'From date' });
  inputs.date_to = h('input', { type: 'date', id: 'f_to', value: d0.to, 'aria-label': 'To date' });
  inputs.sl_no = h('input', { type: 'search', id: 'f_sl', placeholder: 'Sl no. or DC/Inv no.', 'aria-label': 'Search by Sl number', autocomplete: 'off' });
  inputs.include_archived = h('input', { type: 'checkbox', id: 'f_archived' });
  const selections = {};  // key -> Set of chosen values, owned by each multiSelect widget
  const groups = FILTER_GROUPS.map(([key, label]) => {
    selections[key] = new Set();
    return multiSelect(label, options[key] || [], selections[key], () => run());
  });

  const collect = () => {
    const f = {};
    const sl = inputs.sl_no.value.trim();
    if (sl) {
      // Looking up one order: search every date, archived included, so a known number is always found.
      f.sl_no = sl; f.include_archived = true;
    } else {
      if (inputs.date_from.value) f.date_from = inputs.date_from.value;
      if (inputs.date_to.value) f.date_to = inputs.date_to.value;
      if (inputs.include_archived.checked) f.include_archived = true;
    }
    for (const [key] of FILTER_GROUPS) { if (selections[key].size) f[key] = [...selections[key]]; }
    return f;
  };

  const run = () => { st.filter = collect(); st.offset = 0; load(); };
  const load = async () => {
    results.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
    try {
      const data = await api('/admin/orders/search?' + queryFrom(st.filter, { sort: st.sort, dir: st.dir, limit: st.limit, offset: st.offset }));
      const head = h('tr', {}, ORDER_COLS.map(([k, l]) => h('th', { class: 'sortable', 'aria-sort': st.sort === k ? (st.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
        h('button', { class: 'linklike', text: l + (st.sort === k ? (st.dir === 'asc' ? ' ▲' : ' ▼') : ''),
          onclick: () => { st.dir = st.sort === k && st.dir === 'desc' ? 'asc' : 'desc'; st.sort = k; st.offset = 0; load(); } }))), h('th', { text: S.user.role === 'admin' ? 'Actions' : '' }));
      const money = (v) => (v == null ? '' : fmtInt(v));
      const admin = S.user.role === 'admin';
      const body = data.rows.map((r) => h('tr', { class: 'clickable-row', tabindex: '0', title: 'Open order details', onclick: () => openOrderDetail(r.sl_no),
        onkeydown: (e) => { if (e.key === 'Enter') openOrderDetail(r.sl_no); } },
        h('td', { text: r.sl_no }), h('td', { text: fmtDate(r.order_date) }), h('td', { text: r.dc_inv_no }),
        h('td', {}, h('span', { class: 'pill' + (r.is_cancelled ? ' off' : ''), text: r.stage }), r.archived ? h('span', { class: 'pill warn', text: 'Archived' }) : null),
        h('td', { text: r.channel || '' }), h('td', { text: r.delivery_status || '' }), h('td', { text: r.payment_status || '' }),
        h('td', { class: 'num', text: money(r.amount_received) }), h('td', { class: 'num', text: money(r.cartage) }),
        h('td', { class: 'num', text: r.hours_to_deliver == null ? '' : fmtHours(r.hours_to_deliver) }),
        h('td', { class: 'row', style: 'gap:4px' },
          h('button', { class: 'btn small', text: 'View', onclick: (e) => { e.stopPropagation(); openOrderDetail(r.sl_no); } }),
          admin ? h('button', { class: 'btn small', text: 'Edit', onclick: (e) => { e.stopPropagation(); openOrderEdit(r, options, load); } }) : null,
          admin ? h('button', { class: 'btn small danger', text: 'Delete', onclick: (e) => { e.stopPropagation(); deleteOrder(r, load); } }) : null)));
      const last = Math.min(st.offset + st.limit, data.total);
      const prev = h('button', { class: 'btn small', text: '‹ Previous', disabled: st.offset === 0 || null, onclick: () => { st.offset = Math.max(0, st.offset - st.limit); load(); } });
      const next = h('button', { class: 'btn small', text: 'Next ›', disabled: last >= data.total || null, onclick: () => { st.offset += st.limit; load(); } });
      results.replaceChildren(
        h('p', { class: 'note', id: 'orderCount', text: data.total ? `Showing ${st.offset + 1}–${last} of ${fmtInt(data.total)} orders` : 'No orders match these filters.' }),
        data.rows.length ? h('div', { class: 'table-wrap' }, h('table', { id: 'ordersTable' }, h('thead', {}, head), h('tbody', {}, body))) : null,
        h('div', { class: 'row', style: 'margin-top:10px' }, prev, next));
    } catch (ex) { results.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  };

  const exportXlsx = async (btn) => {
    btn.disabled = true;
    try { await downloadFile('/admin/orders/search.xlsx?' + queryFrom(collect(), { sort: st.sort, dir: st.dir }), 'orders.xlsx'); }
    catch (ex) { alert(ex.message); }
    btn.disabled = false;
  };

  const slot = S.actionsSlot || h('div'); // top-right of the tab row (see renderModule)
  const exportBtn = h('button', { class: 'btn primary', id: 'exportBtn', text: '⬇ Export to Excel', onclick: () => exportXlsx(exportBtn) });
  const rangeMsg = h('span', { class: 'msg error', id: 'f_range_msg' });
  const onDates = () => {
    const bad = inputs.date_from.value && inputs.date_to.value && inputs.date_from.value > inputs.date_to.value;
    rangeMsg.textContent = bad ? '"From" date must be on or before "To" date.' : '';
    if (!bad) run();
  };
  inputs.date_from.addEventListener('change', onDates);
  inputs.date_to.addEventListener('change', onDates);
  inputs.include_archived.addEventListener('change', run);
  let slTimer = null;
  inputs.sl_no.addEventListener('input', () => { clearTimeout(slTimer); slTimer = setTimeout(run, 400); });
  inputs.sl_no.addEventListener('keydown', (e) => { if (e.key === 'Enter') { clearTimeout(slTimer); run(); } });
  panel.replaceChildren(
    h('div', { class: 'filter-row' }, h('span', { class: 'filter-label', text: 'Period' }),
      h('label', { class: 'date-field' }, 'From ', inputs.date_from), h('label', { class: 'date-field' }, 'To ', inputs.date_to),
      h('button', { type: 'button', class: 'btn', id: 'f_last30', text: 'Last 30 days',
        onclick: () => { const d = last30(); inputs.date_from.value = d.from; inputs.date_to.value = d.to; onDates(); } }),
      rangeMsg),
    h('div', { class: 'filter-row' }, h('span', { class: 'filter-label', text: 'Filter by' }), ...groups),
    h('div', { class: 'filter-row' }, h('span', { class: 'filter-label', text: 'Find order' }), inputs.sl_no,
      h('span', { class: 'note', text: 'Searches all dates, including archived orders.' })),
    h('div', { class: 'archive-note', id: 'archiveNote' },
      h('strong', { text: 'Archived orders are hidden by default. ' }),
      'An order is archived when it is at least 7 days old (by order date) and fully finished: either Closed (delivered, received and payment recorded) or Cancelled. ',
      h('label', { class: 'check', style: 'display:inline-flex;margin-left:6px' }, inputs.include_archived, ' Include archived orders')),
    results);
  if (panel.isConnected) slot.replaceChildren(exportBtn);
  run();
}

// ------------------------------------------------------------------ order details + timeline
const fmtExact = (iso) => new Date(iso).toLocaleString('en-IN', {
  timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });

async function openOrderDetail(slNo) {
  const body = h('div', {}, h('p', { class: 'note', text: 'Loading…' }));
  const close = h('button', { class: 'btn primary', text: 'Close' });
  const dlg = dialog('Order ' + slNo, [body], [close], 'wide order-modal');
  close.addEventListener('click', () => dlg.close());
  try {
    const { order: o, timeline, timeline_note: note } = await api(`/admin/orders/${slNo}/detail`);
    dlg.querySelector('h2').textContent = `Order ${o.dc_inv_no || o.sl_no}  ·  Sl ${o.sl_no}`;
    const money = (v) => (v == null ? '' : 'Rs ' + fmtInt(v));
    const when = (v) => (v ? fmtExact(v) : '');
    const fields = [
      ['Order date', fmtDate(o.order_date) + ' ' + String(o.order_date).slice(0, 4)], ['DC / Inv no', o.dc_inv_no], ['Order via', o.channel],
      ['Submission type', o.submission_type], ['Delivery status', o.delivery_status], ['Payment status', o.payment_status],
      ['Ready by', o.ready_by], ['Colour making by', o.colour_making_by],
      ['Delivered by', o.delivered_by ? o.delivered_by + (o.delivered_by_phone ? ' (' + o.delivered_by_phone + ')' : '') : ''],
      ['Material delivered', when(o.material_delivery_datetime)], ['Date received', o.date_of_receiving ? fmtDate(o.date_of_receiving) + ' ' + String(o.date_of_receiving).slice(0, 4) : ''],
      ['Amount received', money(o.amount_received)], ['Cartage', money(o.cartage)],
      ['Hours to deliver', o.hours_to_deliver == null ? '' : fmtHours(Number(o.hours_to_deliver))],
      ['Logged by', o.created_by], ['Logged at', when(o.timestamp_created)],
      ['Last updated by', o.last_updated_by], ['Last updated at', when(o.last_updated_at)],
      ['Address', o.shipping_location], ['Remarks', o.detailed_remarks]];
    const events = timeline.map((e) => h('li', { class: 'tl-item tl-' + e.type + (e.reconstructed ? ' tl-rebuilt' : '') },
      h('span', { class: 'tl-dot' }),
      h('div', { class: 'tl-body' },
        h('div', { class: 'tl-head' }, h('strong', { text: e.title }), e.reconstructed ? h('span', { class: 'pill warn', text: 'rebuilt' }) : null),
        h('div', { class: 'tl-when', text: e.at ? fmtExact(e.at) + ' IST' : 'Time not recorded' }),
        h('div', { class: 'tl-by', text: e.by ? 'By ' + e.by + (e.role ? ' (' + roleLabel(e.role) + ')' : '') : 'Person not recorded' }),
        e.changes.length ? h('ul', { class: 'tl-changes' }, e.changes.map((c) => h('li', {},
          h('span', { class: 'tl-field', text: c.field + ': ' }), c.from ? h('span', { class: 'tl-from', text: c.from }) : h('span', { class: 'tl-none', text: 'empty' }),
          ' → ', h('strong', { text: c.to == null ? 'cleared' : c.to })))) : null)));
    body.replaceChildren(
      h('div', { class: 'row', style: 'gap:8px;margin-bottom:12px' }, h('span', { class: 'pill' + (o.is_cancelled ? ' off' : ''), text: o.stage }),
        o.archived ? h('span', { class: 'pill warn', text: 'Archived' }) : null),
      h('dl', { class: 'detail-grid' }, fields.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', { text: v || '–' })])),
      h('h3', { class: 'tl-title', text: 'Order timeline' }),
      note ? h('p', { class: 'note', text: note }) : null,
      h('ol', { class: 'timeline', id: 'orderTimeline' }, events));
  } catch (ex) { body.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
}

// ------------------------------------------------------------------ Setup > Access (who sees which page)
async function renderAccess(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let m, users;
  try { [m, users] = await Promise.all([api('/admin/access/matrix'), api('/admin/users')]); }
  catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  const have = new Set(m.granted.map((g) => g.page_key + '|' + g.role));
  const toggle = async (page, role, cb) => {
    cb.disabled = true;
    try { await api('/admin/access/matrix', { method: 'PUT', body: { page_key: page, role, allowed: cb.checked } }); }
    catch (ex) { cb.checked = !cb.checked; alert(ex.message); }
    cb.disabled = false;
  };
  const matrix = h('div', { class: 'table-wrap' }, h('table', { id: 'accessMatrix' },
    h('thead', {}, h('tr', {}, h('th', { text: 'Page' }), ...m.roles.map((r) => h('th', { class: 'num', text: roleLabel(r) })))),
    h('tbody', {}, m.pages.map((p) => h('tr', {}, h('td', { text: p.label }),
      ...m.roles.map((r) => {
        const cb = h('input', { type: 'checkbox', 'aria-label': `${roleLabel(r)} can open ${p.label}`, 'data-page': p.key, 'data-role': r });
        cb.checked = have.has(p.key + '|' + r);
        cb.addEventListener('change', () => toggle(p.key, r, cb));
        return h('td', { class: 'num' }, cb);
      }))))));

  // one person at a time: each page is "like their role", always allowed, or blocked
  const who = h('select', { id: 'accessPerson', 'aria-label': 'Choose a member' },
    h('option', { value: '', text: 'Choose a member…' }),
    ...users.filter((u) => !u.disabled && u.role !== 'admin').map((u) => h('option', { value: String(u.user_key), text: `${u.display_name} (${roleLabel(u.role)})` })));
  const personBox = h('div', { style: 'margin-top:12px' });
  const loadPerson = async () => {
    if (!who.value) return personBox.replaceChildren();
    personBox.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
    try {
      const d = await api('/admin/access/users/' + who.value);
      const rows = d.pages.map((p) => {
        const sel = h('select', { 'aria-label': 'Access to ' + p.label, 'data-page': p.key },
          h('option', { value: 'default', text: 'Same as role (' + (p.role_default ? 'can open' : 'cannot open') + ')', selected: p.override == null }),
          h('option', { value: 'allow', text: 'Always allow', selected: p.override === true }),
          h('option', { value: 'block', text: 'Block', selected: p.override === false }));
        sel.addEventListener('change', async () => {
          sel.disabled = true;
          try { await api('/admin/access/users/' + who.value, { method: 'PUT', body: { page_key: p.key, allowed: sel.value === 'default' ? null : sel.value === 'allow' } }); }
          catch (ex) { alert(ex.message); loadPerson(); }
          sel.disabled = false;
        });
        return h('tr', {}, h('td', { text: p.label }), h('td', {}, sel));
      });
      personBox.replaceChildren(h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', { text: 'Page' }), h('th', { text: 'Access for ' + d.user.display_name }))), h('tbody', {}, rows))));
    } catch (ex) { personBox.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  };
  who.addEventListener('change', loadPerson);

  panel.replaceChildren(
    h('div', { class: 'card', style: 'margin-bottom:16px' },
      h('h2', { style: 'font-size:15px;margin-bottom:6px', text: 'Who sees which page' }),
      h('p', { class: 'note', text: 'Tick the pages each role opens by default. Admins always open everything, and Setup is for admins only. As new modules are added, their pages appear here.' }),
      h('p', { class: 'note', style: 'margin:6px 0 12px', text: 'Opening a page does not change what someone may do inside it: what a person can see and edit in the O2D screens still follows their role (Setup > Permissions).' }),
      matrix),
    h('div', { class: 'card' },
      h('h2', { style: 'font-size:15px;margin-bottom:6px', text: 'Exceptions for one person' }),
      h('p', { class: 'note', style: 'margin-bottom:10px', text: 'Give one person a page their role does not have, or block one they would normally get. Approved access requests show up here as “Always allow”.' }),
      who, personBox));
}

// ------------------------------------------------------------------ Setup > Access requests
async function renderAccessRequests(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  const filter = h('select', { id: 'reqStatus', 'aria-label': 'Show requests' },
    [['pending', 'Waiting for a decision'], ['approved', 'Approved'], ['rejected', 'Rejected'], ['all', 'All']].map(([v, l]) => h('option', { value: v, text: l })));
  filter.value = S.reqStatus || 'pending';
  const holder = h('div');
  const load = async () => {
    S.reqStatus = filter.value;
    holder.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
    let rows;
    try { rows = await api('/admin/access/requests?status=' + filter.value); }
    catch (ex) { return holder.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
    await refreshPendingRequests();
    if (S.redrawModuleBar) S.redrawModuleBar();
    const decide = (r, approve) => formDialog({
      title: (approve ? 'Approve' : 'Reject') + ` ${r.display_name}'s request`, submitLabel: approve ? 'Approve' : 'Reject',
      fields: [{ name: 'note', label: 'Note for ' + r.display_name + ' (optional)', maxlength: 300,
        hint: approve ? `They will be able to open ${r.page_label} straight away.` : 'They will see this note and can ask again.' }],
      onSubmit: async (v) => { await api(`/admin/access/requests/${r.request_key}/${approve ? 'approve' : 'reject'}`, { method: 'POST', body: { note: v.note || null } }); load(); } });
    holder.replaceChildren(rows.length ? h('div', { class: 'table-wrap' }, h('table', { id: 'requestsTable' },
      h('thead', {}, h('tr', {}, ['Person', 'Role', 'Wants', 'Reason', 'Asked on', 'Status', ''].map((t) => h('th', { text: t })))),
      h('tbody', {}, rows.map((r) => h('tr', {},
        h('td', {}, h('strong', { text: r.display_name }), h('div', { class: 'note', text: r.username })), h('td', { text: roleLabel(r.role) }),
        h('td', { text: r.page_label }), h('td', { text: r.reason, style: 'max-width:300px' }), h('td', { text: fmtExact(r.created_at) }),
        h('td', {}, statusPill(r.status), r.decided_by ? h('div', { class: 'note', text: 'by ' + r.decided_by + (r.decision_note ? ': ' + r.decision_note : '') }) : null),
        h('td', {}, r.status === 'pending' ? h('div', { class: 'row' },
          h('button', { class: 'btn small primary', text: 'Approve', onclick: () => decide(r, true) }),
          h('button', { class: 'btn small danger', text: 'Reject', onclick: () => decide(r, false) })) : null))))))
      : h('div', { class: 'card empty', text: filter.value === 'pending' ? 'No requests waiting. 🎉' : 'Nothing here.' }));
  };
  filter.addEventListener('change', load);
  panel.replaceChildren(h('p', { class: 'note', style: 'margin-bottom:12px', text: 'People who opened a page they cannot see can ask for access. Approving gives that one person the page; their colleagues in the same role are not affected.' }),
    h('div', { class: 'row', style: 'margin-bottom:12px' }, filter), holder);
  load();
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

// ------------------------------------------------------------------ Setup > Clean up names (reconciliation)
async function renderReconcile(panel, kind) {
  kind = kind || S.reconcileKind || 'person';
  S.reconcileKind = kind;
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let overview, data;
  try { [overview, data] = await Promise.all([api('/admin/reconcile'), api('/admin/reconcile/' + kind)]); }
  catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  const reload = () => renderReconcile(panel, kind);
  const roleName = (r) => (data.role_labels && data.role_labels[r]) || r || '';
  const byKey = new Map(data.values.map((v) => [v.key, v]));
  const picked = new Set();
  let letter = '';

  const kinds = h('div', { class: 'subtabs', role: 'tablist' }, overview.map((o) => h('button', {
    class: 'subtab', role: 'tab', 'aria-selected': String(o.kind === kind),
    text: `${o.label} (${o.distinct})` + (o.possible_duplicates ? ` · ${o.possible_duplicates} to check` : ''),
    onclick: () => renderReconcile(panel, o.kind) })));

  const search = h('input', { type: 'text', id: 'rc_search', placeholder: 'Search values', 'aria-label': 'Search values', style: 'max-width:260px' });
  const mergeBtn = h('button', { class: 'btn primary', id: 'rc_mergeBtn', text: 'Merge selected…', disabled: true, onclick: () => openMerge([...picked]) });
  const letters = h('div', { class: 'letters' });
  const tbody = h('tbody');
  const dupBox = h('div');

  const label = (v) => v.name + (v.role ? ` (${roleName(v.role)})` : '');

  function openMerge(keys) {
    const items = keys.map((k) => byKey.get(k)).filter(Boolean);
    if (items.length < 2) return;
    const locked = items.filter((i) => i.protected);
    if (locked.length > 1) return alert('Two built-in values cannot be merged into each other.');
    const roles = new Set(items.map((i) => i.role));
    if (roles.size > 1) return alert('People can only be merged within the same role.');
    const keep = h('select', { id: 'rc_keep', 'aria-label': 'Value to keep' }, items.map((i) => h('option', { value: String(i.key), text: `${i.name} — ${fmtInt(i.orders)} orders`, selected: locked.length ? i.protected : false })));
    if (locked.length) keep.disabled = true;
    const info = h('p', { class: 'note', id: 'rc_preview', text: '' });
    const err = h('div', { class: 'msg error' });
    const ok = h('button', { class: 'btn primary', id: 'rc_confirm', text: 'Merge' });
    const cancel = h('button', { class: 'btn', text: 'Cancel' });
    const dlg = dialog('Merge ' + items.length + ' values', [
      h('p', { text: 'Choose the correct spelling to keep. All orders using the others are moved to it, and the others are then removed everywhere. This cannot be undone with one click.' }),
      h('div', { class: 'field' }, h('label', {}, 'Keep this value'), keep), info, err], [cancel, ok]);
    cancel.addEventListener('click', () => dlg.close());
    const payload = (confirm) => ({ target_key: Number(keep.value), source_keys: items.map((i) => i.key).filter((k) => k !== Number(keep.value)), confirm });
    const refresh = async () => {
      err.textContent = ''; ok.disabled = true;
      try {
        const p = await api(`/admin/reconcile/${kind}/merge`, { method: 'POST', body: payload(false) });
        info.textContent = `${fmtInt(p.orders_moved)} order(s) will be moved to “${p.target}”, and “${p.merging.join('”, “')}” will be removed.`;
        ok.disabled = false;
      } catch (ex) { err.textContent = ex.message; }
    };
    keep.addEventListener('change', refresh);
    ok.addEventListener('click', async () => {
      ok.disabled = true;
      try { await api(`/admin/reconcile/${kind}/merge`, { method: 'POST', body: payload(true) }); dlg.close(); reload(); }
      catch (ex) { err.textContent = ex.message; ok.disabled = false; }
    });
    refresh();
  }

  function openRename(v) {
    formDialog({ title: 'Rename “' + v.name + '”', submitLabel: 'Rename',
      fields: [{ name: 'name', label: 'Correct spelling', value: v.name, required: true, maxlength: 100,
        hint: `Changes it on all ${fmtInt(v.orders)} order(s) that use it. If this spelling already exists, use Merge instead.` }],
      onSubmit: async (vals) => { await api(`/admin/reconcile/${kind}/rename`, { method: 'POST', body: { key: v.key, name: vals.name } }); reload(); } });
  }

  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const shown = data.values.filter((v) => (!q || v.name.toLowerCase().includes(q)) && (!letter || v.name.charAt(0).toUpperCase() === letter));
    const initials = [...new Set(data.values.map((v) => v.name.charAt(0).toUpperCase()))].sort();
    letters.replaceChildren(h('button', { class: 'chip', 'aria-pressed': String(!letter), text: 'All', onclick: () => { letter = ''; draw(); } }),
      ...initials.map((c) => h('button', { class: 'chip', 'aria-pressed': String(letter === c), text: c, onclick: () => { letter = c; draw(); } })));
    const rows = [];
    let last = '';
    for (const v of shown) {
      const c = v.name.charAt(0).toUpperCase();
      if (c !== last) { last = c; rows.push(h('tr', { class: 'letter-row' }, h('td', { colspan: data.role_labels ? 5 : 4, text: c }))); }
      const box = h('input', { type: 'checkbox', 'aria-label': 'Select ' + v.name, checked: picked.has(v.key) });
      box.addEventListener('change', () => { if (box.checked) picked.add(v.key); else picked.delete(v.key); mergeBtn.disabled = picked.size < 2; });
      rows.push(h('tr', { 'data-key': v.key },
        h('td', {}, box), h('td', {}, v.name, v.protected ? h('span', { class: 'pill warn', style: 'margin-left:8px', text: 'Built-in' }) : null),
        data.role_labels ? h('td', { text: roleName(v.role) }) : null,
        h('td', { class: 'num', text: fmtInt(v.orders) }),
        h('td', {}, v.protected ? h('span', { class: 'note', text: 'Used by order rules' }) : h('button', { class: 'btn small', text: 'Rename', onclick: () => openRename(v) }))));
    }
    tbody.replaceChildren(...rows);
    if (!shown.length) tbody.replaceChildren(h('tr', {}, h('td', { colspan: 5, class: 'empty', text: 'No values match.' })));
  };
  search.addEventListener('input', draw);

  const dups = data.suggestions;
  dupBox.replaceChildren(dups.length ? h('div', { class: 'card', style: 'margin-bottom:16px' },
    h('h3', { text: `Possible duplicates (${dups.length})` }),
    h('p', { class: 'note', text: 'These look like the same thing spelled differently. Review each pair, and merge it if it really is the same.' }),
    h('div', { class: 'table-wrap' }, h('table', { id: 'rc_dups' }, h('tbody', {}, dups.map((s) => h('tr', {},
      h('td', { text: s.names[0] }), h('td', { text: '≈' }), h('td', { text: s.names[1] }),
      data.role_labels ? h('td', { text: roleName(s.role) }) : null,
      h('td', {}, h('button', { class: 'btn small', text: 'Review & merge', onclick: () => openMerge(s.keys) })))))))) : h('p', { class: 'note', text: 'No likely duplicates found in this list.' }));

  panel.replaceChildren(kinds, dupBox,
    h('div', { class: 'row', style: 'margin-bottom:10px' }, search, h('span', { class: 'grow' }), mergeBtn),
    letters,
    h('div', { class: 'table-wrap' }, h('table', { id: 'rc_table' },
      h('thead', {}, h('tr', {}, h('th', { text: '' }), h('th', { text: 'Value' }), data.role_labels ? h('th', { text: 'Role' }) : null, h('th', { class: 'num', text: 'Orders' }), h('th', { text: '' }))), tbody)),
    h('p', { class: 'note', style: 'margin-top:10px', text: 'Every change is saved to the database at once, shows up on all order screens and dashboards, and is kept in the audit trail.' }));
  draw();
}
