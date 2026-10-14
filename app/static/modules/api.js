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
      return ask('GET', '/payments/bootstrap').then(function (b) { return { success: true, username: b.username, role: b.role, fullName: b.fullName, expenseLevel: b.expenseLevel || null }; });
    },
    getBootstrapData: function () { return ask('GET', '/payments/bootstrap'); },
    getParties: function (company, txnType) { return ask('GET', '/payments/parties' + qs({ company: company, txn_type: txnType })); },
    getTransactions: function (role, username, company, from, to) { return ask('GET', '/payments/transactions' + qs({ company: company, date_from: from, date_to: to })); },
    getDashboardData: function (role, username, company, from, to) { return ask('GET', '/payments/dashboard' + qs({ company: company, date_from: from, date_to: to })); },
    saveTransaction: function (payload) { return ask('POST', '/payments/transactions', payload); },
    updateTransaction: function (txnId, payload) { return ask('PUT', '/payments/transactions/' + encodeURIComponent(txnId), payload); },
    approveTransaction: function (txnId, decision) { return ask('POST', '/payments/transactions/' + encodeURIComponent(txnId) + '/decision', { decision: decision }); },
    getMergeQueue: function (status) { return ask('GET', '/payments/merge' + qs({ status: status })); },
    saveMergeRow: function (key, payload) { return ask('PUT', '/payments/merge/' + key, payload); },
    mergeRow: function (key, payload) { return ask('POST', '/payments/merge/' + key + '/merge', payload); },
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

    // ---------------------------------------------------------------- Checklist (a part of the Delegation screen)
    chkMe: get('/checklist/me'),
    chkMine: get('/checklist/mine'),
    chkMark: function (key, body) { return ask('POST', '/checklist/occurrences/' + key + '/mark', body); },
    chkCalendar: function (year, month, assignee, category) { return ask('GET', '/checklist/calendar' + qs({ year: year, month: month, assignee: assignee, category: category })); },
    chkList: get('/checklist/checklists'),
    chkCreate: function (body) { return ask('POST', '/checklist/checklists', body); },
    chkUpdate: function (key, body) { return ask('PUT', '/checklist/checklists/' + key, body); },
    chkSetActive: function (key, active) { return ask('POST', '/checklist/checklists/' + key + '/active', { active: active }); },
    chkDelete: function (key) { return ask('DELETE', '/checklist/checklists/' + key); },

    // ---------------------------------------------------------------- Regular expenses (a tab of the Payments screen)
    expBootstrap: get('/payments/expenses/bootstrap'),
    expSummary: function (account, from, to) { return ask('GET', '/payments/expenses/summary' + qs({ account: account, date_from: from, date_to: to })); },
    expEntries: function (account, from, to, category, kind) { return ask('GET', '/payments/expenses/entries' + qs({ account: account, date_from: from, date_to: to, category: category, kind: kind })); },
    expAdd: function (body) { return ask('POST', '/payments/expenses/entries', body); },
    expVoid: function (key, reason) { return ask('POST', '/payments/expenses/entries/' + key + '/void', { reason: reason }); },

    // ---------------------------------------------------------------- Upload challan (a tab of the Payments screen)
    chList: function (from, to) { return ask('GET', '/payments/challans' + qs({ date_from: from, date_to: to })); },
    chUpload: function (body) { return ask('POST', '/payments/challans', body); },

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

  // ---------------------------------------------------------------- Attendance
  // The employee page works without a portal sign-in (staff phones); the owner dashboard needs one. Names are the old script's.
  var salaryPass = {};   // the short-lived pass the server gives after a correct PIN, sent back with the salary request
  function attPublic(action, args) { return ask('POST', '/attendance/public/rpc', { action: action, args: args }); }
  function attOwner(action, args) { return ask('POST', '/attendance/owner/rpc', { action: action, args: args }); }
  var isOwnerPage = function () { return window.VT_MODULE === 'attendance_owner'; };
  fns.getEmployees = function () { return isOwnerPage() ? attOwner('getEmployees', []) : attPublic('getEmployees', []); };
  ['getTodayStatus', 'checkLocation', 'saveAttendance', 'saveCheckout', 'notifyAttendance', 'getDashboardUrl'].forEach(function (n) {
    fns[n] = function () { return attPublic(n, Array.prototype.slice.call(arguments)); };
  });
  fns.verifyEmployeePIN = function (name, pin) {
    return attPublic('verifyEmployeePIN', [name, pin]).then(function (r) {
      if (r && r.token) salaryPass[String(name).toLowerCase().trim()] = r.token;
      return r;
    });
  };
  fns.getEmployeeSalaryView = function (name) { return attPublic('getEmployeeSalaryView', [name, salaryPass[String(name).toLowerCase().trim()] || '']); };
  ['verifyOwnerPassword', 'getTodayAttendance', 'manualCheckout', 'getAllSalaries', 'setEmployeeSalary', 'getAllAdvances', 'addAdvancePayment',
   'deleteAdvance', 'getSalaryReport', 'getMonthlyReport', 'getAbsentStreak', 'getEmployeeStreak', 'getAllEmployees', 'addEmployee',
   'deleteEmployee', 'getAllPINs', 'setEmployeePIN', 'getWorkPlaces', 'setEmployeePlaces'].forEach(function (n) {
    fns[n] = function () { return attOwner(n, Array.prototype.slice.call(arguments)); };
  });

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

  // home(): back to the portal home screen WITHOUT signing out (toPortal signs out). ask(): the pages that make their own calls.
  window.VT = { run: makeRunner(null, null), toPortal: toPortal, token: token, ask: ask, home: function () { window.location.href = '/'; } };

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
    if (window.VT_PUBLIC_PAGE) return;   // staff phones open the check-in page without the portal: no portal link there (it has its own Home)
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
