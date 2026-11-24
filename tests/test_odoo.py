"""The Odoo integration, against a stand-in Odoo seeded from data/odoo_test_data.json (no network): the 3-way check, the
code guide's 5 test cases, the 14 samples, an Odoo that is down or refuses a bill, a person's approval, checking again,
and the switch that keeps other accounts on the CSVs."""
import copy
import json
import time

import pytest

from app import config, main, odoo, odoo_trail, rules
from test_accounts import clean, client, run_sample  # noqa: F401  (clean is an autouse fixture)

DATA = json.loads((config.DATA_DIR / "odoo_test_data.json").read_text())
ODOO_CASES = [m["file"] for m in json.loads((config.SAMPLES_DIR / "odoo" / "manifest.json").read_text())]
SAMPLES = json.loads((config.SAMPLES_DIR / "synthetic" / "manifest.json").read_text())


class FakeOdoo:
    """What app.odoo returns, from the test data. Creating a bill raises qty_invoiced on the PO lines it is linked to,
    as Odoo does (draft bills included)."""

    def __init__(self):
        self.down = self.refuse = False
        self.bills, self.partners, self.pos = [], {}, []
        for i, v in enumerate(DATA["vendors"]):
            self.partners[v["gstin"]] = dict(id=100 + i, name=v["name"], vat=v["gstin"], blocked=bool(v.get("blocked")))
        line_id = 1000
        for i, po in enumerate(DATA["purchase_orders"]):
            lines = []
            for ln in po["lines"]:
                line_id += 1
                lines.append(dict(id=line_id, order_id=[500 + i, po["name"]], name=ln["product"], product_id=[2000 + line_id, ln["product"]],
                                  product_qty=float(ln["qty"]), qty_received=float(ln["received"]), qty_invoiced=0.0, price_unit=float(ln["rate"]), tax_ids=[]))
            net = sum(ln["product_qty"] * ln["price_unit"] for ln in lines)
            p = self.partners[po["vendor"]]
            self.pos.append(dict(id=500 + i, name=po["name"], partner_id=[p["id"], p["name"]], date_order=f"{po['date']} 10:00:00", state="purchase",
                                 amount_untaxed=net, amount_tax=round(net * po["gst"] / 100, 2), lines=lines, order_line=[ln["id"] for ln in lines]))

    def _up(self):
        if self.down:
            raise odoo.OdooUnreachable("Odoo didn't answer (ConnectTimeout)")

    def line(self, po_name, n=0):
        return next(p for p in self.pos if p["name"] == po_name)["lines"][n]

    def vendors(self):
        self._up()
        return [dict(v) for v in self.partners.values()]

    def purchase_orders(self):
        self._up()
        return copy.deepcopy(self.pos)

    def bill_exists(self, partner_id, invoice_no):
        self._up()
        return any(b["partner_id"] == partner_id and b["ref"] == invoice_no for b in self.bills)

    def create_draft_bill(self, partner_id, invoice_no, invoice_date, lines, move_type="in_invoice", due_date=None):
        self._up()
        if self.refuse:
            raise odoo.OdooError("Odoo rejected account.move.create (HTTP 422): the accounting period is locked")
        for ln in lines:
            if ln.get("po_line"):
                for po in self.pos:
                    for pl in po["lines"]:
                        if pl["id"] == ln["po_line"]["id"]:
                            pl["qty_invoiced"] += ln["quantity"]
        self.bills.append(dict(id=len(self.bills) + 1, partner_id=partner_id, ref=invoice_no, date=invoice_date, lines=lines,
                               state="draft", payment_state="not_paid"))
        return len(self.bills)

    # the audit trail and the contacts (phases 1 to 4)
    def po_by_name(self, name):
        self._up()
        return next((dict(id=p["id"], name=p["name"]) for p in self.pos if p["name"] == name), None)

    def upsert_contact(self, ref, name, details_html, email=None):
        self._up()
        found = next((c for c in self.contacts if c["ref"] == ref), None)
        if not found:
            found = dict(id=900 + len(self.contacts), ref=ref, name=name, email=email, tags=["Invoice Flow User"], comment=details_html)
            self.contacts.append(found)
        return found["id"]

    def attach(self, model, res_id, filename, data):
        self._up()
        self.attachments.append(dict(id=700 + len(self.attachments), model=model, res_id=res_id, name=filename, size=len(data)))
        return self.attachments[-1]["id"]

    def note(self, model, res_id, html, attachment_ids=()):
        self._up()
        if self.refuse_notes:
            raise odoo.OdooError("Odoo rejected purchase.order.message_post (HTTP 403): no access")
        self.notes.append(dict(id=800 + len(self.notes), model=model, res_id=res_id, html=html, attachments=list(attachment_ids)))
        return self.notes[-1]["id"]

    def bill_states(self, ids):
        self._up()
        return {b["id"]: (b["state"], b["payment_state"]) for b in self.bills if b["id"] in ids}


