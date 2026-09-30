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

  function sessionFrom(r) {
    if (!r.ok) return { ok: true, success: false, message: detail(r) };
    var b = r.data;
    try { sessionStorage.setItem('vt_token', b.access_token); } catch (e) { /* storage blocked: still works this page */ }
    if (['shop', 'godown', 'shop_dispatch', 'godown_dispatch', 'receiving'].indexOf(b.user.role) === -1) {
      window.location.href = '/';   // admin and other roles use the portal, which finds the sign-in in the session
    }
    return { ok: true, success: true, token: b.access_token, mustChangePassword: !!b.must_change_password,
             role: b.user.role, username: b.user.username, displayName: b.user.display_name };
  }

  var fns = {
    login: function (username, password) {
      if (!username || !password) return Promise.resolve({ ok: true, success: false, message: 'Enter username and password.' });
      return ask('POST', '/auth/login', null, { username: username, password: password }).then(sessionFrom);
    },
    changePassword: function (token, current, next) {
      if (!next || next.length < 10) {
        return Promise.resolve({ ok: true, success: false, message: 'New password must be at least 10 characters.' });
      }
      return ask('POST', '/auth/change-password', token, { current_password: current, new_password: next })
        .then(function (r) { return r.ok ? { ok: true, success: true } : { ok: true, success: false, message: detail(r) }; });
    },
    getShopData: read('/o2d/shop'),
    getGodownData: read('/o2d/godown'),
    getDispatchData: read('/o2d/dispatch'),          // role comes from the login token, not the argument
    getReceivingData: read('/o2d/receiving'),
    getMissingNumbersForWindow: read('/o2d/missing-numbers'),
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
