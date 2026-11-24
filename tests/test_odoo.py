"""The Odoo integration, against a stand-in Odoo seeded from data/odoo_test_data.json (no network): the 3-way check, the
code guide's 5 test cases, the 14 samples, an Odoo that is down or refuses a bill, a person's approval, checking again,
and the switch that keeps other accounts on the CSVs."""
import copy
import json
import time

import pytest

from app import config, main, odoo, rules
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

    def create_draft_bill(self, partner_id, invoice_no, invoice_date, lines):
        self._up()
        if self.refuse:
            raise odoo.OdooError("Odoo rejected account.move.create (HTTP 422): the accounting period is locked")
        for ln in lines:
            if ln.get("po_line"):
                for po in self.pos:
                    for pl in po["lines"]:
                        if pl["id"] == ln["po_line"]["id"]:
                            pl["qty_invoiced"] += ln["quantity"]
        self.bills.append(dict(id=len(self.bills) + 1, partner_id=partner_id, ref=invoice_no, date=invoice_date, lines=lines))
        return len(self.bills)


@pytest.fixture
def fake(monkeypatch):
    f = FakeOdoo()
    for name in ("vendors", "purchase_orders", "bill_exists", "create_draft_bill"):
        monkeypatch.setattr(odoo, name, getattr(f, name))
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
    assert r[0]["result"]["erp"]["bill_id"] == 1 and r[0]["result"]["erp"]["url"].endswith("/odoo/account.move/1")
    assert any(e["stage"] == "erp" and e["status"] == "ok" for e in main.BUS[r[0]["id"]]["events"])
    assert fake.line("P00001")["qty_invoiced"] == 50                        # Odoo now counts the 50 kg as billed
    # nothing else was posted: held and rejected invoices never reach Odoo
    assert all(x["result"].get("erp") is None for x in r[1:])


def test_the_14_samples_decide_the_same_against_odoo(fake):
    c = client("asha")
    got = {m["file"]: run(c, m["file"])["decision"] for m in SAMPLES}
    assert got == {m["file"]: m["expected"] for m in SAMPLES}
    approved = [m["file"] for m in SAMPLES if m["expected"] == "APPROVE"]
    assert len(fake.bills) == len(approved) == 7
    assert all(ln.get("po_line") for b in fake.bills for ln in b["lines"])  # every bill line linked to a PO line
    assert [ln["qty_invoiced"] for ln in next(p for p in fake.pos if p["name"] == "PO-2026-1004")["lines"]] == [1.0, 1.0]


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