@pytest.fixture
def fake(monkeypatch):
    f = FakeOdoo()
    f.contacts, f.attachments, f.notes, f.refuse_notes = [], [], [], False
    for name in ("vendors", "purchase_orders", "bill_exists", "create_draft_bill", "po_by_name", "upsert_contact", "attach", "note", "bill_states"):
        monkeypatch.setattr(odoo, name, getattr(f, name))
    monkeypatch.setattr(odoo_trail, "_last_refresh", {})
    monkeypatch.setattr(odoo_trail, "_tried", {})
    for k, v in dict(ODOO_URL="https://odoo.test", ODOO_DB="test", ODOO_API_KEY="key", ODOO_ACCOUNTS="").items():
        monkeypatch.setenv(k, v)
    return f


def run(c, name):
    rid = run_sample(c, name)
    return c.get(f"/api/runs/{rid}").json()


def codes(r):
    return {x["code"] for x in r["result"]["findings"]}


def wait(rid):
    for _ in range(600):
        if main.BUS[rid]["done"]:
            return
        time.sleep(0.02)
    raise TimeoutError(rid)


# ------------------------------------------------------------ the 3-way check --

LINE = dict(name="Chicken Curry Cut", product_qty=50, qty_received=50, qty_invoiced=0, price_unit=180)


@pytest.mark.parametrize("line, qty, rate, expect", [
    (LINE, 50, 180, set()),                                                   # all received, at the PO rate
    (LINE, 50, 183.6, set()),                                                 # 2% over the rate: inside the tolerance
    (LINE, 50, 160, set()),                                                   # below the PO rate is fine
    (dict(LINE, product_qty=40, qty_received=30), 40, 180, {"QTY_NOT_RECEIVED"}),   # PO B: 40 ordered, 30 received
    (dict(LINE, qty_received=40, qty_invoiced=20), 25, 180, {"QTY_NOT_RECEIVED"}),  # within the PO, but only 20 received and unbilled
    (dict(LINE, qty_invoiced=20), 40, 180, {"QTY_OVER_PO"}),                  # 20 billed, so the PO allows 30 more: 40 is 33% beyond
    (LINE, 60, 180, {"QTY_OVER_PO"}),                                         # 20% beyond the PO itself
    (dict(LINE, qty_invoiced=50), 50, 180, {"QTY_OVER_PO"}),                  # the whole line was billed already
    (LINE, 50, 190.8, {"RATE_ABOVE_PO"}),                                     # 6% over the rate: a person decides
    (dict(LINE, price_unit=190), 20, 220, {"RATE_OVER_PO"}),                  # PO C: 16% over, beyond the 10% band
])
def test_three_way_check(line, qty, rate, expect):
    res, fs = rules.three_way_check(line, qty, rate)
    blocking = {x["code"] for x in fs if x["severity"] in ("hold", "reject")}
    assert blocking == expect
    assert (not expect) == any(x["code"] == "THREE_WAY_OK" for x in fs)
    severity = {"QTY_NOT_RECEIVED": "hold", "RATE_ABOVE_PO": "hold", "QTY_OVER_PO": "reject", "RATE_OVER_PO": "reject"}
    assert all(x["severity"] == severity[x["code"]] for x in fs if x["code"] in severity)


