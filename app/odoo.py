"""Odoo connector: reads vendors, POs and receipts; creates draft vendor bills.

This file is plumbing: it only talks to Odoo. Every decision stays in rules.py.

Two ways in, the same calls either way (ODOO_PROTOCOL):
    xmlrpc   Odoo's classic external API: /xmlrpc/2/common authenticate(db, login, key) gives a user id, then
             /xmlrpc/2/object execute_kw(db, uid, key, model, method, args, kwargs). Needs ODOO_USER (the login).
             Odoo marks it deprecated from version 19, but version 20 still serves it.
    json2    Odoo 19+'s JSON-2 API (the default): one HTTPS POST per call to /json/2/<model>/<method>, the API key as a
             bearer token, every argument by name.
The functions, and what they return, are the ones the code guide describes.

Settings (all three, or the app keeps using its CSVs):
    ODOO_URL       https://invoice-flow.odoo.com
    ODOO_DB        invoice-flow
    ODOO_API_KEY   avatar > My Preferences > Account Security > New API Key (never in code or a commit)
Optional:
    ODOO_PROTOCOL  json2 (default) or xmlrpc
    ODOO_USER      the login the API key belongs to (needed for xmlrpc)
    ODOO_ACCOUNTS  Invoice Flow account names that use Odoo, comma-separated. Empty means every account. On a public
                   server set it, so other people's invoices never reach your Odoo and the built-in demo keeps working.
"""
import base64
import os
import threading
import xmlrpc.client

import requests

TIMEOUT = 15          # seconds per call: a slow Odoo holds an invoice ("ERP unreachable") instead of hanging the worker
BLOCKED_TAG = "Blocked"   # a vendor carrying this tag in Odoo is treated as blocked: no payments
USER_TAG = "Invoice Flow User"   # the tag on the contact each Invoice Flow account gets in Odoo
# the Odoo screens the app links to: window actions by XML id, so links keep working if someone renames the friendly paths
ACTIONS = dict(bill="account.action_move_in_invoice_type", po="purchase.purchase_form_action",
               vendor="account.res_partner_action_supplier")


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


def protocol():
    return "xmlrpc" if os.getenv("ODOO_PROTOCOL", "").strip().lower() in ("xmlrpc", "xml-rpc") else "json2"


def link(kind, rec_id=None):
    """A page in Odoo's web client: the list (no id) or one record of a bill, PO or vendor; "contact" is any partner."""
    url, _, _ = _cfg()
    if kind == "contact":
        return f"{url}/odoo/res.partner/{rec_id}"
    return f"{url}/odoo/action-{ACTIONS[kind]}" + (f"/{rec_id}" if rec_id else "")


def info():
    """For the page: where the account's data comes from, and links into Odoo. Never the key."""
    url, db, _ = _cfg()
    return dict(system="Odoo", url=url, db=db, protocol="XML-RPC" if protocol() == "xmlrpc" else "JSON-2",
                home=f"{url}/odoo", bills=link("bill"), pos=link("po"), vendors=link("vendor"))


def _call(model, method, **params):
    """The one door into Odoo: a model (table), a method (search_read, read, create, ...) and its arguments by name."""
    return _xmlrpc(model, method, params) if protocol() == "xmlrpc" else _json2(model, method, params)


_uid = {}                    # (url, db, login) -> the user id XML-RPC's authenticate gave
_uid_lock = threading.Lock()


class _Transport(xmlrpc.client.SafeTransport):
    """xmlrpc.client with the same timeout as the JSON-2 calls."""

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = TIMEOUT
        return conn


