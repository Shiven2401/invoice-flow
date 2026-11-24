"""End-to-end procure-to-pay against a real Odoo, with Invoice Flow in the middle:

    1. Purchase   a new PO for a stocked product, confirmed
    2. Inventory  the goods received: the receipt (stock picking) validated, stock on hand goes up
    3. Invoice    the vendor's invoice PDF, uploaded to Invoice Flow, reviewed and checked (3-way match)
    4. Bill       approved, so Invoice Flow makes a draft vendor bill linked to the PO line, with the PDF and a note
    5. Post       the bill is posted in Odoo; Invoice Flow shows "Posted"
    6. Pay        a payment is registered in Odoo; Invoice Flow shows "Paid" (or "In payment" until the bank matches it)

    python tools/p2p_e2e.py <Invoice Flow url> <account> <password> [path to .env]

The account must be one the server connects to Odoo (ODOO_ACCOUNTS). The records it makes stay in Odoo as the evidence:
a PO, a receipt, a posted bill and a payment, all for the product "P2P test - Heavy-duty stapler". Uses JSON-2.
"""
import json
import os
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from make_samples import classic  # noqa: E402

BASE, USER, PASSWORD = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
load_dotenv(sys.argv[4] if len(sys.argv) > 4 else ROOT / ".env")
URL, DB, KEY = os.environ["ODOO_URL"].rstrip("/"), os.environ["ODOO_DB"], os.environ["ODOO_API_KEY"]
VENDOR = dict(name="Sharma Office Supplies Pvt Ltd", gstin="08AABCS1234F1Z5", phone="0291-2431100",
              addr1="12 Sardarpura, 1st B Road", addr2="Jodhpur, Rajasthan 342003")
PRODUCT, QTY, RATE = "P2P test - Heavy-duty stapler", 12, 450.0
sys.stdout.reconfigure(encoding="utf-8")


def call(model, method, **params):
    for attempt in range(3):
        try:
            r = requests.post(f"{URL}/json/2/{model}/{method}", json=params, timeout=60,
                              headers={"Authorization": f"bearer {KEY}", "X-Odoo-Database": DB})
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(3)
    if r.status_code != 200:
        sys.exit(f"{model}.{method}: HTTP {r.status_code} {r.text[:400]}")
    return r.json()


def step(n, text):
    print(f"\n{n}. {text}")


# ---------------------------------------------------------------- 1. purchase --
step(1, "Purchase: a PO for a stocked product")
vendor = call("res.partner", "search_read", domain=[["vat", "=", VENDOR["gstin"]]], fields=["id", "name"], limit=1)[0]
tax = call("account.tax", "search_read", domain=[["type_tax_use", "=", "purchase"], ["amount", "=", 18], ["name", "ilike", "GST P"],
                                                  ["name", "not ilike", "IGST"]], fields=["id", "name"], limit=1)[0]
prod = call("product.product", "search_read", domain=[["name", "=", PRODUCT]], fields=["id"], limit=1)
pid = prod[0]["id"] if prod else call("product.product", "create", vals_list=[dict(
    name=PRODUCT, type="consu", is_storable=True, purchase_ok=True, supplier_taxes_id=[[6, 0, [tax["id"]]]])])[0]
qty_before = sum(q["quantity"] for q in call("stock.quant", "search_read", domain=[["product_id", "=", pid], ["location_id.usage", "=", "internal"]], fields=["quantity"]))
reuse = os.getenv("P2P_PO")   # carry on with a PO an earlier, interrupted run made (and maybe received)
if reuse:
    po_id = call("purchase.order", "search_read", domain=[["name", "=", reuse]], fields=["id"], limit=1)[0]["id"]
else:
    po_id = call("purchase.order", "create", vals_list=[dict(partner_id=vendor["id"], order_line=[[0, 0, dict(
        product_id=pid, name=PRODUCT, product_qty=QTY, price_unit=RATE, tax_ids=[[6, 0, [tax["id"]]]])]])])[0]
    call("purchase.order", "button_confirm", ids=[po_id])
po = call("purchase.order", "read", ids=[po_id], fields=["name", "state", "picking_ids", "amount_total", "order_line"])[0]
print(f"   {po['name']} to {vendor['name']}: {QTY} x Rs {RATE:g} + 18% GST = Rs {po['amount_total']:,.2f}, state {po['state']}")

# --------------------------------------------------------------- 2. inventory --
step(2, "Inventory: receive the goods")
for pk in call("stock.picking", "read", ids=po["picking_ids"], fields=["name", "state", "move_ids"]):
    if pk["state"] in ("done", "cancel"):
        print(f"   receipt {pk['name']}: already {pk['state']}")
        continue
    for mv in call("stock.move", "read", ids=pk["move_ids"], fields=["product_uom_qty"]):
        call("stock.move", "write", ids=[mv["id"]], vals={"quantity": mv["product_uom_qty"], "picked": True})
    call("stock.picking", "button_validate", ids=[pk["id"]])
    pk = call("stock.picking", "read", ids=[pk["id"]], fields=["name", "state"])[0]
    print(f"   receipt {pk['name']}: {pk['state']}")
