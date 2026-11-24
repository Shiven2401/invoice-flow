"""Odoo connector: reads vendors, POs and receipts; creates draft vendor bills.

This file is plumbing: it only talks to Odoo. Every decision stays in rules.py.

It uses Odoo's JSON-2 API (Odoo 19 and later): one HTTPS POST per call, to /json/2/<model>/<method>, with the API key
as a bearer token. The code guide's first version used XML-RPC with a login and key; XML-RPC is deprecated from Odoo 19
and this Odoo runs version 20, so the same three reads and one write go through JSON-2 instead. The functions, and what
they return, are the ones the guide describes.

Settings (all three, or the app keeps using its CSVs):
    ODOO_URL       https://invoice-flow.odoo.com
    ODOO_DB        invoice-flow
    ODOO_API_KEY   avatar > My Preferences > Account Security > New API Key (never in code or a commit)
Optional:
    ODOO_ACCOUNTS  Invoice Flow account names that use Odoo, comma-separated. Empty means every account. On a public
                   server set it, so other people's invoices never reach your Odoo and the built-in demo keeps working.
"""
import os

import requests

TIMEOUT = 15          # seconds per call: a slow Odoo holds an invoice ("ERP unreachable") instead of hanging the worker
BLOCKED_TAG = "Blocked"   # a vendor carrying this tag in Odoo is treated as blocked: no payments


class OdooError(Exception):
    """Odoo refused or failed a call (bad key, missing access, a record it wouldn't accept)."""


class OdooUnreachable(OdooError):
    """Odoo didn't answer: network trouble, a timeout, or Odoo itself down."""


def _cfg():
    return (os.getenv("ODOO_URL", "").strip().rstrip("/"), os.getenv("ODOO_DB", "").strip(),
            (os.getenv("ODOO_API_KEY") or os.getenv("ODOO_MCP_KEY") or "").strip())


def enabled() -> bool:
    """True only when all three settings exist. Otherwise the app keeps using the CSVs."""
    return all(_cfg())


def serves(username) -> bool:
    """Does this Invoice Flow account check its invoices against Odoo?"""
    allowed = {a.strip().lower() for a in os.getenv("ODOO_ACCOUNTS", "").split(",") if a.strip()}
    return enabled() and (not allowed or (username or "").strip().lower() in allowed)


def info():
    """For the page: where the account's data comes from. Never the key."""
    url, db, _ = _cfg()
    return dict(system="Odoo", url=url, db=db)


def _call(model, method, **params):
    """The one door into Odoo: a model (table), a method (search_read, read, create, ...) and its arguments by name."""
    url, db, key = _cfg()
    try:
        r = requests.post(f"{url}/json/2/{model}/{method}", json=params, timeout=TIMEOUT,
                          headers={"Authorization": f"bearer {key}", "X-Odoo-Database": db, "User-Agent": "invoice-flow"})
    except requests.RequestException as e:
        raise OdooUnreachable(f"Odoo didn't answer ({type(e).__name__})") from e
    if r.status_code in (502, 503, 504):
        raise OdooUnreachable(f"Odoo is unavailable (HTTP {r.status_code})")
    if r.status_code in (401, 403):
        raise OdooError("Odoo refused the API key (check ODOO_API_KEY and that the user can use Purchase and Accounting)")
    if r.status_code >= 400:
        try:
            msg = (r.json().get("message") or "").strip().splitlines()[0][:300]
        except (ValueError, AttributeError, IndexError):
            msg = r.text[:200]
        raise OdooError(f"Odoo rejected {model}.{method} (HTTP {r.status_code}): {msg or 'no reason given'}")
    return r.json()


def find_vendor(gstin):
    gstin = (gstin or "").replace(" ", "").upper()
    rows = _call("res.partner", "search_read", domain=[["vat", "=", gstin]], fields=["id", "name", "vat"], limit=1)
    return rows[0] if rows else None


def vendors():
    """Every vendor (a partner with a supplier rank), with its GSTIN and whether it carries the Blocked tag."""
    tags = _call("res.partner.category", "search_read", domain=[["name", "=ilike", BLOCKED_TAG]], fields=["id"])
    blocked = {t["id"] for t in tags}
    rows = _call("res.partner", "search_read", domain=[["supplier_rank", ">", 0]], fields=["id", "name", "vat", "category_id"])
    return [dict(id=r["id"], name=r["name"], vat=r["vat"] or "", blocked=bool(blocked & set(r["category_id"] or []))) for r in rows]


def find_po(po_ref):
    rows = _call("purchase.order", "search_read", domain=[["name", "=", po_ref], ["state", "in", ["purchase", "done"]]],
                 fields=["id", "name", "partner_id", "amount_total", "order_line"], limit=1)
    return rows[0] if rows else None


PO_LINE_FIELDS = ["id", "order_id", "name", "product_id", "product_qty", "qty_received", "qty_invoiced", "price_unit", "tax_ids"]


def po_lines(line_ids):
    return _call("purchase.order.line", "read", ids=list(line_ids), fields=PO_LINE_FIELDS)


def purchase_orders():
    """Every confirmed PO with its lines: what was ordered, received and already billed on each. Two calls in all."""
    pos = _call("purchase.order", "search_read", domain=[["state", "in", ["purchase", "done"]]],
                fields=["id", "name", "partner_id", "date_order", "state", "amount_untaxed", "amount_tax", "order_line"])
    lines = _call("purchase.order.line", "search_read", domain=[["order_id", "in", [p["id"] for p in pos]]], fields=PO_LINE_FIELDS) if pos else []
    by_po = {}
    for ln in lines:
        by_po.setdefault(ln["order_id"][0], []).append(ln)
    for p in pos:
        p["lines"] = by_po.get(p["id"], [])
    return pos


def bill_exists(partner_id, invoice_no):
    domain = [["move_type", "=", "in_invoice"], ["partner_id", "=", partner_id], ["ref", "=", invoice_no], ["state", "!=", "cancel"]]
    return _call("account.move", "search_count", domain=domain) > 0


def create_draft_bill(partner_id, invoice_no, invoice_date, lines):
    """lines: list of {"po_line": <row from po_lines>, "quantity": x, "price_unit": y}, or {"name": ..., "quantity": x,
    "price_unit": y} for a vendor that bills without a PO. invoice_date must be "YYYY-MM-DD". The bill stays in draft:
    a person posts it. Linking each line to its PO line (purchase_line_id) makes Odoo update qty_invoiced itself, so a
    second bill for the same goods is caught by Odoo too."""
    bill_lines = []
    for ln in lines:
        if ln.get("po_line"):
            pl = ln["po_line"]
            vals = {"product_id": pl["product_id"][0], "name": pl.get("name") or pl["product_id"][1], "quantity": ln["quantity"],
                    "price_unit": ln["price_unit"], "purchase_line_id": pl["id"]}
            if pl.get("tax_ids") is not None:
                vals["tax_ids"] = [[6, 0, pl["tax_ids"]]]           # the PO line's taxes, not the product's defaults
        else:
            vals = {"name": ln["name"], "quantity": ln["quantity"], "price_unit": ln["price_unit"]}
        bill_lines.append([0, 0, vals])                              # [0, 0, {...}]: "create this child row"
    vals = {"move_type": "in_invoice", "partner_id": partner_id, "ref": invoice_no, "invoice_line_ids": bill_lines}
    if invoice_date:
        vals["invoice_date"] = invoice_date
    return _call("account.move", "create", vals_list=[vals])[0]


def bill_url(bill_id):
    url, _, _ = _cfg()
    return f"{url}/odoo/account.move/{bill_id}"