def test_lines_pair_by_description_and_lump_sums_count_whole_units():
    po = dict(po_number="PO-2026-1004", lines=[dict(id=1, name="Housekeeping services - October 2026", price_unit=90000),
                                                dict(id=2, name="Housekeeping services - November 2026", price_unit=60000)])
    f = dict(line_items=[dict(description="Housekeeping services - November 2026 (balance)", quantity=1, unit_price=60000, amount=60000),
                         dict(description="Additional deep-cleaning - common areas", quantity=1, unit_price=12000, amount=12000)])
    pairs, fs = rules.pair_lines(f, dict(net=72000), po)
    assert [(p["po_line"]["id"], p["qty"], p["rate"]) for p in pairs] == [(2, 1.0, 60000.0)]
    assert [x["code"] for x in fs] == ["LINE_NOT_ON_PO"] and fs[0]["item"]["description"].startswith("Additional")
    one = dict(po_number="PO-2026-1002", lines=[dict(id=9, name="Annual maintenance contract - laptops", price_unit=250000)])
    pairs, _ = rules.pair_lines(dict(line_items=[dict(description="AMC - Q1 instalment", amount=295000)]), dict(net=250000), one)
    assert (pairs[0]["qty"], pairs[0]["rate"]) == (1.0, 250000.0)            # 295,000 incl. GST is one unit of 2,50,000


# ------------------------------------------------------- the guide's 5 cases --

def test_the_guide_five_cases(fake):
    c = client("asha")
    r = [run(c, name) for name in ODOO_CASES]
    assert [x["decision"] for x in r] == ["APPROVE", "HOLD", "REJECT", "REJECT", "HOLD"]
    assert "THREE_WAY_OK" in codes(r[0]) and "QTY_NOT_RECEIVED" in codes(r[1]) and "RATE_OVER_PO" in codes(r[2])
    assert "DUPLICATE_EXACT" in codes(r[3]) and "PO_NOT_FOUND" in codes(r[4])
    # case 1 became a draft bill in Odoo, linked to PO A's line, and the run shows it
    assert len(fake.bills) == 1
    bill = fake.bills[0]
    assert (bill["ref"], bill["date"], bill["partner_id"]) == ("DPF-2001", "2026-11-24", fake.partners["36AAJFD4521K1ZE"]["id"])
    assert [(ln["po_line"]["id"], ln["quantity"], ln["price_unit"]) for ln in bill["lines"]] == [(fake.line("P00001")["id"], 50.0, 180.0)]
    assert r[0]["result"]["erp"]["bill_id"] == 1 and r[0]["result"]["erp"]["url"].endswith("/odoo/action-account.action_move_in_invoice_type/1")
    assert any(e["stage"] == "erp" and e["status"] == "ok" for e in main.BUS[r[0]["id"]]["events"])
    assert fake.line("P00001")["qty_invoiced"] == 50                        # Odoo now counts the 50 kg as billed
    # nothing else was posted: held and rejected invoices never reach Odoo
    assert all(x["result"].get("erp") is None for x in r[1:])


# in Odoo, PO-2026-1004's housekeeping hasn't been received (the roadmap's M1 case), so its two invoices wait for that
ODOO_EXPECT = {"04_split_invoice_1_of_2.pdf": "HOLD", "05_split_invoice_2_of_2.pdf": "HOLD"}


def test_the_14_samples_decide_the_same_against_odoo(fake):
    c = client("asha")
    got = {m["file"]: run(c, m["file"])["decision"] for m in SAMPLES}
    assert got == {m["file"]: ODOO_EXPECT.get(m["file"], m["expected"]) for m in SAMPLES}
    approved = [f for f, d in got.items() if d == "APPROVE"]
    assert len(fake.bills) == len(approved) == 5
    assert all(ln.get("po_line") for b in fake.bills for ln in b["lines"])  # every bill line linked to a PO line
    assert [ln["qty_invoiced"] for ln in next(p for p in fake.pos if p["name"] == "PO-2026-1004")["lines"]] == [0.0, 0.0]