line = call("purchase.order.line", "read", ids=po["order_line"], fields=["qty_received", "qty_invoiced"])[0]
qty_after = sum(q["quantity"] for q in call("stock.quant", "search_read", domain=[["product_id", "=", pid], ["location_id.usage", "=", "internal"]], fields=["quantity"]))
print(f"   PO line received {line['qty_received']:g} of {QTY}; stock on hand {qty_before:g} -> {qty_after:g}")

# ----------------------------------------------------------------- 3. invoice --
step(3, "Invoice: the vendor's PDF through Invoice Flow")
number = f"P2P-{int(time.time()) % 1000000}"
pdf = Path(tempfile.mkdtemp()) / f"{number}.pdf"
classic(pdf, VENDOR, dict(number=number, date=date.today().strftime("%d-%b-%Y"), po=po["name"], gst=18,
                          lines=[(PRODUCT, "8472", QTY, RATE)]), buyer={"name": "Invoice Flow", "addr": "Hyderabad, Telangana", "gstin": None})
s = requests.Session()
r = s.post(f"{BASE}/api/login", json=dict(username=USER, password=PASSWORD))
assert r.ok, f"login failed: {r.text}"
run_id = s.post(f"{BASE}/api/runs", files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))], data=dict(inputs="{}")).json()["runs"][0]["id"]


def wait(*states):
    for _ in range(400):
        run = s.get(f"{BASE}/api/runs/{run_id}").json()
        if run["status"] in states:
            return run
        time.sleep(1)
    sys.exit(f"run {run_id} stuck in {run['status']}")


run = wait("review", "done", "error")
if run["status"] == "review":
    s.post(f"{BASE}/api/runs/{run_id}/continue", json={}).raise_for_status()
    run = wait("done", "error")
res = run["result"]
lines3 = [(ln["line"], ln["invoiced"], ln["received"]) for ln in (res.get("budget") or {}).get("lines") or []]
print(f"   {pdf.name}: {run['decision']} - {res['headline']}")
print(f"   3-way match (line, invoiced, received): {lines3}")
if run["decision"] != "APPROVE":
    sys.exit("   not approved, so the flow stops here: " + json.dumps([x["message"] for x in res["findings"] if x["severity"] != "info"])[:600])

# -------------------------------------------------------------------- 4. bill --
step(4, "Bill: a draft vendor bill in Odoo, with the PDF and the note")
bill_id = res["erp"]["bill_id"]
bill = call("account.move", "read", ids=[bill_id], fields=["name", "state", "ref", "amount_total", "invoice_line_ids", "message_main_attachment_id"])[0]
bl = call("account.move.line", "read", ids=bill["invoice_line_ids"], fields=["purchase_line_id", "quantity", "price_unit"])
notes = call("mail.message", "search_read", domain=[["model", "=", "account.move"], ["res_id", "=", bill_id], ["subtype_id.name", "=", "Notes"]], fields=["body", "attachment_ids"])
line = call("purchase.order.line", "read", ids=po["order_line"], fields=["qty_received", "qty_invoiced"])[0]
print(f"   bill #{bill_id} ref {bill['ref']}: {bill['state']}, Rs {bill['amount_total']:,.2f}; linked to PO line: {all(x['purchase_line_id'] for x in bl)}")
print(f"   PDF attached: {bool(bill['message_main_attachment_id'])}; audit note: {bool(notes)}; PO line now billed {line['qty_invoiced']:g} of {QTY}")
print(f"   {res['erp']['url']}")

# -------------------------------------------------------------------- 5. post --
step(5, "Post: the bill is posted in Odoo")
call("account.move", "action_post", ids=[bill_id])
time.sleep(16)                                        # Invoice Flow reads bill states at most every 15 s per account
shown = lambda: next(x for x in s.get(f"{BASE}/api/runs").json()["runs"] if x["id"] == run_id)["odoo"]["status"]
print(f"   Odoo: {call('account.move', 'read', ids=[bill_id], fields=['state', 'name'])[0]}; Invoice Flow shows: {shown()}")

# --------------------------------------------------------------------- 6. pay --
step(6, "Pay: a payment registered against the bill")
ctx = {"active_model": "account.move", "active_ids": [bill_id]}
wiz = call("account.payment.register", "create", vals_list=[{}], context=ctx)[0]
call("account.payment.register", "action_create_payments", ids=[wiz], context=ctx)
b = call("account.move", "read", ids=[bill_id], fields=["payment_state", "amount_residual"])[0]
time.sleep(16)
print(f"   Odoo: payment_state {b['payment_state']}, still due Rs {b['amount_residual']:,.2f}; Invoice Flow shows: {shown()}")
print(f"\nDone: {po['name']} -> receipt -> {number} approved in Invoice Flow -> bill #{bill_id} posted -> paid.")
