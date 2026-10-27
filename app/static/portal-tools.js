'use strict';
/* Admin tools: the Orders tab (filters, saved filters, Excel export) and the Import tab (bulk upload with template,
   validation, inline fixes, undo). Loaded after portal.js and uses its helpers (h, api, S, dialog, formDialog ...). */

const FILTER_GROUPS = [['stage', 'Stage'], ['channel', 'Order via'], ['submission_type', 'Submission type'],
  ['delivery_status', 'Delivery status'], ['payment_status', 'Payment status'], ['ready_by', 'Ready by'],
  ['colour_making_by', 'Colour making by'], ['delivered_by', 'Delivered by'], ['created_by', 'Logged by'],
  ['bill_kind', 'Challan / Invoice'], ['sale_kind', 'Sales / Sales return']];
// Each pair is [min key, max key]; rendered as one "from – to" field instead of two separate boxes.
const RANGE_FILTERS = [['min_amount', 'max_amount', 'Amount (Rs)'], ['min_cartage', 'max_cartage', 'Cartage (Rs)']];
const ORDER_COLS = [['sl_no', 'Sl'], ['order_date', 'Order date'], ['dc_inv_no', 'DC/Inv'], ['stage', 'Stage'], ['channel', 'Via'],
  ['delivery_status', 'Delivery'], ['payment_status', 'Payment'], ['amount_received', 'Amount'], ['cartage', 'Cartage'], ['hours_to_deliver', 'Hours']];

// Admin can edit or permanently delete any order here, active or archived - no need to go through
// the O2D "view as" screens first. Edit reuses the same endpoint those screens' admin form uses.
// "2026-10-25T11:00:00+00:00" -> "2026-10-25T11:00": the wall-clock time exactly as the dispatch screens store and show it (no zone shift),
// so saving the form without touching the box leaves the delivery time as it was.
function localInput(iso) {
  return iso ? String(iso).replace(' ', 'T').slice(0, 16) : '';
}

