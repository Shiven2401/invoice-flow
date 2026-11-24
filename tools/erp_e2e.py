"""The two-way Odoo integration end to end, against a real Odoo through a running Invoice Flow: the 14 scenarios of the
integration brief. Each step is done the way a person would (through the app's API, or directly in Odoo) and then
checked on the other side.

    python tools/erp_e2e.py <Invoice Flow url> <account> <password> [path to .env] [--outage <proxy control url>]

The account must be connected to Odoo and allowed to approve purchasing. --outage needs the server's Odoo traffic to go
through a proxy that can be switched off (POST <url>/down, /up); without it, scenario 12 is reported as not run here
(tests/test_erp_sync.py covers it). Records it makes stay in Odoo, named "E2E ...", as the evidence.
"""
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

argv = [a for a in sys.argv[1:]]
outage = argv[argv.index("--outage") + 1].rstrip("/") if "--outage" in argv else None
if outage:
    i = argv.index("--outage"); del argv[i:i + 2]
BASE, USER, PASSWORD = argv[0].rstrip("/"), argv[1], argv[2]
load_dotenv(argv[3] if len(argv) > 3 else ROOT / ".env")
URL, DB, KEY = os.environ["ODOO_URL"].rstrip("/"), os.environ["ODOO_DB"], os.environ["ODOO_API_KEY"]
sys.stdout.reconfigure(encoding="utf-8")
STAMP = str(int(time.time()))[-6:]
RESULTS = []


