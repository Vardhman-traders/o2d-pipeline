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
      onSaved(editSummary(row, v));
    },
  });
}

// [form field, order-list column, label]: used to say in plain words what an edit changed.
const EDIT_FIELDS = [['orderRcvdDate', 'order_date', 'Order date'], ['dcNo', 'dc_inv_no', 'DC / Inv no'],
  ['orderVia', 'channel', 'Order via'], ['typeOfSubmission', 'submission_type', 'Submission type'],
  ['deliveryStatus', 'delivery_status', 'Delivery status'], ['paymentStatus', 'payment_status', 'Payment status'],
  ['readyByWhom', 'ready_by', 'Ready by'], ['colourMakingBy', 'colour_making_by', 'Colour making by'],
  ['deliveredByWhom', 'delivered_by', 'Delivered by'], ['amountReceived', 'amount_received', 'Amount received'],
  ['cartage', 'cartage', 'Cartage'], ['shippingLocation', 'shipping_location', 'Address'],
  ['detailedRemarks', 'detailed_remarks', 'Remarks']];

function editSummary(row, v) {
  const changes = [];
  for (const [form, col, label] of EDIT_FIELDS) {
    if (v[form] === '' && ['orderVia', 'typeOfSubmission', 'deliveryStatus', 'paymentStatus', 'readyByWhom', 'colourMakingBy', 'deliveredByWhom'].includes(form)) continue; // "no change"
    const before = row[col] == null ? '' : String(row[col]);
    const after = v[form] == null ? '' : String(v[form]);
    const numeric = form === 'amountReceived' || form === 'cartage';
    if (numeric ? Number(before || 0) === Number(after || 0) : before === after) continue;
    changes.push(`${label}: ${before || 'empty'} → ${after || 'empty'}`);
  }
  return { kind: 'edit', title: `Order ${row.dc_inv_no || row.sl_no} (Sl ${row.sl_no}) was updated`,
    lines: changes.length ? changes : ['You saved it, but no values were different.'] };
}