function openOrderEdit(row, options, onSaved) {
  const photos = h('div', { id: 'editPhotos' });
  showOrderPhotos(row.sl_no, photos, true);   // the order's photos, each with a Delete photo button, plus Add photo
  const sel = (key) => [['', '-- no change --'], ...(options[key] || []).map((v) => [v, v])];
  formDialog({
    title: `Edit order ${row.dc_inv_no || row.sl_no}`, submitLabel: 'Save', after: photos,
    extraButtons: [{ text: 'Delete order', danger: true, onClick: (dlg) => deleteOrder(row, (n) => { dlg.close(); onSaved(n); }) }],
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
      { name: 'deliveredByDetail', label: 'Porter / Company / Transport details', value: row.delivered_by_detail || '' },
      { name: 'materialDeliveryDateTime', label: 'Material delivery date & time', type: 'datetime-local', value: localInput(row.material_delivery_datetime) },
      { name: 'dateOfReceiving', label: 'Date of receiving', type: 'date', value: row.date_of_receiving ? String(row.date_of_receiving).slice(0, 10) : '' },
      { name: 'billSeries', label: 'Challan / Invoice tag (only for a type that can be either)', type: 'select',
        options: [['', '-- no change --'], ['challan', 'Challan'], ['invoice', 'Invoice']], value: row.bill_series || '' },
      { name: 'amountReceived', optional: true, type: 'number', value: row.amount_received_entered ?? '',
        label: 'Amount received (as entered; dashboards show it ÷ 100)' + (row.amount_locked ? ' - entered at the shop as an advance' : '') },
      { name: 'cartage', label: 'Cartage', type: 'number', optional: true, value: row.cartage ?? '' },
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
  ['deliveredByWhom', 'delivered_by', 'Delivered by'], ['deliveredByDetail', 'delivered_by_detail', 'Porter / Company / Transport details'],
  ['dateOfReceiving', 'date_of_receiving', 'Date of receiving'], ['amountReceived', 'amount_received_entered', 'Amount received'],
  ['cartage', 'cartage', 'Cartage'], ['shippingLocation', 'shipping_location', 'Address'],
  ['detailedRemarks', 'detailed_remarks', 'Remarks']];

function editSummary(row, v) {
  const changes = [];
  for (const [form, col, label] of EDIT_FIELDS) {
    if (v[form] === '' && ['orderVia', 'typeOfSubmission', 'deliveryStatus', 'paymentStatus', 'readyByWhom', 'colourMakingBy', 'deliveredByWhom',
      'dateOfReceiving'].includes(form)) continue; // "no change"
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

// ------------------------------------------------------------------ shared Setup widgets
// Two-level picker: one chip per module, and under the chosen module its own short row of items. Used where a flat row of tabs
// grew too long (Import, Dropdown values). groups: [{ id, label, icon, badge?, items: [{ id, label }] }]
const PICKER_MEMO = {};   // picker name -> { group id: last item opened in it }
function groupPicker(name, groups, activeId, onPick, itemsClass) {
  const memo = PICKER_MEMO[name] = PICKER_MEMO[name] || {};
  const group = groups.find((g) => g.items.some((i) => i.id === activeId)) || groups[0];
  memo[group.id] = activeId;
  return h('div', { class: 'picker' },
    h('div', { class: 'picker-groups', role: 'tablist', 'aria-label': 'Module' }, groups.map((g) => h('button', {
      type: 'button', class: 'picker-group', role: 'tab', 'aria-selected': String(g === group), 'data-group': g.id,
      onclick: () => onPick(memo[g.id] && g.items.some((i) => i.id === memo[g.id]) ? memo[g.id] : g.items[0].id) },
      h('span', { 'aria-hidden': 'true', text: g.icon }), h('span', { text: g.label }),
      g.badge ? h('span', { class: 'picker-badge', text: String(g.badge) }) : null))),
    group.items.length > 1 ? h('div', { class: 'subtabs picker-items ' + (itemsClass || ''), role: 'tablist', 'aria-label': group.label }, group.items.map((i) => h('button', {
      type: 'button', class: 'subtab', role: 'tab', 'aria-selected': String(i.id === activeId), text: i.label, onclick: () => onPick(i.id) }))) : null);
}

// A section that opens and closes when its heading is clicked. `open` is the starting state; `onOpen` runs the first time it opens
// (so a heavy table is only fetched when someone wants it).
function collapsible(id, title, summary, body, { open = false, badge = null, onOpen = null } = {}) {
  let opened = false;
  const box = h('section', { class: 'collapse' + (open ? ' open' : ''), id });
  const content = h('div', { class: 'collapse-body', id: id + 'Body', hidden: open ? null : true }, body);
  const toggle = (force) => {
    const next = force === undefined ? content.hidden : force;
    content.hidden = !next;
    box.classList.toggle('open', next);
    head.setAttribute('aria-expanded', String(next));
    if (next && !opened) { opened = true; if (onOpen) onOpen(); }
  };
  const head = h('button', { type: 'button', class: 'collapse-head', 'aria-expanded': String(open), 'aria-controls': id + 'Body',
    onclick: () => toggle() },
    h('span', { class: 'collapse-caret', 'aria-hidden': 'true', text: '▸' }),
    h('span', { class: 'collapse-title' }, h('strong', { text: title }), summary ? h('span', { class: 'note', text: summary }) : null),
    badge ? h('span', { class: 'picker-badge', text: String(badge) }) : null);
  box.append(head, content);
  if (open) { opened = true; if (onOpen) onOpen(); }
  box.toggleOpen = toggle;
  return box;
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
  let view = 'open';  // which KPI card is selected; it filters the whole page (opens on the orders still running: closed ones are one click away)
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
      // Choosing the Closed stage in the Stage filter is asking to see them, so the "open" start view steps aside
      f.view = view === 'open' && selections.stage.has('Closed') ? 'active' : view;
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
  const KPI_CARDS = [['open', 'Open orders', 'shown by default - closed ones are hidden', 'k-active'], ['total', 'Total orders', 'all orders, archived included', ''],
    ['active', 'Active', 'including closed ones', ''],
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
        onclick: () => { view = view === key && key !== 'open' ? 'open' : key; run(); } },
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
      ['Material delivered', when(o.material_delivery_datetime)],
      ['WhatsApp alert', o.whatsapp_alert ? o.whatsapp_alert.title + ' - ' + fmtExact(o.whatsapp_alert.at) + ' IST' : 'No alert sent'], ['Date received', o.date_of_receiving ? fmtDate(o.date_of_receiving) + ' ' + String(o.date_of_receiving).slice(0, 4) : ''],
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
        h('div', { class: 'tl-by', text: (e.by ? 'By ' + e.by + (e.role ? ' (' + roleLabel(e.role) + ')' : '') : 'Person not recorded') + (e.ip ? '  ·  IP ' + e.ip : '') }),
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
    showOrderPhotos(slNo, body);
  } catch (ex) { body.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
}

// ------------------------------------------------------------------ Setup > Order changes (read-only change log)
// Every recorded change on an order, oldest first inside each order: when (IST), who, what changed, from which IP. Admin only.
async function renderOrderChanges(panel) {
  const q = h('input', { type: 'search', id: 'chgQuery', placeholder: 'Order (S) number or DC / invoice no', 'aria-label': 'Order number', value: S.chgQ || '' });
  const screen = h('select', { id: 'chgScreen', 'aria-label': 'Dashboard / screen' }, h('option', { value: '', text: 'All dashboards' }));
  const from = h('input', { type: 'date', id: 'chgFrom', 'aria-label': 'From date' }), to = h('input', { type: 'date', id: 'chgTo', 'aria-label': 'To date' });
  const out = h('div', { id: 'chgResults' });
  let loaded = false;
  const load = async () => {
    S.chgQ = q.value;
    out.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
    const qs = new URLSearchParams();
    if (q.value.trim()) qs.set('q', q.value.trim());
    if (screen.value) qs.set('screen', screen.value);
    if (from.value) qs.set('date_from', from.value);
    if (to.value) qs.set('date_to', to.value);
    let d;
    try { d = await api('/admin/order-events?' + qs); } catch (ex) { return out.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
    if (!loaded) { d.screens.forEach((x) => screen.append(h('option', { value: x.id, text: x.label }))); loaded = true; }
    if (!d.events.length) return out.replaceChildren(h('div', { class: 'card empty', text: 'No recorded changes match.' }));
    const groups = new Map();
    d.events.forEach((e) => { if (!groups.has(e.slNo)) groups.set(e.slNo, []); groups.get(e.slNo).push(e); });
    const what = (e) => (e.changes.length ? h('ul', { class: 'tl-changes' }, e.changes.map((c) => h('li', {},
      h('span', { class: 'tl-field', text: c.field + ': ' }), c.from ? h('span', { class: 'tl-from', text: c.from }) : h('span', { class: 'tl-none', text: 'empty' }),
      ' → ', h('strong', { text: c.to == null ? 'cleared' : c.to })))) : null);
    out.replaceChildren(...[...groups].map(([sl, evs]) => h('div', { class: 'card', style: 'margin-bottom:14px' },
      h('h3', { style: 'font-size:15px;margin-bottom:8px', text: `Order ${evs[0].dcNo || sl}  ·  Sl ${sl}` }),
      h('div', { class: 'table-wrap' }, h('table', { class: 'change-log' },
        h('thead', {}, h('tr', {}, ['Date & time (IST)', 'Dashboard', 'Changed by', 'What changed', 'IP address'].map((t) => h('th', { text: t })))),
        h('tbody', {}, evs.map((e) => h('tr', {},
          h('td', { text: fmtExact(e.at) }), h('td', { text: (d.screens.find((x) => x.id === e.type) || {}).label || e.type }),
          h('td', { text: (e.by || '–') + (e.role ? ' (' + roleLabel(e.role) + ')' : '') }),
          h('td', {}, h('strong', { text: e.title }), what(e)), h('td', { text: e.ip || '–' })))))))),
      d.truncated ? h('p', { class: 'note', text: 'Showing the first changes only - narrow the order number, dashboard or dates.' }) : null);
  };
  [q, screen, from, to].forEach((el) => el.addEventListener('change', load));
  q.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') load(); });
  // wrong-series check: an invoice number typed in the challan field, or the reverse (judged by the size of the number)
  const checkOut = h('div', { id: 'numCheckOut' });
  const runCheck = async () => {
    checkOut.replaceChildren(h('p', { class: 'note', text: 'Checking…' }));
    let d;
    try { d = await api('/admin/number-check'); } catch (ex) { return checkOut.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
    if (!d.items.length) return checkOut.replaceChildren(h('p', { class: 'note', text: '✅ No bill number looks out of place.' }));
    const fix = (it) => formDialog({ title: `Fix the number on Sl ${it.slNo}`, submitLabel: 'Save', fields: [
      { name: 'dcNo', label: `DC / Inv No (now ${it.dcNo}, ${it.series})`, value: it.dcNo, hint: 'The change is saved on the order and shown in the change log with your name. To move it to the other series, change its Submission type in All orders > Edit.' }],
      onSubmit: async (v) => { await api(`/o2d/orders/${it.slNo}/admin`, { method: 'PUT', body: { dcNo: v.dcNo } }); runCheck(); } });
    checkOut.replaceChildren(h('div', { class: 'table-wrap' }, h('table', { id: 'numCheckTable' },
      h('thead', {}, h('tr', {}, ['Sl', 'Order date', 'Number', 'Series', 'Submission type', 'Why it looks wrong', ''].map((t) => h('th', { text: t })))),
      h('tbody', {}, d.items.map((it) => h('tr', {}, h('td', { text: it.slNo }), h('td', { text: it.date }), h('td', { text: it.dcNo }), h('td', { text: it.series }),
        h('td', { text: it.type || '' }), h('td', { text: it.reason, style: 'max-width:420px' }),
        h('td', {}, h('button', { class: 'btn small', text: 'Fix number', onclick: () => fix(it) }))))))));
  };
  panel.replaceChildren(
    h('div', { class: 'card', style: 'margin-bottom:16px' },
      h('h3', { style: 'font-size:15px;margin-bottom:6px', text: 'Wrong bill series check' }),
      h('p', { class: 'note', text: 'Looks for a number that is out of scale for its series: a Challan far above that day’s others (probably an Invoice number), or an Invoice far below the year’s usual size (probably a Challan number). Equal-sized numbers typed in the wrong field cannot be detected.' }),
      h('div', { class: 'row', style: 'margin:8px 0' }, h('button', { class: 'btn primary', id: 'numCheckBtn', text: 'Check now', onclick: runCheck })), checkOut),
    h('p', { class: 'note', text: 'Read-only. Every change made on an order, in time order: when, who, what, and the IP address it came from. Changes made before IP tracking show “–”.' }),
    h('div', { class: 'row', style: 'margin:12px 0;gap:10px;flex-wrap:wrap' }, q, screen, h('label', { class: 'note', text: 'From ' }, from), h('label', { class: 'note', text: 'To ' }, to),
      h('button', { class: 'btn primary', text: 'Show', onclick: load })), out);
  load();
}

// The dispatch and receiving photos taken on the O2D screens, shown on the order's details.
// Shrink a phone photo to a JPEG of at most 1600 px so it stays well under the upload limit.
function shrinkPhoto(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, 1600 / Math.max(img.width, img.height));
      const c = document.createElement('canvas');
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      c.toBlob((b) => resolve(b || file), 'image/jpeg', 0.85);
    };
    img.onerror = () => { URL.revokeObjectURL(url); resolve(file); };   // e.g. HEIC the browser cannot draw: send as is
    img.src = url;
  });
}

// The dispatch and receiving photos taken on the O2D screens, shown on the order's details. canAdd (the admin's Edit) also lets
// photos be added; Delete photo is admin-only on the server.
async function showOrderPhotos(slNo, body, canAdd) {
  let d;
  try { d = await api(`/o2d/orders/${slNo}/photos`); } catch { return; }
  if (!d || !d.enabled) {
    if (canAdd) body.append(h('p', { class: 'note', text: 'Photos: storage is not set up on this server, so dispatch / receiving photos cannot be shown or added here.' }));
    return;
  }
  if (!d.photos.length && !canAdd) return;
  const err = h('div', { class: 'msg error' });
  // Viewing: one list. Editing: a Dispatch and a Receiving section, each with its own Add photo button and a Delete on every photo.
  const kinds = canAdd ? [['dispatch', 'Dispatch photos', 'Add dispatch photo'], ['receiving', 'Receiving photos', 'Add receiving photo']] : [[null, 'Photos', null]];
  const sections = kinds.map(([kind, label, addLabel]) => ({ kind, label, addLabel, title: h('h3', { class: 'tl-title' }),
    grid: h('div', { style: 'display:flex;flex-wrap:wrap;gap:10px' }) }));
  const recount = () => sections.forEach((x) => { x.title.textContent = `${x.label} (${x.grid.children.length})`; });
  const tile = (p) => {
    const box = h('div', { style: 'width:140px' });
    const del = h('button', { type: 'button', class: 'btn danger', text: 'Delete photo', style: 'width:100%;margin-top:4px', onclick: async () => {
      if (!(await confirmDialog('Delete this photo?', `The ${p.kind} photo by ${p.uploadedBy || '?'} is removed from the order for good. Cannot be undone.`, 'Delete', true))) return;
      try { await api(`/o2d/orders/${slNo}/photos/${p.id}`, { method: 'DELETE' }); } catch (ex) { err.textContent = ex.message || 'Could not delete the photo.'; return; }
      box.remove();
      err.textContent = '';
      recount();
    } });
    box.append(h('a', { href: p.url, target: '_blank', rel: 'noopener',
      title: `${p.kind} photo by ${p.uploadedBy || '?'} - ${fmtExact(p.uploadedAt)} IST`, style: 'display:block;width:140px;height:140px;border-radius:10px;overflow:hidden;border:1px solid var(--border)' },
      h('img', { src: p.url, alt: p.kind + ' photo', style: 'width:100%;height:100%;object-fit:cover' })), del);
    return box;
  };
  const fill = (photos) => {
    sections.forEach((x) => { x.grid.replaceChildren(); photos.filter((p) => !x.kind || p.kind === x.kind).forEach((p) => x.grid.append(tile(p))); });
    recount();
  };
  fill(d.photos);
  body.append(err);
  sections.forEach((x) => {
    body.append(x.title, x.grid);
    if (!x.kind) return;
    const file = h('input', { type: 'file', accept: 'image/*', multiple: true, style: 'display:none' });
    const add = h('button', { type: 'button', class: 'btn', text: x.addLabel, onclick: () => file.click() });
    file.addEventListener('change', async () => {
      const files = [...file.files]; file.value = '';
      add.disabled = true; err.textContent = '';
      try {
        for (const f of files) {
          add.textContent = 'Uploading...';
          const blob = await shrinkPhoto(f);
          const res = await fetch(`/o2d/orders/${slNo}/photos?kind=${x.kind}`, { method: 'POST', body: blob,
            headers: { Authorization: 'Bearer ' + S.token, 'Content-Type': blob.type || 'image/jpeg' } });
          if (!res.ok) { let m = res.statusText; try { const j = await res.json(); if (typeof j.detail === 'string') m = j.detail; } catch { /* default */ } throw new Error(m); }
        }
      } catch (ex) { err.textContent = ex.message || 'Could not add the photo.'; }
      add.disabled = false; add.textContent = x.addLabel;
      const fresh = await api(`/o2d/orders/${slNo}/photos`).catch(() => null);   // reload the tiles so the new photos show (and can be deleted)
      if (fresh && fresh.photos) fill(fresh.photos);
    });
    body.append(h('div', { style: 'margin:6px 0 12px' }, add, file));
  });
}

// ------------------------------------------------------------------ Setup > Access & permissions (one page)
// Two layers, kept apart on purpose: ACCESS decides which pages a person can open (the door); PERMISSIONS decide what a role may
// do once inside the O2D screens (which fields it may edit, whether it may view all orders). Requests to open a page are decided here too.
async function renderAccessHub(panel) {
  const useCase = (icon, title, question, example) => h('div', { class: 'usecase' },
    h('div', { class: 'usecase-title' }, h('span', { 'aria-hidden': 'true', text: icon }), h('strong', { text: title })),
    h('p', { class: 'usecase-q', text: question }), h('p', { class: 'note', text: example }));
  const explainer = h('div', { class: 'card hub-explainer', id: 'hubExplainer' },
    h('h2', { class: 'hub-title', text: 'How access and permissions work' }),
    h('p', { class: 'note', text: 'Two separate controls. A person first needs access to a page (the door), and then their role’s permissions decide what they can do inside it. Click a heading below to open or close its table.' }),
    h('div', { class: 'usecase-grid' },
      useCase('🚪', 'Page access', 'Can this person open this page at all?',
        'Example: the cashier role does not get the All orders page, so it never appears for them. Tick pages for a whole role, or add an exception for one person. A person who opens a page they cannot see can send a request, which you approve below.'),
      useCase('✏️', 'Role permissions', 'Once inside, what may this role see or change?',
        'Example: the shop role may set “Order via” and “Remarks” on an order but not “Payment status”. View-only roles such as accounts can see all orders only if you turn that on. Opening a page never grants editing.')));
  // each body keeps its old id; its table is only fetched the first time its heading is opened (requests open by themselves when some are waiting)
  const reqBody = h('div', { id: 'hubRequests' }), pageBody = h('div', { id: 'hubPages' });
  const permBody = h('div', { id: 'hubPermissions' }), viewBody = h('div', { id: 'hubViewAccess' });
  const waiting = S.pendingRequests || 0;
  panel.replaceChildren(explainer,
    h('div', { class: 'collapse-group' },
      collapsible('secRequests', 'Requests waiting for a decision', 'People who opened a page they cannot see can ask for access. Approving gives that one person the page; colleagues in the same role are not affected.',
        reqBody, { open: waiting > 0, badge: waiting || null, onOpen: () => renderAccessRequests(reqBody) }),
      collapsible('secPages', 'Who sees which page', 'The door: tick the pages each role opens by default, then add exceptions for one person.',
        pageBody, { onOpen: () => renderAccess(pageBody) }),
      collapsible('secPermissions', 'What each role can do inside the O2D screens', 'Which order fields each operating role may set. Changes apply immediately.',
        permBody, { onOpen: () => renderFieldPermissions(permBody) }),
      collapsible('secView', 'View access', 'Which roles without their own order screen may see all orders (read-only).',
        viewBody, { onOpen: () => renderViewAccess(viewBody) })));
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
  const matrix = h('div', { class: 'table-wrap matrix-scroll', tabindex: '0', role: 'region', 'aria-label': 'Page access by role (scrolls up, down, left and right)' }, h('table', { id: 'accessMatrix' },
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
// what the picker shows: one chip per module, its uploads underneath
const IMPORT_GROUPS = [
  { id: 'o2d', label: 'Orders & members', icon: '📋', items: [['orders', 'Orders'], ['users', 'Members']] },
  { id: 'pay', label: 'Payments', icon: '💳', items: [['parties', 'Parties'], ['payments', 'Payments dashboard import']] },
  { id: 'deleg', label: 'Delegation', icon: '✅', items: [['tasks', 'Tasks'], ['checklists', 'Checklists']] },
  { id: 'purchase', label: 'Purchase', icon: '🧾', items: [['vendors', 'Parties (vendors)'], ['purchases', 'Purchase dashboard import']] },
  { id: 'decor', label: 'VT Decor', icon: '🎨', items: [['decor_clients', 'Clients'], ['decor_payments', 'Payments'], ['decor_expenses', 'Expenses'], ['decor_items', 'Items'], ['decor_photos', 'Photos & challans'], ['decor_quotations', 'Quotations']] },
  { id: 'att', label: 'Attendance', icon: '🕘', items: [['att_employees', 'Employees'], ['att_attendance', 'Attendance records'], ['att_advances', 'Salary advances']] },
].map((g) => ({ ...g, items: g.items.map(([id, label]) => ({ id, label })) }));
const IMPORT_KINDS = IMPORT_GROUPS.flatMap((g) => g.items.map((i) => [i.id, i.label]));
const IMPORT_LABEL = { orders: 'orders', users: 'members', payments: 'payment entries', parties: 'payment parties', vendors: 'purchase parties (vendors)', tasks: 'tasks', purchases: 'purchase entries', purchases_pf: 'purchase entries (Payments layout)',
  decor_clients: 'VT Decor clients', decor_payments: 'VT Decor payments', decor_expenses: 'VT Decor expenses', decor_items: 'VT Decor items', decor_photos: 'VT Decor photo links', decor_quotations: 'VT Decor quotations',
  att_employees: 'attendance employees', att_attendance: 'attendance records', att_advances: 'salary advances', checklists: 'checklists' };
const IMPORT_TEMPLATE_NOTE = {
  orders: 'The Excel template has dropdowns for every list value and a “Valid values” sheet. Rows marked EXAMPLE are ignored.',
  users: 'The Excel template has a dropdown for Role. Rows marked EXAMPLE are ignored.',
  payments: 'Same columns as the old Payments Transactions sheet. Fill Txn ID (TXN-0001 ...) for an entry carried over from it: it keeps that number, its Amount is taken as already divided by 100, and its status, creator and approver are kept. Without a Txn ID the Amount is divided by 100 like typing it. The Excel template has dropdowns for Type, Company and Mode, and a “Valid values” sheet. Rows marked EXAMPLE are ignored.',
  parties: 'Columns: Type, Company, Party. Party is the name added to the list the Payments entry form suggests from; Type and Company are optional and only checked against the Dropdown values. Names already in the list are rejected as duplicates. Rows marked EXAMPLE are ignored.',
  vendors: 'One column: Party Name. This is the vendor list the Purchase entry form suggests from; names already in it are rejected as duplicates. Rows marked EXAMPLE are ignored.',
  tasks: 'Use the staff username exactly as shown under Members. Rows marked EXAMPLE are ignored.',
  purchases: 'Purchase format: Site, Vendor, Material Received Date, Invoice Date, Invoice No, Invoice Amount, Remarks, Photos (old Google Drive links, separated by |). The Excel template has dropdowns for Site and Vendor, and a “Valid values” sheet. Rows marked EXAMPLE are ignored.',
  purchases_pf: 'The same columns as the Payments upload, plus Site (Godown or Shop). Party is the vendor; Type, Company, Mode and Account Name are ignored. Amount is the invoice amount in rupees as typed. Rows marked EXAMPLE are ignored.',
  decor_clients: 'Same columns as the old VT Decor Master sheet. Put the old Client ID (C001 ...) in the first column to keep it - upload clients first, then payments, expenses and items, which refer to that code. Amounts are real rupees (not divided by 100). Rows marked EXAMPLE are ignored.',
  decor_payments: 'Needs the client code (C001 ...) of a client that already exists. Amounts are real rupees (not divided by 100). Rows marked EXAMPLE are ignored.',
  decor_expenses: 'Needs the client code (C001 ...) of a client that already exists. Amounts are real rupees (not divided by 100). Rows marked EXAMPLE are ignored.',
  decor_items: 'The old ItemsLog sheet. Needs the client code (C001 ...) of a client that already exists. Items do not change the Contract Amount (the old Master amount already includes them). Rows marked EXAMPLE are ignored.',
  decor_photos: 'The old PhotosLog sheet. Needs the client code (C001 ...) of a client that already exists. Only the Google Drive link is saved - the picture stays in Drive and must stay shared. Rows marked EXAMPLE are ignored.',
  decor_quotations: 'The old QuotationsLog sheet. Each quotation keeps its Performa No (VT-0001 ...); a number already in use is rejected. Rows marked EXAMPLE are ignored.',
  checklists: 'The Excel template has dropdowns for Frequency, Category, Day and Month. Day means the weekday for Weekly and the date of the month for Monthly / Quarterly / Yearly. Put several usernames in Assigned To (separated by ;) to give each their own copy. Rows marked EXAMPLE are ignored.',
  att_employees: 'Upload employees before their attendance records and advances. Rows marked EXAMPLE are ignored.',
  att_attendance: 'For days before the portal started (or missed days): employee, date, check-in and check-out time (24-hour). The status is worked out from your Module settings unless you write it. No WhatsApp message is sent. Rows marked EXAMPLE are ignored.',
  att_advances: 'The employee must already exist. Advances are deducted in that month’s salary report. Rows marked EXAMPLE are ignored.' };

async function renderImport(panel, kind) {
  kind = kind || S.importKind || 'orders';
  S.importKind = kind;
  const holder = h('div');
  const bar = groupPicker('import', IMPORT_GROUPS, kind, (id) => renderImport(panel, id));
  panel.replaceChildren(bar, holder);
  importFlow(holder, kind, () => renderImport(panel, kind));
}

function importFlow(root, kind, restart) {
  const label = IMPORT_LABEL[kind] || kind;
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
          h('p', { class: 'note', text: IMPORT_TEMPLATE_NOTE[kind] || '' }),
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
    if (!(await confirmDialog('Undo this import?', `Removes the ${b.row_count} ${IMPORT_LABEL[b.entity] || b.entity} from “${b.filename || 'import'}”. This only works if none of them has been used or edited since.`, 'Undo import', true))) return;
    try { await api(`/admin/bulk/batches/${b.batch_id}/undo`, { method: 'POST' }); restart(); } catch (ex) { alert(ex.message); }
  };
  box.replaceChildren(h('div', { class: 'table-wrap' }, h('table', { id: 'batchTable' },
    h('thead', {}, h('tr', {}, ['#', 'What', 'File', 'Rows', 'By', 'When', ''].map((t) => h('th', { text: t })))),
    h('tbody', {}, list.map((b) => h('tr', {},
      h('td', { text: b.batch_id }), h('td', { text: ((l) => l.charAt(0).toUpperCase() + l.slice(1))(IMPORT_LABEL[b.entity] || b.entity) }), h('td', { text: b.filename || '' }),
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

// Which module each list belongs to (the picker shows one chip per module, and its lists underneath), with the short name used on the chip.
const LIST_GROUPS = [
  { id: 'orders', label: 'Orders', icon: '📋', items: ['channel', 'submission_type', 'delivery_status', 'payment_status', 'ready_by', 'colour_making', 'delivery'] },
  { id: 'payments', label: 'Payments', icon: '💳', items: ['company', 'payment_mode', 'txn_type', 'party', 'expense_account', 'expense_category'] },
  { id: 'checklist', label: 'Checklist', icon: '☑️', items: ['checklist_category'] },
  { id: 'purchase', label: 'Purchase', icon: '🧾', items: ['vendor'] },
  { id: 'decor', label: 'VT Decor', icon: '🎨', items: ['decor_type', 'decor_stage', 'decor_expense_type'] },
  { id: 'att', label: 'Attendance', icon: '🕘', items: ['att_location', 'att_employee'] }];
const LIST_SHORT = { company: 'Companies', payment_mode: 'Payment modes', txn_type: 'Transaction types', party: 'Parties', vendor: 'Vendors',
  decor_type: 'Product types', decor_stage: 'Stages of each product type', decor_expense_type: 'Expense types (suggestions)',
  att_location: 'Work places (GPS spot)', att_employee: 'Employees',
  expense_account: 'Regular expenses - accounts', expense_category: 'Regular expenses - categories', checklist_category: 'Categories' };

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
  let screenOpts = null; const screensOf = new Map();   // the screens a Delivery / Payment status value can be limited to
  let extraFields = null; const extraOf = new Map();    // extra single-choice settings of a list (e.g. Submission type > Bill number series)
  try {
    const calls = [api('/admin/reconcile/' + kind)];
    if (role) calls.push(api('/admin/people?role=' + role));
    if (master) calls.push(api('/admin/masters/' + master.id));
    const [d, extra] = await Promise.all(calls);
    data = d;
    if (slug) {
      const [allScreens, allFields] = await Promise.all([api('/admin/lookup-screens'), api('/admin/lookup-fields')]);
      if (allScreens[slug] || allFields[slug]) {
        const lookupRows = await api('/admin/lookups/' + slug);
        if (allScreens[slug]) { screenOpts = allScreens[slug]; lookupRows.forEach((r) => screensOf.set(r.key, r.screens)); }
        if (allFields[slug]) { extraFields = allFields[slug]; lookupRows.forEach((r) => extraOf.set(r.key, r)); }
      }
    }
    if (role && extra) extra.forEach((r) => { phones.set(r.key, r.phone_number || ''); scopes.set(r.key, r.dispatch_scope || 'both'); });
    if (master && extra) { mdef = { ...master, fields: extra.fields }; extra.rows.forEach((r) => mrows.set(r.key, r)); }
  } catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  // people (and parties vs vendors) share one list in the database; show only the chosen kind
  const values = master ? [...mrows.values()].map((r) => ({ ...r, orders: r.used, protected: false })) : data.values.filter((v) => !role || v.role === role);
  const dups = data.suggestions.filter((sg) => !roleFilter || sg.role === roleFilter);
  const reload = () => renderReconcile(panel, tab);
  const byKey = new Map(values.map((v) => [v.key, v]));
  const picked = new Set();
  const screenLabel = (v) => { const sc = screensOf.get(v.key); return sc ? sc.map((id) => (screenOpts.find(([i]) => i === id) || [id, id])[1]).join(', ') : 'All screens'; };
  const allScreenIds = () => (screenOpts || []).map(([i]) => i);
  // one choice: every screen, or a single screen (a value set earlier to several screens stays selectable as it is)
  const screenField = (current) => {
    const multi = current && current.length > 1;
    const opts = [['', 'All screens'], ...screenOpts];
    if (multi) opts.push(['__keep', current.map((id) => (screenOpts.find(([i]) => i === id) || [id, id])[1]).join(' + ') + ' (as set now)']);
    return { name: 'screens', label: 'Offered on', type: 'select', options: opts, value: multi ? '__keep' : (current && current[0]) || '',
      hint: 'Only this screen lists the value in its dropdown. Orders already using it are not changed.' };
  };
  const extraInputs = (key) => (extraFields || []).map((f) => ({ name: f.name, label: f.label, type: 'select', options: f.options, value: ((extraOf.get(key) || {})[f.name]) || '' }));
  const extraBody = (v) => Object.fromEntries((extraFields || []).map((f) => [f.name, v[f.name] || '']));
  const extraLabel = (key, f) => { const val = ((extraOf.get(key) || {})[f.name]) || ''; return (f.options.find(([o]) => o === val) || ['', val])[1]; };
  const screensBody = (v, current) => (v.screens === '' ? allScreenIds() : v.screens === '__keep' ? current : [v.screens]);

  // drag a row (or use the arrows) to set the order the values appear in every dropdown
  let dragKey = null;
  const persistOrder = async () => {
    try { await api(`/admin/reconcile/${kind}/order`, { method: 'PUT', body: { keys: values.map((v) => v.key), role: roleFilter || null } }); }
    catch (ex) { alert(ex.message); reload(); }
  };
  const moveTo = (key, to) => {
    const from = values.findIndex((v) => v.key === key);
    if (from < 0 || to < 0 || to >= values.length || from === to) return;
    values.splice(to, 0, values.splice(from, 1)[0]);
    draw(); persistOrder();
  };

  // a list the server adds that is not named in LIST_GROUPS still shows, under "Other"
  const known = new Set(LIST_GROUPS.flatMap((g) => g.items));
  const tabInfo = new Map(allTabs.map(([id, , , label, , m]) => [id, { label: LIST_SHORT[id] || label, pending: m && m.pending ? m.pending : 0 }]));
  const pickerGroups = LIST_GROUPS.concat([{ id: 'other', label: 'Other', icon: '🗂', items: allTabs.map((t) => t[0]).filter((id) => !known.has(id)) }])
    .map((g) => {
      const items = g.items.filter((id) => tabInfo.has(id)).map((id) => ({ id, label: tabInfo.get(id).label + (tabInfo.get(id).pending ? ` (${tabInfo.get(id).pending})` : '') }));
      return { ...g, items, badge: g.items.reduce((n, id) => n + ((tabInfo.get(id) || {}).pending || 0), 0) };
    }).filter((g) => g.items.length);
  const kinds = groupPicker('lists', pickerGroups, tab, (id) => renderReconcile(panel, id), 'list-tabs');

  const longList = !!(master && master.reviewed);          // typed-in lists (parties, vendors, companies) can run to thousands of names
  let reviewOnly = longList && [...mrows.values()].some((r) => r.review_status === 'pending');   // open on what needs a decision
  let shownLimit = 100;
  const filterBtn = h('button', { class: 'btn', id: 'rc_filter', type: 'button', style: longList ? '' : 'display:none', onclick: () => { reviewOnly = !reviewOnly; shownLimit = 100; draw(); } });
  const moreBtn = h('button', { class: 'btn', id: 'rc_more', type: 'button', style: 'display:none;margin-top:8px', onclick: () => { shownLimit += 100; draw(); } });
  const approveSel = h('button', { class: 'btn', id: 'rc_approveSel', type: 'button', text: 'Approve selected', style: longList ? '' : 'display:none', disabled: true, onclick: async () => {
    try { for (const k of [...picked]) { if (isNew(byKey.get(k) || { key: k })) await api(`/admin/masters/${master.id}/${k}/approve`, { method: 'POST' }); } reload(); }
    catch (ex) { alert(ex.message); } } });
  const search = h('input', { type: 'search', id: 'rc_search', placeholder: 'Search ' + tabLabel.toLowerCase(), 'aria-label': 'Search values', style: 'max-width:260px' });
  const mergeBtn = h('button', { class: 'btn', id: 'rc_mergeBtn', text: 'Merge selected…', disabled: true, onclick: () => openMerge([...picked]) });
  const addBtn = h('button', { class: 'btn primary', id: 'rc_addBtn', text: '+ Add ' + tabLabel.toLowerCase(), onclick: () => openAdd() });
  const tbody = h('tbody');
  const orderNote = h('p', { class: 'note', id: 'rc_orderNote', style: 'margin:0 0 8px' });
  const dupBox = h('div');

  // the flags of a master list (checkbox / choice / number), as form fields
  const masterFields = (row) => (mdef ? mdef.fields : []).map((f) => ({ name: f.name, label: f.label, type: f.type === 'decimal' ? 'number' : f.type, step: f.type === 'decimal' ? 'any' : undefined, maxlength: f.max && f.type === 'text' ? f.max : undefined,
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
        role === 'delivery' ? SCOPE_FIELD : null, screenOpts ? screenField(null) : null, ...(extraFields ? extraInputs(null) : [])].filter(Boolean),
      onSubmit: async (v) => {
        if (role) await api('/admin/people?role=' + role, { method: 'POST', body: { full_name: v.name, phone_number: v.phone || null, dispatch_scope: v.dispatch_scope || 'both' } });
        else await api('/admin/lookups/' + slug, { method: 'POST', body: { name: v.name, ...(screenOpts ? { screens: screensBody(v, null) } : {}), ...(extraFields ? { fields: extraBody(v) } : {}) } });
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
    if (v.protected && screenOpts) {      // a built-in value: its name and routing are fixed, only the screens that offer it can change
      return formDialog({ title: '“' + (v.label || v.name) + '” - offered on', fields: [screenField(screensOf.get(v.key))],
        onSubmit: async (vals) => { await api(`/admin/lookups/${slug}/${v.key}`, { method: 'PUT', body: { name: v.name, screens: screensBody(vals, screensOf.get(v.key)) } }); reload(); } });
    }
    if (screenOpts || extraFields) {
      return formDialog({ title: 'Edit “' + v.name + '”', fields: [{ name: 'name', label: 'Name', value: v.name, required: true, maxlength: 100,
        hint: `Changes it on all ${fmtInt(v.orders)} order(s) that use it. If this spelling already exists, use Merge instead.` },
        ...(screenOpts ? [screenField(screensOf.get(v.key))] : []), ...(extraFields ? extraInputs(v.key) : [])],
      onSubmit: async (vals) => {
        await api(`/admin/lookups/${slug}/${v.key}`, { method: 'PUT', body: { name: vals.name,
          ...(screenOpts ? { screens: screensBody(vals, screensOf.get(v.key)) } : {}), ...(extraFields ? { fields: extraBody(vals) } : {}) } });
        reload();
      } });
    }
    formDialog({ title: 'Rename “' + v.name + '”', submitLabel: 'Rename',
      fields: [{ name: 'name', label: 'Correct spelling', value: v.name, required: true, maxlength: 100,
        hint: `Changes it on all ${fmtInt(v.orders)} order(s) that use it. If this spelling already exists, use Merge instead.` }],
      onSubmit: async (vals) => { await api(`/admin/reconcile/${kind}/rename`, { method: 'POST', body: { key: v.key, name: vals.name } }); reload(); } });
  }

  async function remove(v) {
    const emp = master && master.id === 'att_employee';   // an attendance employee is deleted with their attendance and advance records
    if (!(await confirmDialog('Delete "' + v.name + '"?',
      emp ? 'This permanently deletes the employee together with ALL their attendance days and advances. It cannot be undone. (If they are still a portal member they come back as a new employee with no history; disable the member first to stop that.)'
        : v.orders ? `${fmtInt(v.orders)} order(s) use it, so this will be refused. Merge it into another value instead.` : 'It is not used by any order.', 'Delete', true))) return;
    try { await api(master ? `/admin/masters/${master.id}/${v.key}${emp ? '?with_records=true' : ''}` : role ? '/admin/people/' + v.key : `/admin/lookups/${slug}/${v.key}`, { method: 'DELETE' }); reload(); }
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
    const matching = values.filter((v) => (!q || v.name.toLowerCase().includes(q) || (v.label || '').toLowerCase().includes(q)) && (!reviewOnly || q || isNew(v)));
    const shown = longList ? matching.slice(0, shownLimit) : matching;
    // a typed-in list is alphabetical (no dragging); the other lists keep their hand-set order
    const canMove = !q && values.length > 1 && !longList && !reviewOnly;
    if (longList) {
      filterBtn.textContent = reviewOnly ? `Showing: needs review (${newCount}) - show all ${values.length}` : (newCount ? `Showing: all ${values.length} - show only the ${newCount} to review` : `All ${values.length}`);
      filterBtn.disabled = !newCount && !reviewOnly;
      moreBtn.style.display = matching.length > shown.length ? '' : 'none';
      moreBtn.textContent = `Show 100 more (${matching.length - shown.length} not shown - or type in the search box)`;
    }
    orderNote.textContent = longList ? '' : q ? 'Clear the search to change the order.' : (values.length > 1 ? 'Drag a row (or use the arrows) to set the order these values appear in the dropdowns.' : '');
    const rows = shown.map((v) => {
      const box = h('input', { type: 'checkbox', 'aria-label': 'Select ' + v.name, checked: picked.has(v.key) });
      box.addEventListener('change', () => { if (box.checked) picked.add(v.key); else picked.delete(v.key); mergeBtn.disabled = picked.size < 2; approveSel.disabled = !picked.size; });
      const idx = values.findIndex((x) => x.key === v.key);
      const tr = h('tr', { 'data-key': v.key, draggable: canMove ? 'true' : null });
      if (canMove) {
        tr.addEventListener('dragstart', (e) => { dragKey = v.key; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(v.key)); tr.classList.add('dragging'); });
        tr.addEventListener('dragend', () => { dragKey = null; tr.classList.remove('dragging'); });
        tr.addEventListener('dragover', (e) => { if (dragKey == null) return; e.preventDefault(); tr.classList.add('drop-over'); });
        tr.addEventListener('dragleave', () => tr.classList.remove('drop-over'));
        tr.addEventListener('drop', (e) => { e.preventDefault(); tr.classList.remove('drop-over'); if (dragKey != null && dragKey !== v.key) moveTo(dragKey, idx); });
      }
      tr.append(...[
        h('td', {}, box),
        h('td', { class: 'drag-cell' }, canMove ? h('span', { class: 'drag-handle', title: 'Drag to change the order', 'aria-hidden': 'true', text: '⠿' }) : null,
          canMove ? h('button', { class: 'btn small icon', 'aria-label': 'Move ' + v.name + ' up', text: '▲', disabled: idx === 0 ? true : null, onclick: () => moveTo(v.key, idx - 1) }) : null,
          canMove ? h('button', { class: 'btn small icon', 'aria-label': 'Move ' + v.name + ' down', text: '▼', disabled: idx === values.length - 1 ? true : null, onclick: () => moveTo(v.key, idx + 1) }) : null),
        h('td', {}, v.label || v.name, v.protected ? h('span', { class: 'pill warn', style: 'margin-left:8px', text: 'Built-in' }) : null,
          isNew(v) ? h('span', { class: 'pill warn', style: 'margin-left:8px', title: 'Typed on a module screen' + (mrows.get(v.key).created_by ? ' by ' + mrows.get(v.key).created_by : ''), text: 'New - review' }) : null),
        role ? h('td', { text: phones.get(v.key) || '' }) : null,
        ...(master ? mdef.fields.map((f) => h('td', { text: masterCell(f, mrows.get(v.key)) })) : []),
        role === 'delivery' ? h('td', { text: SCOPE_LABEL[scopes.get(v.key) || 'both'] }) : null,
        screenOpts ? h('td', { text: screenLabel(v) }) : null,
        ...(extraFields ? extraFields.map((f) => h('td', { text: extraLabel(v.key, f) })) : []),
        h('td', { class: 'num', text: fmtInt(v.orders) }),
        h('td', {}, v.protected ? h('div', { class: 'row' }, h('span', { class: 'note', text: 'Used by order rules' }),
          screenOpts ? h('button', { class: 'btn small', text: 'Offered on', onclick: () => openEdit(v) }) : null) : h('div', { class: 'row' },
          isNew(v) ? h('button', { class: 'btn small primary', text: 'Approve', onclick: async () => { try { await api(`/admin/masters/${master.id}/${v.key}/approve`, { method: 'POST' }); reload(); } catch (ex) { alert(ex.message); } } }) : null,
          h('button', { class: 'btn small', text: role || master ? 'Edit' : 'Rename', onclick: () => openEdit(v) }),
          h('button', { class: 'btn small danger', text: 'Delete', onclick: () => remove(v) })))].filter(Boolean));
      return tr;
    });
    tbody.replaceChildren(...(rows.length ? rows : [h('tr', {}, h('td', { colspan: 7, class: 'empty', text: reviewOnly && !q ? 'Nothing left to review.' : values.length ? 'No values match.' : 'Nothing yet - add the first one.' }))]));
  };
  search.addEventListener('input', draw);

  let dupLimit = longList ? 10 : dups.length;
  const drawDups = () => dupBox.replaceChildren(...(dups.length ? [h('details', { class: 'card', style: 'margin-bottom:16px', open: longList ? null : '' },
    h('summary', { style: 'cursor:pointer;font-weight:700', text: `Possible duplicates (${dups.length})` + (longList ? ' - click to open' : '') }),
    h('p', { class: 'note', text: 'These look like the same thing spelled differently. Review each pair, and merge it if it really is the same.' }),
    h('div', { class: 'table-wrap' }, h('table', { id: 'rc_dups' }, h('tbody', {}, dups.slice(0, dupLimit).map((sg) => h('tr', {},
      h('td', { text: sg.names[0] }), h('td', { text: '≈' }), h('td', { text: sg.names[1] }),
      h('td', {}, h('button', { class: 'btn small', text: 'Review & merge', onclick: () => openMerge(sg.keys) }))))))),
    dups.length > dupLimit ? h('button', { class: 'btn small', type: 'button', style: 'margin-top:8px', text: `Show all ${dups.length}`, onclick: () => { dupLimit = dups.length; drawDups(); } }) : null)] : []));
  drawDups();

  panel.replaceChildren(
    h('p', { class: 'note', style: 'margin-bottom:12px', text: 'The values behind every dropdown and dashboard filter. Add, rename, delete or merge look-alike spellings here: each change is saved to the database at once, shows on all order screens and dashboards, and is kept in the audit trail. A value that orders still use cannot be deleted - merge it into the right one instead.' }),
    kinds, reviewBox, dupBox,
    h('div', { class: 'row', style: 'margin-bottom:10px' }, search, filterBtn, h('span', { class: 'grow' }), approveSel, mergeBtn, addBtn), orderNote,
    h('div', { class: 'table-wrap' }, h('table', { id: 'rc_table' },
      h('thead', {}, h('tr', {}, h('th', { text: '' }), h('th', { text: 'Order' }), h('th', { text: 'Value' }), role ? h('th', { text: 'Phone' }) : null, ...(master ? mdef.fields.map((f) => h('th', { text: f.label })) : []), role === 'delivery' || screenOpts ? h('th', { text: 'Offered on' }) : null, ...(extraFields ? extraFields.map((f) => h('th', { text: f.label })) : []), h('th', { class: 'num', text: master ? 'Used by' : 'Orders using it' }), h('th', { text: '' }))), tbody)),
    moreBtn);
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
    h('p', { class: 'note', style: 'margin-bottom:10px', text: group === 'Delegation' ? 'Changes apply to new activity straight away. Scores already given are not recalculated.' : 'Changes apply straight away.' }),
    ...items.map((s) => {
      const isNum = s.type === 'int' || s.type === 'number';
      const input = s.type === 'textarea'
        ? h('textarea', { rows: '4', maxlength: String(s.max || 600), id: 'set_' + s.key, 'aria-label': s.label, style: 'min-width:260px;flex:1;font-family:inherit' })
        : h('input', { type: isNum ? 'number' : s.type === 'time' ? 'time' : 'text', value: String(s.value),
          ...(isNum ? { min: String(s.min), max: String(s.max), ...(s.type === 'number' ? { step: 'any' } : {}) } : { maxlength: String(s.max || 300) }),
          id: 'set_' + s.key, 'aria-label': s.label, style: isNum || s.type === 'time' ? 'max-width:140px' : 'min-width:260px;flex:1' });
      if (s.type === 'textarea') input.value = String(s.value);
      const msg = h('span', { class: 'msg' });
      const save = h('button', { class: 'btn small', text: 'Save', onclick: async () => {
        msg.className = 'msg'; msg.textContent = '';
        try { await api('/admin/module-settings', { method: 'PUT', body: { key: s.key, value: isNum ? Number(input.value) : input.value } }); msg.textContent = 'Saved'; }
        catch (ex) { msg.className = 'msg error'; msg.textContent = ex.message; }
      } });
      return h('div', { class: 'row', style: 'margin:8px 0;align-items:center;gap:10px;flex-wrap:wrap' }, h('label', { style: 'flex:1;min-width:220px', text: s.label }), input, save, msg);
    })));
  cards.push(h('div', { class: 'card', style: 'margin-bottom:16px' },
    h('h2', { style: 'font-size:15px;margin-bottom:6px', text: 'Challan / invoice check (PERFECT)' }),
    h('p', { class: 'note', text: 'PDFs attached to an order are read automatically. To also read PHOTOS of challans, the server administrator must set the environment variables DOC_CHECK_VISION=1 and ANTHROPIC_API_KEY on the web service (this sends the photo to the Anthropic API to read the number, date and total). Until then photos are not read and are never marked PERFECT.' })));
  panel.replaceChildren(...cards);
}


// ------------------------------------------------------------------ Setup > WhatsApp
// One card per kind of alert: the group it goes to, an on/off switch and the message template ({placeholders} are filled in per order).
// The provider login stays in the server environment; only the groups and wording are kept here.
async function renderWhatsApp(panel) {
  panel.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
  let data;
  try { data = await api('/admin/whatsapp'); } catch (ex) { return panel.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
  const fill = (a, t) => t.replace(/\{(\w+)\}/g, (m, k) => (k in a.sample ? String(a.sample[k]) : m));
  data.alerts = data.alerts.filter((a) => !a.hidden);   // Delegation / Checklist alerts are not managed here (their web bell still works)
  const cards = data.alerts.map((a) => {
    const group = h('input', { type: 'text', id: 'wa_group_' + a.key, value: a.group_id, maxlength: '100', placeholder: 'e.g. 1234567890@g.us', 'aria-label': 'WhatsApp group ID', style: 'max-width:340px' });
    const on = h('input', { type: 'checkbox', id: 'wa_on_' + a.key, checked: a.enabled });
    const text = h('textarea', { id: 'wa_template_' + a.key, rows: '10', maxlength: '2000', 'aria-label': 'Message template', style: 'width:100%;font-family:inherit' });
    text.value = a.template;
    const preview = h('pre', { id: 'wa_preview_' + a.key, style: 'white-space:pre-wrap;word-break:break-word;background:#e7fbd8;border:1px solid #c8e8b4;border-radius:10px;padding:10px 12px;margin:0;font-family:inherit;font-size:13px;max-width:520px' });
    const draw = () => { preview.textContent = fill(a, text.value); };
    text.addEventListener('input', draw); draw();
    const msg = h('span', { class: 'msg', role: 'status' });
    const say = (t, bad) => { msg.className = 'msg' + (bad ? ' error' : ''); msg.textContent = t; };
    const chips = Object.entries(a.placeholders).map(([name, help]) => h('button', { type: 'button', class: 'btn small', title: help, text: '{' + name + '}',
      onclick: () => { const i = text.selectionStart ?? text.value.length; text.setRangeText('{' + name + '}', i, text.selectionEnd ?? i, 'end'); text.focus(); draw(); } }));
    const save = async () => {
      say('Saving…');
      try { await api('/admin/whatsapp/' + a.key, { method: 'PUT', body: { group_id: group.value, enabled: on.checked, template: text.value } }); say('Saved.'); }
      catch (ex) { say(ex.message, true); }
    };
    // posts the message below, filled with the sample details, to the group typed above - nothing has to be saved first
    const test = async () => {
      say('Sending the sample message…');
      try { const r = await api('/admin/whatsapp/' + a.key + '/test', { method: 'POST', body: { group_id: group.value, template: text.value } }); say(r.message, !r.success); }
      catch (ex) { say(ex.message, true); }
    };
    const where = a.group_from_environment ? 'Empty here: the group from the server settings (WHATSAPP_GROUP_ID) is being used. Fill this in to take over.'
      : a.group_source === 'fallback' ? 'Empty here: this alert uses the group of “' + a.group_from_label + '”. Fill this in to give it a group of its own.'
      : a.group_from ? 'Empty = the group of “' + a.group_from_label + '”.' : '';
    // one heading per alert: click it to open its group, switch and wording (a switched-off alert, or one with no group yet, says so on its heading)
    return collapsible('wa_card_' + a.key, a.label, a.when, h('div', { class: 'collapse-inner' },
      h('div', { class: 'field' }, h('label', { text: 'WhatsApp group ID' + (a.group_label ? ' — ' + a.group_label : '') }), group, where ? h('div', { class: 'hint', text: where }) : null),
      h('div', { class: 'field' }, h('label', {}, on, ' Send this alert' + (a.web_bell ? ' (to the group; it also shows on the web bell of the person and every admin)' : ''))),
      h('div', { class: 'field' }, h('label', { text: 'Message' }), text,
        h('div', { class: 'row', style: 'margin-top:6px;flex-wrap:wrap;gap:6px;align-items:center' }, h('span', { class: 'note', text: 'Insert:' }), ...chips),
        h('div', { class: 'hint', text: 'Placeholders in { } are replaced with the real details when the message is sent.' })),
      h('div', { class: 'field' }, h('label', { text: 'How it looks in WhatsApp (sample details)' }), preview),
      h('div', { class: 'row', style: 'gap:10px;align-items:center;flex-wrap:wrap' },
        h('button', { class: 'btn primary', text: 'Save', onclick: save }),
        h('button', { class: 'btn', text: 'Send a sample message to the group', onclick: test }),
        h('button', { class: 'btn', text: 'Restore the standard wording', onclick: () => { text.value = a.default_template; draw(); } }), msg)),
      { badge: !a.enabled ? 'Off' : (a.group_effective ? null : 'No group yet') });
  });
  const missing = data.alerts.filter((a) => a.enabled && !a.group_effective);
  panel.replaceChildren(
    h('p', { class: 'note', style: 'margin-bottom:12px', text: 'Every alert goes to a WhatsApp group. Type the group ID on its card, press “Send a sample message to the group” to see the message arrive there (the group ID and the wording are used as typed - no need to save first), then Save. Click an alert to open it.' }),
    data.credentials_set ? null : h('div', { class: 'msg error', role: 'alert', style: 'margin-bottom:12px', text: 'The WhatsApp API key (WHATSAPP_API_KEY) is not set on the server, so nothing can be sent yet.' }),
    h('div', { class: missing.length ? 'msg error' : 'note', role: 'status', id: 'wa_summary', style: 'margin-bottom:12px',
      text: missing.length ? missing.length + ' of ' + data.alerts.length + ' alerts have no group yet and send nothing: ' + missing.map((a) => a.label).join(', ') + '.'
        : 'All ' + data.alerts.length + ' alerts have a group.' }),
    ...cards);
}


// ------------------------------------------------------------------ Setup > Export & Delete
// Export: every dashboard's data, one Excel file each (zipped). Delete: everything except completed orders - always previewed first,
// and only done after the confirmation word is typed.
async function renderDataTools(panel) {
  const exportMsg = h('span', { class: 'msg', role: 'status' });
  const exportBtn = h('button', { class: 'btn primary', id: 'exportAllBtn', text: '⬇ Export all dashboards (Excel, zipped)', onclick: async () => {
    exportBtn.disabled = true; exportMsg.className = 'msg'; exportMsg.textContent = 'Preparing the files…';
    try { await downloadFile('/admin/data/export.zip', 'vardhman_export.zip'); exportMsg.className = 'msg ok'; exportMsg.textContent = 'Downloaded.'; }
    catch (ex) { exportMsg.className = 'msg error'; exportMsg.textContent = ex.message; }
    exportBtn.disabled = false;
  } });

  const preview = h('div', { id: 'deletePreview' });
  const previewBtn = h('button', { class: 'btn danger', id: 'deletePreviewBtn', text: 'Delete completed orders and Payments entries…', onclick: async () => {
    previewBtn.disabled = true; preview.replaceChildren(h('p', { class: 'note', text: 'Counting…' }));
    try {
      const d = await api('/admin/data/delete-preview');
      const word = h('input', { type: 'text', id: 'deleteWord', autocomplete: 'off', placeholder: `Type ${d.confirm_word}`, 'aria-label': 'Type the confirmation word', style: 'max-width:200px' });
      const go = h('button', { class: 'btn danger', id: 'deleteGoBtn', text: 'Delete now', disabled: true });
      const msg = h('div', { class: 'msg', role: 'status' });
      word.addEventListener('input', () => { go.disabled = word.value.trim() !== d.confirm_word; });
      go.addEventListener('click', async () => {
        go.disabled = true; msg.className = 'msg'; msg.textContent = 'Deleting…';
        try {
          const r = await api('/admin/data/delete', { method: 'POST', body: { confirm: word.value } });
          msg.className = 'msg ok';
          msg.textContent = `Done. ${fmtInt(r.deleted.reduce((n, a) => n + a.count, 0))} records deleted; ${fmtInt(r.kept_orders)} orders that are not completed were kept.`;
          word.disabled = true; previewBtn.disabled = false;
        } catch (ex) { msg.className = 'msg error'; msg.textContent = ex.message; go.disabled = false; }
      });
      preview.replaceChildren(h('div', { class: 'card', style: 'border-left:4px solid var(--bad);margin-top:12px' },
        h('h3', { text: 'This will permanently delete:' }),
        h('div', { class: 'table-wrap' }, h('table', { id: 'deleteCounts' }, h('tbody', {}, d.areas.map((a) => h('tr', {}, h('td', { text: a.label }), h('td', { class: 'num', text: fmtInt(a.count) })))))),
        h('p', { class: 'note', text: `${fmtInt(d.files)} uploaded photo/file record(s) are removed as well.` }),
        h('p', { class: 'msg ok', text: `${fmtInt(d.kept_orders)} order(s) that are not completed are kept. ${d.kept_note}` }),
        h('p', { class: 'msg error', role: 'alert', text: 'This cannot be undone. Export first if you may need this data.' }),
        h('div', { class: 'row', style: 'gap:10px;align-items:center;flex-wrap:wrap' }, word, go), msg));
    } catch (ex) { preview.replaceChildren(h('div', { class: 'msg error', text: ex.message })); }
    previewBtn.disabled = false;
  } });

  panel.replaceChildren(
    h('div', { class: 'card', style: 'margin-bottom:16px' },
      h('h2', { style: 'font-size:15px;margin-bottom:6px', text: 'Export' }),
      h('p', { class: 'note', style: 'margin-bottom:10px', text: 'One Excel file for each dashboard (O2D orders, Payments with expenses, Delegation with checklist, Purchase, VT Decor, Attendance), all in one zip.' }),
      h('div', { class: 'row', style: 'gap:10px;align-items:center' }, exportBtn, exportMsg)),
    h('div', { class: 'card' },
      h('h2', { style: 'font-size:15px;margin-bottom:6px', text: 'Delete' }),
      h('p', { class: 'note', style: 'margin-bottom:10px', text: 'Permanently removes every completed order (closed or cancelled, archived ones too) and every Payments entry from the database. Orders that are not completed are kept, and so is everything else (Delegation, Purchase, VT Decor, Attendance, members, dropdown lists, settings). You see exactly what will go before anything is deleted.' }),
      previewBtn, preview));
}
