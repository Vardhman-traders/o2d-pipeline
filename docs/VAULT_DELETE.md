# Encrypted export and the guarded delete

Two optional features, both OFF unless switched on, both behind the same lock (admin sign-in + export password + one-time authenticator code):

1. **Encrypted export** (`app/vault_export.py`): turns orders, payments, expenses, purchases, VT Decor, the customer/reference lists and
   their photos into encrypted files that only an offline key can open. This server only holds the public key, so it cannot read them.
2. **Guarded delete** (`app/vault_delete.py`): removes the exported orders, money and VT Decor data (and their stored photos) from THIS server,
   but only after an export was uploaded, checked and confirmed, and only if nothing changed since.

## Switching them on (environment variables)
| Variable | Meaning |
|---|---|
| `VAULT_EXPORT_ENABLED=1` | mounts the export routes (`/admin/vault/page`). Needs the four below. |
| `VAULT_PUBLIC_KEY` | base64 of the 65-byte public key. The private key is never here. |
| `VAULT_EXPORT_PASSWORD_HASH` | bcrypt hash of the export password (kept in the environment, not the database). |
| `VAULT_EXPORT_TOTP_SECRET` | base32 secret of the authenticator app used for the code. |
| `VAULT_FINGERPRINT_KEY` | 32+ character secret for the row fingerprints; keep it the same for every export. |
| `VAULT_DELETE_ENABLED=1` | a SEPARATE switch that also mounts the delete routes (`/admin/vault/delete/page`). |
| `VAULT_DELETE_COOLING_HOURS` | wait between a preview and the delete (default 24; 0 only for tests). |

Turn both on for the work window only, then remove them. With them off, none of these routes exist.

## What the delete removes
All rows of: `fact_orders`, `order_event`, `order_attachments`, `order_doc_read`, `order_reminder`, `fact_challan_upload`, `doc_gap_void`,
`daily_delivery_report`, `fact_payment_txn`, `payment_push`, `fact_expense_entry`, `fact_purchase_entry`, `fact_extra_work`, the six `fact_decor_*`
tables, `decor_fixed_expense`, and the `file_attachment` rows of the challan, purchase and decor modules, plus their stored files when asked.
Only tables the export covers can ever be deleted (a test fails otherwise).

**Never touched:** attendance and salary, logins and permissions, settings, tasks and checklists, the audit log, and every customer and
reference list (parties, companies, people, VT Decor clients, payment modes, dates ...). A test proves every other table keeps exactly its rows.

## The steps, and what stops each one
1. **Preview** (password + code). Deletes nothing. Refuses, with the reason, unless:
   - the last export batch was **confirmed** and covered orders, money and VT Decor;
   - **nothing changed** in those tables since it: no new, changed or removed row (compared by the same keyed fingerprints);
   - for deleting stored photos: every file was part of the confirmed export, none missing or damaged;
   - no table outside the scope has a row pointing at data that would go.
   A change to a *kept* list (for example a renamed customer) only warns.
2. **Wait** the cooling-off time (default 24 h). A preview older than 7 days is void.
3. **Delete** (password + a new code + the typed phrase + a tick that backups still hold the data):
   - the same checks run again while the tables are locked, and the row counts must still equal the preview's;
   - it is ONE transaction. If anything is off, a table fails, or any table outside the scope would change its row count, **everything is
     rolled back** and nothing is deleted;
   - afterwards the space is reclaimed (`VACUUM FULL`) and the stored files are removed. Files that could not be removed stay on a list and
     can be retried (`/retry-files`). The preview is single use.
4. Every step is written to the audit log with counts and ids, never data.

## What it cannot do
- **Backups.** Database backups and storage versioning keep the old data until they expire. Purge them (or wait) for a complete removal.
- **Switch off what depends on the data.** Reports, WhatsApp alerts and screens that read the deleted tables will show nothing or error. Turn
  them off first.
- **Undo.** There is no undo here. The only copy afterwards is the encrypted export at the destination, which is why the confirmation step exists.
- It does not look at the destination. "Confirmed" is an admin's statement, backed by the validation tools, that the upload was checked.
