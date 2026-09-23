/*********************************************************************
 *  VARDHMAN TRADERS - SALES ORDER TRACKER
 *  Thin Apps Script front end for the FastAPI + Postgres backend.
 *
 *  This file no longer reads/writes the Google Sheet. It only:
 *    1. Serves App.html.
 *    2. Proxies every action to the API at API_BASE_URL, attaching the
 *       caller's JWT (kept client-side in a JS variable, passed back on
 *       every call).
 *    3. Reshapes API responses into the same field names the existing
 *       front end (App.html) already expects, so the UI markup barely
 *       has to change.
 *
 *  Set the deployed API's URL once, either here or (preferred, so it
 *  isn't hardcoded in source) as a Script Property named API_BASE_URL:
 *  Project Settings -> Script Properties -> add API_BASE_URL.
 *********************************************************************/

var DEFAULT_API_BASE_URL = 'https://vardhman-o2d.onrender.com';

function apiBase_() {
  var fromProps = PropertiesService.getScriptProperties().getProperty('API_BASE_URL');
  return (fromProps || DEFAULT_API_BASE_URL).replace(/\/$/, '');
}

/** Proves this call comes from the one authorized Apps Script deployment, not a
 *  copy-pasted duplicate: Script Properties are per-project, so copying this file's
 *  text into another Sheet does NOT carry the secret over. Set once via Project
 *  Settings -> Script Properties -> CLIENT_KEY (must match Render's
 *  APPS_SCRIPT_CLIENT_KEY exactly). If unset, calls simply omit the header - the API
 *  then either rejects them (if it requires the key) or allows them (if it doesn't
 *  have one configured yet, e.g. before you've set this up). */
function clientKey_() {
  return PropertiesService.getScriptProperties().getProperty('CLIENT_KEY') || '';
}

/* ============================== ROUTING ============================== */

function doGet(e) {
  var template = HtmlService.createTemplateFromFile('App');
  // Set by the Render portal's "Open <app>" button: a 90-second, one-time ticket,
  // never the real session token, so nothing long-lived sits in the URL/history.
  template.ssoTicket = (e && e.parameter && e.parameter.ssoTicket) || '';
  return template.evaluate()
    .setTitle('Vardhman Traders - Sales Portal')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

/* ============================== LOW-LEVEL API CALL ============================== */

/**
 * method: 'get' | 'post' | 'patch'
 * path: e.g. '/orders' or '/orders/123'
 * token: JWT string or null (for /auth/login)
 * payload: object to send as JSON body, or null
 * Returns { httpOk, status, body } where body is the parsed JSON (or null).
 */
function apiCall_(method, path, token, payload) {
  var options = {
    method: method,
    contentType: 'application/json',
    muteHttpExceptions: true,
    headers: {}
  };
  if (token) options.headers['Authorization'] = 'Bearer ' + token;
  var ck = clientKey_(); if (ck) options.headers['X-Client-Key'] = ck;
  if (payload !== null && payload !== undefined) options.payload = JSON.stringify(payload);

  var response = UrlFetchApp.fetch(apiBase_() + path, options);
  var status = response.getResponseCode();
  var text = response.getContentText();
  var body = null;
  try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }
  return { httpOk: status >= 200 && status < 300, status: status, body: body };
}

/** Extracts a human-readable message from a FastAPI error body. */
function apiErrorMessage_(res) {
  if (res.body && res.body.detail) {
    return typeof res.body.detail === 'string' ? res.body.detail : JSON.stringify(res.body.detail);
  }
  if (res.status === 401) return 'Session expired. Please log in again.';
  if (res.status === 429) return 'Too many attempts. Please wait a few minutes and try again.';
  return 'Request failed (HTTP ' + res.status + ').';
}

/**
 * Same contract as apiCall_, but for several independent GETs/POSTs at once.
 * UrlFetchApp.fetchAll sends them in parallel over one round trip instead of
 * one-at-a-time, which is where most of the "page feels slow" time was going
 * (each dashboard load was making 2-4 sequential calls to a free-tier Render
 * instance). specs: [{method, path, token, payload}]. Returns an array of
 * { httpOk, status, body } in the same order as specs.
 */
