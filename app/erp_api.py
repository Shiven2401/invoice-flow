"""The Odoo ERP API: what Invoice Flow has mirrored from Odoo, and the writes that go back (through erp_sync's queue).

Every endpoint needs a signed-in account that is connected to Odoo (ODOO_ACCOUNTS). Writes that change what the
company buys (create, change or approve a PO; add a vendor or a product) are allowed to the accounts in
ODOO_APPROVERS, or to every connected account when it isn't set. Each write takes an optional request_id: the same
request_id twice is one write (a double click, or a retry after a dropped connection).

    GET  /api/erp/status                          connection, mirror freshness, queue
    GET  /api/erp/overview?date_from&date_to&vendor  dashboard figures (invoices, POs, money, matching)
    GET  /api/erp/purchase-orders?state&receipt&q  POs (app id, Odoo id, state, receipt status, totals, sync status)
    GET  /api/erp/purchase-orders/{id}            one PO: lines (ordered/received/billed), receipts, bills, invoices, queue
    POST /api/erp/purchase-orders                 create (as an RFQ in Odoo; "approve": true confirms it too)
    PATCH /api/erp/purchase-orders/{id}           change vendor reference, dates, lines (needs base_version: conflicts refused)
    POST /api/erp/purchase-orders/{id}/approve    approve = Odoo's Confirm Order (Odoo's approval rules still apply)
    GET  /api/erp/vendors?q    POST /api/erp/vendors
    GET  /api/erp/products?q   POST /api/erp/products
    GET  /api/erp/inventory                       products with stock, warehouses, recent receipt moves
    GET  /api/erp/receipts?state                  goods receipts (GRNs) with their moves
    GET  /api/erp/bills?state&payment             vendor bills and credit notes
    GET  /api/erp/options                         taxes, units and payment terms, for the forms
    GET  /api/erp/sync     POST /api/erp/sync     the queue and the mirror; pull now ({"full": true} = reconcile)
    POST /api/erp/sync/{op_id}/retry              try a failed or conflicting write again
    POST /api/odoo/webhook?token=...              called by Odoo's automation rules (no sign-in; the token is the secret)
"""
import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from loguru import logger

from . import auth, db, erp_sync, odoo, rules

router = APIRouter()
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def connected(user: dict = Depends(auth.current_user)):
    if not rules.uses_odoo(user["id"]):
        raise HTTPException(404, "This account isn't connected to Odoo.")
    return user


def approver(user: dict = Depends(connected)):
    allowed = {a.strip().lower() for a in os.getenv("ODOO_APPROVERS", "").split(",") if a.strip()}
    if allowed and user["username"].lower() not in allowed:
        raise HTTPException(403, "Only the accounts allowed to approve purchasing (ODOO_APPROVERS) can change purchase orders, vendors or products.")
    return user


def _fresh():
    """Serve the mirror at once; if it's more than 30 s old, refresh it in the background (the page asks again)."""
    last = db.erp_state("last_pull")
    stale = not last or (datetime.now() - datetime.fromisoformat(last)).total_seconds() > 30
    if stale:
        erp_sync.request_pull(0)          # one pull, however many screens ask at once
    return dict(last_pull=last, refreshing=stale)


def _idem(kind, user, body):
    rid = str(body.get("request_id") or "").strip()[:80]
    basis = rid or json.dumps(body, sort_keys=True, default=str) + time.strftime("%Y%m%d%H%M")
    return f"{kind}:{user['id']}:" + hashlib.sha256(basis.encode()).hexdigest()[:24]


def _row(r):
    return dict(json.loads(r["data_json"] or "{}"), _id=r["id"], _odoo_id=r["odoo_id"], _status=r["sync_status"], _error=r["last_error"],
                _source=r["source"], _synced=r["synced_at"], _version=(r["odoo_write_date"] or "").split("|")[0])


def _rows(model):
    return [_row(r) for r in db.erp_list(model)]


def _q(rec, q, *keys):
    q = (q or "").strip().lower()
    return not q or any(q in str(rec.get(k) if not isinstance(rec.get(k), list) else rec.get(k)[-1]).lower() for k in keys)


