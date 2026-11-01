"""The messages of the "WhatsApp Template" document: the five dispatch formats (VT Dispatch / Porter / Company / Transport / Receiving groups) and the
automatic report PDFs (7:30 PM and 9:00 PM) sent to the Reports group."""
import json
import re
from datetime import datetime, timedelta

import pytest

from app import attachments, daily_reports, o2d_reports, o2d_screens, whatsapp
from tests.test_modules import FakeStore
from tests.test_order_journey import _dc, _dispatch, _status, people  # noqa: F401  (fixture + helpers)
from tests.test_o2d_screens import add, client, login, seed  # noqa: F401  (fixtures + helpers)


@pytest.fixture()
def wa(monkeypatch):
    monkeypatch.setenv("WHATSAPP_API_KEY", "key-1")
    sent = []

    def fake_post(url, body, headers):
        j = json.loads(body)
        sent.append({"to": j["recipientIds"][0], "text": j["message"][0]})
        return 200, '{"success": true}'

    monkeypatch.setattr(whatsapp, "_http_post", fake_post)
    return sent


def person(db_conn, name, phone=None):
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_person (full_name, phone_number, person_role) VALUES (%s, %s, 'delivery') "
                    "ON CONFLICT (lower(btrim(full_name)), person_role) DO UPDATE SET phone_number = EXCLUDED.phone_number", (name, phone))
    db_conn.commit()


def groups(client, admin, **kw):
    """Type the group IDs of the alerts (blank = none)."""
    for key, group in {"dispatch_godown": "", "dispatch_porter": "", "dispatch_receiving": "", "reports_evening": "", "reports_night": "", **kw}.items():
        r = client.put(f"/admin/whatsapp/{key}", json={"group_id": group, "enabled": True, "template": whatsapp.ALERTS[key]["template"]}, headers=admin)
        assert r.status_code == 200, r.text


def order(**kw):
    return {"orderRcvdDate": "2026-10-10", "dcNo": "94", "readyByWhom": "Jairam", "deliveryStatus": "Sent", "shippingLocation": "Home: H-2/84,85 Sec-16 Rohini",
            "detailedRemarks": "", "deliveredByWhom": "Ranjeet(E-R)", "deliveredByDetail": "", "cartage": 50.0, **kw}


HEAD = ("New Dispatch - Godown\n\nOrder Date: 10/10/2026\nDC No: 94\nReady By: Jairam\nDelivery Status: Sent\n"
        "Address: Home: H-2/84,85 Sec-16 Rohini\nRemarks: -\n")


# ---------------------------------------------------------------- the five dispatch formats
def test_dispatch_message_follows_each_format_of_the_document(client, db_conn, seed):
    person(db_conn, "Ranjeet(E-R)", "8796637988")
    # (1) a delivery person: Cartage, name and phone, Photo
    o = order()
    assert o2d_screens._dispatch_alert_key(o) == "dispatch_godown"
    assert o2d_screens.build_dispatch_message(o) == HEAD + "Cartage: 50\nDelivered By: Ranjeet(E-R) 8796637988"
    assert o2d_screens.build_dispatch_message(order(cartage=None)).endswith("Cartage: -\nDelivered By: Ranjeet(E-R) 8796637988")
    # (2) By Porter: its own group, Cartage, By Porter, the extra text
    o = order(deliveredByWhom="By Porter", deliveredByDetail="Ramesh 9811122233")
    assert o2d_screens._dispatch_alert_key(o) == "dispatch_porter"
    assert o2d_screens.build_dispatch_message(o) == HEAD + "Cartage: 50\nDelivered By: By Porter\nDetails: Ramesh 9811122233"
    # (3) By Company and (4) By Transport: VT Dispatch group, no Cartage, the extra text
    for who, text in (("By Company", "Pitampura branch van"), ("By Transport", "Tempo DL1LA2345")):
        o = order(deliveredByWhom=who, deliveredByDetail=text, cartage=80.0)
        assert o2d_screens._dispatch_alert_key(o) == "dispatch_godown"
        assert o2d_screens.build_dispatch_message(o) == HEAD + f"Delivered By: {who}\nDetails: {text}"
    assert o2d_screens.build_dispatch_message(order(), resend=True).startswith("(Sent again)\nNew Dispatch - Godown")
    for who, kind in (("Transport (Tempo)", "transport"), ("By Porter", "porter"), ("Self", "person"), ("", "person")):
        assert o2d_screens.delivery_kind(who) == kind