function apiCallBatch_(specs) {
  var ck = clientKey_();
  var requests = specs.map(function (s) {
    var options = { method: s.method, contentType: 'application/json', muteHttpExceptions: true, headers: {} };
    if (s.token) options.headers['Authorization'] = 'Bearer ' + s.token;
    if (ck) options.headers['X-Client-Key'] = ck;
    if (s.payload !== null && s.payload !== undefined) options.payload = JSON.stringify(s.payload);
    options.url = apiBase_() + s.path;
    return options;
  });
  var responses = UrlFetchApp.fetchAll(requests);
  return responses.map(function (r) {
    var status = r.getResponseCode();
    var text = r.getContentText();
    var body = null;
    try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }
    return { httpOk: status >= 200 && status < 300, status: status, body: body };
  });
}

/* ============================== LOGIN / SESSION ============================== */

function login(username, password) {
  try {
    if (!username || !password) return { ok: true, success: false, message: 'Enter username and password.' };
    var res = apiCall_('post', '/auth/login', null, { username: username, password: password });
    if (!res.httpOk) return { ok: true, success: false, message: apiErrorMessage_(res) };
    var b = res.body;
    return {
      ok: true, success: true,
      token: b.access_token,
      mustChangePassword: !!b.must_change_password,
      role: b.user.role,
      username: b.user.username,
      displayName: b.user.display_name
    };
  } catch (err) {
    return { ok: false, error: 'login() failed: ' + err.message };
  }
}

/** Exchanges a one-time ticket (from the Render portal's "Open" button) for a session,
 *  without ever handling the user's password here. Same response shape as login(). */
function ssoLogin(ticket) {
  try {
    if (!ticket) return { ok: true, success: false, message: 'No ticket supplied.' };
    var res = apiCall_('post', '/auth/sso-exchange', null, { ticket: ticket });
    if (!res.httpOk) return { ok: true, success: false, message: apiErrorMessage_(res) };
    var b = res.body;
    return {
      ok: true, success: true,
      token: b.access_token,
      mustChangePassword: !!b.must_change_password,
      role: b.user.role,
      username: b.user.username,
      displayName: b.user.display_name
    };
  } catch (err) {
    return { ok: false, error: 'ssoLogin() failed: ' + err.message };
  }
}

function changePassword(token, currentPassword, newPassword) {
  try {
    if (!newPassword || newPassword.length < 10) {
      return { ok: true, success: false, message: 'New password must be at least 10 characters.' };
    }
    var res = apiCall_('post', '/auth/change-password', token,
      { current_password: currentPassword, new_password: newPassword });
    if (!res.httpOk) return { ok: true, success: false, message: apiErrorMessage_(res) };
    return { ok: true, success: true };
  } catch (err) {
    return { ok: false, error: 'changePassword() failed: ' + err.message };
  }
}

/* ============================== LOOKUP / PEOPLE MAPS ============================== */
/* Each dropdown the UI shows is backed by a real DB table. We fetch the
   current {key,name} list per section and cache it briefly, so submit
   handlers can turn the name the user picked back into a key. */

var LOOKUP_CACHE_TTL = 120; // seconds

function lookupCacheKey_(path, token) {
  return 'lk_' + Utilities.base64Encode(path) + '_' + Utilities.base64Encode(token || '').slice(0, 20);
}

function fetchList_(token, path) {
  return fetchListsBatch_(token, [path])[path];
}

/** Fetches several lookup/people lists in one parallel round trip, reusing
 *  the cache for any that are still fresh. Returns { path: list }. */