def test_roadmap_m1_received_goods_approve_and_missing_goods_hold(fake):
    c = client("asha")
    received = run(c, "01_happy_path_clean_exact.pdf")                       # PO-2026-1001: everything received
    assert received["decision"] == "APPROVE" and received["result"]["po"]["po_number"] == "PO-2026-1001"
    missing = run(c, "04_split_invoice_1_of_2.pdf")                          # PO-2026-1004: nothing received yet
    assert missing["decision"] == "HOLD" and missing["result"]["po"]["po_number"] == "PO-2026-1004"
    reasons = [x for x in missing["result"]["findings"] if x["severity"] == "hold"]
    assert [x["code"] for x in reasons] == ["QTY_NOT_RECEIVED"] and reasons[0]["message"].startswith("Goods not received.")
    assert rules.CODE_LABEL["QTY_NOT_RECEIVED"] == "Goods not received" and "Goods not received" in missing["result"]["headline"]
    # M2: the approved one is a draft bill linked to its PO lines, with the PDF attached
    assert fake.bills[0]["ref"] == "SOS/26-27/0412" and all(ln.get("po_line") for ln in fake.bills[0]["lines"])
    assert fake.notes[0]["model"] == "account.move" and fake.notes[0]["attachments"]


# ---------------------------------------------- when Odoo is down or says no --

def test_odoo_down_holds_and_never_approves(fake):
    fake.down = True
    r = run(client("asha"), ODOO_CASES[0])
    assert r["decision"] == "HOLD" and "ERP_UNREACHABLE" in codes(r) and "ERP unreachable, retry" in r["result"]["headline"]
    assert not fake.bills


def test_a_refused_bill_holds_the_invoice(fake):
    fake.refuse = True
    r = run(client("asha"), ODOO_CASES[0])
    assert r["decision"] == "HOLD" and "ERP_POST_FAILED" in codes(r) and not fake.bills
    assert not main.db.ledger_for("P00001", main.db.find_user("asha")["id"])  # nothing on the local ledger either


def test_a_person_approving_a_held_invoice_posts_the_same_bill(fake):
    c = client("asha")
    r = run(c, ODOO_CASES[1])                                                # 40 kg billed, 30 received: held
    assert r["decision"] == "HOLD"
    fake.refuse = True                                                       # Odoo says no: nothing is approved
    resp = c.post(f"/api/runs/{r['id']}/override", json=dict(decision="APPROVE", note="vendor confirmed the backorder"))
    assert resp.status_code == 502 and "stays on hold" in resp.json()["detail"]
    assert c.get(f"/api/runs/{r['id']}").json()["final_decision"] is None
    fake.refuse = False
    assert c.post(f"/api/runs/{r['id']}/override", json=dict(decision="APPROVE")).status_code == 200
    after = c.get(f"/api/runs/{r['id']}").json()
    assert after["final_decision"] == "APPROVE" and after["result"]["erp"]["bill_id"] == 1
    assert [(ln["quantity"], ln["price_unit"]) for ln in fake.bills[0]["lines"]] == [(40.0, 420.0)]


def test_check_again_after_the_goods_arrive(fake):
    c = client("asha")
    r = run(c, ODOO_CASES[1])
    assert r["decision"] == "HOLD"
    fake.line("P00002")["qty_received"] = 40                                 # the backorder is received in Odoo
    assert c.post(f"/api/runs/{r['id']}/recheck").status_code == 200
    wait(r["id"])
    again = c.get(f"/api/runs/{r['id']}").json()
    assert again["decision"] == "APPROVE" and again["result"]["erp"]["bill_id"] == 1
    assert c.post(f"/api/runs/{r['id']}/recheck").status_code == 400         # only held invoices are checked again


# ------------------------------------------------ the switch, and Odoo's data --

def test_only_listed_accounts_use_odoo(fake, monkeypatch):
    monkeypatch.setenv("ODOO_ACCOUNTS", "asha")
    asha, ravi = client("asha"), client("ravi")
    assert asha.get("/api/config").json()["erp"]["db"] == "test" and ravi.get("/api/config").json()["erp"] is None
    assert ODOO_CASES[0] in {s["file"] for s in asha.get("/api/samples").json()["samples"]}
    assert ODOO_CASES[0] not in {s["file"] for s in ravi.get("/api/samples").json()["samples"]}
    assert ravi.post("/api/runs/sample", json=dict(files=[ODOO_CASES[0]])).status_code == 404
    r = run(ravi, SAMPLES[0]["file"])                                        # the built-in data, exactly as before
    assert r["decision"] == "APPROVE" and r["result"].get("erp") is None and not fake.bills


