"""Demo data for Delegation and Checklist, on the LOCAL development database only.

Creates a manager and four staff (password Demo-Pass-123), then drives the real API as those people so the data goes
through exactly the rules the screens use: tasks are assigned, completed on time / late, approved, sent back for revision,
rejected, extended, weekly plans and extra work are saved, checklists of every frequency are created and their items marked
Done / Not Done.

    python scripts/seed_delegation_demo.py --pg-password YOUR_POSTGRES_PASSWORD            # add the demo data
    python scripts/seed_delegation_demo.py --pg-password YOUR_POSTGRES_PASSWORD --reset    # remove it again (and exit)

Re-running adds nothing twice: it refuses when the demo people already have tasks (use --reset first). Like run_local.py it
refuses to touch any database that is not on this machine.
"""
import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSWORD = "Demo-Pass-123"
MANAGER = ("demo_manager", "Meera Manager", "manager")
STAFF = [("demo_asha", "Asha Verma"), ("demo_vikram", "Vikram Singh"), ("demo_neha", "Neha Gupta"), ("demo_sunil", "Sunil Yadav")]
STAFF_PAGES = ("chk_mine", "chk_calendar")            # the Delegation pages come with the "staff" / "manager" role; these are per person
MANAGER_PAGES = ("chk_mine", "chk_calendar", "chk_manage")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-password", default="postgres", help="password of the postgres user of your local PostgreSQL")
    ap.add_argument("--db", default="vt_dev")
    ap.add_argument("--port", default="5432")
    ap.add_argument("--reset", action="store_true", help="remove the demo people and everything they own, then stop")
    args = ap.parse_args()
    url = f"postgresql://postgres:{quote(args.pg_password)}@localhost:{args.port}/{args.db}"
    if urlparse(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        sys.exit("Refusing to run: the database is not on this machine.")
    os.environ.update(DATABASE_URL=url, VT_SCHEDULER="off")
    os.environ.setdefault("JWT_SECRET", "local-demo-secret-" + "x" * 32)

    from fastapi.testclient import TestClient

    from app import auth, db, main as app_main
    from app.modcommon import today_ist

    with TestClient(app_main.app) as client:
        if args.reset:
            reset(db)
            print("Demo people, tasks, checklists and plans removed.")
            return
        people = ensure_people(db, auth)
        with db.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM fact_task WHERE staff_user_key = ANY(%s)", ([p["user_key"] for p in people.values()],))
            if cur.fetchone()["n"]:
                sys.exit("The demo people already have tasks - run with --reset first.")
        today = today_ist()
        tok = {name: {"Authorization": "Bearer " + auth.make_token(p)} for name, p in people.items()}
        seed_delegation(client, tok, today)
        seed_checklists(client, tok, today)
        summary(client, tok, today)


def ensure_people(db, auth) -> dict:
    out = {}
    with db.cursor() as cur:
        for username, name, role in [MANAGER] + [(u, n, "staff") for u, n in STAFF]:
            cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, must_change_password) VALUES (%s, %s, %s, %s, false) "
                        "ON CONFLICT (username) DO UPDATE SET role = EXCLUDED.role, display_name = EXCLUDED.display_name, must_change_password = false "
                        "RETURNING user_key, username, role, display_name, must_change_password", (username, auth.hash_password(PASSWORD), role, name))
            u = cur.fetchone()
            out[username] = u
            for page in (MANAGER_PAGES if role == "manager" else STAFF_PAGES):
                cur.execute("INSERT INTO user_page_access (user_key, page_key, allowed) VALUES (%s, %s, true) "
                            "ON CONFLICT (user_key, page_key) DO UPDATE SET allowed = true, view_only = false", (u["user_key"], page))
    return out