def _with_link(model, rec, kind):
    if rec.get("_odoo_id"):
        rec["_odoo_url"] = odoo.link(kind, rec["_odoo_id"])
    return rec


@router.get("/api/erp/status")
def status(user: dict = Depends(connected)):
    return dict(odoo.info(), connected=True, approver=_is_approver(user), webhook=bool(db.erp_state("last_webhook")), **erp_sync.status())


def _is_approver(user):
    allowed = {a.strip().lower() for a in os.getenv("ODOO_APPROVERS", "").split(",") if a.strip()}
    return not allowed or user["username"].lower() in allowed


@router.get("/api/erp/overview")
def overview(user: dict = Depends(connected), date_from: str = "", date_to: str = "", vendor: str = ""):
    for v in (date_from, date_to):
        if v and not DATE_RE.match(v):
            raise HTTPException(400, "Dates are YYYY-MM-DD.")
    if vendor and not vendor.isdigit():
        raise HTTPException(400, "vendor is an Odoo vendor id.")
    fresh = _fresh()
    return dict(erp_sync.overview(db.list_runs(user["id"], limit=-1), date_from or None, date_to or None, vendor or None), fresh=fresh)


# ------------------------------------------------------------ purchase orders --

@router.get("/api/erp/purchase-orders")
def purchase_orders(user: dict = Depends(connected), state: str = "", receipt: str = "", q: str = ""):
    fresh = _fresh()
    rows = [_with_link("purchase.order", p, "po") for p in _rows("purchase.order")
            if (not state or p.get("state") == state or (state == "pending_sync" and p["_status"] in ("pending", "error", "conflict")))
            and (not receipt or p.get("receipt_status") == receipt) and _q(p, q, "name", "partner_id", "partner_ref", "origin")]
    for p in rows:
        p["state_label"] = erp_sync.PO_STATE.get(p.get("state"), p.get("state") or "Waiting for Odoo")
        p["received_pct"] = _pct(p, "qty_received")
        p["billed_pct"] = _pct(p, "qty_invoiced")
        p.pop("order_line", None)
    return dict(purchase_orders=rows, fresh=fresh)


def _pct(p, field):
    ordered = sum(ln.get("product_qty") or 0 for ln in p.get("lines") or [])
    return round(sum(ln.get(field) or 0 for ln in p.get("lines") or []) / ordered * 100, 1) if ordered else 0.0


@router.get("/api/erp/purchase-orders/{record_id}")
def purchase_order(record_id: int, user: dict = Depends(connected)):
    po = erp_sync.po_detail(record_id, db.list_runs(user["id"], limit=-1))
    if not po:
        raise HTTPException(404, "No such purchase order.")
    po["state_label"] = erp_sync.PO_STATE.get(po.get("state"), po.get("state") or "Waiting for Odoo")
    if po.get("_odoo_id"):
        po["_odoo_url"] = odoo.link("po", po["_odoo_id"])
    for b in po["bills"]:
        b["_odoo_url"] = odoo.link("bill", b["_odoo_id"])
    po["can_edit"] = po.get("state") in (None, "draft", "sent", "to approve", "purchase") and po["_status"] != "pending"
    po["can_approve"] = po.get("state") in (None, "draft", "sent")
    return po


