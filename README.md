# Vardhman Traders – Database

PostgreSQL (hosted on Render). Schema is managed by plain numbered SQL files in `migrations/`.

## Run migrations

```powershell
pip install -r requirements.txt
copy .env.example .env      # then edit .env and put your Render "External Database URL" in it
python migrate.py
```

`.env` is git-ignored, so the password never gets committed. A `DATABASE_URL` set in your
shell overrides the value in `.env`.

`migrate.py` applies any `migrations/NNN_*.sql` not yet recorded in the `schema_migrations`
table, in order, each in its own transaction (a failed migration rolls back). It opens one
connection and closes it when done. Re-running is safe.

## Add a migration

1. Create the next numbered file, e.g. `migrations/002_add_something.sql`.
2. Write plain SQL. Never edit a migration that has already been applied
   (the runner checks a checksum and refuses) – add a new one instead.
3. Run `python migrate.py`.

## Setup for the API
`.env` needs `DATABASE_URL` and `JWT_SECRET` (see `.env.example`).

## Import from Google Sheets (one-off, already done)
`python import_sheet.py` (dry run) / `python import_sheet.py --commit`. Reads `data/orders.csv` and
`data/users.csv` (git-ignored). Refuses to run once `fact_orders` has rows.

## API
```powershell
uvicorn app.main:app --reload      # docs at http://127.0.0.1:8000/docs
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login` | `{username, password}` -> bearer token (12h) |
| GET | `/auth/me` | current user |
| GET | `/lookups/{channels\|submission-types\|delivery-statuses\|payment-statuses}` | dropdown values (`key`, `name`) |
| POST | `/lookups/{kind}` `{name}` | get-or-create (case/space-insensitive); returns `created` |
| GET / POST | `/people?role=` | list / get-or-create (`full_name`, `person_role`, `phone_number`) |
| GET | `/orders` | filters: `delivery_status_key`, `date_from`, `date_to`, `q`, `include_cancelled`, `limit` (<=200), `offset` |
| GET | `/orders/{sl_no}` | one order |
| POST | `/orders` | create; `sl_no` is assigned by the server, creator is the logged-in user |
| PATCH | `/orders/{sl_no}` | partial update; sets last-updated user/time |

All routes except `/health` and `/auth/login` need `Authorization: Bearer <token>`.

### Roles and visibility (see `app/roles.py`)
| Role | Sees | May set |
|---|---|---|
| `shop` | orders they created | date, channel, submission type, DC/Inv no, address, remarks (and creates orders) |
| `godown` | orders awaiting godown work + ones they last updated | ready-by, colour-making, delivery status |
| `godown_dispatch` | non-"Shop" orders not yet fully closed + ones they last updated | delivery status, delivery time, delivered-by, cartage |
| `shop_dispatch` | "Shop" orders not yet fully closed + ones they last updated | dispatch fields + date of receiving, payment status, amount received |
| `receiving` | delivered non-"Shop" orders missing receiving details + ones they last updated | date of receiving, payment status, amount received |
| `admin` | every order (active and archived) | any field |
| `cashier`, `accounts`, `cartage` | nothing by default; admin can switch on read-only viewing (Setup -> Permissions) | - |

An order you may not see returns 404 (same as missing). Fields outside your role return 403.
`is_cancelled` follows the delivery status and cannot be set directly.

### Users and passwords
- Everyone logs in as themselves; new/reset users must change their password on first login
  (`POST /auth/change-password`) before any other call works.
- 5 failed logins lock that username/IP for 15 minutes (in-memory, per process).
- Admins add, edit, disable and delete members (one at a time or many via bulk upload) in the portal's
  **Members** and **Import** tabs. `python manage_users.py` remains for creating the very first admin on a new
  database (list / add / set-password / set-role / disable).
The DB pool is capped at 5 connections (`DB_POOL_MAX`) for Render's connection limit.

### Order screens
The screens for each role (shop, godown, dispatch, receiving) are served by this app at `/sales/`. The portal opens
them with one click and signs you in automatically; they can also be opened directly and signed into there.
Their data routes live in `app/o2d_screens.py` (`/o2d/...`). There is no Apps Script and no Google Sheets anywhere.
WhatsApp dispatch alerts use `WHATSAPP_API_USERNAME` / `WHATSAPP_API_PASSWORD` (and optionally `WHATSAPP_GROUP_ID`);
without them the alert is simply skipped.

### Deploy on Render (Web Service)
Build: `pip install -r requirements.txt` - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
Set `DATABASE_URL` (use the *Internal* URL there) and `JWT_SECRET` in the service's environment.


### Admin tools (`/admin/*`, admin only)
| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/orders/filter-options` | values for every filter drop-down |
| GET | `/admin/orders/search` | filtered, sorted, paged orders (`stage`, `channel`, `q`, `date_from`, `min_amount` ... `batch_id`) |
| GET | `/admin/orders/search.xlsx` | the same filter as an Excel file (cap 20,000 rows) |
| GET/POST/DELETE | `/admin/saved-filters` | personal or shared saved filters |
| GET | `/admin/bulk/{orders\|users}/template.csv` | upload template with EXAMPLE rows |
| POST | `/admin/bulk/{entity}/validate` `/revalidate` `/confirm` | check a file, re-check fixed rows, import (all-or-nothing) |
| GET / POST | `/admin/bulk/batches`, `/admin/bulk/batches/{id}/undo` | import history and undo |
| GET | `/admin/reconcile`, `/admin/reconcile/{kind}` | distinct values per master list (A-Z, with order counts) and likely duplicates |
| POST | `/admin/reconcile/{kind}/rename` `/merge` | rename in place, or merge (preview with `confirm=false`, then `confirm=true`); `kind` = channel, submission_type, delivery_status, payment_status, person |