function fetchListsBatch_(token, paths) {
  var cache = CacheService.getScriptCache();
  var results = {}, toFetch = [];
  paths.forEach(function (p) {
    var cached = cache.get(lookupCacheKey_(p, token));
    if (cached) {
      try { results[p] = JSON.parse(cached); return; } catch (e) {}
    }
    toFetch.push(p);
  });
  if (toFetch.length) {
    var specs = toFetch.map(function (p) { return { method: 'get', path: p, token: token, payload: null }; });
    var responses = apiCallBatch_(specs);
    toFetch.forEach(function (p, i) {
      var res = responses[i];
      var list = res.httpOk ? (res.body || []) : [];
      results[p] = list;
      if (res.httpOk) {
        try { cache.put(lookupCacheKey_(p, token), JSON.stringify(list), LOOKUP_CACHE_TTL); } catch (e) {}
      }
    });
  }
  return results;
}

function nameToKeyMap_(list) {
  var m = {};
  (list || []).forEach(function (row) { m[String(row.name).trim().toLowerCase()] = row.key; });
  return m;
}

function keyByName_(list, name) {
  if (!name) return null;
  var m = nameToKeyMap_(list);
  var k = m[String(name).trim().toLowerCase()];
  return k === undefined ? null : k;
}

function namesOnly_(list) {
  return (list || []).map(function (r) { return r.name; });
}

/* ============================== ORDER SHAPE MAPPING ============================== */
/* Converts an API order object (snake_case, DB names) into the camelCase
   shape App.html already knows how to render. */

function toIsoDate_(v) {
  if (!v) return '';
  return String(v).slice(0, 10); // API returns 'YYYY-MM-DD'
}

function toIsoDateTime_(v) {
  if (!v) return '';
  // API returns ISO 8601 with seconds/offset; UI wants 'YYYY-MM-DDTHH:mm'
  return String(v).slice(0, 16);
}

function mapOrder_(o) {
  return {
    slNo: o.sl_no,
    orderKey: o.order_key,
    orderRcvdDate: toIsoDate_(o.order_received_date),
    orderVia: o.order_via || '',
    shippingLocation: o.shipping_location || '',
    dcNo: o.dc_inv_no || '',
    typeOfSubmission: o.submission_type || '',
    detailedRemarks: o.detailed_remarks || '',
    readyByWhom: o.ready_by || '',
    colourMakingBy: o.colour_making_by || '',
    deliveryStatus: o.delivery_status || '',
    materialDeliveryDateTime: toIsoDateTime_(o.material_delivery_datetime),
    dateOfReceiving: toIsoDate_(o.date_of_receiving),
    paymentStatus: o.payment_status || '',
    amountReceived: o.amount_received === null || o.amount_received === undefined ? '' : Number(o.amount_received),
    deliveredByWhom: o.delivered_by || '',
    cartage: o.cartage === null || o.cartage === undefined ? '' : Number(o.cartage),
    lastUpdatedBy: o.last_updated_by || '',
    createdBy: o.created_by || ''
  };
}

function mapOrders_(list) { return (list || []).map(mapOrder_); }

/* ============================== DATE WINDOW (unchanged rule) ============================== */

function getDashboardDateWindow_() {
  var tz = Session.getScriptTimeZone() || 'Asia/Kolkata';
  var today = new Date();
  var todayStr = Utilities.formatDate(today, tz, 'yyyy-MM-dd');
  var prev = new Date(today);
  do { prev.setDate(prev.getDate() - 1); } while (prev.getDay() === 1);
  var prevStr = Utilities.formatDate(prev, tz, 'yyyy-MM-dd');
  return [prevStr, todayStr];
}

/* ============================== SHOP ============================== */

/** Fetches the order list and the role's dropdown lists in one parallel round
 *  trip (instead of one call, then another) - the main lever for page speed. */