def _lines(raw, partial=False):
    """Validate PO lines from a form: product (an Odoo product the mirror knows), quantity, price, taxes, discount."""
    if not isinstance(raw, list) or (not raw and not partial):
        raise HTTPException(400, "A purchase order needs at least one line.")
    products = {p["_odoo_id"]: p for p in _rows("product.product") if p["_odoo_id"]}
    out = []
    for i, ln in enumerate(raw, 1):
        if not isinstance(ln, dict):
            raise HTTPException(400, f"Line {i} isn't a line.")
        item = {}
        if ln.get("id") is not None:
            item["id"] = int(ln["id"])
            if ln.get("delete"):
                out.append(dict(item, delete=True))
                continue
        if ln.get("product_id") is not None or "id" not in item:
            pid = int(ln.get("product_id") or 0)
            if pid not in products:
                raise HTTPException(400, f"Line {i}: choose a product Odoo has (or add it first).")
            item["product_id"] = pid
            item["name"] = str(ln.get("name") or products[pid].get("name"))[:200]
        for key, lo, hi in (("qty", 0.0001, 1e9), ("price", 0, 1e12), ("discount", 0, 100)):
            if ln.get(key) not in (None, ""):
                try:
                    v = float(ln[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"Line {i}: {key} must be a number.")
                if not lo <= v <= hi:
                    raise HTTPException(400, f"Line {i}: {key} is out of range.")
                item[key] = v
            elif key == "qty" and "id" not in item:
                raise HTTPException(400, f"Line {i}: give a quantity.")
        if ln.get("tax_ids") is not None:
            item["tax_ids"] = [int(t) for t in ln["tax_ids"]]
        out.append(item)
    return out


@router.post("/api/erp/purchase-orders")
def create_po(body: dict = Body(...), user: dict = Depends(approver)):
    vendors = {v["_odoo_id"]: v for v in _rows("res.partner") if v["_odoo_id"]}
    vid = int(body.get("vendor_id") or 0)
    if vid not in vendors:
        raise HTTPException(400, "Choose a vendor Odoo has (or add the vendor first).")
    date_planned = str(body.get("date_planned") or "").strip()
    if date_planned and not DATE_RE.match(date_planned):
        raise HTTPException(400, "The expected date is YYYY-MM-DD.")
    lines = _lines(body.get("lines"))
    payload = dict(vendor_id=vid, partner_ref=str(body.get("partner_ref") or "").strip()[:64] or None,
                   date_planned=f"{date_planned} 10:00:00" if date_planned else None, lines=lines, by=user["username"])
    key = _idem("po_create", user, body)
    existing = next((q for q in db.outbox_list(500) if q["idem_key"] == key), None)
    if existing:
        return _po_reply(existing["record_id"], db.outbox_get(existing["id"]))
    products = {p["_odoo_id"]: p for p in _rows("product.product")}
    draft = dict(name=None, partner_id=[vid, vendors[vid]["name"]], partner_ref=payload["partner_ref"], state=None,
                 amount_untaxed=round(sum(ln["qty"] * ln.get("price", 0) * (1 - ln.get("discount", 0) / 100) for ln in lines), 2),
                 lines=[dict(product_id=[ln["product_id"], products.get(ln["product_id"], {}).get("name")], name=ln["name"], product_qty=ln["qty"],
                             price_unit=ln.get("price", 0), qty_received=0, qty_invoiced=0) for ln in lines], date_order=db.now())
    record_id = db.erp_new_local("purchase.order", json.dumps(draft), "new PO", user["id"])
    op = erp_sync.enqueue("po_create", payload, key, user["id"], record_id=record_id)
    if body.get("approve") and op["status"] == "done":
        erp_sync.enqueue("po_confirm", dict(by=user["username"]), f"po_confirm:{record_id}", user["id"], record_id=record_id)
    elif body.get("approve"):    # queued behind the creation: runs once the PO exists in Odoo
        erp_sync.enqueue("po_confirm", dict(by=user["username"]), f"po_confirm:{record_id}", user["id"], record_id=record_id, run_now=False)
    return _po_reply(record_id, op)


def _po_reply(record_id, op):
    rec = db.erp_get(record_id=record_id)
    return dict(id=record_id, odoo_id=rec and rec["odoo_id"], ref=rec and rec["ref"], sync_status=rec and rec["sync_status"],
                queue=dict(id=op["id"], status=op["status"], error=op["last_error"]),
                message="Saved in Odoo." if op["status"] == "done" else
                "Saved here and queued: Odoo didn't answer, so it's sent as soon as it does." if op["status"] == "pending" else
                f"Odoo refused it: {op['last_error']}")


@router.patch("/api/erp/purchase-orders/{record_id}")
def update_po(record_id: int, body: dict = Body(...), user: dict = Depends(approver)):
    rec = db.erp_get(record_id=record_id)
    if not rec or rec["model"] != "purchase.order":
        raise HTTPException(404, "No such purchase order.")
    if not body.get("base_version"):
        raise HTTPException(400, "base_version (the version you edited) is required, so a change made in Odoo meanwhile isn't overwritten.")
    header, summary = {}, []
    if body.get("partner_ref") is not None:
        header["partner_ref"] = str(body["partner_ref"]).strip()[:64]
        summary.append("vendor reference")
    if body.get("date_planned"):
        if not DATE_RE.match(str(body["date_planned"])):
            raise HTTPException(400, "The expected date is YYYY-MM-DD.")
        header["date_planned"] = f"{body['date_planned']} 10:00:00"
        summary.append("expected date")
    if body.get("vendor_id"):
        if int(body["vendor_id"]) not in {v["_odoo_id"] for v in _rows("res.partner")}:
            raise HTTPException(400, "Choose a vendor Odoo has.")
        header["partner_id"] = int(body["vendor_id"])
        summary.append("vendor")
    lines = _lines(body.get("lines") or [], partial=True) if body.get("lines") else []
    if lines:
        summary.append(f"{len(lines)} line change{'s' * (len(lines) != 1)}")
    if not header and not lines:
        raise HTTPException(400, "Nothing to change.")
    payload = dict(base_write_date=body["base_version"], header=header, lines=lines, summary=summary, by=user["username"])
    op = erp_sync.enqueue("po_update", payload, _idem(f"po_update:{record_id}", user, body), user["id"], record_id=record_id)
    return _po_reply(record_id, op)


@router.post("/api/erp/purchase-orders/{record_id}/approve")
def approve_po(record_id: int, body: dict = Body(default={}), user: dict = Depends(approver)):
    rec = db.erp_get(record_id=record_id)
    if not rec or rec["model"] != "purchase.order":
        raise HTTPException(404, "No such purchase order.")
    state = json.loads(rec["data_json"] or "{}").get("state")
    if state in ("purchase", "done"):
        raise HTTPException(400, "This purchase order is already confirmed in Odoo.")
    if state == "cancel":
        raise HTTPException(400, "This purchase order is cancelled in Odoo.")
    op = erp_sync.enqueue("po_confirm", dict(by=user["username"]), f"po_confirm:{record_id}:{rec['odoo_write_date'] or 'new'}", user["id"], record_id=record_id)
    out = _po_reply(record_id, op)
    out["state"] = json.loads((db.erp_get(record_id=record_id) or {}).get("data_json") or "{}").get("state")
    return out


# ------------------------------------------------------------ vendors, products --

@router.get("/api/erp/vendors")
def vendors(user: dict = Depends(connected), q: str = ""):
    fresh = _fresh()
    rows = [_with_link("res.partner", v, "vendor") for v in _rows("res.partner") if _q(v, q, "name", "vat", "email", "city")]
    return dict(vendors=rows, fresh=fresh)


@router.post("/api/erp/vendors")
def create_vendor(body: dict = Body(...), user: dict = Depends(approver)):
    name = str(body.get("name") or "").strip()[:200]
    vat = str(body.get("vat") or "").strip().upper()
    if len(name) < 2:
        raise HTTPException(400, "Give the vendor's name.")
    if vat and not rules.GSTIN_RE.match(vat):
        raise HTTPException(400, f"'{vat}' is not a valid 15-character GSTIN.")
    email = str(body.get("email") or "").strip()[:120]
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "That email address doesn't look right.")
    for v in _rows("res.partner"):                       # never a second vendor for the same GSTIN or name
        if (vat and (v.get("vat") or "").upper() == vat) or (v.get("name") or "").strip().lower() == name.lower():
            raise HTTPException(409, f"Odoo already has this vendor: {v.get('name')} (#{v['_odoo_id']}).")
    payload = dict(name=name, vat=vat or None, email=email or None, phone=str(body.get("phone") or "").strip()[:40] or None,
                   street=str(body.get("street") or "").strip()[:200] or None, city=str(body.get("city") or "").strip()[:80] or None,
                   zip=str(body.get("zip") or "").strip()[:12] or None, payment_term_id=int(body["payment_term_id"]) if body.get("payment_term_id") else None,
                   by=user["username"])
    record_id = db.erp_new_local("res.partner", json.dumps(dict(payload, id=None)), name, user["id"])
    op = erp_sync.enqueue("vendor_create", payload, _idem("vendor_create", user, body), user["id"], record_id=record_id)
    return _po_reply(record_id, op)