async function deleteOrder(row, onDeleted) {
  if (!(await confirmDialog(`Permanently delete order ${row.dc_inv_no || row.sl_no}?`,
      'This removes it from the database entirely - not the same as cancelling. It cannot be undone.', 'Delete', true))) return;
  try {
    await api(`/orders/${row.sl_no}?archived=${row.archived ? 'true' : 'false'}`, { method: 'DELETE' });
    onDeleted({ kind: 'delete', title: `Order ${row.dc_inv_no || row.sl_no} (Sl ${row.sl_no}) was deleted`,
      lines: [`Order date: ${fmtDate(row.order_date)} ${String(row.order_date).slice(0, 4)}`, `Stage when deleted: ${row.stage}`,
        'It is permanently removed from the database. A record of the deletion is kept in the audit log.'] });
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

// Keeps a CSS variable equal to an element's height, so sticky parts can stack under each other.
function trackSticky(el, varName) {
  const root = document.documentElement;
  const set = () => root.style.setProperty(varName, (el.isConnected ? el.offsetHeight : 0) + 'px');
  if (typeof ResizeObserver === 'function') new ResizeObserver(set).observe(el);
  set();
}

// ------------------------------------------------------------------ Orders
async function renderOrders(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let options;
  try { options = await api('/admin/orders/filter-options'); }
  catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }

  const st = { filter: {}, sort: 'order_date', dir: 'desc', offset: 0, limit: 50, seq: 0 };
  const inputs = {};
  const results = h('div');

  // Same top panel as the Dashboard (Period + Filter by); the only extra is the Sl / DC number search.
  const last30 = () => { const to = todayIST(); return { from: shiftDate(to, -29), to }; };
  const d0 = last30();
  inputs.date_from = h('input', { type: 'date', id: 'f_from', value: d0.from, 'aria-label': 'From date' });
  inputs.date_to = h('input', { type: 'date', id: 'f_to', value: d0.to, 'aria-label': 'To date' });
  inputs.sl_no = h('input', { type: 'search', id: 'f_sl', placeholder: 'Sl no. or DC/Inv no.', 'aria-label': 'Search by Sl number', autocomplete: 'off' });
  let view = 'active';  // which KPI card is selected; it filters the whole page
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
      f.view = view;
    }
    for (const [key] of FILTER_GROUPS) { if (selections[key].size) f[key] = [...selections[key]]; }
    return f;
  };

  const run = () => { st.filter = collect(); st.offset = 0; load(); loadKpis(); };

  // What was just done (an edit or a delete), kept at the top of the page until dismissed.
  const notice = h('div', { id: 'actionSummary', role: 'status', 'aria-live': 'polite' });
  const showNotice = (n) => {
    if (!n) return;
    notice.replaceChildren(h('div', { class: 'action-summary ' + n.kind },
      h('button', { class: 'linklike dismiss', 'aria-label': 'Dismiss', text: '✕', onclick: () => notice.replaceChildren() }),
      h('strong', { text: (n.kind === 'delete' ? '🗑 ' : '✔ ') + n.title }),
      h('ul', {}, n.lines.map((l) => h('li', { text: l }))),
      h('div', { class: 'note', text: 'Done by ' + S.user.display_name + ' at ' + fmtExact(new Date().toISOString()) + ' IST' })));
    notice.scrollIntoView({ block: 'nearest' });
  };

  const load = async () => {
    const seq = ++st.seq;  // a slow, older answer must never overwrite a newer one
    results.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
    try {
      const data = await api('/admin/orders/search?' + queryFrom(st.filter, { sort: st.sort, dir: st.dir, limit: st.limit, offset: st.offset }));
      if (seq !== st.seq) return;
      // Click a column name to sort by it; click again to flip the direction.
      const head = h('tr', {}, ORDER_COLS.map(([k, l]) => h('th', { class: 'sortable' + (st.sort === k ? ' sorted' : ''), 'aria-sort': st.sort === k ? (st.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
        h('button', { class: 'sort-btn', title: 'Sort by ' + l, 'data-sort': k,
          onclick: () => { st.dir = st.sort === k ? (st.dir === 'asc' ? 'desc' : 'asc') : (k === 'order_date' || k === 'sl_no' ? 'desc' : 'asc'); st.sort = k; st.offset = 0; load(); } },
          l, h('span', { class: 'sort-arrow', 'aria-hidden': 'true', text: st.sort === k ? (st.dir === 'asc' ? ' ▲' : ' ▼') : ' ⇅' })))), h('th', { text: S.user.role === 'admin' ? 'Actions' : '' }));
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
          admin ? h('button', { class: 'btn small', text: 'Edit', onclick: (e) => { e.stopPropagation(); openOrderEdit(r, options, (n) => { showNotice(n); load(); loadKpis(); }); } }) : null,
          admin ? h('button', { class: 'btn small danger', text: 'Delete', onclick: (e) => { e.stopPropagation(); deleteOrder(r, (n) => { showNotice(n); load(); loadKpis(); }); } }) : null)));
      const last = Math.min(st.offset + st.limit, data.total);
      const prev = h('button', { class: 'btn small', text: '‹ Previous', disabled: st.offset === 0 || null, onclick: () => { st.offset = Math.max(0, st.offset - st.limit); load(); } });
      const next = h('button', { class: 'btn small', text: 'Next ›', disabled: last >= data.total || null, onclick: () => { st.offset += st.limit; load(); } });
      results.replaceChildren(
        h('p', { class: 'note', id: 'orderCount', text: data.total ? `Showing ${st.offset + 1}–${last} of ${fmtInt(data.total)} orders` : 'No orders match these filters.' }),
        data.rows.length ? h('div', { class: 'table-wrap sticky-head' }, h('table', { id: 'ordersTable' }, h('thead', {}, head), h('tbody', {}, body))) : null,
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
  let slTimer = null;
  inputs.sl_no.addEventListener('input', () => { clearTimeout(slTimer); slTimer = setTimeout(run, 400); });
  inputs.sl_no.addEventListener('keydown', (e) => { if (e.key === 'Enter') { clearTimeout(slTimer); run(); } });
  const findBtn = h('button', { type: 'button', class: 'btn primary', id: 'f_find', text: 'Find', onclick: () => { clearTimeout(slTimer); run(); } });
  const clearBtn = h('button', { type: 'button', class: 'btn', id: 'f_clearfind', text: 'Clear', onclick: () => { inputs.sl_no.value = ''; clearTimeout(slTimer); run(); inputs.sl_no.focus(); } });

  // The tab row, the filters and (below) the table header stay put while the list scrolls.
  const extraRows = [
    h('div', { class: 'filter-row' }, h('span', { class: 'filter-label', text: 'Filter by' }), ...groups),
    h('div', { class: 'filter-row' }, h('span', { class: 'filter-label', text: 'Find order' }), inputs.sl_no, findBtn, clearBtn,
      h('span', { class: 'note', text: 'Searches all dates, including archived orders.' }))];
  // Counts for the current filters. Each card is also a filter: click one to narrow the whole page to it.
  const kpis = h('div', { class: 'kpi-strip', id: 'orderKpis' });
  const KPI_CARDS = [['total', 'Total orders', 'all orders, archived included', ''], ['active', 'Active', 'shown by default', 'k-active'],
    ['archived', 'Archived', 'closed or cancelled, 7+ days old', 'k-archived'], ['in_progress', 'In progress', 'active, not yet closed', ''],
    ['closed', 'Closed', 'delivered, received, paid', ''], ['cancelled', 'Cancelled', '', '']];
  let kpiSeq = 0;
  const loadKpis = async () => {
    const seq = ++kpiSeq;
    try {
      const k = await api('/admin/orders/summary?' + queryFrom(collect(), {}));
      if (seq !== kpiSeq) return;
      kpis.replaceChildren(...KPI_CARDS.map(([key, label, sub, cls]) => h('button', {
        type: 'button', class: 'kpi clickable ' + cls + (view === key ? ' selected' : ''), 'data-view': key, 'aria-pressed': String(view === key),
        title: 'Show only: ' + label,
        onclick: () => { view = view === key && key !== 'active' ? 'active' : key; run(); } },
        h('div', { class: 'k-label', text: label }), h('div', { class: 'k-value', text: fmtInt(k[key]) }), sub ? h('div', { class: 'k-sub', text: sub }) : null)));
    } catch { kpis.replaceChildren(); }
  };
  const toggle = h('button', { type: 'button', class: 'btn small', id: 'f_toggle', 'aria-expanded': 'true', text: 'Hide filters ▲' });
  const filtersBox = h('div', { class: 'sticky-filters', id: 'orderFilters' },
    h('div', { class: 'filter-row' }, h('span', { class: 'filter-label', text: 'Period' }),
      h('label', { class: 'date-field' }, 'From ', inputs.date_from), h('label', { class: 'date-field' }, 'To ', inputs.date_to),
      h('button', { type: 'button', class: 'btn', id: 'f_last30', text: 'Last 30 days',
        onclick: () => { const d = last30(); inputs.date_from.value = d.from; inputs.date_to.value = d.to; onDates(); } }),
      rangeMsg, h('span', { class: 'grow' }), toggle),
    ...extraRows);
  toggle.addEventListener('click', () => {
    const hide = toggle.getAttribute('aria-expanded') === 'true';
    extraRows.forEach((r) => { r.hidden = hide; });
    toggle.setAttribute('aria-expanded', String(!hide));
    toggle.textContent = hide ? 'Show filters ▼' : 'Hide filters ▲';
  });
  panel.replaceChildren(
    notice,
    filtersBox,
    kpis,
    h('div', { class: 'note', id: 'archiveNote', text: 'Archived orders (closed or cancelled, 7+ days old) are hidden by default. Click the Archived or Total orders card to see them.' }),
    results);
  trackSticky(filtersBox, '--filters-h');
  loadKpis();
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

// ------------------------------------------------------------------ Setup > Access & permissions (one page)
// Two layers, kept apart on purpose: ACCESS decides which pages a person can open (the door); PERMISSIONS decide what a role may
// do once inside the O2D screens (which fields it may edit, whether it may view all orders). Requests to open a page are decided here too.
async function renderAccessHub(panel) {
  const section = (id, title, intro) => {
    const body = h('div', { id });
    return [h('div', { class: 'hub-section' }, h('h2', { class: 'hub-title', text: title }), h('p', { class: 'note', text: intro })), body];
  };
  const [reqHead, reqBody] = section('hubRequests', 'Requests waiting for a decision',
    'People who opened a page they cannot see can ask for access. Approving gives that one person the page; colleagues in the same role are not affected.');
  const [pageHead, pageBody] = section('hubPages', 'Who can open which page',
    'The door: tick the pages each role opens by default, then add exceptions for one person. Opening a page does not change what someone may do inside it.');
  const [permHead, permBody] = section('hubPermissions', 'What each role can do inside the O2D screens',
    'The rules inside the door: which order fields a role may set, and which view-only roles may see all orders. Changes apply immediately.');
  const jump = h('nav', { class: 'hub-jump', 'aria-label': 'Jump to a section' },
    ...[['Requests', reqHead], ['Page access', pageHead], ['Role permissions', permHead]].map(([t, el]) => h('button', { type: 'button', class: 'linklike', text: t,
      onclick: () => el.scrollIntoView({ behavior: 'smooth', block: 'start' }) })));
  const useCase = (icon, title, question, example) => h('div', { class: 'usecase' },
    h('div', { class: 'usecase-title' }, h('span', { 'aria-hidden': 'true', text: icon }), h('strong', { text: title })),
    h('p', { class: 'usecase-q', text: question }), h('p', { class: 'note', text: example }));
  const explainer = h('div', { class: 'card hub-explainer', id: 'hubExplainer' },
    h('h2', { class: 'hub-title', text: 'How access and permissions work' }),
    h('p', { class: 'note', text: 'Two separate controls. A person first needs access to a page (the door), and then their role’s permissions decide what they can do inside it.' }),
    h('div', { class: 'usecase-grid' },
      useCase('🚪', 'Page access', 'Can this person open this page at all?',
        'Example: the cashier role does not get the All orders page, so it never appears for them. Tick pages for a whole role, or add an exception for one person. A person who opens a page they cannot see can send a request, which you approve below.'),
      useCase('✏️', 'Role permissions', 'Once inside, what may this role see or change?',
        'Example: the shop role may set “Order via” and “Remarks” on an order but not “Payment status”. View-only roles such as accounts can see all orders only if you turn that on. Opening a page never grants editing.')));
  panel.replaceChildren(explainer, jump, reqHead, reqBody, pageHead, pageBody, permHead, permBody);
  renderAccessRequests(reqBody);
  renderAccess(pageBody);
  renderPermissions(permBody);
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
          h('option', { value: 'allow', text: 'Always allow', selected: p.override === true && !p.view_only }),
          h('option', { value: 'view', text: 'View only (cannot save or edit)', selected: p.override === true && !!p.view_only }),
          h('option', { value: 'block', text: 'Block', selected: p.override === false }));
        sel.addEventListener('change', async () => {
          sel.disabled = true;
          try { await api('/admin/access/users/' + who.value, { method: 'PUT', body: { page_key: p.key, allowed: sel.value === 'default' ? null : sel.value !== 'block', view_only: sel.value === 'view' } }); }
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
      h('p', { class: 'note', style: 'margin:6px 0 12px', text: 'Opening a page does not change what someone may do inside it: what a person can see and edit in the O2D screens still follows their role (the section below).' }),
      matrix),
    h('div', { class: 'card' },
      h('h2', { style: 'font-size:15px;margin-bottom:6px', text: 'Exceptions for one person' }),
      h('p', { class: 'note', style: 'margin-bottom:10px', text: 'Give one person a page their role does not have, block one they would normally get, or let them open a page as “View only” (they see everything on it but cannot save, enter or upload). Approved access requests show up here as “Always allow”.' }),
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
  panel.replaceChildren(h('div', { class: 'row', style: 'margin-bottom:12px' }, filter), holder);
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

  const dl = (path, name) => () => downloadFile(path, name).catch((ex) => alert(ex.message));
  root.replaceChildren(
    h('div', { class: 'card import-steps' },
      h('h3', { text: `Upload ${label} in bulk` }),
      h('div', { class: 'import-step' }, h('span', { class: 'step-no', text: '1' }),
        h('div', {}, h('strong', { text: 'Download the template' }),
          h('p', { class: 'note', text: kind === 'orders'
            ? 'The Excel template has dropdowns for every list value and a “Valid values” sheet. Rows marked EXAMPLE are ignored.'
            : 'The Excel template has a dropdown for Role. Rows marked EXAMPLE are ignored.' }),
          h('div', { class: 'row' },
            h('button', { class: 'btn primary', id: 'templateXlsxBtn', text: '⬇ Excel template (.xlsx)', onclick: dl(`/admin/bulk/${kind}/template.xlsx`, kind + '_upload_template.xlsx') }),
            h('button', { class: 'btn', id: 'templateBtn', text: '⬇ CSV template', onclick: dl(`/admin/bulk/${kind}/template.csv`, kind + '_upload_template.csv') })))),
      h('div', { class: 'import-step' }, h('span', { class: 'step-no', text: '2' }),
        h('div', {}, h('strong', { text: 'Fill it in and upload it here' }),
          h('p', { class: 'note', text: 'Every row is checked before anything is saved: you will see how many passed, how many failed, and what to fix in each failed row.' }),
          file, status)),
      h('div', { class: 'import-step' }, h('span', { class: 'step-no', text: '3' }),
        h('div', {}, h('strong', { text: 'Fix any failed rows, then import' }),
          h('p', { class: 'note', text: 'Correct the cells on this page (or download the failed rows, fix them and upload again). An import can be undone from the list below.' })))),
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
        h('div', { class: 'kpi-strip import-result', id: 'importResult' },
          h('div', { class: 'kpi' }, h('div', { class: 'k-label', text: 'Rows checked' }), h('div', { class: 'k-value', text: fmtInt(rep.total) })),
          h('div', { class: 'kpi k-pass' }, h('div', { class: 'k-label', text: 'Passed' }), h('div', { class: 'k-value', id: 'passedCount', text: fmtInt(validCount) }), h('div', { class: 'k-sub', text: 'ready to import' })),
          h('div', { class: 'kpi k-fail' }, h('div', { class: 'k-label', text: 'Failed' }), h('div', { class: 'k-value', id: 'failedCount', text: fmtInt(problems) }), h('div', { class: 'k-sub', text: problems ? 'see what to fix below' : 'nothing to fix' }))),
        problems ? h('div', {},
          h('p', { class: 'note', text: 'Each failed row says what to fix. Correct the cells below and press Re-check. Rows you leave with problems are skipped.' }),
          h('div', { class: 'row', style: 'margin-bottom:8px' }, h('button', { class: 'btn small', id: 'downloadFailedBtn', text: '⬇ Download failed rows (CSV)', onclick: downloadFailed })),
          h('div', { class: 'table-wrap' }, h('table', { id: 'errorTable' },
            h('thead', {}, h('tr', {}, h('th', { text: 'Row in file' }), h('th', { text: 'What to fix' }), columns.map((c) => h('th', { text: c })))),
            h('tbody', {}, rowsFor))),
          h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn', id: 'recheckBtn', text: 'Re-check', onclick: recheck }))) : h('p', { class: 'note', text: 'Everything looks good.' }),
        h('div', { class: 'row', style: 'margin-top:14px' }, importBtn),
        rep.help ? h('details', { class: 'fgroup', style: 'margin-top:14px' }, h('summary', { text: 'What each column means' }), h('p', { class: 'note', text: rep.help })) : null);
    };
    const downloadFailed = () => {
      const q = (v) => '"' + String(v ?? '').replace(/"/g, '""') + '"';
      const lines = [['Row in file', 'What to fix', ...columns].map(q).join(',')];
      for (const e of errors) lines.push([e.row, e.error, ...columns.map((c) => held.get(e.row)[c])].map(q).join(','));
      const url = URL.createObjectURL(new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }));
      const a = h('a', { href: url, download: kind + '_failed_rows.csv' });
      document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
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

// ------------------------------------------------------------------ Setup > Dropdown values (lists + clean-up in one place)
// Every list behind the dashboard / order-entry dropdowns: add, rename, delete, merge look-alike spellings. Each change is
// written to the PostgreSQL database at once and shows on every screen. [tab id, reconcile list, role, label, lookup slug]
const LIST_TABS = [
  ['channel', 'channel', null, 'Order via', 'channels'], ['submission_type', 'submission_type', null, 'Submission type', 'submission-types'],
  ['delivery_status', 'delivery_status', null, 'Delivery status', 'delivery-statuses'], ['payment_status', 'payment_status', null, 'Payment status', 'payment-statuses'],
  ['ready_by', 'person', 'ready_by', 'Ready by', null], ['colour_making', 'person', 'colour_making', 'Colour making by', null],
  ['delivery', 'person', 'delivery', 'Delivered by', null]];

// how one flag of a master list reads in the table
function masterCell(f, row) {
  const v = row && row[f.name];
  if (f.type === 'bool') return v ? 'Yes' : 'No';
  if (f.type === 'select') { const o = (f.options || []).find(([ov]) => ov === v || (ov === null && v == null)); return o ? o[1] : (v == null ? '–' : String(v)); }
  return v == null ? '' : String(v);
}

const SCOPE_LABEL = { both: 'Both dispatch screens', shop: 'Shop dispatch & receiving only', godown: 'Godown dispatch only' };
const SCOPE_FIELD = { name: 'dispatch_scope', label: 'Offered on', type: 'select', value: 'both',
  options: Object.entries(SCOPE_LABEL), hint: 'Which dispatch screen lists this person under "Delivered by".' };

async function renderReconcile(panel, tab) {
  tab = tab || S.listTab || 'channel';
  S.listTab = tab;
  // lists of the other modules (companies, payment modes, transaction types, parties, vendors) are described by the server
  let masters = [];
  try { masters = await api('/admin/masters'); } catch { /* the order lists still work */ }
  const allTabs = LIST_TABS.concat(masters.map((m) => [m.id, m.kind, m.role, m.label, null, m]));
  const [, kind, role0, tabLabel, slug, master] = allTabs.find((t) => t[0] === tab) || LIST_TABS[0];
  const role = master ? null : role0;          // `role` below means "a list of people" (phone, delivery scope)
  const roleFilter = master ? master.role : role0;
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let data, phones = new Map(), scopes = new Map(), mrows = new Map(), mdef = master;
  try {
    const calls = [api('/admin/reconcile/' + kind)];
    if (role) calls.push(api('/admin/people?role=' + role));
    if (master) calls.push(api('/admin/masters/' + master.id));
    const [d, extra] = await Promise.all(calls);
    data = d;
    if (role && extra) extra.forEach((r) => { phones.set(r.key, r.phone_number || ''); scopes.set(r.key, r.dispatch_scope || 'both'); });
    if (master && extra) { mdef = { ...master, fields: extra.fields }; extra.rows.forEach((r) => mrows.set(r.key, r)); }
  } catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  // people (and parties vs vendors) share one list in the database; show only the chosen kind
  const values = master ? [...mrows.values()].map((r) => ({ ...r, orders: r.used, protected: false })) : data.values.filter((v) => !role || v.role === role);
  const dups = data.suggestions.filter((sg) => !roleFilter || sg.role === roleFilter);
  const reload = () => renderReconcile(panel, tab);
  const byKey = new Map(values.map((v) => [v.key, v]));
  const picked = new Set();

  const kinds = h('div', { class: 'subtabs list-tabs', role: 'tablist' }, allTabs.map(([id, , , label, , m]) => h('button', {
    class: 'subtab', role: 'tab', 'aria-selected': String(id === tab), text: label + (m && m.pending ? ` (${m.pending})` : ''), onclick: () => renderReconcile(panel, id) })));

  const search = h('input', { type: 'search', id: 'rc_search', placeholder: 'Search ' + tabLabel.toLowerCase(), 'aria-label': 'Search values', style: 'max-width:260px' });
  const mergeBtn = h('button', { class: 'btn', id: 'rc_mergeBtn', text: 'Merge selected…', disabled: true, onclick: () => openMerge([...picked]) });
  const addBtn = h('button', { class: 'btn primary', id: 'rc_addBtn', text: '+ Add ' + tabLabel.toLowerCase(), onclick: () => openAdd() });
  const tbody = h('tbody');
  const dupBox = h('div');

  // the flags of a master list (checkbox / choice / number), as form fields
  const masterFields = (row) => (mdef ? mdef.fields : []).map((f) => ({ name: f.name, label: f.label, type: f.type === 'number' ? 'number' : f.type,
    options: f.options && f.options.map(([v, l]) => [v === null ? '' : String(v), l]),
    value: row ? (f.type === 'select' ? (row[f.name] == null ? '' : String(row[f.name])) : row[f.name]) : (f.type === 'select' && f.options ? String(f.options[0][0] ?? '') : (f.type === 'bool' ? false : '')) }));
  const masterBody = (v, name) => { const b = { name }; (mdef ? mdef.fields : []).forEach((f) => { b[f.name] = f.type === 'select' ? (v[f.name] === '' ? null : (isNaN(Number(v[f.name])) ? v[f.name] : Number(v[f.name]))) : v[f.name]; }); return b; };

  function openAdd() {
    if (master) {
      return formDialog({ title: 'Add ' + tabLabel.toLowerCase(), submitLabel: 'Add',
        fields: [{ name: 'name', label: 'Name', required: true, maxlength: 150 }, ...masterFields(null)],
        onSubmit: async (v) => { await api('/admin/masters/' + master.id, { method: 'POST', body: masterBody(v, v.name) }); reload(); } });
    }
    formDialog({ title: 'Add ' + tabLabel.toLowerCase(), submitLabel: 'Add',
      fields: [{ name: 'name', label: 'Name', required: true, maxlength: 100 }, role ? { name: 'phone', label: 'Phone (optional)' } : null,
        role === 'delivery' ? SCOPE_FIELD : null].filter(Boolean),
      onSubmit: async (v) => {
        if (role) await api('/admin/people?role=' + role, { method: 'POST', body: { full_name: v.name, phone_number: v.phone || null, dispatch_scope: v.dispatch_scope || 'both' } });
        else await api('/admin/lookups/' + slug, { method: 'POST', body: { name: v.name } });
        reload();
      } });
  }

  function openMerge(keys) {
    const items = keys.map((k) => byKey.get(k)).filter(Boolean);
    if (items.length < 2) return;
    const locked = items.filter((i) => i.protected);
    if (locked.length > 1) return alert('Two built-in values cannot be merged into each other.');
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

  function openEdit(v) {
    if (master) {
      return formDialog({ title: 'Edit “' + v.name + '”', fields: [{ name: 'name', label: 'Name', value: v.name, required: true, maxlength: 150 }, ...masterFields(mrows.get(v.key))],
        onSubmit: async (vals) => { await api('/admin/masters/' + master.id + '/' + v.key, { method: 'PUT', body: masterBody(vals, vals.name) }); reload(); } });
    }
    if (role) {
      formDialog({ title: 'Edit “' + v.name + '”', fields: [
        { name: 'full_name', label: 'Name', value: v.name, required: true, maxlength: 100 },
        { name: 'phone_number', label: 'Phone (optional)', value: phones.get(v.key) || '' },
        role === 'delivery' ? { ...SCOPE_FIELD, value: scopes.get(v.key) || 'both' } : null].filter(Boolean),
      onSubmit: async (vals) => { await api('/admin/people/' + v.key, { method: 'PUT', body: { full_name: vals.full_name, phone_number: vals.phone_number || null, dispatch_scope: vals.dispatch_scope || 'both' } }); reload(); } });
      return;
    }
    formDialog({ title: 'Rename “' + v.name + '”', submitLabel: 'Rename',
      fields: [{ name: 'name', label: 'Correct spelling', value: v.name, required: true, maxlength: 100,
        hint: `Changes it on all ${fmtInt(v.orders)} order(s) that use it. If this spelling already exists, use Merge instead.` }],
      onSubmit: async (vals) => { await api(`/admin/reconcile/${kind}/rename`, { method: 'POST', body: { key: v.key, name: vals.name } }); reload(); } });
  }

  async function remove(v) {
    if (!(await confirmDialog('Delete "' + v.name + '"?', v.orders ? `${fmtInt(v.orders)} order(s) use it, so this will be refused. Merge it into another value instead.` : 'It is not used by any order.', 'Delete', true))) return;
    try { await api(master ? `/admin/masters/${master.id}/${v.key}` : role ? '/admin/people/' + v.key : `/admin/lookups/${slug}/${v.key}`, { method: 'DELETE' }); reload(); }
    catch (ex) { alert(ex.message); }
  }

  const isNew = (v) => !!(master && master.reviewed && mrows.get(v.key) && mrows.get(v.key).review_status === 'pending');
  const newCount = master && master.reviewed ? [...mrows.values()].filter((r) => r.review_status === 'pending').length : 0;
  const reviewBox = h('div');
  if (newCount) {
    reviewBox.append(h('div', { class: 'card', id: 'rc_review', style: 'margin-bottom:16px' },
      h('h3', { text: `${newCount} new value${newCount === 1 ? '' : 's'} to review` }),
      h('p', { class: 'note', text: 'Typed on the module screens. They already work there. Approve the ones that are right, correct a spelling with Edit, or Merge a duplicate into the value it should be.' }),
      h('button', { class: 'btn', id: 'rc_approveAll', text: 'Approve all', onclick: async () => {
        try { await api(`/admin/masters/${master.id}/approve-all`, { method: 'POST' }); reload(); } catch (ex) { alert(ex.message); } } })));
  }

  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const shown = values.filter((v) => !q || v.name.toLowerCase().includes(q));
    const rows = shown.map((v) => {
      const box = h('input', { type: 'checkbox', 'aria-label': 'Select ' + v.name, checked: picked.has(v.key) });
      box.addEventListener('change', () => { if (box.checked) picked.add(v.key); else picked.delete(v.key); mergeBtn.disabled = picked.size < 2; });
      return h('tr', { 'data-key': v.key },
        h('td', {}, box), h('td', {}, v.name, v.protected ? h('span', { class: 'pill warn', style: 'margin-left:8px', text: 'Built-in' }) : null,
          isNew(v) ? h('span', { class: 'pill warn', style: 'margin-left:8px', title: 'Typed on a module screen' + (mrows.get(v.key).created_by ? ' by ' + mrows.get(v.key).created_by : ''), text: 'New - review' }) : null),
        role ? h('td', { text: phones.get(v.key) || '' }) : null,
        ...(master ? mdef.fields.map((f) => h('td', { text: masterCell(f, mrows.get(v.key)) })) : []),
        role === 'delivery' ? h('td', { text: SCOPE_LABEL[scopes.get(v.key) || 'both'] }) : null,
        h('td', { class: 'num', text: fmtInt(v.orders) }),
        h('td', {}, v.protected ? h('span', { class: 'note', text: 'Used by order rules' }) : h('div', { class: 'row' },
          isNew(v) ? h('button', { class: 'btn small primary', text: 'Approve', onclick: async () => { try { await api(`/admin/masters/${master.id}/${v.key}/approve`, { method: 'POST' }); reload(); } catch (ex) { alert(ex.message); } } }) : null,
          h('button', { class: 'btn small', text: role || master ? 'Edit' : 'Rename', onclick: () => openEdit(v) }),
          h('button', { class: 'btn small danger', text: 'Delete', onclick: () => remove(v) }))));
    });
    tbody.replaceChildren(...(rows.length ? rows : [h('tr', {}, h('td', { colspan: 5, class: 'empty', text: values.length ? 'No values match.' : 'Nothing yet - add the first one.' }))]));
  };
  search.addEventListener('input', draw);

  dupBox.replaceChildren(...(dups.length ? [h('div', { class: 'card', style: 'margin-bottom:16px' },
    h('h3', { text: `Possible duplicates (${dups.length})` }),
    h('p', { class: 'note', text: 'These look like the same thing spelled differently. Review each pair, and merge it if it really is the same.' }),
    h('div', { class: 'table-wrap' }, h('table', { id: 'rc_dups' }, h('tbody', {}, dups.map((sg) => h('tr', {},
      h('td', { text: sg.names[0] }), h('td', { text: '≈' }), h('td', { text: sg.names[1] }),
      h('td', {}, h('button', { class: 'btn small', text: 'Review & merge', onclick: () => openMerge(sg.keys) }))))))))] : []));

  panel.replaceChildren(
    h('p', { class: 'note', style: 'margin-bottom:12px', text: 'The values behind every dropdown and dashboard filter. Add, rename, delete or merge look-alike spellings here: each change is saved to the database at once, shows on all order screens and dashboards, and is kept in the audit trail. A value that orders still use cannot be deleted - merge it into the right one instead.' }),
    kinds, reviewBox, dupBox,
    h('div', { class: 'row', style: 'margin-bottom:10px' }, search, h('span', { class: 'grow' }), mergeBtn, addBtn),
    h('div', { class: 'table-wrap' }, h('table', { id: 'rc_table' },
      h('thead', {}, h('tr', {}, h('th', { text: '' }), h('th', { text: 'Value' }), role ? h('th', { text: 'Phone' }) : null, ...(master ? mdef.fields.map((f) => h('th', { text: f.label })) : []), role === 'delivery' ? h('th', { text: 'Offered on' }) : null, h('th', { class: 'num', text: master ? 'Used by' : 'Orders using it' }), h('th', { text: '' }))), tbody)));
  draw();
}


// ------------------------------------------------------------------ Setup > Module settings
// The numbers the Delegation module scores with and the day its week starts on (they used to be constants in the old script).
async function renderModuleSettings(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let list;
  try { list = await api('/admin/module-settings'); } catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  const groups = {};
  list.forEach((s) => { (groups[s.group] = groups[s.group] || []).push(s); });
  const cards = Object.entries(groups).map(([group, items]) => h('div', { class: 'card', style: 'margin-bottom:16px' },
    h('h2', { style: 'font-size:15px;margin-bottom:6px', text: group }),
    h('p', { class: 'note', style: 'margin-bottom:10px', text: 'Changes apply to new activity straight away. Scores already given are not recalculated.' }),
    ...items.map((s) => {
      const input = h('input', { type: 'number', value: String(s.value), min: String(s.min), max: String(s.max), id: 'set_' + s.key, 'aria-label': s.label, style: 'max-width:110px' });
      const msg = h('span', { class: 'msg' });
      const save = h('button', { class: 'btn small', text: 'Save', onclick: async () => {
        msg.className = 'msg'; msg.textContent = '';
        try { await api('/admin/module-settings', { method: 'PUT', body: { key: s.key, value: Number(input.value) } }); msg.textContent = 'Saved'; }
        catch (ex) { msg.className = 'msg error'; msg.textContent = ex.message; }
      } });
      return h('div', { class: 'row', style: 'margin:8px 0;align-items:center;gap:10px;flex-wrap:wrap' }, h('label', { style: 'flex:1;min-width:220px', text: s.label }), input, save, msg);
    })));
  panel.replaceChildren(...cards);
}
