/* Entry point for the O2D screens. There is no sign-in form here - the portal ("/") is the only
 * place anyone signs in. The portal keeps the session in sessionStorage ('vt_token'), and this
 * page is on the same origin, so an already signed-in user lands straight on their screen.
 * Anyone who reaches this page without a valid, ready-to-use session is sent to the portal,
 * which finds (or starts) their sign-in and sends operating roles back here via its O2D Portal link.
 * Runs after the main inline script (loaded at the end of <body>), so its globals and functions exist.
 */
(function () {
  'use strict';

  var origLogout = window.logout;  // clears the auto-refresh timer; the redirect itself happens here instead
  window.logout = function () {
    origLogout();
    try { sessionStorage.removeItem('vt_token'); } catch (e) { /* storage blocked: nothing to clear */ }
    window.location.href = '/';
  };

  function toPortal() {
    try { sessionStorage.removeItem('vt_token'); } catch (e) { /* ignore */ }
    window.location.href = '/';
  }

  var token = null;
  try { token = sessionStorage.getItem('vt_token'); } catch (e) { token = null; }
  if (!token) { toPortal(); return; }

  fetch('/auth/me', { headers: { Authorization: 'Bearer ' + token } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (me) {
      // No session or a forced password change still pending: the portal is the only place that handles
      // those, so hand off to it rather than show a second sign-in here.
      if (!me || me.must_change_password) { toPortal(); return; }
      // The screens this person may open come from the server (one list, kept in app/access.py).
      var screens = me.o2d_views || [];
      var views = screens.map(function (s) { return s.view; });
      // Signed in but not given any O2D screen: say so, and offer the way to ask for access (never a blank page).
      if (!views.length) { showNoAccess(token); return; }
      window.O2D_VIEWS = views;
      window.O2D_SCREENS = screens;
      window.O2D_PAGES = me.pages || [];
      window.O2D_VIEW_ONLY = me.view_only_pages || [];
      window.CURRENT_TOKEN = token;
      window.CURRENT_ROLE = me.role;
      window.CURRENT_USER_NAME = me.display_name;
      document.getElementById('loginScreen').style.display = 'none';
      window.enterDashboard();
    })
    .catch(function () {
      // Offline or the server is unreachable: nothing useful to do on this page either, so the
      // portal (which the user may already have open elsewhere) is still the right place to land.
      toPortal();
    });

  // ------------------------------------------------------------------ no access: message + request form
  function el(tag, props, text) {
    var e = document.createElement(tag);
    if (props) for (var k in props) e.setAttribute(k, props[k]);
    if (text != null) e.textContent = text;
    return e;
  }

  function api(method, path, token, body) {
    var opts = { method: method, headers: { 'Authorization': 'Bearer ' + token } };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) { return { ok: r.ok, data: data }; });
    });
  }

  function showNoAccess(token) {
    document.getElementById('loginScreen').style.display = 'none';
    document.getElementById('appScreen').style.display = 'none';
    var root = el('div', { id: 'noAccessScreen' });
    var bar = el('div', { 'class': 'topbar' });
    var title = el('h1'); title.appendChild(el('span', { 'class': 'badge' }, 'VT')); title.appendChild(document.createTextNode(' O2D Portal'));
    var right = el('div', { style: 'display:flex;gap:14px;align-items:center;' });
    var home = el('button', { 'class': 'btn-home' }, '⌂ Back to Portal'); home.onclick = function () { window.location.href = '/'; };
    var out = el('button', { 'class': 'btn-home' }, 'Logout'); out.onclick = function () { window.logout(); };
    right.appendChild(home); right.appendChild(out); bar.appendChild(title); bar.appendChild(right);

    var wrap = el('div', { 'class': 'wrap', style: 'max-width:640px;' });
    var card = el('div', { 'class': 'card', style: 'margin-top:40px;text-align:center;' });
    card.appendChild(el('div', { style: 'font-size:40px;' }, '🔒'));
    card.appendChild(el('h2', { style: 'margin:6px 0 10px;' }, 'Access not provided'));
    card.appendChild(el('p', null, 'You do not have access to any O2D screen yet.'));
    card.appendChild(el('p', { style: 'color:var(--muted);font-size:13px;' },
      'Ask the administrator for access. They will see your request and the reason you give, and can approve it.'));
    var pick = el('select', { id: 'naPage', style: 'width:100%;margin-top:12px;padding:8px;' });
    var reason = el('input', { id: 'naReason', type: 'text', maxlength: '500', placeholder: 'Why do you need access?', style: 'width:100%;margin-top:8px;padding:8px;' });
    var send = el('button', { id: 'naSend', 'class': 'btn-home', style: 'background:var(--navy);margin-top:12px;' }, 'Request access');
    var msg = el('div', { id: 'naMsg', style: 'margin-top:10px;font-size:13px;min-height:18px;' });
    var list = el('div', { id: 'naList', style: 'margin-top:14px;text-align:left;font-size:13px;' });
    var form = el('div'); form.appendChild(pick); form.appendChild(reason); form.appendChild(send);
    card.appendChild(form); card.appendChild(msg); card.appendChild(list);
    wrap.appendChild(card); root.appendChild(bar); root.appendChild(wrap);
    document.body.appendChild(root);

    function load() {
      api('GET', '/access/pages', token).then(function (r) {
        if (!r.ok) { msg.textContent = r.data.detail || 'Could not load.'; return; }
        var d = r.data, label = {};
        d.all_pages.forEach(function (p) { label[p.key] = p.label; });
        var pending = {};
        d.requests.forEach(function (q) { if (q.status === 'pending') pending[q.page_key] = true; });
        pick.innerHTML = '';
        d.all_pages.filter(function (p) { return p.key.indexOf('o2d_') === 0 && d.pages.indexOf(p.key) === -1 && !pending[p.key]; })
          .forEach(function (p) { pick.appendChild(el('option', { value: p.key }, p.label)); });
        form.style.display = pick.options.length ? 'block' : 'none';
        list.innerHTML = '';
        d.requests.forEach(function (q) {
          list.appendChild(el('div', { style: 'padding:6px 0;border-top:1px solid var(--border);' },
            (label[q.page_key] || q.page_key) + ' · ' + q.status + (q.decision_note ? ' · ' + q.decision_note : '')));
        });
      });
    }
    send.onclick = function () {
      msg.style.color = ''; msg.textContent = '';
      if (!pick.value) return;
      if (reason.value.trim().length < 5) { msg.style.color = '#d8433f'; msg.textContent = 'Please say briefly why you need access.'; return; }
      send.disabled = true;
      api('POST', '/access/request', token, { page_key: pick.value, reason: reason.value.trim() }).then(function (r) {
        send.disabled = false;
        if (r.ok) { msg.style.color = '#1f9d55'; msg.textContent = 'Request sent. The administrator will review it.'; reason.value = ''; load(); }
        else { msg.style.color = '#d8433f'; msg.textContent = (typeof r.data.detail === 'string' ? r.data.detail : 'Could not send the request.'); }
      });
    };
    load();
  }
})();