@router.get("/api/erp/products")
def products(user: dict = Depends(connected), q: str = ""):
    fresh = _fresh()
    return dict(products=[p for p in _rows("product.product") if _q(p, q, "name", "default_code", "barcode", "categ_id")], fresh=fresh)


@router.post("/api/erp/products")
def create_product(body: dict = Body(...), user: dict = Depends(approver)):
    name = str(body.get("name") or "").strip()[:200]
    code = str(body.get("default_code") or "").strip()[:60]
    if len(name) < 2:
        raise HTTPException(400, "Give the product's name.")
    for p in _rows("product.product"):
        if (code and (p.get("default_code") or "") == code) or (p.get("name") or "").strip().lower() == name.lower():
            raise HTTPException(409, f"Odoo already has this product: {p.get('name')} (#{p['_odoo_id']}).")
    try:
        price = float(body.get("standard_price") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "The purchase price must be a number.")
    payload = dict(name=name, default_code=code or None, kind="service" if body.get("kind") == "service" else "goods", standard_price=price,
                   tax_ids=[int(t) for t in body["tax_ids"]] if body.get("tax_ids") is not None else None,
                   uom_id=int(body["uom_id"]) if body.get("uom_id") else None, by=user["username"])
    record_id = db.erp_new_local("product.product", json.dumps(dict(payload, id=None)), code or name, user["id"])
    op = erp_sync.enqueue("product_create", payload, _idem("product_create", user, body), user["id"], record_id=record_id)
    return _po_reply(record_id, op)