def reset(db):
    names = [MANAGER[0]] + [u for u, _ in STAFF]
    with db.cursor() as cur:
        cur.execute("SELECT user_key FROM dim_user WHERE username = ANY(%s)", (names,))
        keys = [r["user_key"] for r in cur.fetchall()]
        if not keys:
            return
        cur.execute("DELETE FROM fact_checklist_occurrence WHERE assignee_user_key = ANY(%s) OR checklist_key IN "
                    "(SELECT checklist_key FROM dim_checklist WHERE assignee_user_key = ANY(%s) OR created_by_user_key = ANY(%s))", (keys, keys, keys))
        cur.execute("DELETE FROM dim_checklist WHERE assignee_user_key = ANY(%s) OR created_by_user_key = ANY(%s)", (keys, keys))
        cur.execute("DELETE FROM fact_task WHERE staff_user_key = ANY(%s) OR assigned_by_user_key = ANY(%s)", (keys, keys))
        cur.execute("DELETE FROM fact_weekly_plan WHERE staff_user_key = ANY(%s)", (keys,))
        cur.execute("DELETE FROM fact_extra_work WHERE user_key = ANY(%s)", (keys,))
        cur.execute("DELETE FROM user_page_access WHERE user_key = ANY(%s)", (keys,))
        cur.execute("DELETE FROM admin_audit_log WHERE admin_user_key = ANY(%s)", (keys,))   # the checklists the manager created were logged
        cur.execute("DELETE FROM dim_user WHERE user_key = ANY(%s)", (keys,))


def ok(r, what):
    """The API answers with a status code, or (delegation) a plain sentence. Anything unexpected stops the seeding loudly."""
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    if r.status_code >= 400:
        sys.exit(f"{what}: HTTP {r.status_code} {body}")
    return body