def test_dispatch_message_carries_no_photo_link_any_more(client, people, db_conn, monkeypatch):
    h, today = people
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "bkt" if k == "R2_BUCKET" else "x")
    attachments.set_storage(FakeStore())
    try:
        sl = add(client, h["shop"], today, _dc())["slNo"]
        with db_conn.cursor() as cur:
            cur.execute("SELECT order_key FROM fact_orders WHERE sl_no = %s", (sl,))
            key = cur.fetchone()[0]
            cur.execute("INSERT INTO order_attachments (order_key, kind, object_key, content_type, size_bytes) VALUES (%s, 'dispatch', %s, 'image/jpeg', 3), (%s, 'receiving', %s, 'image/jpeg', 3)",
                        (key, f"orders/{key}/dispatch/a.jpg", key, f"orders/{key}/receiving/b.jpg"))
        db_conn.commit()
        links = attachments.photo_links(key, "dispatch")
        assert len(links) == 1 and "dispatch/a.jpg" in links[0] and not attachments.photo_links(key, "nothing")      # only the dispatch photo, not the receiving one
        assert o2d_screens._dispatch_values(order(orderKey=key))["photo"] == ""                        # photos are no longer part of WhatsApp messages
        assert "Photo" not in o2d_screens.build_dispatch_message(order(orderKey=key)) and "r2.cloudflarestorage" not in o2d_screens.build_dispatch_message(order(orderKey=key))
    finally:
        attachments.set_storage(None)


# ---------------------------------------------------------------- who gets it
def dispatch_new(client, h, who, role="godown_dispatch", status="Godown to Dispatch", **extra):
    today = o2d_screens.today_ist()
    sl = add(client, h["shop"], today, _dc())["slNo"]
    _status(client, h, sl, status)
    _dispatch(client, h, role, sl, deliveredByWhom=who, **extra)
    return sl


def test_first_dispatch_goes_to_vt_dispatch_or_porter_group_and_not_to_the_receiving_group(client, people, db_conn, wa):
    h, _ = people
    person(db_conn, "Amit", "9811000111")
    person(db_conn, "By Porter")
    person(db_conn, "By Company")
    groups(client, h["admin"], dispatch_godown="VT@g.us", dispatch_porter="PORTER@g.us", dispatch_receiving="RECV@g.us")

    dispatch_new(client, h, "Amit", cartage=40)
    assert [s["to"] for s in wa] == ["VT@g.us"]                                                   # the Receiving group hears at receiving, not now
    assert "Cartage: 40" in wa[0]["text"] and "Delivered By: Amit 9811000111" in wa[0]["text"]
    wa.clear()
    dispatch_new(client, h, "By Porter", cartage=60, deliveredByDetail="Ramesh 9811122233")
    assert [s["to"] for s in wa] == ["PORTER@g.us"] and "Details: Ramesh 9811122233" in wa[0]["text"] and "Cartage: 60" in wa[0]["text"]
    wa.clear()
    dispatch_new(client, h, "By Company", deliveredByDetail="Pitampura van")
    assert [s["to"] for s in wa] == ["VT@g.us"] and "Cartage" not in wa[0]["text"] and "Details: Pitampura van" in wa[0]["text"]

    # no Porter group typed: the Porter messages still reach the VT Dispatch group
    groups(client, h["admin"], dispatch_godown="VT@g.us")
    wa.clear()
    dispatch_new(client, h, "By Porter", deliveredByDetail="Suresh")
    assert [s["to"] for s in wa] == ["VT@g.us"]