# ------------------------------------------------------- inventory, receipts, bills --

@router.get("/api/erp/inventory")
def inventory(user: dict = Depends(connected), q: str = ""):
    fresh = _fresh()
    prods = [p for p in _rows("product.product") if _q(p, q, "name", "default_code")]
    receipts = _rows("stock.picking")
    moves = sorted(({**mv, "receipt": r.get("name"), "po": (r.get("purchase_id") or [None, None])[1]} for r in receipts for mv in r.get("moves") or []),
                   key=lambda m: m.get("date") or "", reverse=True)[:100]
    pending = [r for r in receipts if r.get("state") not in ("done", "cancel")]
    return dict(products=prods, warehouses=_options().get("warehouses", []), moves=moves, pending_receipts=len(pending),
                stock_value=round(sum((p.get("qty_available") or 0) * (p.get("standard_price") or 0) for p in prods), 2), fresh=fresh)


@router.get("/api/erp/receipts")
def receipts(user: dict = Depends(connected), state: str = ""):
    fresh = _fresh()
    return dict(receipts=[r for r in _rows("stock.picking") if not state or r.get("state") == state], fresh=fresh)


@router.get("/api/erp/bills")
def bills(user: dict = Depends(connected), state: str = "", payment: str = ""):
    fresh = _fresh()
    rows = [dict(b, _odoo_url=odoo.link("bill", b["_odoo_id"])) for b in _rows("account.move")
            if (not state or b.get("state") == state) and (not payment or b.get("payment_state") == payment)]
    return dict(bills=rows, fresh=fresh)


def _options():
    cached = db.erp_state("options")
    if cached:
        data = json.loads(cached)
        if time.time() - data.get("_at", 0) < 600:
            return data
    try:
        data = dict(taxes=odoo._call("account.tax", "search_read", domain=[["type_tax_use", "=", "purchase"], ["active", "=", True]], fields=["id", "name", "amount"]),
                    uoms=odoo._call("uom.uom", "search_read", domain=[], fields=["id", "name"]),
                    payment_terms=odoo._call("account.payment.term", "search_read", domain=[], fields=["id", "name"]),
                    warehouses=odoo._call("stock.warehouse", "search_read", domain=[], fields=["id", "name", "code"]), _at=time.time())
        db.erp_state("options", json.dumps(data))
        return data
    except odoo.OdooError:
        return json.loads(cached) if cached else {}