def seed_delegation(client, tok, today):
    d = lambda n: (today + timedelta(days=n)).isoformat()   # noqa: E731
    mgr = tok["demo_manager"]

    def assign(staff, desc, due):
        msg = ok(client.post("/delegation/tasks", json={"staffId": staff, "desc": desc, "dueDate": d(due)}, headers=mgr), "assign")
        if not str(msg).startswith("Task assigned"):
            sys.exit(f"assign {desc}: {msg}")

    # (staff, description, due offset in days)
    plan = [
        ("demo_asha", "Count and tag the new stock in godown rack B", -6),
        ("demo_asha", "Update the price list for wallpaper rolls", 3),
        ("demo_asha", "Clean and re-arrange the display window", 2),
        ("demo_asha", "Call 10 customers with pending payments", -2),
        ("demo_vikram", "Prepare the Diwali offer banner list", 0),
        ("demo_vikram", "Verify last week's delivery challans", -8),
        ("demo_vikram", "Reconcile the petty cash register", 5),
        ("demo_vikram", "Photograph new arrivals for the catalogue", 1),
        ("demo_neha", "Follow up with the transporter on the lost parcel", -1),
        ("demo_neha", "Update the customer ledger sheet", 4),
        ("demo_neha", "Arrange samples for the architect meeting", -4),
        ("demo_neha", "Stock check of adhesives", 7),
        ("demo_sunil", "Set up the weekly shop-cleaning roster", -3),
        ("demo_sunil", "Collect the cheque from the party", 0),
        ("demo_sunil", "Check the godown fire extinguishers", 10),
        ("demo_sunil", "Re-label the old stock", -12),
    ]
    from app import db
    from app.modcommon import date_key
    for staff, desc, due in plan:
        # the API no longer accepts a deadline in the past, so a task that should already be overdue is assigned for today
        # and its deadline is moved back afterwards
        assign(staff, desc, max(due, 0))
        if due < 0:
            with db.cursor() as cur:
                cur.execute("UPDATE fact_task SET due_date_key = %s WHERE description = %s", (date_key(today + timedelta(days=due)), desc))

    rows = ok(client.get("/delegation/tasks", headers=mgr), "list tasks")
    no = {r["taskDesc"]: r["taskId"] for r in rows}

    def task_no(desc):
        return no[desc]

    def complete(staff, desc, notes=""):
        msg = ok(client.post(f"/delegation/tasks/{task_no(desc)}/complete", json={"notes": notes}, headers=tok[staff]), "complete")
        if "submitted" not in str(msg).lower():
            sys.exit(f"complete {desc}: {msg}")

    def review(desc, status, remark=""):
        msg = ok(client.post(f"/delegation/tasks/{task_no(desc)}/review", json={"status": status, "remark": remark}, headers=mgr), "review")
        if status != "Revise" and "successfully" not in str(msg):
            sys.exit(f"review {desc}: {msg}")

    # --- Asha: one late (approved), one on time (approved), one open, one overdue that gets a new deadline
    complete("demo_asha", "Count and tag the new stock in godown rack B", "Counted 142 rolls, tags done")
    review("Count and tag the new stock in godown rack B", "Approved", "Good, but this was late")
    complete("demo_asha", "Update the price list for wallpaper rolls")
    review("Update the price list for wallpaper rolls", "Approved", "Perfect")
    ok(client.post(f"/delegation/tasks/{task_no('Call 10 customers with pending payments')}/extend",
                   json={"newDueDate": d(3), "remark": "Customers were travelling - new deadline"}, headers=mgr), "extend")
    # --- Vikram: on time -> sent back once -> done again -> approved (yellow); late -> rejected; one waiting for review
    complete("demo_vikram", "Prepare the Diwali offer banner list")
    review("Prepare the Diwali offer banner list", "Revise", "Add the discount % next to each item")
    complete("demo_vikram", "Prepare the Diwali offer banner list", "Added discount % column")
    review("Prepare the Diwali offer banner list", "Approved", "Now it is fine")
    complete("demo_vikram", "Verify last week's delivery challans")
    review("Verify last week's delivery challans", "Rejected", "Three challans were not checked against the invoices")
    complete("demo_vikram", "Photograph new arrivals for the catalogue", "Photos of 18 designs")
    # --- Neha: two approved (one late), the rest open (one overdue)
    complete("demo_neha", "Update the customer ledger sheet")
    review("Update the customer ledger sheet", "Approved")
    complete("demo_neha", "Arrange samples for the architect meeting")
    review("Arrange samples for the architect meeting", "Approved", "Late but the meeting went well")
    # --- Sunil: one late waiting for review, one on time approved, one overdue that was extended and left pending
    complete("demo_sunil", "Set up the weekly shop-cleaning roster", "Roster pinned in the godown")
    complete("demo_sunil", "Collect the cheque from the party")
    review("Collect the cheque from the party", "Approved")
    ok(client.post(f"/delegation/tasks/{task_no('Re-label the old stock')}/extend", json={"newDueDate": d(2), "remark": "More stock came in"}, headers=mgr), "extend")

    # weekly plan (planned share of green / yellow / red) for this week and next, for two people
    from app.delegation import week_bounds
    start, _ = week_bounds(today, 6)
    for staff, plan_ in (("demo_asha", (70, 20, 10)), ("demo_vikram", (60, 30, 10)), ("demo_neha", (50, 30, 20))):
        for ws in (start, start + timedelta(days=7)):
            msg = ok(client.post("/delegation/weekly-plan", json={"staffId": staff, "weekStart": ws.isoformat(), "green": plan_[0],
                                                                   "yellow": plan_[1], "red": plan_[2]}, headers=mgr), "weekly-plan")
            if "saved" not in str(msg):
                sys.exit(f"weekly plan: {msg}")
    for staff, text in (("demo_asha", "Helped unload a truck of 60 cartons"), ("demo_neha", "Covered the cash counter for two hours"),
                        ("demo_sunil", "Delivered an urgent parcel to a customer on the way home")):
        ok(client.post("/delegation/extra-work", json={"desc": text}, headers=tok[staff]), "extra work")


