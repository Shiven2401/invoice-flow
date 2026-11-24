"""The Odoo side of the test data, from data/odoo_test_data.json.

    python tools/odoo_test_data.py setup        vendors, products and POs: created, confirmed and received (safe to re-run:
                                               vendors are found by GSTIN, products by name, POs by number)
    python tools/odoo_test_data.py reset-bills  deletes the DRAFT vendor bills of the test vendors, so the test invoices
                                               can be run again from a clean state (posted bills are never touched)

Needs ODOO_URL, ODOO_DB and ODOO_API_KEY in .env. Uses Odoo's JSON-2 API, like app/odoo.py.
"""
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
URL, DB, KEY = os.environ["ODOO_URL"].rstrip("/"), os.environ["ODOO_DB"], os.environ["ODOO_API_KEY"]
DATA = json.loads((ROOT / "data" / "odoo_test_data.json").read_text())
INDIA_STATES = {"TS": "Telangana", "AP": "Andhra Pradesh", "RJ": "Rajasthan"}


def call(model, method, **params):
    r = requests.post(f"{URL}/json/2/{model}/{method}", json=params, timeout=60,
                      headers={"Authorization": f"bearer {KEY}", "X-Odoo-Database": DB})
    if r.status_code != 200:
        try:
            msg = r.json().get("message", "")
        except ValueError:
            msg = r.text
        sys.exit(f"{model}.{method}: HTTP {r.status_code} {msg[:400]}")
    return r.json()


def one(model, domain, fields=("id",)):
    rows = call(model, "search_read", domain=domain, fields=list(fields), limit=1)
    return rows[0] if rows else None


def setup():
    india = one("res.country", [["code", "=", "IN"]])["id"]
    states = {s["code"]: s["id"] for s in call("res.country.state", "search_read", domain=[["country_id", "=", india]], fields=["code"])}
    tag = one("res.partner.category", [["name", "=", "Blocked"]])
    blocked_tag = tag["id"] if tag else call("res.partner.category", "create", vals_list=[{"name": "Blocked"}])[0]
    partner = {}
    for v in DATA["vendors"]:
        found = one("res.partner", [["vat", "=", v["gstin"]]])
        tags = [[6, 0, [blocked_tag] if v.get("blocked") else []]]
        if found:
            partner[v["gstin"]] = found["id"]
            call("res.partner", "write", ids=[found["id"]], vals={"category_id": tags})
        else:
            partner[v["gstin"]] = call("res.partner", "create", vals_list=[dict(
                name=v["name"], is_company=True, supplier_rank=1, vat=v["gstin"], country_id=india, state_id=states.get(v["state"]),
                city=v["city"], l10n_in_gst_treatment="regular", category_id=tags)])[0]
        print(f"vendor {v['name']} ({v['gstin']}){' [Blocked]' if v.get('blocked') else ''}: partner {partner[v['gstin']]}")
    kg = one("uom.uom", [["name", "=", "kg"]])["id"]
    taxes = {0: [], 18: [one("account.tax", [["type_tax_use", "=", "purchase"], ["amount", "=", 18], ["name", "ilike", "GST P"], ["name", "not ilike", "IGST"]])["id"]]}
    for po in DATA["purchase_orders"]:
        found = one("purchase.order", [["name", "=", po["name"]]], ("id", "state"))
        if found:
            po_id, state = found["id"], found["state"]
        else:
            lines = []
            for ln in po["lines"]:
                prod = one("product.product", [["name", "=", ln["product"]]])
                if not prod:
                    vals = dict(name=ln["product"], type="consu" if ln["kind"] == "goods" else "service", purchase_ok=True,
                                supplier_taxes_id=[[6, 0, taxes[po["gst"]]]])
                    if ln["kind"] == "goods":
                        vals["is_storable"] = True
                    if ln.get("uom") == "kg":
                        vals["uom_id"] = kg
                    prod = {"id": call("product.product", "create", vals_list=[vals])[0]}
                lines.append([0, 0, dict(product_id=prod["id"], name=ln["product"], product_qty=ln["qty"], price_unit=ln["rate"],
                                         tax_ids=[[6, 0, taxes[po["gst"]]]])])
            po_id = call("purchase.order", "create", vals_list=[dict(name=po["name"], partner_id=partner[po["vendor"]], partner_ref=po.get("ref") or False,
                                                                     date_order=f"{po['date']} 10:00:00", order_line=lines)])[0]
            state = "draft"
        if state in ("draft", "sent"):
            call("purchase.order", "button_confirm", ids=[po_id])
        rec = call("purchase.order", "search_read", domain=[["id", "=", po_id]], fields=["name", "picking_ids", "order_line"])[0]
        rows = call("purchase.order.line", "read", ids=rec["order_line"], fields=["name", "product_type", "qty_received"])
        want = {ln["product"]: ln["received"] for ln in po["lines"]}
        for pk in call("stock.picking", "search_read", domain=[["id", "in", rec["picking_ids"]], ["state", "not in", ["done", "cancel"]]], fields=["move_ids"]):
            for mv in call("stock.move", "read", ids=pk["move_ids"], fields=["purchase_line_id"]):
                row = next(r for r in rows if r["id"] == mv["purchase_line_id"][0])
                call("stock.move", "write", ids=[mv["id"]], vals={"quantity": want[row["name"]] - row["qty_received"], "picked": True})
            call("stock.picking", "button_validate", ids=[pk["id"]], context={"skip_backorder": True})   # a partial receipt leaves a backorder
        for row in rows:
            if row["product_type"] == "service" and row["qty_received"] != want[row["name"]]:
                call("purchase.order.line", "write", ids=[row["id"]], vals={"qty_received": want[row["name"]]})
        print(f"PO {rec['name']}: confirmed and received")


def reset_bills():
    vendors = [p["id"] for v in DATA["vendors"] for p in [one("res.partner", [["vat", "=", v["gstin"]]])] if p]
    bills = call("account.move", "search_read", domain=[["move_type", "=", "in_invoice"], ["state", "=", "draft"], ["partner_id", "in", vendors]],
                 fields=["name", "ref", "partner_id"])
    if bills:
        call("account.move", "unlink", ids=[b["id"] for b in bills])
    print(f"deleted {len(bills)} draft bill(s): " + ", ".join(f"{b['ref']} ({b['partner_id'][1]})" for b in bills))


if __name__ == "__main__":
    {"setup": setup, "reset-bills": reset_bills}[sys.argv[1] if len(sys.argv) > 1 else "setup"]()