@router.get("/api/erp/options")
def options(user: dict = Depends(connected)):
    return _options()


# ------------------------------------------------------------------ the sync --

@router.get("/api/erp/sync")
def sync_log(user: dict = Depends(connected), status: str = ""):
    rows = db.outbox_list(200, status or None)
    names = {u: db.username(u) for u in {r["user_id"] for r in rows}}
    return dict(entries=[dict(id=r["id"], op=r["op"], record_id=r["record_id"], status=r["status"], attempts=r["attempts"], error=r["last_error"],
                            by=names.get(r["user_id"]), at=r["created_at"], done_at=r["done_at"], result=json.loads(r["result_json"] or "null"))
                       for r in rows], **erp_sync.status())


@router.post("/api/erp/sync")
def sync_now(body: dict = Body(default={}), user: dict = Depends(connected)):
    result = erp_sync.pull_all(full=bool(body.get("full")))
    erp_sync.run_due()
    return dict(result=result, **erp_sync.status())


@router.post("/api/erp/sync/{op_id}/retry")
def sync_retry(op_id: int, user: dict = Depends(approver)):
    row = erp_sync.retry(op_id)
    if not row:
        raise HTTPException(404, "No such queue entry.")
    return dict(id=row["id"], status=row["status"], error=row["last_error"])


# ----------------------------------------------- dry run, vendor sync, backfill --
# The preview is read-only (Odoo is only read). Sending vendors and backfilling old invoices each need an approver and
# {"confirm": true}: nothing reaches Odoo by accident.

def previewer(user: dict = Depends(auth.current_user)):
    preview = {a.strip().lower() for a in os.getenv("ODOO_PREVIEW_ACCOUNTS", "").split(",") if a.strip()}
    if not odoo.enabled() or not (rules.uses_odoo(user["id"]) or user["username"].lower() in preview):
        raise HTTPException(404, "This account has no Odoo preview.")
    return user


def _partners():
    return odoo._call("res.partner", "search_read", domain=["|", ["supplier_rank", ">", 0], ["vat", "!=", False]],
                      fields=["id", "name", "vat", "supplier_rank"])


