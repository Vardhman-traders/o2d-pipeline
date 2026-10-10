/* The O2D screens' client for this app's own API.
 *
 *   VT.run.withSuccessHandler(ok).withFailureHandler(err).getShopData(token)
 *
 * Each named function below maps to one HTTP route (see app/o2d_screens.py) and returns the {ok, success, message, ...}
 * shapes the screens expect.
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
    // role comes from the login token; only admin's "View as" is passed along (the server ignores it for anyone else)
    getDispatchData: function (token, viewAs) {
      return ask('GET', '/o2d/dispatch' + (viewAs ? '?view_as=' + encodeURIComponent(viewAs) : ''), token)
        .then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
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
      return ask('GET', '/o2d/doc-check?type_name=' + encodeURIComponent(docType) + '&date=' + encodeURIComponent(date) + '&number=' + encodeURIComponent(number), token)
        .then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
    getMissingNumbersForWindow: read('/o2d/missing-numbers'),
    getAdminFormOptions: read('/o2d/admin/form-options'),
    getMissingEntryOptions: read('/o2d/missing-entry/options'),
    addMissingEntry: write('POST', function (form) { return { path: '/o2d/missing-entry', body: form }; }),
    getOrderPhotos: function (token, slNo) {
      return ask('GET', '/o2d/orders/' + slNo + '/photos', token).then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
    // raw image body (already shrunk by the screen); resolves {ok, success, message}
    uploadOrderPhoto: function (token, slNo, kind, blob) {
      return fetch('/o2d/orders/' + slNo + '/photos?kind=' + encodeURIComponent(kind), {
        method: 'POST', headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': blob.type || 'image/jpeg' }, body: blob
      }).then(function (res) {
        return res.json().catch(function () { return null; }).then(function (d) {
          return res.ok ? d : { ok: true, success: false, message: detail({ data: d, status: res.status }) };
        });
      });
    },
    // downloads a PDF report: needs the login header, so fetch it as a blob and hand that to the browser
    getCartageData: function (token, from, to, person) {
      var qs = [];
      if (from) qs.push('date_from=' + encodeURIComponent(from));
      if (to) qs.push('date_to=' + encodeURIComponent(to));
      if (person) qs.push('delivered_by=' + encodeURIComponent(person));
      return ask('GET', '/o2d/cartage' + (qs.length ? '?' + qs.join('&') : ''), token)
        .then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; });
    },
    updateCartage: write('PUT', function (sl, form) { return { path: '/o2d/cartage/' + sl, body: form }; }),
    downloadReport: function (token, kind, from, to, person) {
      var qs = [];
      if (person) qs.push('delivered_by=' + encodeURIComponent(person));
      if (from) qs.push('date_from=' + encodeURIComponent(from));
      if (to) qs.push('date_to=' + encodeURIComponent(to));
      return fetch('/o2d/reports/' + kind + '.pdf' + (qs.length ? '?' + qs.join('&') : ''), { headers: { 'Authorization': 'Bearer ' + token } })
        .then(function (res) {
          if (!res.ok) return res.json().catch(function () { return null; }).then(function (d) { return { ok: false, error: detail({ data: d, status: res.status }) }; });
          var m = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '');
          return res.blob().then(function (b) { return { ok: true, blob: b, filename: m ? m[1] : kind + '.pdf' }; });
        });
    },
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
    listDailyReports: function (token, day) { return ask('GET', '/o2d/daily-reports' + (day ? '?date=' + encodeURIComponent(day) : ''), token).then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; }); },
    runDailyReports: function (token, day) { return ask('POST', '/o2d/daily-reports/run' + (day ? '?date=' + encodeURIComponent(day) : ''), token).then(function (r) { return r.ok ? r.data : { ok: false, error: detail(r) }; }); },
    downloadDailyReport: function (token, key) {
      return fetch('/o2d/daily-reports/' + key + '.pdf', { headers: { 'Authorization': 'Bearer ' + token } }).then(function (res) {
        if (!res.ok) return { ok: false, error: 'Could not download the report (' + res.status + ').' };
        var m = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '');
        return res.blob().then(function (b) { return { ok: true, blob: b, filename: m ? m[1] : 'delivery_report.pdf' }; });
      });
    },
    resendDispatchAlert: write('POST', function (sl) { return { path: '/o2d/orders/' + sl + '/whatsapp-resend' }; }),
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

  window.VT = window.VT || {};
  window.VT.run = makeRunner(null, null);
})();
