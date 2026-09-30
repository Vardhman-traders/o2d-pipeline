/* Sign-in for the O2D screens. The portal keeps the session in sessionStorage ('vt_token'), and this page is on
 * the same origin, so an already signed-in user lands straight on their screen - no second login, no ticket.
 * Signing in here works the same way in reverse, and logging out here signs out of both.
 * Runs after the main inline script (loaded at the end of <body>), so its globals and functions exist.
 */
(function () {
  'use strict';
  var OPERATING = ['shop', 'godown', 'shop_dispatch', 'godown_dispatch', 'receiving'];

  var origLogout = window.logout;
  window.logout = function () {
    try { sessionStorage.removeItem('vt_token'); } catch (e) { /* storage blocked: nothing to clear */ }
    origLogout();
  };

  var token = null;
  try { token = sessionStorage.getItem('vt_token'); } catch (e) { token = null; }
  if (!token) return;

  fetch('/auth/me', { headers: { Authorization: 'Bearer ' + token } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (me) {
      if (!me) { try { sessionStorage.removeItem('vt_token'); } catch (e) { /* ignore */ } return; }
      if (me.must_change_password) return;                     // the portal handles the forced change
      if (OPERATING.indexOf(me.role) === -1) { window.location.href = '/'; return; }   // admin etc. use the portal
      window.CURRENT_TOKEN = token;
      window.CURRENT_ROLE = me.role;
      window.CURRENT_USER_NAME = me.display_name;
      document.getElementById('loginScreen').style.display = 'none';
      window.enterDashboard();
    })
    .catch(function () { /* offline: fall back to the normal login form */ });
})();