def test_vendors_and_pos_come_from_odoo(fake):
    c = client("asha")
    data = c.get("/api/pos").json()
    assert data["erp"]["db"] == "test" and len(data["pos"]) == len(DATA["purchase_orders"])
    assert {v["name"]: v["status"] for v in data["vendors"]}["Kiran Courier Services"] == "blocked"   # the Blocked tag
    assert "lines" not in data["pos"][0]
    for path, body in [("/api/vendors", dict(name="New Vendor")), ("/api/pos", dict(po_number="PO-1", vendor_id="OD100"))]:
        resp = c.post(path, json=body)
        assert resp.status_code == 400 and "come from Odoo" in resp.json()["detail"]
    fake.down = True
    assert c.get("/api/pos").status_code == 502


def test_without_settings_nothing_changes(fake, monkeypatch):
    monkeypatch.setenv("ODOO_API_KEY", "")
    c = client("asha")
    assert c.get("/api/config").json()["erp"] is None
    r = run(c, SAMPLES[0]["file"])
    assert r["decision"] == "APPROVE" and "erp" not in r["result"] and not fake.bills


# ------------------------------------------- the 5-phase workflow: users, audit trail, status back --

def test_phase1_an_account_becomes_a_tagged_contact_once(fake):
    c = client("asha")
    uid = main.db.find_user("asha")["id"]
    for _ in range(100):                                                     # signing up syncs in the background
        if main.db.user_info(uid)["odoo_partner_id"]:
            break
        time.sleep(0.02)
    odoo_trail._tried.clear()
    pid = odoo_trail.sync_user(uid) or main.db.user_info(uid)["odoo_partner_id"]
    assert len(fake.contacts) == 1 and fake.contacts[0]["ref"] == "invoice-flow:asha" and "Invoice Flow User" in fake.contacts[0]["tags"]
    assert main.db.user_info(uid)["odoo_partner_id"] == pid == fake.contacts[0]["id"]
    assert c.get("/api/config").json()["erp"]["contact"].endswith(f"/odoo/res.partner/{pid}")
    assert c.post("/api/login", json=dict(username="asha", password="correct horse")).status_code == 200
    time.sleep(0.1)
    assert len(fake.contacts) == 1                                           # signing in again never makes a second one


def test_phase2_an_approved_bill_gets_the_pdf_and_a_note(fake):
    c = client("asha")
    r = run(c, ODOO_CASES[0])
    assert r["decision"] == "APPROVE"
    (n,) = fake.notes
    assert (n["model"], n["res_id"]) == ("account.move", 1) and n["attachments"] == [fake.attachments[0]["id"]]
    assert fake.attachments[0]["name"] == ODOO_CASES[0] and fake.attachments[0]["size"] > 1000
    assert "Invoice DPF-2001 approved" in n["html"] and "asha" in n["html"] and "3-way match" in n["html"]
    assert r["odoo_trail"][0]["target"]["label"] == "draft bill #1"
    row = next(x for x in c.get("/api/runs").json()["runs"] if x["id"] == r["id"])
    assert row["odoo"]["bill_id"] == 1 and row["odoo"]["status"] == "draft"