def _xmlrpc(model, method, params):
    """execute_kw takes positional args and keyword args: the record ids (if any) go first, everything else by name, so
    each call reads the same as its JSON-2 twin."""
    url, db, key = _cfg()
    login = os.getenv("ODOO_USER", "").strip()
    if not login:
        raise OdooError("ODOO_PROTOCOL=xmlrpc needs ODOO_USER, the login the API key belongs to")
    proxy = lambda path: xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/{path}", transport=_Transport(), allow_none=True)
    try:
        with _uid_lock:
            uid = _uid.get((url, db, login))
            if not uid:
                uid = proxy("common").authenticate(db, login, key, {})
                if not uid:
                    raise OdooError("Odoo refused the login and API key (check ODOO_USER and ODOO_API_KEY)")
                _uid[(url, db, login)] = uid
        params = dict(params)
        args = [params.pop("ids")] if "ids" in params else []
        if method == "create":   # XML-RPC's create reads its records from the first positional argument only
            args = [params.pop("vals_list")]
        return proxy("object").execute_kw(db, uid, key, model, method, args, params)
    except xmlrpc.client.Fault as e:
        msg = (str(e.faultString or "").strip().splitlines() or ["no reason given"])[-1][:300]
        if "AccessDenied" in str(e.faultString) or "Access Denied" in str(e.faultString):
            raise OdooError("Odoo refused the API key (check ODOO_API_KEY and that the user can use Purchase and Accounting)") from e
        raise OdooError(f"Odoo rejected {model}.{method}: {msg}") from e
    except xmlrpc.client.ProtocolError as e:
        if e.errcode in (502, 503, 504):
            raise OdooUnreachable(f"Odoo is unavailable (HTTP {e.errcode})") from e
        raise OdooError(f"Odoo's XML-RPC endpoint answered HTTP {e.errcode}") from e
    except (OSError, xmlrpc.client.ResponseError) as e:   # DNS, refused, timeout, TLS
        raise OdooUnreachable(f"Odoo didn't answer ({type(e).__name__})") from e


def _json2(model, method, params):
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
    return link("bill", bill_id)


def po_by_name(po_ref):
    """{id, name} of a PO by its number, whatever its state, or None."""
    rows = _call("purchase.order", "search_read", domain=[["name", "=", po_ref]], fields=["id", "name"], limit=1)
    return rows[0] if rows else None


# ------------------------------------------------------------- phase 1: users --

_tags = {}


def tag_id(name):
    """The contact tag with this name, made if Odoo doesn't have it yet."""
    if name not in _tags:
        rows = _call("res.partner.category", "search_read", domain=[["name", "=ilike", name]], fields=["id"], limit=1)
        _tags[name] = rows[0]["id"] if rows else _call("res.partner.category", "create", vals_list=[{"name": name}])[0]
    return _tags[name]


def upsert_contact(ref, name, details_html, email=None):
    """The contact for an Invoice Flow account, found by its reference (so signing in again never makes a second one),
    tagged "Invoice Flow User". Returns the partner id."""
    tag = tag_id(USER_TAG)
    vals = {"name": name, "ref": ref, "comment": details_html, "category_id": [[4, tag]]}   # [4, id]: add the tag
    if email:
        vals["email"] = email
    found = _call("res.partner", "search_read", domain=[["ref", "=", ref]], fields=["id"], limit=1)
    if found:
        _call("res.partner", "write", ids=[found[0]["id"]], vals={"category_id": [[4, tag]], "comment": details_html})
        return found[0]["id"]
    return _call("res.partner", "create", vals_list=[vals])[0]


# ------------------------------------------------- phases 2 and 3: the audit trail --

def attach(model, res_id, filename, data):
    """Attach a file (bytes) to a record. Odoo 20 takes the content as `raw`, base64 in JSON or XML-RPC."""
    return _call("ir.attachment", "create", vals_list=[{"name": filename, "raw": base64.b64encode(data).decode(),
                                                         "res_model": model, "res_id": res_id}])[0]


def note(model, res_id, html, attachment_ids=()):
    """An internal note in the record's chatter (seen by the team, never emailed to the vendor). Returns its id."""
    mid = _call(model, "message_post", ids=[res_id], body=html, body_is_html=True, message_type="comment",
                subtype_xmlid="mail.mt_note", attachment_ids=list(attachment_ids))
    return mid[0] if isinstance(mid, list) else mid


# ------------------------------------------------- phase 4: back from Odoo --

def bill_states(bill_ids):
    """{bill id: (state, payment_state)} for the bills that still exist; a deleted bill is simply missing."""
    if not bill_ids:
        return {}
    rows = _call("account.move", "search_read", domain=[["id", "in", list(bill_ids)]], fields=["id", "state", "payment_state"])
    return {r["id"]: (r["state"], r["payment_state"]) for r in rows}