def odoo(model, method, **params):
    for attempt in range(4):
        try:
            r = requests.post(f"{URL}/json/2/{model}/{method}", json=params, timeout=60, headers={"Authorization": f"bearer {KEY}", "X-Odoo-Database": DB})
            if r.status_code == 429 and attempt < 3:   # Odoo Online's rate limit: wait it out
                time.sleep(30)
                continue
            break
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(5)
    if r.status_code != 200:
        raise RuntimeError(f"{model}.{method}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


s = requests.Session()


def app(method, path, **kw):
    for attempt in range(3):
        try:
            r = s.request(method, BASE + path, timeout=120, **kw)
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(5)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def check(n, title, ok, detail):
    RESULTS.append((n, title, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {n:>2}. {title}: {detail}", flush=True)


def until(fn, seconds=120, every=5):
    t0 = time.time()
    while time.time() - t0 < seconds:
        v = fn()
        if v:
            return v, time.time() - t0
        time.sleep(every)
    return None, time.time() - t0


def gstin(state="27"):
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    body = f"{state}AAECE{STAMP[-4:]}E1Z"
    total = sum((lambda v: v // 36 + v % 36)(chars.index(ch) * (2 if i % 2 else 1)) for i, ch in enumerate(body))
    return body + chars[(36 - total % 36) % 36]


def run_invoice(pdf):
    rid = app("POST", "/api/runs", files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))], data=dict(inputs="{}"))["runs"][0]["id"]
    run, _ = until(lambda: (lambda r: r if r["status"] in ("review", "done", "error") else None)(app("GET", f"/api/runs/{rid}")), 300, 2)
    if run["status"] == "review":
        app("POST", f"/api/runs/{rid}/continue", json={})
        run, _ = until(lambda: (lambda r: r if r["status"] in ("done", "error") else None)(app("GET", f"/api/runs/{rid}")), 300, 2)
    return run


assert s.post(f"{BASE}/api/login", json=dict(username=USER, password=PASSWORD), timeout=60).ok, "login failed"
st = app("GET", "/api/erp/status")
print(f"Invoice Flow at {BASE}, connected to {st['db']} over {st['protocol']}; approver: {st['approver']}; run {STAMP}\n")
tax = next(t["id"] for t in app("GET", "/api/erp/options")["taxes"] if t["amount"] == 18 and "IGST" not in t["name"] and "GST P" in t["name"])

# 10a. vendors and products created in the app reach Odoo
VENDOR_GSTIN = gstin()
v = app("POST", "/api/erp/vendors", json=dict(name=f"E2E Vendor {STAMP}", vat=VENDOR_GSTIN, email=f"ap{STAMP}@e2e.test", city="Pune", request_id=f"v-{STAMP}"))
p = app("POST", "/api/erp/products", json=dict(name=f"E2E Widget {STAMP}", default_code=f"E2E-{STAMP}", standard_price=250, tax_ids=[tax], request_id=f"p-{STAMP}"))
ov = odoo("res.partner", "search_read", domain=[["vat", "=", VENDOR_GSTIN]], fields=["id", "name", "supplier_rank"])
op = odoo("product.product", "search_read", domain=[["default_code", "=", f"E2E-{STAMP}"]], fields=["id", "name", "is_storable"])
vendor_id, product_id = v["odoo_id"], p["odoo_id"]

# 4. a PO created in Invoice Flow is created in Odoo, with its app id as origin
po = app("POST", "/api/erp/purchase-orders", json=dict(vendor_id=vendor_id, partner_ref=f"E2E-REQ-{STAMP}", lines=[dict(product_id=product_id, qty=10, price=250, tax_ids=[tax])],
                                                     request_id=f"po-{STAMP}"))
opo = odoo("purchase.order", "search_read", domain=[["origin", "=", f"Invoice Flow #{po['id']}"]], fields=["id", "name", "state", "amount_total"])
check(4, "PO created in Invoice Flow appears in Odoo", len(opo) == 1 and opo[0]["state"] == "draft", f"app PO #{po['id']} -> Odoo {opo[0]['name'] if opo else '?'} ({opo[0]['state'] if opo else '-'})")
PO_NAME, PO_ODOO = opo[0]["name"], opo[0]["id"]

# 11b. the same request again makes no second PO
again = app("POST", "/api/erp/purchase-orders", json=dict(vendor_id=vendor_id, lines=[dict(product_id=product_id, qty=10, price=250)], request_id=f"po-{STAMP}"))
n = odoo("purchase.order", "search_count", domain=[["origin", "=", f"Invoice Flow #{po['id']}"]])

# 8. a PO changed in Invoice Flow changes in Odoo
d = app("GET", f"/api/erp/purchase-orders/{po['id']}")
upd = app("PATCH", f"/api/erp/purchase-orders/{po['id']}", json=dict(base_version=d["_version"], lines=[dict(id=d["lines"][0]["id"], qty=12)], request_id=f"upd-{STAMP}"))
qty = odoo("purchase.order.line", "search_read", domain=[["order_id", "=", PO_ODOO]], fields=["product_qty"])[0]["product_qty"]
check(8, "PO updated in Invoice Flow updates Odoo", upd["queue"]["status"] == "done" and qty == 12, f"quantity 10 -> {qty:g} in Odoo")

# 5. approving in Invoice Flow confirms it in Odoo
ap = app("POST", f"/api/erp/purchase-orders/{po['id']}/approve", json={})
ostate = odoo("purchase.order", "read", ids=[PO_ODOO], fields=["state", "date_approve"])[0]
check(5, "PO approved in Invoice Flow is confirmed in Odoo", ostate["state"] == "purchase" and ostate["date_approve"], f"Odoo state {ostate['state']}, approved {ostate['date_approve']}")

# 7. a change made in Odoo reaches Invoice Flow
odoo("purchase.order", "write", ids=[PO_ODOO], vals={"partner_ref": f"E2E-ODOO-{STAMP}"})
seen, t7 = until(lambda: app("GET", f"/api/erp/purchase-orders/{po['id']}").get("partner_ref") == f"E2E-ODOO-{STAMP}", 150)
check(7, "PO updated in Odoo is reflected in Invoice Flow", seen, f"vendor reference changed in Odoo, seen in the app after {t7:.0f}s")

# 6. a PO created directly in Odoo reaches Invoice Flow
direct = odoo("purchase.order", "create", vals_list=[dict(partner_id=vendor_id, order_line=[[0, 0, dict(product_id=product_id, name=f"E2E Widget {STAMP}", product_qty=5, price_unit=240)]])])[0]
dname = odoo("purchase.order", "read", ids=[direct], fields=["name"])[0]["name"]
found, t6 = until(lambda: next((x for x in app("GET", f"/api/erp/purchase-orders?q={dname}")["purchase_orders"] if x["name"] == dname), None), 150)
check(6, "PO created directly in Odoo appears in Invoice Flow", found and found["_odoo_id"] == direct, f"{dname} visible in the app after {t6:.0f}s ({'webhook' if st.get('webhook') else 'scheduled pull'})")

# 9. receiving goods in Odoo Inventory reaches Invoice Flow (and the 3-way match)
pk = odoo("purchase.order", "read", ids=[PO_ODOO], fields=["picking_ids"])[0]["picking_ids"][0]
for mv in odoo("stock.move", "search_read", domain=[["picking_id", "=", pk]], fields=["id", "product_uom_qty"]):
    odoo("stock.move", "write", ids=[mv["id"]], vals={"quantity": 8, "picked": True})          # 8 of 12 arrive
odoo("stock.picking", "button_validate", ids=[pk], context={"skip_backorder": True})
rec, t9 = until(lambda: (lambda x: x if x["lines"][0]["qty_received"] == 8 else None)(app("GET", f"/api/erp/purchase-orders/{po['id']}")), 150)
check(9, "Goods received in Odoo show in Invoice Flow", rec, f"8 of 12 received in Odoo; the app shows {rec['lines'][0]['qty_received']:g} after {t9:.0f}s, receipt status {rec['receipt_status']}" if rec else "not seen")

# 2/1. an invoice citing the PO: matched against what was received; the bill in Odoo carries what was read
VENDOR = dict(name=f"E2E Vendor {STAMP}", gstin=VENDOR_GSTIN, phone="020-2550000", addr1="12 FC Road", addr2="Pune, Maharashtra 411004")
folder = Path(tempfile.mkdtemp())
over = folder / f"E2E-{STAMP}-A.pdf"                                          # bills all 12 though only 8 arrived: held
classic(over, VENDOR, dict(number=f"E2E-{STAMP}-A", date=date.today().strftime("%d-%b-%Y"), po=PO_NAME, gst=18, lines=[(f"E2E Widget {STAMP}", "8472", 12, 250.0)]),
        buyer={"name": "Invoice Flow", "addr": "Hyderabad", "gstin": None})
held = run_invoice(over)
good = folder / f"E2E-{STAMP}-B.pdf"                                          # bills the 8 received: approved
classic(good, VENDOR, dict(number=f"E2E-{STAMP}-B", date=date.today().strftime("%d-%b-%Y"), po=PO_NAME, gst=18, lines=[(f"E2E Widget {STAMP}", "8472", 8, 250.0)]),
        buyer={"name": "Invoice Flow", "addr": "Hyderabad", "gstin": None})
ok = run_invoice(good)
codes = lambda r: {x["code"] for x in r["result"]["findings"]}
check(2, "Invoice with a valid PO number is matched", ok["decision"] == "APPROVE" and "THREE_WAY_OK" in codes(ok) and held["decision"] == "HOLD" and "QTY_NOT_RECEIVED" in codes(held),
      f"8 of 8 received -> {ok['decision']} (3-way OK); 12 billed with 8 received -> {held['decision']} ({', '.join(sorted(codes(held) & {'QTY_NOT_RECEIVED'}))})")
bill = (ok["result"].get("erp") or {}).get("bill_id")
ob = odoo("account.move", "read", ids=[bill], fields=["ref", "invoice_date", "partner_id", "amount_untaxed", "invoice_line_ids", "message_main_attachment_id"])[0] if bill else {}
lines = odoo("account.move.line", "read", ids=ob.get("invoice_line_ids", []), fields=["quantity", "price_unit", "purchase_line_id", "display_type"]) if bill else []
prod_lines = [ln for ln in lines if ln.get("display_type") in (None, False, "product")]
check(1, "Uploaded invoice's extracted data is in Odoo", bill and ob["ref"] == f"E2E-{STAMP}-B" and ob["invoice_date"] == date.today().isoformat() and ob["partner_id"][0] == vendor_id
      and abs(ob["amount_untaxed"] - 2000) < 0.01 and all(ln["purchase_line_id"] for ln in prod_lines) and ob["message_main_attachment_id"],
      f"draft bill #{bill}: ref {ob.get('ref')}, date {ob.get('invoice_date')}, vendor {(ob.get('partner_id') or [0, '?'])[1]}, untaxed {ob.get('amount_untaxed')}, "
      f"{len(prod_lines)} line(s) linked to the PO, PDF attached: {bool(ob.get('message_main_attachment_id'))}")

# 11a. the same invoice again: no second bill
dup = run_invoice(good)
nbills = odoo("account.move", "search_count", domain=[["ref", "=", f"E2E-{STAMP}-B"], ["move_type", "=", "in_invoice"]])
check(11, "No duplicate invoices, bills or POs", dup["decision"] == "REJECT" and nbills == 1 and again["id"] == po["id"] and n == 1,
      f"same invoice again -> {dup['decision']}, bills with that number in Odoo: {nbills}; same PO request twice -> one Odoo PO ({n})")

# 3. a wrong PO number: held, with the reason noted in Odoo
bad = folder / f"E2E-{STAMP}-C.pdf"
classic(bad, VENDOR, dict(number=f"E2E-{STAMP}-C", date=date.today().strftime("%d-%b-%Y"), po="P99999", gst=18, lines=[(f"E2E Widget {STAMP}", "8472", 2, 250.0)]),
        buyer={"name": "Invoice Flow", "addr": "Hyderabad", "gstin": None})
nopo = run_invoice(bad)
trail = (nopo.get("odoo_trail") or [{}])[-1]
check(3, "Invoice with an invalid PO number is held", nopo["decision"] == "HOLD" and "PO_NOT_FOUND" in codes(nopo) and trail.get("target"),
      f"{nopo['decision']} ({', '.join(sorted(codes(nopo) & {'PO_NOT_FOUND'}))}), noted in Odoo on {(trail.get('target') or {}).get('label')}")

# 14. posting and paying the bill in Odoo reaches Invoice Flow
odoo("account.move", "action_post", ids=[bill])
ctx = {"active_model": "account.move", "active_ids": [bill]}
wiz = odoo("account.payment.register", "create", vals_list=[{}], context=ctx)[0]
odoo("account.payment.register", "action_create_payments", ids=[wiz], context=ctx)
paid, t14 = until(lambda: (lambda r: r if (r.get("odoo") or {}).get("status") in ("paid", "in_payment") else None)(
    next(x for x in app("GET", "/api/runs")["runs"] if x["id"] == ok["id"])), 150)
mb, _ = until(lambda: next((b for b in app("GET", "/api/erp/bills")["bills"] if b["_odoo_id"] == bill and b.get("payment_state") in ("paid", "in_payment")), None), 120)
check(14, "Bill posting and payment are synchronised", paid and mb, f"posted and paid in Odoo; the invoice shows {(paid or {}).get('odoo', {}).get('status')} after {t14:.0f}s, "
      f"the mirrored bill {(mb or {}).get('payment_state')}")

# 10b. a vendor changed in Odoo reaches Invoice Flow
odoo("res.partner", "write", ids=[vendor_id], vals={"email": f"billing{STAMP}@e2e.test"})
ve, t10 = until(lambda: next((x for x in app("GET", "/api/erp/vendors")["vendors"] if x["_odoo_id"] == vendor_id and x.get("email") == f"billing{STAMP}@e2e.test"), None), 150)
check(10, "Vendor and product information stays synchronised", len(ov) == 1 and ov[0]["supplier_rank"] and len(op) == 1 and op[0]["is_storable"] and ve,
      f"vendor and product made in the app are in Odoo (#{vendor_id}, #{product_id}); vendor email changed in Odoo seen in the app after {t10:.0f}s")

# 13. the dashboard's figures against Odoo's own records
app("POST", "/api/erp/sync", json={})
o = app("GET", "/api/erp/overview")
po_all = odoo("purchase.order", "search_read", domain=[], fields=["state", "amount_total", "receipt_status"])
bills = odoo("account.move", "search_read", domain=[["move_type", "in", ["in_invoice", "in_refund"]]], fields=["state", "payment_state", "amount_total", "amount_residual", "move_type"])
sign = lambda b: -1 if b["move_type"] == "in_refund" else 1
expect = dict(po_total=len(po_all), confirmed=sum(1 for x in po_all if x["state"] == "purchase"), partial=sum(1 for x in po_all if x["state"] != "cancel" and x["receipt_status"] == "partial"),
              posted=sum(1 for b in bills if b["state"] == "posted"), drafts=sum(1 for b in bills if b["state"] == "draft"),
              po_value=round(sum(x["amount_total"] for x in po_all if x["state"] != "cancel"), 2),
              pending=round(sum(sign(b) * b["amount_residual"] for b in bills if b["state"] == "posted"), 2))
got = dict(po_total=o["purchase_orders"]["total"], confirmed=o["purchase_orders"]["confirmed"], partial=o["purchase_orders"]["partially_received"],
           posted=o["invoices"]["bills_posted"], drafts=o["invoices"]["bills_draft"], po_value=o["money"]["po_value"], pending=o["money"]["pending_payment"])
check(13, "Dashboard figures match Odoo's records", got == expect, f"app {got} | Odoo {expect}")

# 12. Odoo unavailable: the write waits and goes through once, when Odoo is back
if outage:
    requests.post(f"{outage}/down", timeout=10)
    try:
        q = app("POST", "/api/erp/purchase-orders", json=dict(vendor_id=vendor_id, lines=[dict(product_id=product_id, qty=1, price=250)], request_id=f"out-{STAMP}"))
    finally:
        requests.post(f"{outage}/up", timeout=10)
    queued = q["queue"]["status"] == "pending"
    done, t12 = until(lambda: (lambda: app("POST", "/api/erp/sync", json={}) and next((e for e in app("GET", "/api/erp/sync")["entries"] if e["id"] == q["queue"]["id"] and e["status"] == "done"), None))(), 180, 10)
    cnt = odoo("purchase.order", "search_count", domain=[["origin", "=", f"Invoice Flow #{q['id']}"]])
    check(12, "Odoo failure: pending operations are retried", queued and done and cnt == 1, f"while Odoo was unreachable the PO was {q['queue']['status']}; "
          f"{'sent' if done else 'not sent'} {t12:.0f}s after it came back; Odoo has {cnt} copy")
else:
    RESULTS.append((12, "Odoo failure: pending operations are retried", None, "not run against this server (needs --outage); covered by tests/test_erp_sync.py"))
    print("  -   12. Odoo failure: not run here (needs --outage); covered by tests/test_erp_sync.py")

passed = sum(1 for r in RESULTS if r[2])
print(f"\n{passed} of {sum(1 for r in RESULTS if r[2] is not None)} scenarios passed (run {STAMP}).")
sys.exit(0 if all(r[2] is not False for r in RESULTS) else 1)
