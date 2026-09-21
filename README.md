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
| `admin`, `cashier`, `accounts`, `cartage` | nothing (no order access) | - |

An order you may not see returns 404 (same as missing). Fields outside your role return 403.
`is_cancelled` follows the delivery status and cannot be set directly.

### Users and passwords
- Everyone logs in as themselves; new/reset users must change their password on first login
  (`POST /auth/change-password`) before any other call works.
- 5 failed logins lock that username/IP for 15 minutes (in-memory, per process).
- Manage users from your machine with `python manage_users.py` (list / add / set-password / set-role /
  disable). There is deliberately no user-admin API.
The DB pool is capped at 5 connections (`DB_POOL_MAX`) for Render's connection limit.

### Calling from Apps Script
```javascript
function apiLogin_() {
  const r = UrlFetchApp.fetch(API + '/auth/login', {method: 'post', contentType: 'application/json',
    payload: JSON.stringify({username: USER, password: PASS})});
  return JSON.parse(r.getContentText()).access_token;
}
function createOrder(order, token) {
  const r = UrlFetchApp.fetch(API + '/orders', {method: 'post', contentType: 'application/json',
    headers: {Authorization: 'Bearer ' + token}, payload: JSON.stringify(order), muteHttpExceptions: true});
  return JSON.parse(r.getContentText());
}
```

### Deploy on Render (Web Service)
Build: `pip install -r requirements.txt` - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
Set `DATABASE_URL` (use the *Internal* URL there) and `JWT_SECRET` in the service's environment.
