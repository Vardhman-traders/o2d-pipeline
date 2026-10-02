/* Drop-in replacement for Apps Script's google.script.run, so the O2D screens (index.html) run unchanged
 * against this app's own API. No Apps Script and no Google Sheets are involved anywhere.
 *
 *   google.script.run.withSuccessHandler(ok).withFailureHandler(err).getShopData(token)
 *
 * keeps working; each named function below maps to one HTTP route (see app/o2d_screens.py) and returns the same
 * {ok, success, message, ...} shapes the screens already expect.
 */
(function () {
  'use strict';

  function ask(method, path, token, body) {
    var opts = { method: method, headers: { 'Accept': 'application/json' } };
    if (token) opts.headers['Authorization'] = 'Bearer ' + token;
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        return { ok: res.ok, status: res.status, data: data };
      });
    });
  }

  function detail(r) {
    var d = r.data && r.data.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map(function (x) { return x.msg || JSON.stringify(x); }).join('; ');
    if (r.status === 401) return 'Session expired. Please log in again.';
    if (r.status === 403) return 'Your role is not allowed to do this.';
    return 'Request failed (' + r.status + ').';
  }

  // reads: failure -> {ok:false, error}
  function read(path) {
    return function (token) {
      return ask('GET', path, token).then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    };
  }
  // writes: failure -> {ok:true, success:false, message}
  function write(method, pathFn) {
    return function (token) {
      var args = Array.prototype.slice.call(arguments, 1);
      var p = pathFn.apply(null, args);
      return ask(method, p.path, token, p.body).then(function (r) {
        return r.ok ? r.data : { ok: true, success: false, message: detail(r) };
      });
    };
  }

  var fns = {
    getShopData: read('/o2d/shop'),
    getGodownData: read('/o2d/godown'),
    getDispatchData: read('/o2d/dispatch'),          // role comes from the login token, not the argument
    getReceivingData: read('/o2d/receiving'),
    getKanban: function (token, filters) {
      var qs = new URLSearchParams();
      for (var key in (filters || {})) (filters[key] || []).forEach(function (v) { qs.append(key, v); });
      var q = qs.toString();
      return ask('GET', '/o2d/admin/kanban' + (q ? '?' + q : ''), token)
        .then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
    getReminders: function (token, role) {
      return ask('GET', '/o2d/reminders' + (role ? '?role=' + encodeURIComponent(role) : ''), token)
        .then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
    ackReminder: function (token, id) {
      return ask('POST', '/o2d/reminders/' + id + '/ack', token).then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
    sendReminder: function (token, slNo, message) {
      return ask('POST', '/o2d/admin/reminders', token, { sl_no: slNo, message: message || null })
        .then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
    getDocGaps: read('/o2d/doc-gaps'),
    voidDocGap: function (token, body) {
      return ask('POST', '/o2d/doc-gaps/void', token, body).then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
    checkDocNumber: function (token, docType, date, number) {
      return ask('GET', '/o2d/doc-check?doc_type=' + encodeURIComponent(docType) + '&date=' + encodeURIComponent(date) + '&number=' + encodeURIComponent(number), token)
        .then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
    getMissingNumbersForWindow: read('/o2d/missing-numbers'),
    getAdminFormOptions: read('/o2d/admin/form-options'),
    searchOrders: function (token, date, dcNo) {
      var qs = [];
      if (date) qs.push('date=' + encodeURIComponent(date));
      if (dcNo) qs.push('dc_no=' + encodeURIComponent(dcNo));
      return ask('GET', '/o2d/search' + (qs.length ? '?' + qs.join('&') : ''), token)
        .then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
    addOrder: write('POST', function (form) { return { path: '/o2d/orders', body: form }; }),
    updateShopFields: write('PUT', function (sl, form) { return { path: '/o2d/orders/' + sl + '/shop', body: form }; }),
    updateGodownFields: write('PUT', function (sl, form) { return { path: '/o2d/orders/' + sl + '/godown', body: form }; }),
    updateDispatchFields: write('PUT', function (sl, form) { return { path: '/o2d/orders/' + sl + '/dispatch', body: form }; }),
    updateReceivingFields: write('PUT', function (sl, form) { return { path: '/o2d/orders/' + sl + '/receiving', body: form }; }),
    debugInfo: function (token) {
      return Promise.all([ask('GET', '/health'), token ? ask('GET', '/auth/me', token) : Promise.resolve(null)])
        .then(function (rs) {
          return { ok: true, apiHealth: rs[0].data, whoAmI: rs[1] ? rs[1].data : 'not logged in' };
        });
    }
  };

  function makeRunner(onOk, onFail) {
    var runner = {
      withSuccessHandler: function (f) { return makeRunner(f, onFail); },
      withFailureHandler: function (f) { return makeRunner(onOk, f); }
    };
    Object.keys(fns).forEach(function (name) {
      runner[name] = function () {
        var args = arguments;
        Promise.resolve().then(function () { return fns[name].apply(null, args); })
          .then(function (res) { if (onOk) onOk(res); },
                function (err) { if (onFail) onFail(err instanceof Error ? err : new Error(String(err))); });
      };
    });
    return runner;
  }

  window.google = window.google || {};
  window.google.script = { run: makeRunner(null, null) };
})();