function getRoleData_(token, role, ordersPath) {
  var lookupPaths = [];
  if (role === 'shop') lookupPaths = ['/lookups/channels', '/lookups/submission-types'];
  else if (role === 'godown') lookupPaths = ['/people?role=ready_by', '/people?role=colour_making', '/lookups/delivery-statuses'];
  else if (role === 'godown_dispatch' || role === 'shop_dispatch') {
    lookupPaths = ['/lookups/delivery-statuses', '/people?role=delivery'];
    if (role === 'shop_dispatch') lookupPaths.push('/lookups/payment-statuses');
  } else if (role === 'receiving') lookupPaths = ['/lookups/payment-statuses'];

  var cache = CacheService.getScriptCache();
  var lists = {}, toFetch = [];
  lookupPaths.forEach(function (p) {
    var cached = cache.get(lookupCacheKey_(p, token));
    if (cached) { try { lists[p] = JSON.parse(cached); return; } catch (e) {} }
    toFetch.push(p);
  });

  var specs = [{ method: 'get', path: ordersPath, token: token, payload: null }];
  toFetch.forEach(function (p) { specs.push({ method: 'get', path: p, token: token, payload: null }); });
  var responses = apiCallBatch_(specs);

  var ordersRes = responses[0];
  toFetch.forEach(function (p, i) {
    var res = responses[i + 1];
    var list = res.httpOk ? (res.body || []) : [];
    lists[p] = list;
    if (res.httpOk) { try { cache.put(lookupCacheKey_(p, token), JSON.stringify(list), LOOKUP_CACHE_TTL); } catch (e) {} }
  });

  var out = { orderVia: [], typeOfSubmission: [], readyByWhom: [], colourMakingBy: [],
              deliveryStatus: [], paymentStatus: [], deliveredByWhom: [] };
  if (role === 'shop') {
    out.orderVia = namesOnly_(lists['/lookups/channels']);
    out.typeOfSubmission = namesOnly_(lists['/lookups/submission-types']);
  } else if (role === 'godown') {
    out.readyByWhom = namesOnly_(lists['/people?role=ready_by']);
    out.colourMakingBy = namesOnly_(lists['/people?role=colour_making']);
    out.deliveryStatus = namesOnly_(lists['/lookups/delivery-statuses']);
  } else if (role === 'godown_dispatch' || role === 'shop_dispatch') {
    out.deliveryStatus = namesOnly_(lists['/lookups/delivery-statuses']);
    out.deliveredByWhom = namesOnly_(lists['/people?role=delivery']);
    if (role === 'shop_dispatch') out.paymentStatus = namesOnly_(lists['/lookups/payment-statuses']);
  } else if (role === 'receiving') {
    out.paymentStatus = namesOnly_(lists['/lookups/payment-statuses']);
  }

  return { ordersRes: ordersRes, dropdowns: out };
}

function getShopData(token) {
  try {
    var window = getDashboardDateWindow_();
    var r = getRoleData_(token, 'shop', '/orders?date_from=' + window[0] + '&date_to=' + window[1] + '&limit=200');
    if (!r.ordersRes.httpOk) return { ok: false, error: apiErrorMessage_(r.ordersRes) };
    return {
      ok: true,
      dropdowns: r.dropdowns,
      recentOrders: mapOrders_(r.ordersRes.body),
      dateWindow: window
    };
  } catch (err) {
    return { ok: false, error: 'getShopData() failed: ' + err.message };
  }
}

function addOrder(token, form, userName) {
  try {
    var missing = [];
    if (!form.orderRcvdDate) missing.push('Order Received Date');
    if (!form.orderVia) missing.push('Order Received Thru');
    if (!form.dcNo) missing.push('DC/Inv No.');
    if (!form.typeOfSubmission) missing.push('Type of Submission');
    if (missing.length) return { ok: true, success: false, message: 'Missing required field(s): ' + missing.join(', ') };

    var lists = fetchListsBatch_(token, ['/lookups/channels', '/lookups/submission-types']);
    var orderViaKey = keyByName_(lists['/lookups/channels'], form.orderVia);
    var subTypeKey = keyByName_(lists['/lookups/submission-types'], form.typeOfSubmission);
    if (!orderViaKey || !subTypeKey) return { ok: true, success: false, message: 'Unknown dropdown value selected. Please refresh and try again.' };

    var payload = {
      order_received_date: form.orderRcvdDate,
      order_via_key: orderViaKey,
      submission_type_key: subTypeKey,
      dc_inv_no: form.dcNo,
      shipping_location: form.shippingLocation || null,
      detailed_remarks: form.detailedRemarks || null
    };
    var res = apiCall_('post', '/orders', token, payload);
    if (!res.httpOk) return { ok: true, success: false, message: apiErrorMessage_(res) };
    return { ok: true, success: true, slNo: res.body.sl_no };
  } catch (err) {
    return { ok: false, error: 'addOrder() failed: ' + err.message };
  }
}

