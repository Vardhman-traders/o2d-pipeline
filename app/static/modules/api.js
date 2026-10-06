/* The Payments, Delegation and Purchase screens' client for this app's own API.
 *
 *   VT.run.withSuccessHandler(ok).withFailureHandler(err).getTasksForUser(...)
 *
 * Who the person is, and what they may do, always comes from the sign-in token (the portal is the only place anyone
 * signs in); the role / username arguments the pages pass along are ignored. Each named function below maps to one
 * route; a refused request reaches the page's failure handler with the server's own sentence.
 */
(function () {
  'use strict';

  function token() { try { return sessionStorage.getItem('vt_token'); } catch (e) { return null; } }

  function toPortal() {
    try { sessionStorage.removeItem('vt_token'); } catch (e) { /* storage blocked: nothing to clear */ }
    window.location.href = '/';
  }

  function ask(method, path, body) {
    var opts = { method: method, headers: { 'Accept': 'application/json', 'Authorization': 'Bearer ' + token() } };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (res.status === 401) { toPortal(); throw new Error('Session expired. Please sign in again.'); }
        if (!res.ok) {
          var d = data && data.detail;
          throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map(function (x) { return x.msg; }).join('; ') : 'Request failed (' + res.status + ').');
        }
        return data;
      });
    });
  }

  function qs(obj) {
    var parts = [];
    Object.keys(obj).forEach(function (k) { if (obj[k] !== undefined && obj[k] !== null && obj[k] !== '') parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(obj[k])); });
    return parts.length ? '?' + parts.join('&') : '';
  }
  var get = function (path) { return function () { return ask('GET', path); }; };

  var fns = {
    // ---------------------------------------------------------------- Payments
    login_payments: function () {   // the page starts from the portal session; this just tells it who that is
      return ask('GET', '/payments/bootstrap').then(function (b) { return { success: true, username: b.username, role: b.role, fullName: b.fullName }; });
    },
    getBootstrapData: function () { return ask('GET', '/payments/bootstrap'); },
    getParties: function (company, txnType) { return ask('GET', '/payments/parties' + qs({ company: company, txn_type: txnType })); },
    getTransactions: function (role, username, company, from, to) { return ask('GET', '/payments/transactions' + qs({ company: company, date_from: from, date_to: to })); },
    getDashboardData: function (role, username, company, from, to) { return ask('GET', '/payments/dashboard' + qs({ company: company, date_from: from, date_to: to })); },
    saveTransaction: function (payload) { return ask('POST', '/payments/transactions', payload); },
    updateTransaction: function (txnId, payload) { return ask('PUT', '/payments/transactions/' + encodeURIComponent(txnId), payload); },
    approveTransaction: function (txnId, decision) { return ask('POST', '/payments/transactions/' + encodeURIComponent(txnId) + '/decision', { decision: decision }); },
    addCompany: function (name) { return ask('POST', '/payments/companies', { name: name }); },
    addParty: function (name, company, role, txnType) { return ask('POST', '/payments/parties', { name: name, company: company, txnType: txnType }); },

    // ---------------------------------------------------------------- Delegation
    checkLogin: function () { return ask('GET', '/delegation/me'); },
    getTasksForUser: function (role, name, id, due) { return ask('GET', '/delegation/tasks' + qs({ due: due })); },
    getMyScore: get('/delegation/my-score'),
    getAssignableStaff: get('/delegation/staff'),
    assignTask: function (staffId, staffName, desc, dueDate) { return ask('POST', '/delegation/tasks', { staffId: staffId, staffName: staffName, desc: desc, dueDate: dueDate }); },
    updateTaskStatus: function (taskId, notes, photo) { return ask('POST', '/delegation/tasks/' + encodeURIComponent(taskId) + '/complete', { notes: notes, photo: photo }); },
    managerReview: function (taskId, status, remark) { return ask('POST', '/delegation/tasks/' + encodeURIComponent(taskId) + '/review', { status: status, remark: remark }); },
    extendTaskDeadline: function (taskId, newDueDate, remark) { return ask('POST', '/delegation/tasks/' + encodeURIComponent(taskId) + '/extend', { newDueDate: newDueDate, remark: remark }); },
    getScoreboard: get('/delegation/scoreboard'),
    getWeeklyReview: function (staffId, anchor) { return ask('GET', '/delegation/weekly' + qs({ staff_id: staffId, anchor: anchor })); },
    saveWeeklyPlan: function (staffId, staffName, weekStart, weekEnd, g, y, r) { return ask('POST', '/delegation/weekly-plan', { staffId: staffId, weekStart: weekStart, green: g, yellow: y, red: r }); },
    logExtraWork: function (userId, userName, role, desc, photo) { return ask('POST', '/delegation/extra-work', { desc: desc, photo: photo }); },

    // ---------------------------------------------------------------- Purchase
    login_purchase: function () { return ask('GET', '/purchase/me'); },
    getVendors: get('/purchase/vendors'),
    getAllEntries: function (type) { return ask('GET', '/purchase/entries' + qs({ site: type })); },
    saveEntry: function (type, data) { return ask('POST', '/purchase/entries/' + type, data); },
    updateEntry: function (type, rowId, data) { return ask('PUT', '/purchase/entries/' + type + '/' + rowId, data); },
    deleteEntry: function (type, rowId) { return ask('DELETE', '/purchase/entries/' + type + '/' + rowId); },
    uploadPhoto: function (type, base64, mime) { return ask('POST', '/purchase/photos/' + type, { data: 'data:' + (mime || 'image/jpeg') + ';base64,' + base64 }); }
  };
  // Payments and Purchase both start with login(); which one it is depends on the page.
  fns.login = function () { return window.VT_MODULE === 'purchase' ? fns.login_purchase() : fns.login_payments(); };

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
                function (err) { if (onFail) onFail(err instanceof Error ? err : new Error(String(err))); else console.error(err); });
      };
    });
    return runner;
  }

  window.VT = { run: makeRunner(null, null), toPortal: toPortal, token: token };

  /* ---- small helpers the pages share ---- */
  // Shrinks a photo on the phone before it is sent (full-size camera photos are several MB). Resolves a data: URL.
  window.VT.shrinkImage = function (file, maxSide) {
    maxSide = maxSide || 1600;
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onerror = function () { reject(new Error('Could not read the selected photo.')); };
      reader.onload = function (e) {
        var raw = e.target.result;
        if (!/^image\/(jpeg|png|webp)$/.test(file.type)) { resolve(raw); return; }
        var img = new Image();
        img.onload = function () {
          var k = Math.min(1, maxSide / Math.max(img.width, img.height));
          var c = document.createElement('canvas'); c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
          c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
          var out = c.toDataURL('image/jpeg', 0.8);
          resolve(out.length < raw.length ? out : raw);
        };
        img.onerror = function () { resolve(raw); };
        img.src = raw;
      };
      reader.readAsDataURL(file);
    });
  };

  // Signs the page in with the portal session: checks the person may open this module, then runs `enter` (which is the page's
  // own "login succeeded" path), or sends them back to the portal.
  window.VT.boot = function (moduleKey, enter) {
    if (!token()) { toPortal(); return; }
    fetch('/auth/me', { headers: { Authorization: 'Bearer ' + token() } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (me) {
        if (!me || me.must_change_password) { toPortal(); return; }
        var ok = (me.modules || []).some(function (m) { return m.key === moduleKey; });
        if (!ok) { document.body.innerHTML = '<div style="font-family:system-ui;padding:32px;max-width:520px;margin:40px auto;text-align:center"><h2>Access not provided for this page</h2><p>Use “Request access” on the portal home screen to ask an admin.</p><p><a href="/">Back to portal</a></p></div>'; return; }
        window.VT.me = me;
        enter(me);
      })
      .catch(function () { toPortal(); });
  };

  // A "Home" button next to the log-out control on every module page (sign-in lives in the portal, so Home is the way back to it).
  // Styled like the log-out control beside it so it matches each page's own design; a floating link covers any page without one.
  document.addEventListener('DOMContentLoaded', function () {
    var out = document.querySelector('[onclick*="handleLogout"], [onclick*="logout()"], #logoutBtn');
    var a;
    if (out) {
      a = document.createElement('button');
      a.type = 'button';
      a.className = out.className;
      a.title = 'Back to the portal home';
      a.setAttribute('aria-label', 'Back to the portal home');
      a.innerHTML = out.id === 'logoutBtn' || out.querySelector('i') ? '&#8962;' : '&#8962; Home';
      a.style.cssText = out.style.cssText;
      if (out.querySelector('i')) a.className = 'p-2 text-slate-500 hover:text-indigo-700 text-lg';
      a.addEventListener('click', function () { window.location.href = '/'; });
      out.parentNode.insertBefore(a, out);
    } else {
      a = document.createElement('a');
      a.href = '/'; a.textContent = '⌂ Portal'; a.className = 'vt-portal-link'; a.setAttribute('aria-label', 'Back to the portal');
      document.body.appendChild(a);
    }
  });
})();