def seed_checklists(client, tok, today):
    d = lambda n: (today + timedelta(days=n)).isoformat()   # noqa: E731
    mgr = tok["demo_manager"]
    lists = [
        dict(title="Open the shop - switch on lights and AC", category="Shop / Godown routine", assignee="demo_asha", frequency="Daily", startDate=d(-20)),
        dict(title="Close the cash register and lock the godown", category="Shop / Godown routine", assignee="demo_sunil", frequency="Daily", startDate=d(-14)),
        dict(title="Weekly stock register check", category="Accounts", assignee="demo_vikram", frequency="Weekly", dueDow=6, startDate=d(-45)),
        dict(title="File the GSTR-3B return", category="GST", assignee="demo_manager", frequency="Monthly", dueDay=20, startDate=d(-70)),
        dict(title="Pay and file the TDS challan", category="TDS / Income Tax", assignee="demo_neha", frequency="Monthly", dueDay=7, startDate=d(-70)),
        dict(title="Fire extinguisher inspection", category="Licences & Renewals", assignee="demo_neha", frequency="Quarterly", dueMonth=1, dueDay=15, startDate=d(-120)),
        dict(title="Shop licence renewal", category="Licences & Renewals", assignee="demo_sunil", frequency="Yearly", dueMonth=3, dueDay=31, startDate=d(-400)),
        dict(title="Submit the PF challan for the month", category="PF / ESI", assignee="demo_manager", frequency="Monthly", dueDay=15, startDate=d(-70)),
        dict(title="Meet the auditor for the half-yearly review", category="Accounts", assignee="demo_neha", frequency="Once", startDate=d(5)),
    ]
    from app import db
    from app.modcommon import date_key
    for body in lists:
        # the API does not accept a start date in the past, so a checklist that should already have history is created from today
        # and its start date is moved back afterwards (this is what makes the overdue / done / not-done history)
        wanted = body["startDate"]
        ok(client.post("/checklist/checklists", json={**body, "startDate": max(wanted, today.isoformat())}, headers=mgr), "create checklist " + body["title"])
        if wanted < today.isoformat():
            with db.cursor() as cur:
                cur.execute("UPDATE dim_checklist SET start_date_key = %s WHERE title = %s", (date_key(date.fromisoformat(wanted)), body["title"]))
    # materialise every occurrence for everyone (the calendar does it), then work through each person's own list
    for m in {(today + timedelta(days=n)).replace(day=1) for n in (-60, -30, 0, 30)}:
        ok(client.get("/checklist/calendar", params={"year": m.year, "month": m.month}, headers=mgr), "calendar")
    for who in ("demo_asha", "demo_sunil", "demo_vikram", "demo_neha", "demo_manager"):
        mine = ok(client.get("/checklist/mine", headers=tok[who]), "mine " + who)
        overdue = mine["overdue"]
        # most overdue items were done, a couple were not (with the reason), the newest ones are left open (they show as overdue)
        for i, it in enumerate(overdue[:-3] if len(overdue) > 4 else overdue[:-1]):
            if i % 7 == 3:
                ok(client.post(f"/checklist/occurrences/{it['key']}/mark", json={"status": "Not Done", "remark": "Staff on leave - done next day"}, headers=tok[who]), "mark")
            else:
                ok(client.post(f"/checklist/occurrences/{it['key']}/mark", json={"status": "Done", "remark": ""}, headers=tok[who]), "mark")
        for it in mine["dueToday"][:1]:          # one of today's items is done, the rest stay open
            ok(client.post(f"/checklist/occurrences/{it['key']}/mark", json={"status": "Done", "remark": "Done on time"}, headers=tok[who]), "mark")


def summary(client, tok, today):
    mgr = tok["demo_manager"]
    print("\n=== Delegation ===")
    tasks = ok(client.get("/delegation/tasks", headers=mgr), "tasks")
    by = {}
    for t in tasks:
        if t["assignedId"].startswith("demo_"):
            by[t["status"]] = by.get(t["status"], 0) + 1
    print("tasks by status:", by)
    print("scoreboard:", [(r["name"], r["totalScore"]) for r in ok(client.get("/delegation/scoreboard", headers=mgr), "scoreboard")
                          if r["id"].startswith("demo_")])
    print("\n=== Checklist ===")
    for c in ok(client.get("/checklist/checklists", headers=mgr), "checklists"):
        if c["assigneeId"].startswith("demo_"):
            print(f"{c['title'][:44]:44} {c['assignee']:14} {c['when'][:28]:28} done={c['done']} not_done={c['notDone']} overdue={c['overdue']}")
    print(f"\nSign in as any of: demo_manager, {', '.join(u for u, _ in STAFF)}   password {PASSWORD}")


if __name__ == "__main__":
    main()