def build_preview(user_id):
    """What sending this account's vendors and decided invoices to Odoo would do. Reads Odoo; writes nothing."""
    from . import bill_sync, extract
    partners, links = _partners(), db.vendor_links(user_id)
    master_ids = {v["vendor_id"] for v in rules._app_vendors(None)}
    vendors, vendor_partner = [], {}
    for v in rules._app_vendors(user_id):
        link = links.get(v["vendor_id"]) or {}
        if link.get("odoo_id"):
            action, oid, why = "linked already", link["odoo_id"], link.get("detail") or ""
        else:
            action, oid, why = bill_sync.vendor_plan(v["name"], v.get("gstin"), v.get("status"), partners)
        vendor_partner[v["vendor_id"]] = oid if action in ("match", "linked already") else ("new" if action == "create" else None)
        vendors.append(dict(vendor_id=v["vendor_id"], name=v["name"], clean_name=extract.clean_vendor_name(v["name"]), gstin=v.get("gstin") or "",
                            status=v.get("status"), list="master list" if v["vendor_id"] in master_ids else "approved in Invoice Flow",
                            action=action, odoo_id=oid, why=why))
    names = {}
    for v in vendors:
        if v["clean_name"] != v["name"]:
            names.setdefault(v["name"], dict(before=v["name"], after=v["clean_name"], where=set()))["where"].add("vendor list")
    key_links = {}
    with db._lock:
        for r in db.conn().execute("SELECT * FROM invoice_bills"):
            key_links[r["invoice_key"]] = dict(r)
    invoices = []
    for r in reversed(db.list_runs(user_id, limit=-1)):      # oldest first, as the backfill runs: an original before its resubmission
        if r["status"] not in ("done", "error"):
            continue
        full = db.get_run(r["id"], user_id) or {}
        res = json.loads(full.get("result_json") or "null") or {}
        f, vend = res.get("fields") or {}, res.get("vendor") or {}
        decision = r.get("final_decision") or r.get("decision") or "HOLD"
        raw = f.get("vendor_name") or r.get("vendor_name") or ""
        if raw and extract.clean_vendor_name(raw) != raw:
            names.setdefault(raw, dict(before=raw, after=extract.clean_vendor_name(raw), where=set()))["where"].add("invoices")
        f2 = dict(f, vendor_name=extract.clean_vendor_name(raw)) if raw else f
        codes = set(r.get("codes") or [])
        action, state = bill_sync.preview_bill(f2, vend, decision, codes, key_links)
        key = bill_sync.invoice_key(f2, vend)
        if key and not full.get("odoo_bill_id") and not action.startswith("note on the original") and key not in key_links:
            key_links[key] = dict(odoo_bill_id="(new)", state=state)   # the bills made earlier in this same run
        partner = vendor_partner.get(vend.get("vendor_id")) if vend else None
        if vend and str(vend.get("vendor_id", "")).startswith("OD"):
            partner = int(vend["vendor_id"][2:])
        invoices.append(dict(run_id=r["id"], file=r["filename"], invoice_number=r.get("invoice_number") or "", vendor=extract.clean_vendor_name(raw) or "unknown",
                             decision=decision, amount=r.get("total"), date=r.get("invoice_date") or "", reasons=", ".join(sorted(codes)) or "-",
                             bill_action=action, bill_state={"draft": "draft", "cancel": "cancelled", "posted": "posted (unchanged)", "note": "no new bill"}[state],
                             bill_vendor=("Odoo vendor #%s" % partner) if isinstance(partner, int) else
                             "vendor created in Odoo first" if partner == "new" else "no vendor (name in the note)",
                             has_bill=bool(full.get("odoo_bill_id"))))
    pending = [i for i in invoices if not i["has_bill"]]
    count = lambda pred: sum(1 for i in pending if pred(i))
    return dict(
        vendors=vendors, cleaned_names=[dict(n, where=sorted(n["where"])) for n in names.values()],
        vendor_summary=dict(match=sum(1 for v in vendors if v["action"] == "match"), create=sum(1 for v in vendors if v["action"] == "create"),
                            skip=sum(1 for v in vendors if v["action"] == "skip"), linked=sum(1 for v in vendors if v["action"] == "linked already")),
        invoices=invoices,
        bill_summary=dict(draft_approved=count(lambda i: i["decision"] == "APPROVE" and i["bill_state"] == "draft"),
                          draft_on_hold=count(lambda i: i["decision"] == "HOLD" and i["bill_state"] == "draft"),
                          cancelled_rejected=count(lambda i: i["bill_state"] == "cancelled"),
                          duplicate_notes=count(lambda i: i["bill_action"].startswith("note on the original")),
                          already_billed=len(invoices) - len(pending), total_amount=round(sum(i["amount"] or 0 for i in pending), 2)),
        connected=rules.uses_odoo(user_id), generated_at=db.now())


@router.get("/api/erp/preview")
def preview(user: dict = Depends(previewer)):
    try:
        return build_preview(user["id"])
    except odoo.OdooError as e:
        raise HTTPException(502, f"Odoo didn't answer, so the preview can't be made ({e}).")


