const token = sessionStorage.getItem('vt_token');
const out = document.getElementById('out');
if (!token) out.textContent = 'You are not signed in. Sign in to the main app as an admin in THIS browser tab, then open this page again in the same tab.';
const body = () => JSON.stringify({
  password: document.getElementById('pw').value, totp: document.getElementById('otp').value,
  groups: [...document.querySelectorAll('.g:checked')].map(x => x.value),
  attachments: document.getElementById('files').checked});
const call = (path, opt) => fetch('/admin/vault' + path, {...opt, headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'}});
const save = async (path, name) => {
  const d = await call(path); const blob = await d.blob(); const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name; a.click(); await new Promise(res => setTimeout(res, 500));
};
const clearSecrets = () => { document.getElementById('pw').value = ''; document.getElementById('otp').value = ''; };
document.getElementById('plan').onclick = async () => {
  out.textContent = 'Working...';
  const r = await call('/plan', {method: 'POST', body: body()}); clearSecrets();
  if (!r.ok) { out.textContent = 'Refused (' + r.status + ')'; return; }
  const j = await r.json();
  out.textContent = Object.entries(j.tables).map(([t, n]) => t + ': ' + n).join('\n') + '\n\nNever exported: ' + j.never_exported.join(', ');
};
const bout = document.getElementById('bout');
document.getElementById('batches').onclick = async () => {
  const r = await call('/batches');
  if (!r.ok) { bout.textContent = 'Refused (' + r.status + ')'; return; }
  const rows = await r.json();
  bout.textContent = rows.length ? rows.map(b => 'batch ' + b.batch_no + '  ' + b.kind + (b.kind === 'delta' ? ' (on ' + b.base_batch + ')' : '') + '  ' + b.status + '  ' + b.created_at.slice(0, 19)).join('\n') : 'No batches yet.';
};
document.getElementById('confirm').onclick = async () => {
  const n = document.getElementById('bn').value.trim();
  if (!/^[0-9]+$/.test(n)) { bout.textContent = 'Type the batch number first.'; return; }
  const r = await call('/batches/' + n + '/confirm', {method: 'POST', body: body()}); clearSecrets();
  bout.textContent = r.ok ? 'Batch ' + n + ' confirmed. The next export will only carry what changes after it.' :
    r.status === 409 ? 'That batch cannot be confirmed (it is not waiting for confirmation).' : r.status === 404 ? 'No such batch.' : 'Refused (' + r.status + ')';
};
document.getElementById('go').onclick = async () => {
  out.textContent = 'Starting...';
  const r = await call('/export', {method: 'POST', body: body()}); clearSecrets();
  if (!r.ok) { out.textContent = 'Refused (' + r.status + ')'; return; }
  const {job} = await r.json();
  for (;;) {
    await new Promise(res => setTimeout(res, 2000));
    const s = await (await call('/export/' + job)).json();
    if (s.state === 'failed') { out.textContent = 'Export failed (' + s.error + '). See the server log.'; return; }
    if (s.state === 'ready') {
      const ch = Object.entries(s.changes || {}).map(([t, c]) => '  ' + t + ': ' + c.changed + ' new or changed, ' + c.removed + ' removed').join('\n');
      out.textContent = 'Ready: ' + s.size + ' bytes\nbatch ' + s.batch_no + ' (' + s.kind + (s.kind === 'delta' ? ', built on batch ' + s.base_batch : '') + ')\nexport id: ' + s.export_id + '\nsha256: ' + s.sha256 +
        (s.kind === 'delta' ? '\nChanges since the last confirmed batch:\n' + (ch || '  none') : '') + '\nDownloading...';
      await save('/export/' + job + '/download', 'export-' + s.export_id.slice(0, 8) + '.bin');
      for (const p of (s.packs || [])) {
        out.textContent += '\nfiles pack ' + p.n + ': ' + p.size + ' bytes, sha256 ' + p.sha256;
        await save('/export/' + job + '/pack/' + p.n, 'files-' + s.export_id.slice(0, 8) + '-' + p.n + '.bin');
      }
      out.textContent += '\nDone. Keep the sha256 values above to compare after upload. The server has deleted its copies.' + (s.files_note ? '\nNote: ' + s.files_note : '');
      return;
    }
    const pr = s.progress ? ' (' + s.progress.files_done + ' of ' + s.progress.files_total + ' files)' : '';
    out.textContent = 'Building the encrypted export...' + pr;
  }
};
