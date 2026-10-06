/* Click a column heading to sort the rows currently on screen (it never re-queries the server, so a sort only
 * covers what is visible). Works for every table on the O2D screens, including ones drawn later, and the sort is
 * re-applied when a table refreshes itself (the screens reload every 20 seconds). */
(function () {
  'use strict';
  var applying = false, timer = null;

  function parseDate(t) {  // dd-mm-yyyy [hh:mm]
    var m = /^(\d{1,2})-(\d{1,2})-(\d{4})(?:\D+(\d{1,2}):(\d{2}))?/.exec(t);
    return m ? Date.UTC(+m[3], +m[2] - 1, +m[1], +(m[4] || 0), +(m[5] || 0)) : null;
  }
  function keyOf(cell) {
    var t = (cell ? cell.textContent : '').trim();
    var d = parseDate(t);
    if (d !== null) return { n: d };
    if (/^-?[\d,]+(\.\d+)?$/.test(t)) return { n: parseFloat(t.replace(/,/g, '')) };
    return { s: t.toLowerCase() };
  }
  function cmp(a, b) {
    if (a.n !== undefined && b.n !== undefined) return a.n - b.n;
    if (a.n !== undefined) return -1;   // numbers/dates before text, blanks last
    if (b.n !== undefined) return 1;
    if (a.s === '' && b.s !== '') return 1;
    if (b.s === '' && a.s !== '') return -1;
    return a.s.localeCompare(b.s, undefined, { numeric: true });
  }
  function sortTable(table) {
    var idx = Number(table.dataset.sortIdx), dir = table.dataset.sortDir === 'desc' ? -1 : 1;
    var body = table.tBodies[0];
    if (!body || isNaN(idx)) return;
    var rows = Array.prototype.slice.call(body.rows).filter(function (r) { return r.cells.length > idx; });
    var keyed = rows.map(function (r, i) { return { r: r, i: i, k: keyOf(r.cells[idx]) }; });
    keyed.sort(function (x, y) { return dir * cmp(x.k, y.k) || x.i - y.i; });
    keyed.forEach(function (x) { body.appendChild(x.r); });
  }
  function mark() {
    var heads = document.querySelectorAll('thead th:not(.sortable)');
    Array.prototype.forEach.call(heads, function (th) {
      if (th.textContent.trim()) th.classList.add('sortable');
    });
    Array.prototype.forEach.call(document.querySelectorAll('table[data-sort-idx]'), sortTable);
  }
  function refresh() {
    if (applying) return;
    applying = true;
    try { mark(); } finally { setTimeout(function () { applying = false; }, 0); }
  }

  document.addEventListener('click', function (e) {
    var th = e.target.closest ? e.target.closest('th.sortable') : null;
    if (!th) return;
    var table = th.closest('table');
    if (!table) return;
    var same = table.dataset.sortIdx === String(th.cellIndex);
    table.dataset.sortDir = same && table.dataset.sortDir === 'asc' ? 'desc' : 'asc';
    table.dataset.sortIdx = String(th.cellIndex);
    Array.prototype.forEach.call(table.querySelectorAll('th.sortable'), function (h) { h.removeAttribute('data-dir'); });
    th.setAttribute('data-dir', table.dataset.sortDir);
    sortTable(table);
  });

  new MutationObserver(function () {
    if (applying) return;
    clearTimeout(timer);
    timer = setTimeout(refresh, 80);
  }).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener('DOMContentLoaded', refresh);
})();