function updateShopFields(token, slNo, form, userName) {
  try {
    var missing = [];
    if (!form.orderRcvdDate) missing.push('Order Received Date');
    if (!form.orderVia) missing.push('Order Received Thru');
    if (!form.dcNo) missing.push('DC/Inv No.');
    if (!form.typeOfSubmission) missing.push('Type of Submission');
    if (missing.length) return { ok: true, success: false, message: 'Missing required field(s): ' + missing.join(', ') };

    var lists = fetchListsBatch_(token, ['/lookups/channels', '/lookups/submission-types']);
    var payload = {
      order_received_date: form.orderRcvdDate,
      order_via_key: keyByName_(lists['/lookups/channels'], form.orderVia),
      submission_type_key: keyByName_(lists['/lookups/submission-types'], form.typeOfSubmission),
      dc_inv_no: form.dcNo,
      shipping_location: form.shippingLocation || null,
      detailed_remarks: form.detailedRemarks || null
    };
    var res = apiCall_('patch', '/orders/' + slNo, token, payload);
    if (!res.httpOk) return { ok: true, success: false, message: apiErrorMessage_(res) };
    return { ok: true, success: true };
  } catch (err) {
    return { ok: false, error: 'updateShopFields() failed: ' + err.message };
  }
}

/* ============================== GODOWN ============================== */

function getGodownData(token) {
  try {
    var r = getRoleData_(token, 'godown', '/orders?limit=200');
    if (!r.ordersRes.httpOk) return { ok: false, error: apiErrorMessage_(r.ordersRes) };
    var orders = mapOrders_(r.ordersRes.body);
    var pending = orders.filter(function (o) { return !o.deliveryStatus; });
    var completed = orders.filter(function (o) { return !!o.deliveryStatus; });
    return { ok: true, dropdowns: r.dropdowns, pending: pending, completed: completed, dateWindow: getDashboardDateWindow_() };
  } catch (err) {
    return { ok: false, error: 'getGodownData() failed: ' + err.message };
  }
}

function updateGodownFields(token, slNo, form, userName) {
  try {
    var lists = fetchListsBatch_(token, ['/people?role=ready_by', '/people?role=colour_making', '/lookups/delivery-statuses']);
    var payload = {};
    if (form.readyByWhom) payload.ready_by_person_key = keyByName_(lists['/people?role=ready_by'], form.readyByWhom);
    if (form.colourMakingBy) payload.colour_making_person_key = keyByName_(lists['/people?role=colour_making'], form.colourMakingBy);
    if (form.deliveryStatus) payload.delivery_status_key = keyByName_(lists['/lookups/delivery-statuses'], form.deliveryStatus);
    var res = apiCall_('patch', '/orders/' + slNo, token, payload);
    if (!res.httpOk) return { ok: true, success: false, message: apiErrorMessage_(res) };
    return { ok: true, success: true };
  } catch (err) {
    return { ok: false, error: 'updateGodownFields() failed: ' + err.message };
  }
}

/* ============================== DISPATCH ============================== */

function getDispatchData(token, role) {
  try {
    var r = getRoleData_(token, role, '/orders?limit=200');
    if (!r.ordersRes.httpOk) return { ok: false, error: apiErrorMessage_(r.ordersRes) };
    var orders = mapOrders_(r.ordersRes.body);
    var pending = orders.filter(function (o) { return !o.materialDeliveryDateTime; });
    var completed = orders.filter(function (o) {
      return !!o.materialDeliveryDateTime && (!o.dateOfReceiving || !o.paymentStatus);
    });
    completed.forEach(function (o) { o.stuckReason = 'Receiving pending'; });
    return { ok: true, dropdowns: r.dropdowns, pending: pending, completed: completed, dateWindow: getDashboardDateWindow_() };
  } catch (err) {
    return { ok: false, error: 'getDispatchData() failed: ' + err.message };
  }
}