def test_phase3_held_and_rejected_invoices_are_noted_on_their_po(fake):
    c = client("asha")
    held, rejected = run(c, ODOO_CASES[1]), run(c, ODOO_CASES[2])
    assert (held["decision"], rejected["decision"]) == ("HOLD", "REJECT") and not fake.bills
    po = {p["name"]: p["id"] for p in fake.pos}
    assert [(n["model"], n["res_id"]) for n in fake.notes] == [("purchase.order", po["P00002"]), ("purchase.order", po["P00003"])]
    assert "Invoice GFF-3101 put on hold" in fake.notes[0]["html"] and "Invoice GFF-3102 rejected" in fake.notes[1]["html"]
    assert all(n["attachments"] for n in fake.notes) and "Decision recorded by <b>asha</b>" in fake.notes[1]["html"]
    nopo = run(c, ODOO_CASES[4])                                             # the PO isn't in Odoo: noted on the vendor
    assert nopo["decision"] == "HOLD" and fake.notes[-1]["model"] == "res.partner"
    # checked again with the same answer: no second note; a person's decision: a note of its own
    assert c.post(f"/api/runs/{held['id']}/recheck").status_code == 200
    wait(held["id"])
    assert len(fake.notes) == 3
    assert c.post(f"/api/runs/{held['id']}/override", json=dict(decision="REJECT", note="goods short, vendor told")).status_code == 200
    assert fake.notes[-1]["res_id"] == po["P00002"] and "rejected" in fake.notes[-1]["html"] and "goods short, vendor told" in fake.notes[-1]["html"]
    assert [t["by_person"] for t in c.get(f"/api/runs/{held['id']}").json()["odoo_trail"]] == [False, True]


def test_phase4_posted_and_paid_come_back_from_odoo(fake):
    c = client("asha")
    r = run(c, ODOO_CASES[0])
    status = lambda: next(x for x in c.get("/api/runs").json()["runs"] if x["id"] == r["id"])["odoo"]["status"]
    for state, pay, shown in [("posted", "not_paid", "posted"), ("posted", "in_payment", "in_payment"), ("posted", "paid", "paid")]:
        fake.bills[0].update(state=state, payment_state=pay)
        odoo_trail._last_refresh.clear()
        assert status() == shown
    fake.bills[0].update(state="draft")                                      # paid is final: no more reads for it
    odoo_trail._last_refresh.clear()
    assert status() == "paid" and main.db.open_bills() == []


def test_the_record_never_changes_a_decision(fake):
    fake.refuse_notes = True
    r = run(client("asha"), ODOO_CASES[0])
    assert r["decision"] == "APPROVE" and r["result"]["erp"]["bill_id"] == 1
    assert "didn't take the note" in r["odoo_trail"][0]["error"]
    fake.refuse_notes, fake.down = False, True
    down = run(client("ravi"), ODOO_CASES[0])
    assert down["decision"] == "HOLD" and "didn't answer" in down["odoo_trail"][0]["error"] and len(fake.notes) == 0


def test_xmlrpc_sends_the_same_calls(monkeypatch):
    """ODOO_PROTOCOL=xmlrpc: authenticate once, then execute_kw with the ids first and the rest by name."""
    sent = []

    class Proxy:
        def __init__(self, url, **kw):
            self.url = url

        def authenticate(self, db, login, key, ctx):
            sent.append(("authenticate", db, login))
            return 7

        def execute_kw(self, *a):
            sent.append(a)
            return [1]

    monkeypatch.setattr(odoo.xmlrpc.client, "ServerProxy", Proxy)
    monkeypatch.setattr(odoo, "_uid", {})
    for k, v in dict(ODOO_URL="https://odoo.test", ODOO_DB="db", ODOO_API_KEY="key", ODOO_USER="me@x.test", ODOO_PROTOCOL="xmlrpc").items():
        monkeypatch.setenv(k, v)
    odoo.note("purchase.order", 5, "<p>hi</p>")
    odoo._call("res.partner", "search_read", domain=[["vat", "=", "X"]], fields=["id"])
    odoo._call("res.partner", "create", vals_list=[{"name": "A"}])
    assert sent[3][4:7] == ("create", [[{"name": "A"}]], {})                   # create takes its records positionally
    assert sent[0] == ("authenticate", "db", "me@x.test") and len(sent) == 4   # one login for every call
    assert sent[1][:6] == ("db", 7, "key", "purchase.order", "message_post", [[5]]) and sent[1][6]["body_is_html"] is True
    assert sent[2][3:7] == ("res.partner", "search_read", [], dict(domain=[["vat", "=", "X"]], fields=["id"]))
    assert odoo.info()["protocol"] == "XML-RPC"