@router.get("/api/erp/preview.csv")
def preview_csv(user: dict = Depends(previewer)):
    import csv
    import io
    from fastapi.responses import Response
    p = preview(user)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["section", "id", "name / file", "vendor", "GSTIN / invoice no.", "decision / list", "amount", "action", "Odoo state / id", "why / reasons"])
    for v in p["vendors"]:
        w.writerow(["vendor", v["vendor_id"], v["name"], v["clean_name"], v["gstin"], v["list"], "", v["action"], v["odoo_id"] or "", v["why"]])
    for n in p["cleaned_names"]:
        w.writerow(["cleaned name", "", n["before"], n["after"], "", ", ".join(n["where"]), "", "label removed", "", ""])
    for i in p["invoices"]:
        w.writerow(["invoice", i["run_id"], i["file"], i["vendor"], i["invoice_number"], i["decision"], i["amount"] if i["amount"] is not None else "",
                    "already has a bill" if i["has_bill"] else i["bill_action"], i["bill_state"], i["reasons"]])
    return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="odoo-sync-preview.csv"'})


@router.post("/api/erp/sync-vendors")
def sync_vendors(body: dict = Body(default={}), user: dict = Depends(approver)):
    """Send the app's approved vendors to Odoo as the preview showed (match by GSTIN, then cleaned name; else create;
    no GSTIN: flagged). Needs {"confirm": true}."""
    if body.get("confirm") is not True:
        raise HTTPException(400, 'Send {"confirm": true} once you have checked the preview.')
    master_ids = {v["vendor_id"] for v in rules._app_vendors(None)}
    queued = flagged = skipped = 0
    for v in rules._app_vendors(user["id"]):
        if v.get("status") != "approved":
            skipped += 1
            continue
        scope = erp_sync.MASTER if v["vendor_id"] in master_ids else user["id"]
        op = erp_sync.link_vendor_later(user["id"], v["vendor_id"], v["name"], v.get("gstin"), user["username"], scope=scope)
        queued, flagged = queued + bool(op), flagged + (not op)
    return dict(queued=queued, flagged_no_gstin=flagged, skipped_not_approved=skipped, **erp_sync.status())


@router.post("/api/erp/backfill")
def backfill(body: dict = Body(default={}), user: dict = Depends(approver)):
    """Give every already-decided invoice of this account (without a bill yet) its bill, as the preview listed.
    Needs {"confirm": true}. Queued, in order, and safe to repeat (one bill per invoice)."""
    if body.get("confirm") is not True:
        raise HTTPException(400, 'Send {"confirm": true} once you have checked the backfill list.')
    queued = 0
    for r in reversed(db.list_runs(user["id"], limit=-1)):   # oldest first: an original gets its bill before a resubmission is noted on it
        if r["status"] in ("done", "error") and not r.get("odoo_bill_id"):
            erp_sync.sync_bill_later(r["id"], user["id"], r.get("final_decision") or r.get("decision") or "HOLD",
                                     note="backfill of an invoice decided before the Odoo sync", acting_user=user["username"], run_now=False)
            queued += 1
    threading.Thread(target=erp_sync.run_due, kwargs=dict(limit=1000), daemon=True).start()
    return dict(queued=queued)


# ------------------------------------------------------------------ webhook --

_hits = []


@router.post("/api/odoo/webhook")
async def webhook(request: Request, token: str = ""):
    """Odoo's automation rules call this when a PO, receipt, bill, vendor or product is created or changed. The body
    only says which record; it's then read from Odoo, so a forged body can do no more than trigger a re-read."""
    expected = odoo.webhook_token()
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(403, "Bad token.")
    now = time.time()
    _hits[:] = [t for t in _hits if now - t < 60] + [now]
    if len(_hits) > 600:
        raise HTTPException(429, "Too many calls.")
    try:
        body = await request.json()
    except ValueError:
        body = {}
    model, rec_id = str(body.get("_model") or body.get("model") or ""), body.get("_id") or body.get("id")
    if model not in erp_sync.MODELS or not str(rec_id or "").isdigit():
        return dict(ok=True, ignored=True)
    threading.Thread(target=_safe_webhook, args=(model, int(rec_id)), daemon=True).start()
    return dict(ok=True, model=model, id=int(rec_id))


def _safe_webhook(model, rec_id):
    try:
        erp_sync.on_webhook(model, rec_id)
    except Exception as e:                                       # the next pull catches up
        logger.warning("webhook {} {}: {}", model, rec_id, e)
