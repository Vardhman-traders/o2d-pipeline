# Development guide

## One-time setup
```powershell
pip install -r requirements-dev.txt
docker compose up -d db          # Postgres 16 on localhost:5433 (your dev DB)
$env:DATABASE_URL = "postgresql://vt:vt@localhost:5433/vt_dev"
python migrate.py                # builds the dev schema
```
Your dev database is separate from Render. Never point `DATABASE_URL` at production while developing.

## Every day
```powershell
pytest -q                         # ~5 s; creates and drops its own throwaway databases
pytest -q -m "not slow"           # quick loop
ruff check tests scripts          # new code must be clean
python scripts/lint_ratchet.py check    # legacy code may not get worse
sqlfluff lint migrations          # new migrations only (001-007 are frozen)
```
Tests use `TEST_DATABASE_URL` (default `postgresql://vt:vt@localhost:5433/postgres`, i.e. the compose db).

## What CI checks on every push and pull request
| Job | Blocks merge? | What it does |
|---|---|---|
| lint | yes | ruff on new code; ratchet so legacy problems can only shrink; sqlfluff on new migrations |
| secrets | yes | gitleaks scans the repo history for keys and passwords |
| test | yes | applies all migrations to a fresh Postgres 16, runs the API characterisation tests |
| test-change report | yes, until approved | if an EXISTING test is edited/deleted/renamed, posts a diff report on the PR and blocks until you add the `tests-change-approved` label |
| dependency-audit | no (advisory) | pip-audit on requirements.txt |

## Rules that keep the migration safe
1. Never edit an applied migration; add the next numbered file (migrate.py refuses edits).
2. Adding tests is free. Changing an existing test needs your approval via the label above.
3. Every finding or business rule that gets a test is listed in `tests/registry.yaml`.
4. When you fix legacy lint problems, run `python scripts/lint_ratchet.py update-baseline` to lock the gain in.

## Apps Script and Google Sheets: removed
`app/o2d_screens.py` does in Python everything `apps_script/Code.gs` did (role screens, name-to-key mapping,
Monday-skipping date window, missing DC/invoice numbers, WhatsApp dispatch alert), and the order rules exist in one
place (`main.update_order` / `main.create_order`). The screens themselves are `app/static/sales/` (the original
HTML, unchanged, plus `gas-shim.js` which points its server calls at this app and `autologin.js` for one sign-in).
Migration 008 allows own-page links, hides old Google links and adds the Sales Portal link.
The SSO ticket routes, the client-key check and its admin screen, and the Google-link wording are deleted.
Guard test: `tests/test_apps_script_removed.py` fails if any of it comes back.

Still yours to do by hand: delete the `apps_script/` folder and the stray `app/main-1.py`, and once live set
`WHATSAPP_API_USERNAME` / `WHATSAPP_API_PASSWORD` on Render for the dispatch alert.

## Known legacy items (not fixed yet, tracked by the ratchet)
- 60 ruff/mypy problems in app/*.py at the time CI was added (mostly line length and `;` one-liners).
- README describes admin as having no order access; the code gives admin read access to the orders list, and
  cashier/accounts/cartage get it only when admin switches them on (Setup -> Permissions). Tests pin the code.

## Admin portal and bulk upload
`app/admin_tools.py` (routes), `app/bulk.py` (CSV/Excel reading, template), `app/bulk_entities.py` (per-entity
validation and insert), `app/static/portal-tools.js` (Orders and Import screens). Every import runs in one
transaction under an `import_batch` id (migration 009), so it can be undone; undo is refused once an imported
order was edited or an imported member has signed in. To add another uploadable thing, add an entry to
`bulk_entities.ENTITIES` with its columns, `validate` and `insert`.
