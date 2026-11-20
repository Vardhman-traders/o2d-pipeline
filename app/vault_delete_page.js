const token = sessionStorage.getItem('vt_token');
const out = document.getElementById('out');
const done = document.getElementById('done');
if (!token) out.textContent = 'You are not signed in. Sign in to the main app as an admin in THIS browser tab, then open this page again in the same tab.';
const call = (path, body) => fetch('/admin/vault/delete' + path, {
  method: 'POST', headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const gate = () => ({password: document.getElementById('pw').value, totp: document.getElementById('otp').value});
const clearSecrets = () => { document.getElementById('pw').value = ''; document.getElementById('otp').value = ''; };
const lines = (obj) => Object.entries(obj).map(([k, v]) => '  ' + k + ': ' + v).join('\n');

document.getElementById('preview').onclick = async () => {
  out.textContent = 'Checking...';
  const r = await call('/plan', {...gate(), delete_files: document.getElementById('files').checked}); clearSecrets();
  if (!r.ok) { out.textContent = 'Refused (' + r.status + ')'; return; }
  const j = await r.json();
  document.getElementById('plan').value = j.plan_id;
  let t = j.can_execute ? 'READY. Nothing has been deleted.\n' : 'BLOCKED. Nothing has been deleted, and nothing can be until this is fixed:\n';
  if (j.blockers.length) t += '\nBlocked because:\n' + j.blockers.map(b => ' - ' + b).join('\n') + '\n';
  if (j.warnings.length) t += '\nNote:\n' + j.warnings.map(b => ' - ' + b).join('\n') + '\n';
  t += '\nConfirmed export batch: ' + j.confirmed_batch + '\n\nWould delete (rows):\n' + lines(j.will_delete);
  t += '\n\nStored photo/document files: ' + j.stored_files + (j.delete_stored_files ? ' (would be deleted)' : ' (would stay)');
  t += '\n\nWill NOT be touched (rows now):\n' + lines(j.will_not_touch);
  t += '\n\nEarliest time this preview can be used: ' + j.earliest_execute_at + '\n\nAfter it:\n' + j.after_it.map(b => ' - ' + b).join('\n');
  out.textContent = t;
};

document.getElementById('go').onclick = async () => {
  done.textContent = 'Working... (the tables are briefly locked)';
  const r = await call('/execute', {...gate(), plan_id: document.getElementById('plan').value.trim(),
    phrase: document.getElementById('typed').value, backups_understood: document.getElementById('backups').checked}); clearSecrets();
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { done.textContent = 'Not deleted (' + r.status + '): ' + (j.error || j.detail || 'refused'); return; }
  done.textContent = 'DELETED ' + j.rows_total + ' rows in ' + j.seconds + ' s.\n\n' + lines(j.deleted) +
    '\n\nStored files: ' + j.stored_files.deleted + ' deleted, ' + j.stored_files.failed + ' failed' +
    (j.stored_files.failed ? ' (retry later: they are kept on a list)' : '') + '\n' + (j.space.note || 'Disk space was reclaimed.');
};