function updateDispatchFields(token, slNo, form, userName, role) {
  try {
    // The WhatsApp "first filled in" alert only applies to godown_dispatch, so only
    // that role needs the extra "before" order fetch - and it rides in the same
    // parallel batch as the lookups instead of its own separate round trip.
    var needsBefore = role === 'godown_dispatch';
    var lookupPaths = ['/lookups/delivery-statuses', '/people?role=delivery'];
    if (role === 'shop_dispatch') lookupPaths.push('/lookups/payment-statuses');
    var specs = lookupPaths.map(function (p) { return { method: 'get', path: p, token: token, payload: null }; });
    if (needsBefore) specs.push({ method: 'get', path: '/orders/' + slNo, token: token, payload: null });
    var batchRes = apiCallBatch_(specs);
    var lists = {};
    lookupPaths.forEach(function (p, i) { lists[p] = batchRes[i].httpOk ? (batchRes[i].body || []) : []; });
    var before = needsBefore && batchRes[lookupPaths.length].httpOk ? mapOrder_(batchRes[lookupPaths.length].body) : null;

    var payload = {};
    if (form.deliveryStatus) payload.delivery_status_key = keyByName_(lists['/lookups/delivery-statuses'], form.deliveryStatus);
    if (form.materialDeliveryDateTime) payload.material_delivery_datetime = form.materialDeliveryDateTime;
    if (form.deliveredByWhom) payload.delivered_by_person_key = keyByName_(lists['/people?role=delivery'], form.deliveredByWhom);
    if (form.cartage !== '' && form.cartage !== undefined && form.cartage !== null) payload.cartage = Number(form.cartage);

    if (role === 'shop_dispatch') {
      if (form.dateOfReceiving) payload.date_of_receiving = form.dateOfReceiving;
      if (form.paymentStatus) payload.payment_status_key = keyByName_(lists['/lookups/payment-statuses'], form.paymentStatus);
      if (form.amountReceived !== '' && form.amountReceived !== undefined && form.amountReceived !== null) {
        payload.amount_received = Number(form.amountReceived);
      }
    }

    var res = apiCall_('patch', '/orders/' + slNo, token, payload);
    if (!res.httpOk) return { ok: true, success: false, message: apiErrorMessage_(res) };
    var updated = mapOrder_(res.body);

    try {
      var wasEmpty = before && !before.materialDeliveryDateTime && !before.deliveredByWhom;
      var nowFilled = !!updated.materialDeliveryDateTime && !!updated.deliveredByWhom;
      if (role === 'godown_dispatch' && wasEmpty && nowFilled) {
        sendGodownDispatchWhatsAppAlert_(updated);
      }
    } catch (waErr) {
      Logger.log('WhatsApp notification failed (order saved fine): ' + waErr.message);
    }

    return { ok: true, success: true };
  } catch (err) {
    return { ok: false, error: 'updateDispatchFields() failed: ' + err.message };
  }
}

/* ============================== WHATSAPP ============================== */

var WHATSAPP_GROUP_ID = '120363410985827601@g.us';
var WHATSAPP_API_URL = 'https://app.messageautosender.com/api/v1/message/create';