def test_the_dispatch_message_is_sent_once_and_the_receiving_group_hears_at_receiving(client, people, db_conn, wa):
    h, today = people
    person(db_conn, "Amit", "9811000111")
    groups(client, h["admin"], dispatch_godown="VT@g.us", dispatch_receiving="RECV@g.us")
    sl = dispatch_new(client, h, "Amit")
    assert [s["to"] for s in wa] == ["VT@g.us"]
    wa.clear()
    _dispatch(client, h, "godown_dispatch", sl, deliveredByWhom="Amit", cartage=99)               # saved again: nothing new goes out
    assert wa == []
    r = client.post(f"/o2d/orders/{sl}/whatsapp-resend", headers=h["godown_dispatch"]).json()
    assert r["success"] and [s["to"] for s in wa] == ["VT@g.us"] and wa[0]["text"].startswith("(Sent again)")
    wa.clear()
    # Godown Receiving: the entry that completes the receiving tells the Receiving group - once
    r = client.put(f"/o2d/orders/{sl}/receiving", headers=h["receiving"], json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid", "amountReceived": 1250})
    assert r.json()["success"], r.text
    assert [s["to"] for s in wa] == ["RECV@g.us"]
    text = wa[0]["text"]
    assert text.startswith("Order Received") and "Payment Mode: Paid" in text and "Amount: 12.50" in text and "Entered By:" in text and "DC No:" in text
    wa.clear()
    client.put(f"/o2d/orders/{sl}/receiving", headers=h["receiving"], json={"dateOfReceiving": today.isoformat(), "paymentStatus": "Paid", "amountReceived": 1300})
    assert wa == []                                                                              # corrected later: not announced again
    # Shop Dispatch & Receiving: nothing at dispatch; the receiving details saved on the same screen tell the Receiving group
    sl2 = dispatch_new(client, h, "Amit", role="shop_dispatch", status="Dispatch for Shop")
    assert wa == []
    _dispatch(client, h, "shop_dispatch", sl2, deliveredByWhom="Amit", dateOfReceiving=today.isoformat(), paymentStatus="Paid", amountReceived=500)
    assert [s["to"] for s in wa] == ["RECV@g.us"] and wa[0]["text"].startswith("Order Received")


def test_a_sent_to_the_company_dispatch_is_also_posted_to_the_porter_group(client, people, db_conn, wa):
    h, _ = people
    person(db_conn, "Amit", "9811000111")
    groups(client, h["admin"], dispatch_godown="VT@g.us", dispatch_porter="PORTER@g.us")
    dispatch_new(client, h, "Amit", status="Sent to the Company")
    assert [s["to"] for s in wa] == ["VT@g.us", "PORTER@g.us"] and wa[0]["text"] == wa[1]["text"]
    wa.clear()
    dispatch_new(client, h, "Amit", status="Godown to Dispatch")
    assert [s["to"] for s in wa] == ["VT@g.us"]                                                  # any other status: the VT Dispatch group only


def test_photos_no_longer_hold_the_message_back_or_ride_in_it(client, people, db_conn, wa):
    h, today = people
    person(db_conn, "Amit", "9811000111")
    groups(client, h["admin"], dispatch_godown="VT@g.us", dispatch_receiving="RECV@g.us")
    sl = add(client, h["shop"], today, _dc())["slNo"]
    _status(client, h, sl, "Godown to Dispatch")
    body = {"materialDeliveryDateTime": today.isoformat() + "T10:00", "deliveredByWhom": "Amit", "photosFollow": True}
    r = client.put(f"/o2d/orders/{sl}/dispatch", headers=h["godown_dispatch"], json=body).json()
    assert r["success"] and not r.get("alertPending") and [s["to"] for s in wa] == ["VT@g.us"]       # sent straight away, whatever the page says about photos
    assert "Photo" not in wa[0]["text"]


# ---------------------------------------------------------------- the report PDFs
def titles(*kinds):
    return [o2d_reports.REPORTS[k]["title"] for k in kinds]


def test_evening_reports_go_to_the_reports_group_with_working_links(client, people, db_conn, wa, monkeypatch):
    h, today = people
    person(db_conn, "Amit", "9811000111")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://vt.example.com/")
    groups(client, h["admin"], reports_evening="REPORTS@g.us")
    dispatch_new(client, h, "Amit")
    now = datetime.now(daily_reports.IST)
    persons, stored = daily_reports.generate_batch(today, now, "evening")
    assert any(p["person"] == "Amit" for p in persons)
    daily_reports.send_reports(today, "evening", stored)
    assert [s["to"] for s in wa] == ["REPORTS@g.us"]
    text = wa[0]["text"]
    assert text.startswith("*Reports - 7:30 PM*") and today.strftime("%d-%m-%Y") in text
    # the document's order: each delivery person, then Godown cash, Shop cash
    order_of = [text.index(t) for t in ["Delivery - Amit"] + titles("godown-received-cash", "shop-received-cash")]
    assert order_of == sorted(order_of)
    assert "Awaiting" not in text and "Pending Orders" not in text                                   # the 9:00 PM reports are not in this message
    link = re.search(r"https://vt\.example\.com(/o2d/daily-reports/shared/\d+\.pdf\?exp=\d+&sig=[0-9a-f]+)", text).group(1)
    pdf = client.get(link)                                                                           # no login needed
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and "inline" in pdf.headers["content-disposition"]
    assert client.get(link.replace("sig=", "sig=0")).status_code == 403                              # a changed signature
    assert client.get(re.sub(r"/shared/(\d+)", lambda m: f"/shared/{int(m.group(1)) + 1000}", link)).status_code == 403     # the signature is for one report only
    assert client.get(re.sub(r"exp=\d+", "exp=1", link)).status_code == 403                          # expired
    assert client.get("/o2d/daily-reports/shared/1.pdf?exp=9999999999&sig=abc").status_code == 403
    assert re.search(r"Delivery - Amit \(\d+ orders?\)", text)


def test_night_reports_and_the_group_fallback(client, people, db_conn, wa, monkeypatch):
    h, today = people
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://vt.example.com")
    groups(client, h["admin"], reports_evening="REPORTS@g.us")
    now = datetime.now(daily_reports.IST)
    _, stored = daily_reports.generate_batch(today, now, "night")
    assert {r["kind"] for r in stored} == set(daily_reports.BATCHES["night"]["kinds"]) and all(r["kind"] for r in stored)
    daily_reports.send_reports(today, "night", stored)
    assert [s["to"] for s in wa] == ["REPORTS@g.us"]                                                 # no 9:00 PM group typed: the Reports group
    text = wa[0]["text"]
    assert text.startswith("*Reports - 9:00 PM*")
    order_of = [text.index(t) for t in titles("receiving-pending", "godown-pending", "shop-dispatch-pending", "godown-dispatch-pending")]
    assert order_of == sorted(order_of) and "Received & Payments" not in text
    wa.clear()
    groups(client, h["admin"], reports_evening="REPORTS@g.us", reports_night="NIGHT@g.us")
    daily_reports.send_reports(today, "night", stored)
    assert [s["to"] for s in wa] == ["NIGHT@g.us"]


def test_without_a_public_address_the_message_still_lists_the_reports_and_a_long_list_is_split(client, people, db_conn, wa, monkeypatch):
    h, today = people
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    groups(client, h["admin"], reports_evening="REPORTS@g.us")
    now = datetime.now(daily_reports.IST)
    _, stored = daily_reports.generate_batch(today, now, "evening")
    daily_reports.send_reports(today, "evening", stored)
    assert len(wa) == 1 and "http" not in wa[0]["text"] and "O2D > Reports" in wa[0]["text"] and titles("godown-received-cash")[0] in wa[0]["text"]
    wa.clear()
    monkeypatch.setattr(daily_reports, "MESSAGE_LIMIT", 120)
    daily_reports.send_reports(today, "evening", stored)
    assert len(wa) > 1 and all(s["to"] == "REPORTS@g.us" and s["text"].startswith("*Reports - 7:30 PM*") for s in wa)
    joined = "\n".join(s["text"] for s in wa)
    assert all(t in joined for t in titles("godown-received-cash", "shop-received-cash"))


def test_both_batches_run_once_each_day_and_message_the_group(client, people, db_conn, wa, monkeypatch):
    h, _ = people
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://vt.example.com")
    groups(client, h["admin"], reports_evening="REPORTS@g.us")
    day = datetime.now(daily_reports.IST).replace(year=datetime.now().year + 4, hour=18, minute=0)           # a day nothing has run for
    assert daily_reports.run_if_due(day) is False and wa == []
    assert daily_reports.run_if_due(day.replace(hour=19, minute=31)) is True
    assert [s["text"].split("\n")[0] for s in wa] == ["*Reports - 7:30 PM* - Code Blue"]
    assert daily_reports.run_if_due(day.replace(hour=20, minute=30)) is False                          # 9:00 PM is not here yet
    assert daily_reports.run_if_due(day.replace(hour=21, minute=1)) is True
    assert [s["text"].split("\n")[0] for s in wa] == ["*Reports - 7:30 PM* - Code Blue", "*Reports - 9:00 PM* - Code Blue"]
    assert daily_reports.run_if_due(day.replace(hour=23, minute=0)) is False and len(wa) == 2         # each batch once a day
    with db_conn.cursor() as cur:
        cur.execute("SELECT batch FROM daily_report_run WHERE report_date = %s ORDER BY batch", (day.date(),))
        assert [r[0] for r in cur.fetchall()] == ["evening", "night"]
