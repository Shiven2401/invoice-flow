"""Adding vendors and purchase orders on the PO ledger page: checked like the rest, kept per account, and used by the checks."""
import pytest

from app import db, main
from test_accounts import SAMPLE, UNKNOWN_VENDOR, clean, client, run_sample  # noqa: F401  (clean is an autouse fixture)

PO = dict(po_number="PO-2026-2001", vendor_id="V001", description="Printer toner", net_amount=25000, gst_rate=18, po_date="2026-11-01")


def pos(c):
    return {p["po_number"]: p for p in c.get("/api/pos").json()["pos"]}


def vendors(c):
    return {v["vendor_id"]: v for v in c.get("/api/pos").json()["vendors"]}


def test_a_vendor_added_by_hand_joins_the_list():
    c = client("asha")
    r = c.post("/api/vendors", json=dict(name="Belle Boutique", gstin="08abcpb1234q1z5", aliases="Belle, Belle Tailors", no_po=True))
    assert r.status_code == 200, r.text
    v = vendors(c)[r.json()["vendor_id"]]
    assert (v["name"], v["gstin"], v["status"], v["source"]) == ("Belle Boutique", "08ABCPB1234Q1Z5", "approved", "approved in app")
    assert v["aliases"] == ["Belle", "Belle Tailors"]
    assert r.json()["vendor_id"] in db.no_po_vendors(db.find_user("asha")["id"])     # its bills won't be held for having no PO


@pytest.mark.parametrize("body, says", [
    (dict(name=""), "name is needed"),
    (dict(name="New Vendor", gstin="12345"), "not a valid 15-character GSTIN"),
    (dict(name="Totally New Name", gstin="08AABCS1234F1Z5"), "already belongs to Sharma Office Supplies"),   # another vendor's GSTIN
    (dict(name="Kiran Courier Service"), "looks like existing vendor 'Kiran Courier Services' (blocked)"),    # a blocked vendor's lookalike
])
def test_vendors_are_checked_before_they_are_added(body, says):
    r = client("asha").post("/api/vendors", json=body)
    assert r.status_code == 400 and says in r.json()["detail"]


def test_a_purchase_order_added_by_hand_joins_the_register():
    c = client("asha")
    r = c.post("/api/pos", json=PO)
    assert r.status_code == 200 and r.json()["po_number"] == "PO-2026-2001"
    p = pos(c)["PO-2026-2001"]
    assert p["source"] == "added in app" and p["vendor_name"] == "Sharma Office Supplies Pvt Ltd"
    assert (p["net_amount"], p["gst_rate"], p["billed"], p["remaining"], p["status"]) == (25000, 18, 0, 25000, "open")
    other = pos(client("ravi"))
    assert other["PO-2026-1001"] and "PO-2026-2001" not in other                    # each account has its own


@pytest.mark.parametrize("change, status, says", [
    (dict(po_number="po 2026 1001"), 409, "already in your PO register"),     # the master list's PO-2026-1001, written differently
    (dict(po_number="#"), 400, "2 to 40 letters"),
    (dict(vendor_id="V999"), 400, "Choose a vendor"),
    (dict(vendor_id="V005"), 400, "is blocked"),
    (dict(description=" "), 400, "what the purchase order is for"),
    (dict(net_amount=0), 400, "more than zero"),
    (dict(net_amount="lots"), 400, "must be a number"),
    (dict(gst_rate=7), 400, "GST must be one of"),
    (dict(po_date="01-11-2026"), 400, "must look like"),
])
def test_purchase_orders_are_checked_before_they_are_added(change, status, says):
    r = client("asha").post("/api/pos", json=dict(PO, **change))
    assert r.status_code == status and says in r.json()["detail"], r.text


def test_a_po_added_twice_is_refused():
    c = client("asha")
    assert c.post("/api/pos", json=PO).status_code == 200
    assert c.post("/api/pos", json=dict(PO, po_number="PO-2026-2001 ")).status_code == 409


def test_the_checks_use_a_vendor_and_po_added_by_hand():
    reader = client("reader")                                   # first see who sample 11 is from: a vendor nobody has on file
    seen = reader.get(f"/api/runs/{run_sample(reader, UNKNOWN_VENDOR)}").json()
    f = seen["result"]["fields"]
    assert seen["decision"] == "HOLD" and "VENDOR_UNKNOWN" in {x["code"] for x in seen["result"]["findings"]}
    c = client("asha")                                          # another account adds that vendor and raises a PO for it
    assert c.post("/api/vendors", json=dict(name=f["vendor_name"], gstin=f["vendor_gstin"])).status_code == 200
    vendor_id = next(v["vendor_id"] for v in vendors(c).values() if v["name"] == f["vendor_name"])
    number = f.get("po_reference") or "PO-2026-2002"            # the bill cites none, so it's matched to the vendor's only open PO
    net = f.get("subtotal") or f["total"]                       # a PO for what the bill is for: it must fit to be matched without a number
    what = "Office furniture - 8 workstations"                  # what sample 11 bills for
    assert c.post("/api/pos", json=dict(PO, po_number=number, vendor_id=vendor_id, net_amount=net, description=what[:200],
                                        po_date="2026-09-01")).status_code == 200      # raised before the bill
    run = c.get(f"/api/runs/{run_sample(c, UNKNOWN_VENDOR)}").json()
    assert run["decision"] == "APPROVE", ([x["code"] for x in run["result"]["findings"] if x["severity"] != "info"], (run["result"].get("po") or {}).get("candidates"), what)
    assert run["po_number"] == number.upper() and pos(c)[number.upper()]["billed"] > 0   # and it's billed against the new PO


def test_only_an_unbilled_po_added_in_the_app_can_be_removed():
    c = client("asha")
    c.post("/api/pos", json=dict(PO, po_number="PO/2026/12"))
    assert c.delete("/api/pos/PO-2026-1001").status_code == 404                   # the master list isn't edited here
    assert c.delete("/api/pos/PO/2026/12").status_code == 200 and "PO/2026/12" not in pos(c)
    assert c.delete("/api/pos/PO/2026/12").status_code == 404


def test_a_billed_po_stays():
    c = client("asha")
    c.post("/api/pos", json=PO)
    db.add_ledger("PO-2026-2001", run_sample(c, SAMPLE), 1000)                      # an invoice posted against it
    r = c.delete("/api/pos/PO-2026-2001")
    assert r.status_code == 400 and "already billed" in r.json()["detail"]


def test_reset_clears_added_pos():
    c = client("asha")
    c.post("/api/pos", json=PO)
    assert c.post("/api/reset").status_code == 200
    assert "PO-2026-2001" not in pos(c)


def test_the_ledger_page_offers_both_forms():
    html = client().get("/").text
    assert 'id="add-po"' in html and 'id="add-vendor"' in html and 'id="ledger-dialog"' in html