function sendGodownDispatchWhatsAppAlert_(o) {
  var props = PropertiesService.getScriptProperties();
  var username = props.getProperty('WHATSAPP_API_USERNAME');
  var password = props.getProperty('WHATSAPP_API_PASSWORD');
  if (!username || !password) return { skipped: true, reason: 'Credentials not set.' };
  var messageText =
    'New Dispatch - Godown\n\nOrder Date: ' + formatDatePdf_(o.orderRcvdDate) +
    '\nDC No: ' + (o.dcNo || '-') +
    '\nReady By: ' + (o.readyByWhom || '-') +
    '\nDelivery Status: ' + (o.deliveryStatus || '-') +
    '\nAddress: ' + (o.shippingLocation || '-') +
    '\nRemarks: ' + (o.detailedRemarks || '-') +
    '\nDelivered By: ' + (o.deliveredByWhom || '-');
  var options = {
    method: 'post', contentType: 'application/json',
    payload: JSON.stringify({ recipientIds: [WHATSAPP_GROUP_ID], message: [messageText] }),
    headers: { 'Authorization': 'Basic ' + Utilities.base64Encode(username + ':' + password) },
    muteHttpExceptions: true
  };
  try {
    var response = UrlFetchApp.fetch(WHATSAPP_API_URL, options);
    return { code: response.getResponseCode(), body: response.getContentText() };
  } catch (fetchErr) {
    return { fetchError: fetchErr.message };
  }
}

/* ============================== RECEIVING ============================== */

function getReceivingData(token) {
  try {
    var r = getRoleData_(token, 'receiving', '/orders?limit=200');
    if (!r.ordersRes.httpOk) return { ok: false, error: apiErrorMessage_(r.ordersRes) };
    var orders = mapOrders_(r.ordersRes.body);
    var pending = orders.filter(function (o) { return !o.dateOfReceiving || !o.paymentStatus; });
    var completed = orders.filter(function (o) { return !!o.dateOfReceiving && !!o.paymentStatus; });
    return { ok: true, dropdowns: r.dropdowns, pending: pending, completed: completed, dateWindow: getDashboardDateWindow_() };
  } catch (err) {
    return { ok: false, error: 'getReceivingData() failed: ' + err.message };
  }
}

/* ============================== ADMIN (READ-ONLY, ALL ORDERS) ============================== */
/* Admin has no entry in EDITABLE_FIELDS on the API, so this is view-only by design -
   admin manages members/roles/apps from the Render portal, not order data here. */

function getAdminOrders(token) {
  try {
    var res = apiCall_('get', '/orders?limit=200', token, null); // 200 is the API's max per request
    if (!res.httpOk) return { ok: false, error: apiErrorMessage_(res) };
    return { ok: true, orders: mapOrders_(res.body) };
  } catch (err) {
    return { ok: false, error: 'getAdminOrders() failed: ' + err.message };
  }
}

function updateReceivingFields(token, slNo, form, userName) {
  try {
    var payments = fetchList_(token, '/lookups/payment-statuses');
    var payload = {};
    if (form.dateOfReceiving) payload.date_of_receiving = form.dateOfReceiving;
    if (form.paymentStatus) payload.payment_status_key = keyByName_(payments, form.paymentStatus);
    if (form.amountReceived !== '' && form.amountReceived !== undefined && form.amountReceived !== null) {
      payload.amount_received = Number(form.amountReceived);
    }
    var res = apiCall_('patch', '/orders/' + slNo, token, payload);
    if (!res.httpOk) return { ok: true, success: false, message: apiErrorMessage_(res) };
    return { ok: true, success: true };
  } catch (err) {
    return { ok: false, error: 'updateReceivingFields() failed: ' + err.message };
  }
}

/* ============================== SHARED SEARCH ============================== */

function searchOrders(token, date, dcNo) {
  try {
    var qs = ['limit=100'];
    if (date) { qs.push('date_from=' + date); qs.push('date_to=' + date); }
    if (dcNo) qs.push('q=' + encodeURIComponent(dcNo));
    var res = apiCall_('get', '/orders?' + qs.join('&'), token, null);
    if (!res.httpOk) return { ok: false, error: apiErrorMessage_(res) };
    return { ok: true, results: mapOrders_(res.body) };
  } catch (err) {
    return { ok: false, error: 'searchOrders() failed: ' + err.message };
  }
}

/* ============================== MISSING NUMBERS ============================== */
/* NOTE: unlike the old sheet-based version (which scanned every row
   regardless of role), the API scopes /orders to what each role may
   see. So this now only flags gaps within the orders visible to the
   signed-in user, not the whole book. Good enough for shop (sees all
   orders it created) but a narrower view for other roles. */

function extractNumericDcNos_(orders) {
  var smallNums = [], largeNums = [];
  orders.forEach(function (o) {
    var n = parseInt(o.dcNo, 10);
    if (isNaN(n) || String(n) !== String(o.dcNo).trim()) return;
    if (n < 1000) smallNums.push(n); else largeNums.push(n);
  });
  return { small: smallNums, large: largeNums };
}

function findMissingInSequence_(nums) {
  if (nums.length === 0) return { min: null, max: null, missing: [] };
  var min = Math.min.apply(null, nums), max = Math.max.apply(null, nums);
  var present = {};
  nums.forEach(function (n) { present[n] = true; });
  var missing = [];
  for (var i = min; i <= max; i++) { if (!present[i]) missing.push(i); }
  return { min: min, max: max, missing: missing };
}

function getMissingNumbersForWindow(token) {
  try {
    var dates = getDashboardDateWindow_();
    var res = apiCall_('get', '/orders?date_from=' + dates[0] + '&date_to=' + dates[1] + '&limit=200', token, null);
    if (!res.httpOk) return { ok: false, error: apiErrorMessage_(res) };
    var windowOrders = mapOrders_(res.body);

    var challanByDate = dates.map(function (d) {
      var dayOrders = windowOrders.filter(function (o) { return o.orderRcvdDate === d; });
      var nums = extractNumericDcNos_(dayOrders);
      return { date: d, challan: findMissingInSequence_(nums.small) };
    });

    var invoiceByDate = dates.map(function (d) {
      var dayOrders = windowOrders.filter(function (o) { return o.orderRcvdDate === d; });
      var nums = extractNumericDcNos_(dayOrders);
      var largeNums = nums.large;
      if (largeNums.length === 0) return { date: d, invoice: { min: null, max: null, missing: [] } };
      largeNums.sort(function (a, b) { return a - b; });
      var missing = [];
      for (var i = 0; i < largeNums.length - 1; i++) {
        var curr = largeNums[i], next = largeNums[i + 1];
        if (next - curr > 1 && next - curr <= 10) {
          for (var g = curr + 1; g < next; g++) missing.push(g);
        }
      }
      return { date: d, invoice: { min: largeNums[0], max: largeNums[largeNums.length - 1], missing: missing } };
    });

    return { ok: true, challanByDate: challanByDate, invoiceByDate: invoiceByDate };
  } catch (err) {
    return { ok: false, error: 'getMissingNumbersForWindow() failed: ' + err.message };
  }
}

/* ============================== PDF / MISC HELPERS ============================== */

function formatDatePdf_(iso) {
  if (!iso) return '';
  var parts = String(iso).split('-');
  if (parts.length !== 3) return escapeHtml_(iso);
  return parts[2] + '/' + parts[1] + '/' + parts[0];
}

function escapeHtml_(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* ============================== DEBUG ============================== */

function debugInfo(token) {
  try {
    var res = apiCall_('get', '/health', null, null);
    var me = token ? apiCall_('get', '/auth/me', token, null) : null;
    // SpreadsheetApp.getActiveSpreadsheet() does not carry the container binding
    // across when this script runs as a deployed Web App (only works from inside
    // the Sheet's own UI), so it can never answer "which sheet is this" here.
    // Set a plain label once instead: Script Properties -> SHEET_LABEL.
    var label = PropertiesService.getScriptProperties().getProperty('SHEET_LABEL');
    return {
      ok: true,
      apiBase: apiBase_(),
      apiHealth: res.body,
      whoAmI: me ? me.body : 'not logged in',
      sheetLabel: label || '(not set - add a SHEET_LABEL script property so this deployment identifies itself)',
      clientKeyConfigured: !!clientKey_() // never show the value itself
    };
  } catch (err) {
    return { ok: false, error: 'debugInfo() failed: ' + err.message };
  }
}
