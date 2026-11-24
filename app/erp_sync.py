"""Two-way sync between Invoice Flow and Odoo (odoo.py does the talking; this decides what to send and when).

Odoo -> Invoice Flow: a mirror (db table erp_records) of Odoo's purchase orders (with their lines), vendors, products
(with stock on hand), receipts (with their moves) and vendor bills/credit notes. Kept fresh three ways:
  - events: Odoo automation rules call POST /api/odoo/webhook when one of those records is created or changed
    (tools/odoo_webhooks.py sets them up); the record is fetched from Odoo, never taken from the request
  - incremental pull every minute, by write_date, plus the records a change touches without its own write_date
    moving (a receipt changes a PO line's received quantity and a product's stock; a payment changes a bill)
  - a full reconcile every 15 minutes, which also notices records deleted or archived in Odoo

Invoice Flow -> Odoo: every write goes through a queue (db table erp_outbox), in order, and is kept after it's done as
the audit trail of what the app changed in Odoo and who asked. A write is tried at once; if Odoo is down it waits and
is retried with exponential backoff (15 s doubling up to 15 min), forever for a network problem, three times for a
refusal before it waits for a person (POST /api/erp/sync/<id>/retry). Nothing is dropped. Each handler looks before
it creates (a PO by its origin "Invoice Flow #<id>", a vendor by GSTIN or name, a product by reference or name, a bill
by vendor and number), so a retry after a timeout never makes a second record.

Conflicts: an edit made in the app carries the Odoo write_date it was based on. If Odoo changed the record since, the
edit is not applied: it's marked "conflict", the mirror shows Odoo's version and the person redoes the edit. Pulls
never write to Odoo, so a change can't bounce between the systems.

Invoice decisions never use the mirror: the checks read Odoo live (rules.load_pos / load_vendors), and an Odoo that
doesn't answer holds the invoice. The mirror serves the dashboard, the backend API and the screens.
"""
import json
import threading
import time
from datetime import date, datetime

from loguru import logger

from . import db, odoo

FULL_EVERY, PULL_EVERY, QUEUE_EVERY = 900, 60, 15
BACKOFF_MAX = 900
PO_LINE_FIELDS = ["id", "order_id", "name", "product_id", "product_qty", "qty_received", "qty_invoiced", "price_unit", "tax_ids",
                  "discount", "uom_id", "date_planned", "price_subtotal", "price_total", "write_date"]
MODELS = {
    "purchase.order": dict(domain=[], fields=["id", "name", "partner_id", "partner_ref", "origin", "state", "receipt_status", "invoice_status",
                                              "date_order", "date_approve", "date_planned", "amount_untaxed", "amount_tax", "amount_total",
                                              "currency_id", "payment_term_id", "user_id", "picking_ids", "invoice_ids", "order_line", "write_date"]),
    "res.partner": dict(domain=[["supplier_rank", ">", 0]], fields=["id", "name", "vat", "email", "phone", "street", "street2", "city", "zip",
                                                                     "state_id", "country_id", "property_supplier_payment_term_id", "category_id",
                                                                     "ref", "write_date"]),
    "product.product": dict(domain=[["purchase_ok", "=", True]], fields=["id", "name", "default_code", "barcode", "categ_id", "uom_id",
                                                                          "standard_price", "supplier_taxes_id", "type", "is_storable",
                                                                          "qty_available", "incoming_qty", "virtual_available", "write_date"]),
    "stock.picking": dict(domain=[["picking_type_code", "=", "incoming"]], fields=["id", "name", "state", "origin", "purchase_id", "partner_id",
                                                                                   "scheduled_date", "date_done", "location_dest_id", "move_ids",
                                                                                   "write_date"]),
    "account.move": dict(domain=[["move_type", "in", ["in_invoice", "in_refund"]]], fields=["id", "name", "ref", "move_type", "state", "payment_state",
                                                                                            "partner_id", "invoice_date", "invoice_date_due",
                                                                                            "amount_untaxed", "amount_tax", "amount_total",
                                                                                            "amount_residual", "invoice_origin", "write_date"]),
}
MOVE_FIELDS = ["id", "picking_id", "product_id", "product_uom_qty", "quantity", "state", "purchase_line_id", "date", "location_dest_id"]


class Conflict(Exception):
    """Odoo changed the record after the person opened it: their edit isn't applied."""


# ======================================================== Odoo -> Invoice Flow --

def _fetch(model, ids):
    """Full records from Odoo, with a PO's lines and a receipt's moves inside."""
    ids = list(ids)
    if not ids:
        return []
    out = []
    for i in range(0, len(ids), 200):
        out += odoo._call(model, "read", ids=ids[i:i + 200], fields=MODELS[model]["fields"])
    if model == "purchase.order":
        lines = odoo._call("purchase.order.line", "search_read", domain=[["order_id", "in", ids]], fields=PO_LINE_FIELDS)
        by = {}
        for ln in lines:
            by.setdefault(ln["order_id"][0], []).append(ln)
        for r in out:
            r["lines"] = by.get(r["id"], [])
    elif model == "stock.picking":
        moves = odoo._call("stock.move", "search_read", domain=[["picking_id", "in", ids]], fields=MOVE_FIELDS)
        by = {}
        for mv in moves:
            by.setdefault(mv["picking_id"][0], []).append(mv)
        for r in out:
            r["moves"] = by.get(r["id"], [])
    return out


def _ref(model, rec):
    return rec.get("default_code") or rec.get("name") or str(rec["id"]) if model == "product.product" else rec.get("name") or str(rec["id"])


def store(model, rec, source="odoo", record_id=None, created_by=None):
    """Put one record from Odoo in the mirror. Returns (row id, changed)."""
    # a product's stock moves without its write_date moving: count its quantities as part of the version
    version = rec.get("write_date") or ""
    if model == "product.product":
        version += f"|{rec.get('qty_available')}|{rec.get('incoming_qty')}"
    elif model == "purchase.order":
        version += "|" + ",".join(f"{ln['qty_received']}/{ln['qty_invoiced']}" for ln in rec.get("lines", []))
    elif model == "account.move":
        version += f"|{rec.get('payment_state')}|{rec.get('amount_residual')}"
    return db.erp_upsert(model, rec["id"], _ref(model, rec), json.dumps(rec, default=str), version, source=source,
                         record_id=record_id, created_by=created_by)


def _touched(model, since):
    """Ids of `model` whose data changed after `since` without their own write_date moving, and the newest write_date
    among the records that showed it (Odoo's clock, so the next pull starts there)."""
    ids, seen = set(), []

    def rows(m, domain, fields):
        out = odoo._call(m, "search_read", domain=domain, fields=fields + ["write_date"])
        seen.extend(r["write_date"] for r in out if r.get("write_date"))
        return out
    try:
        if model == "purchase.order":
            ids |= {r["order_id"][0] for r in rows("purchase.order.line", [["write_date", ">=", since]], ["order_id"])}
            ids |= {r["purchase_id"][0] for r in rows("stock.picking", [["write_date", ">=", since], ["purchase_id", "!=", False]], ["purchase_id"])}
            lines = rows("account.move.line", [["write_date", ">=", since], ["purchase_line_id", "!=", False]], ["purchase_line_id"])
            if lines:
                pl = odoo._call("purchase.order.line", "read", ids=list({ln["purchase_line_id"][0] for ln in lines}), fields=["order_id"])
                ids |= {r["order_id"][0] for r in pl}
        elif model == "product.product":
            ids |= {r["product_id"][0] for r in rows("stock.move", [["write_date", ">=", since]], ["product_id"])}
        elif model == "account.move":
            for p in rows("account.payment", [["write_date", ">=", since]], ["reconciled_bill_ids"]):
                ids |= set(p.get("reconciled_bill_ids") or [])
    except odoo.OdooUnreachable:
        raise
    except odoo.OdooError as e:                                 # an optional link Odoo doesn't offer: the full reconcile covers it
        logger.debug("sync: related changes for {} not read: {}", model, e)
    return ids, max(seen, default=None)


def pull(model, full=False):
    """Bring the mirror of one model up to date. Returns {"changed": n, "deleted": n}."""
    spec, cursor = MODELS[model], db.erp_state(f"cursor:{model}")
    related_newest = None
    if full or not cursor:
        rows = odoo._call(model, "search_read", domain=spec["domain"], fields=["id", "write_date"])
        known = {r["odoo_id"]: r["odoo_write_date"] for r in db.erp_list(model, include_deleted=True) if r["odoo_id"]}
        # products' quantities and POs' received/billed quantities change without write_date: re-read those every time
        stale = [r["id"] for r in rows if model in ("product.product", "purchase.order", "account.move")
                 or (known.get(r["id"]) or "").split("|")[0] != r["write_date"]]
        deleted = db.erp_mark_missing(model, [r["id"] for r in rows])
    else:
        rows = odoo._call(model, "search_read", domain=spec["domain"] + [["write_date", ">=", cursor]], fields=["id", "write_date"])
        touched, related_newest = _touched(model, cursor)
        stale = sorted({r["id"] for r in rows} | touched)
        deleted = 0
    changed = sum(store(model, rec)[1] for rec in _fetch(model, stale))
    # the cursor is always one of Odoo's own write_dates (UTC), never this machine's clock
    newest = max([r["write_date"] for r in rows if r.get("write_date")] + [x for x in (cursor, related_newest) if x], default=None)
    if newest:
        db.erp_state(f"cursor:{model}", newest)
    db.erp_state(f"pulled:{model}", db.now())
    return dict(changed=changed, deleted=deleted)


_pull_lock = threading.Lock()


def pull_all(full=False):
    """Every model, in an order where a PO's vendor and products arrive before it. Returns {model: result or error}."""
    if not odoo.enabled():
        return {}
    if not _pull_lock.acquire(timeout=30):
        return {"busy": "another sync is running"}
    try:
        out = {}
        for model in ("res.partner", "product.product", "purchase.order", "stock.picking", "account.move"):
            try:
                out[model] = pull(model, full=full)
                if db.erp_state(f"error:{model}"):
                    db.erp_state(f"error:{model}", "")       # recovered: the Sync tab shows only current problems
            except odoo.OdooError as e:
                out[model] = dict(error=str(e))
                db.erp_state(f"error:{model}", f"{db.now()} {e}")
                if isinstance(e, odoo.OdooUnreachable):
                    break                                        # Odoo is down: the rest would fail too
        db.erp_state("last_pull", db.now())
        if full:
            db.erp_state("last_full", db.now())
        return out
    finally:
        _pull_lock.release()


def ensure_fresh(max_age=30):
    """Pull if the mirror is older than max_age seconds. Never raises: a screen shows the mirror when Odoo is down."""
    last = db.erp_state("last_pull")
    if last and (datetime.now() - datetime.fromisoformat(last)).total_seconds() < max_age:
        return
    try:
        pull_all()
    except Exception as e:                                        # a screen still opens on what the mirror has
        logger.warning("sync: pull before showing the ERP screen failed: {}", e)


_req_lock = threading.Lock()
_pull_requested = [False]
_recent = {}                 # (model, id) -> when it was last fetched for a webhook
COALESCE = 8                 # seconds: a burst of webhooks (a bulk import, a confirmed PO touching lines and receipts) = one pull


def request_pull(delay=COALESCE):
    """Ask for one incremental pull shortly. Requests made meanwhile join it, so Odoo isn't flooded (Odoo Online
    rate-limits a server that sends too many calls)."""
    with _req_lock:
        if _pull_requested[0]:
            return False
        _pull_requested[0] = True

    def run():
        with _req_lock:
            _pull_requested[0] = False
        try:
            pull_all()
        except Exception as e:                                    # the scheduled pull catches up
            logger.warning("sync: pull after webhooks failed: {}", e)
    threading.Timer(delay, run).start()
    return True


def on_webhook(model, rec_id):
    """Odoo says this record changed: fetch it (the request's own data is never trusted), then catch up on what it touched."""
    if model not in MODELS:
        return dict(ignored=model)
    now = time.time()
    if now - _recent.get((model, rec_id), 0) < COALESCE:          # the same record again within seconds: the coming pull covers it
        request_pull()
        return dict(model=model, id=rec_id, coalesced=True)
    _recent[(model, rec_id)] = now
    if len(_recent) > 5000:
        _recent.clear()
    recs = _fetch(model, [rec_id])
    if not recs:                                                  # deleted, archived, or outside what's mirrored
        row = db.erp_get(model, rec_id)
        if row:
            db.erp_mark(row["id"], "deleted")
        return dict(model=model, id=rec_id, found=False)
    changed = store(model, recs[0])[1]
    db.erp_state("last_webhook", f"{db.now()} {model} {rec_id}")
    request_pull()                                                # a receipt moves PO quantities and stock: pick those up too
    return dict(model=model, id=rec_id, changed=changed)


# ======================================================== Invoice Flow -> Odoo --

HANDLERS = {}
RECHECK = [None]   # set by main: re-run a held invoice's checks (main._requeue)


def handler(op):
    def wrap(fn):
        HANDLERS[op] = fn
        return fn
    return wrap


def enqueue(op, payload, idem_key, user_id, record_id=None, run_now=True):
    """Queue a write into Odoo and, unless told otherwise, try it straight away. Returns the queue entry."""
    oid = db.outbox_add(op, json.dumps(payload, default=str), idem_key, user_id, record_id)
    if run_now:
        run_one(oid)
    return db.outbox_get(oid)


def run_one(op_id=None):
    """Run one due entry (or this one). Returns True if Odoo answered (whatever it said), False if it's unreachable."""
    row = db.outbox_claim(op_id, time.time())
    if not row:
        return None
    payload, attempts = json.loads(row["payload_json"] or "{}"), row["attempts"] + 1
    try:
        result = HANDLERS[row["op"]](payload, row)
        db.outbox_finish(row["id"], "done", result_json=json.dumps(result or {}, default=str), attempts=attempts)
        if row["record_id"]:
            rec = db.erp_get(record_id=row["record_id"])
            if rec and rec["sync_status"] in ("pending", "error"):
                db.erp_mark(row["record_id"], "synced")
        return True
    except Conflict as e:
        db.outbox_finish(row["id"], "conflict", error=str(e), attempts=attempts)
        if row["record_id"]:
            db.erp_mark(row["record_id"], "conflict", str(e))
        logger.warning("sync: {} #{} not applied, conflict: {}", row["op"], row["id"], e)
        return True
    except odoo.OdooUnreachable as e:
        wait = min(15 * 2 ** (attempts - 1), BACKOFF_MAX)
        db.outbox_finish(row["id"], "pending", error=f"Odoo unreachable, retrying in {wait}s: {e}", next_try_at=time.time() + wait, attempts=attempts)
        if row["record_id"]:
            db.erp_mark(row["record_id"], "pending", f"waiting for Odoo: {e}")
        return False
    except Exception as e:                                        # Odoo refused it, or a bug: retried, then left for a person
        wait = min(15 * 2 ** (attempts - 1), BACKOFF_MAX)
        give_up = attempts >= 3
        db.outbox_finish(row["id"], "error" if give_up else "pending", error=f"{type(e).__name__}: {e}"[:500],
                         next_try_at=None if give_up else time.time() + wait, attempts=attempts)
        if row["record_id"]:
            db.erp_mark(row["record_id"], "error", str(e)[:500])
        logger.warning("sync: {} #{} failed (attempt {}): {}", row["op"], row["id"], attempts, e)
        return True


def run_due(limit=50):
    """Work through due queue entries until Odoo stops answering."""
    for _ in range(limit):
        ok = run_one()
        if ok is None or ok is False:
            return


def retry(op_id):
    """A person asks for a failed or conflicting write to be tried again."""
    row = db.outbox_get(op_id)
    if not row or row["status"] not in ("error", "pending", "conflict"):
        return row
    db.outbox_finish(op_id, "pending", error=row["last_error"], next_try_at=0)
    run_one(op_id)
    return db.outbox_get(op_id)


def _note(model, rec_id, text):
    try:
        odoo.note(model, rec_id, text)
    except odoo.OdooError as e:                                   # the change itself went through; the note is extra
        logger.warning("sync: note on {} {} not added: {}", model, rec_id, e)


def _line_cmds(lines, existing=()):
    """Odoo's x2many commands for PO lines: [0,0,vals] new, [1,id,vals] change, [2,id] remove."""
    cmds = []
    for ln in lines:
        vals = {k: v for k, v in dict(product_id=ln.get("product_id"), name=ln.get("name"), product_qty=ln.get("qty"),
                                      price_unit=ln.get("price"), discount=ln.get("discount"), date_planned=ln.get("date_planned")).items() if v is not None}
        if ln.get("tax_ids") is not None:
            vals["tax_ids"] = [[6, 0, list(ln["tax_ids"])]]
        if ln.get("id") and ln.get("delete"):
            cmds.append([2, ln["id"]])
        elif ln.get("id"):
            cmds.append([1, ln["id"], vals])
        else:
            cmds.append([0, 0, vals])
    return cmds


def _refresh(model, odoo_id, record_id=None, source="odoo"):
    recs = _fetch(model, [odoo_id])
    if recs:
        store(model, recs[0], source=source, record_id=record_id)
        return recs[0]
    return None


@handler("po_create")
def _po_create(p, row):
    origin = f"Invoice Flow #{row['record_id']}"
    found = odoo._call("purchase.order", "search_read", domain=[["origin", "=", origin]], fields=["id"], limit=1)
    if found:
        po_id = found[0]["id"]                                    # made on an earlier try that timed out
    else:
        vals = dict(partner_id=p["vendor_id"], origin=origin, order_line=_line_cmds(p["lines"]))
        for k in ("partner_ref", "date_planned"):
            if p.get(k):
                vals[k] = p[k]
        po_id = odoo._call("purchase.order", "create", vals_list=[vals])[0]
        _note("purchase.order", po_id, f"<p>Created in Invoice Flow by <b>{p['by']}</b> (app PO #{row['record_id']}).</p>")
    rec = _refresh("purchase.order", po_id, record_id=row["record_id"], source="app")
    return dict(odoo_id=po_id, name=rec and rec["name"], state=rec and rec["state"])


@handler("po_update")
def _po_update(p, row):
    mirror = db.erp_get(record_id=row["record_id"])
    if not mirror or not mirror["odoo_id"]:
        raise odoo.OdooUnreachable("the PO hasn't reached Odoo yet")   # queued behind its creation: try again later
    cur = odoo._call("purchase.order", "read", ids=[mirror["odoo_id"]], fields=["write_date", "state", "name"])
    if not cur:
        raise odoo.OdooError("the PO no longer exists in Odoo")
    cur = cur[0]
    if p.get("base_write_date") and cur["write_date"] != p["base_write_date"]:
        _refresh("purchase.order", mirror["odoo_id"], record_id=row["record_id"])
        raise Conflict(f"{cur['name']} was changed in Odoo at {cur['write_date']} after you opened it; your edit wasn't applied. "
                       "Open it again and redo the change.")
    if cur["state"] == "cancel":
        raise odoo.OdooError(f"{cur['name']} is cancelled in Odoo")
    vals = {k: p["header"][k] for k in ("partner_id", "partner_ref", "date_planned") if (p.get("header") or {}).get(k) is not None}
    if p.get("lines"):
        vals["order_line"] = _line_cmds(p["lines"])
    if vals:
        odoo._call("purchase.order", "write", ids=[mirror["odoo_id"]], vals=vals)
        _note("purchase.order", mirror["odoo_id"], f"<p>Updated in Invoice Flow by <b>{p['by']}</b>: {', '.join(p.get('summary') or vals)}.</p>")
    rec = _refresh("purchase.order", mirror["odoo_id"], record_id=row["record_id"], source="app")
    return dict(odoo_id=mirror["odoo_id"], state=rec and rec["state"])


@handler("po_confirm")
def _po_confirm(p, row):
    """Approval in Invoice Flow = Odoo's Confirm Order. Odoo applies its own approval rule: with two-step approval a large
    PO goes to "To Approve" and waits for a purchase manager in Odoo; that state is reported, never overridden."""
    mirror = db.erp_get(record_id=row["record_id"])
    if not mirror or not mirror["odoo_id"]:
        raise odoo.OdooUnreachable("the PO hasn't reached Odoo yet")
    cur = odoo._call("purchase.order", "read", ids=[mirror["odoo_id"]], fields=["state", "name"])[0]
    if cur["state"] in ("draft", "sent"):
        odoo._call("purchase.order", "button_confirm", ids=[mirror["odoo_id"]])
    rec = _refresh("purchase.order", mirror["odoo_id"], record_id=row["record_id"], source="app")
    state = rec["state"] if rec else cur["state"]
    if cur["state"] in ("draft", "sent"):
        _note("purchase.order", mirror["odoo_id"], f"<p>Approved in Invoice Flow by <b>{p['by']}</b>. Odoo state now: <b>"
              + {"purchase": "Purchase Order", "to approve": "To Approve (waits for a purchase manager in Odoo)"}.get(state, state) + "</b>.</p>")
    return dict(odoo_id=mirror["odoo_id"], state=state, was=cur["state"])


@handler("vendor_create")
def _vendor_create(p, row):
    dom = [["vat", "=", p["vat"]]] if p.get("vat") else [["name", "=ilike", p["name"]]]
    found = odoo._call("res.partner", "search_read", domain=dom, fields=["id", "supplier_rank"], limit=1)
    if found:
        pid = found[0]["id"]
        if not found[0]["supplier_rank"]:
            odoo._call("res.partner", "write", ids=[pid], vals={"supplier_rank": 1})
    else:
        vals = {k: p[k] for k in ("name", "vat", "email", "phone", "street", "city", "zip") if p.get(k)}
        vals.update(supplier_rank=1, is_company=True)
        if p.get("payment_term_id"):
            vals["property_supplier_payment_term_id"] = p["payment_term_id"]
        pid = odoo._call("res.partner", "create", vals_list=[vals])[0]
        _note("res.partner", pid, f"<p>Vendor created in Invoice Flow by <b>{p['by']}</b>.</p>")
    _refresh("res.partner", pid, record_id=row["record_id"], source="app")
    return dict(odoo_id=pid, existed=bool(found))


@handler("product_create")
def _product_create(p, row):
    dom = [["default_code", "=", p["default_code"]]] if p.get("default_code") else [["name", "=ilike", p["name"]]]
    found = odoo._call("product.product", "search_read", domain=dom, fields=["id"], limit=1)
    if found:
        pid = found[0]["id"]
    else:
        vals = dict(name=p["name"], type="service" if p.get("kind") == "service" else "consu", purchase_ok=True)
        if p.get("kind") != "service":
            vals["is_storable"] = True
        for k in ("default_code", "standard_price", "uom_id", "categ_id"):
            if p.get(k) not in (None, ""):
                vals[k] = p[k]
        if p.get("tax_ids") is not None:
            vals["supplier_taxes_id"] = [[6, 0, list(p["tax_ids"])]]
        pid = odoo._call("product.product", "create", vals_list=[vals])[0]
    _refresh("product.product", pid, record_id=row["record_id"], source="app")
    return dict(odoo_id=pid, existed=bool(found))


MASTER = 0   # vendor_odoo.user_id for the shared master list


def link_vendor_later(user_id, vendor_id, name, gstin, by, scope=None):
    """A vendor approved in Invoice Flow goes to Odoo (found by GSTIN, then by cleaned name, or created there), and its
    Odoo id is kept with it. Without a GSTIN it isn't sent: it's flagged instead. Queued, so Odoo being away loses nothing."""
    from . import rules
    if not rules.uses_odoo(user_id):
        return None
    scope = user_id if scope is None else scope
    if not (gstin or "").strip():
        db.set_vendor_link(scope, vendor_id, None, "skipped", "no GSTIN: flagged, not sent to Odoo")
        return None
    return enqueue("vendor_link", dict(scope=scope, vendor_id=vendor_id, name=name, gstin=gstin.strip().upper(), by=by),
                   f"vendor_link:{scope}:{vendor_id}", user_id)


@handler("vendor_link")
def _vendor_link(p, row):
    from . import extract, rules
    found = odoo._call("res.partner", "search_read", domain=[["vat", "=", p["gstin"]]], fields=["id", "supplier_rank"], limit=1)
    how = "matched by GSTIN"
    if not found:
        clean = rules.norm_name(extract.clean_vendor_name(p["name"]))
        first = (clean.split() or [""])[0]
        candidates = odoo._call("res.partner", "search_read", domain=[["name", "ilike", first]], fields=["id", "name", "supplier_rank", "vat"], limit=50) if first else []
        found = [c for c in candidates if rules.norm_name(c["name"]) == clean and not c.get("vat")]   # same name, and no other GSTIN
        how = "matched by name"
    if found:
        pid = found[0]["id"]
        if not found[0]["supplier_rank"]:
            odoo._call("res.partner", "write", ids=[pid], vals={"supplier_rank": 1})
    else:
        pid = odoo._call("res.partner", "create", vals_list=[dict(name=extract.clean_vendor_name(p["name"]), vat=p["gstin"], supplier_rank=1,
                                                                    is_company=True)])[0]
        _note("res.partner", pid, f"<p>Vendor approved in Invoice Flow by <b>{p['by']}</b> ({p['vendor_id']}).</p>")
        how = "created in Odoo"
    db.set_vendor_link(p["scope"], p["vendor_id"], pid, "linked", how)
    _refresh("res.partner", pid)
    return dict(odoo_id=pid, how=how)


@handler("bill_sync")
def _bill_sync(p, row):
    """An invoice's bill (held, rejected, or a decision that couldn't reach Odoo at once): created or updated."""
    from . import bill_sync
    run = db.get_run(p["run_id"], p["user_id"])
    if not run or not run["result_json"]:
        return dict(skipped="run gone or not decided")
    decision = p.get("decision") or run["final_decision"] or run["decision"]
    try:
        out = bill_sync.sync(p["run_id"], p["user_id"], decision, json.loads(run["result_json"]), by_person=p.get("by_person", False),
                             person_note=p.get("note", ""), acting_user=p.get("acting_user"))
    except odoo.OdooError as e:
        db.activity_for_run(p["run_id"], sync_status=f"waiting for Odoo: {str(e)[:120]}" if isinstance(e, odoo.OdooUnreachable) else f"failed: {str(e)[:120]}")
        raise
    return dict(bill_id=out["bill_id"], state=out["state"], action=out["action"])


def sync_bill_later(run_id, user_id, decision=None, by_person=False, note="", acting_user=None, run_now=True):
    return enqueue("bill_sync", dict(run_id=run_id, user_id=user_id, decision=decision, by_person=by_person, note=note, acting_user=acting_user),
                   f"bill_sync:{run_id}:{decision}:{time.time():.3f}", user_id, run_now=run_now)


@handler("run_recheck")
def _run_recheck(p, row):
    """An invoice held because Odoo didn't answer: once Odoo answers again, check it again (never approved without it)."""
    odoo._call("res.partner", "search_count", domain=[["id", "=", 0]])     # Odoo up? (raises OdooUnreachable if not)
    if not RECHECK[0]:
        raise RuntimeError("re-check isn't wired up")
    return dict(result=RECHECK[0](p["run_id"], p["user_id"]))


@handler("trail_retry")
def _trail_retry(p, row):
    from . import odoo_trail
    run = db.get_run(p["run_id"], p["user_id"])
    if not run or not run["result_json"]:
        return dict(skipped="run gone")
    entry = odoo_trail.record(p["run_id"], p["user_id"], json.loads(run["result_json"]), p["decision"],
                              by_person=p.get("by_person", False), person_note=p.get("note", ""))
    if entry.get("error"):
        raise odoo.OdooUnreachable(entry["error"]) if "didn't answer" in entry["error"] else odoo.OdooError(entry["error"])
    return dict(target=entry.get("target"))


@handler("contact_sync")
def _contact_sync(p, row):
    from . import odoo_trail
    odoo_trail._tried.pop(p["user_id"], None)
    pid = odoo_trail.sync_user(p["user_id"])
    if not pid:
        raise odoo.OdooUnreachable("the contact couldn't be made yet")
    return dict(partner_id=pid)


# ================================================================ the screens --

def _rows(model):
    return [dict(json.loads(r["data_json"] or "{}"), _id=r["id"], _odoo_id=r["odoo_id"], _status=r["sync_status"], _error=r["last_error"],
                 _source=r["source"], _synced=r["synced_at"]) for r in db.erp_list(model)]


def _day(s):
    return (s or "")[:10]


def _in(d, date_from, date_to):
    d = _day(d)
    return (not date_from or (d and d >= date_from)) and (not date_to or (d and d <= date_to))


PO_STATE = {"draft": "Draft (RFQ)", "sent": "RFQ sent", "to approve": "Pending approval", "purchase": "Confirmed", "done": "Locked", "cancel": "Cancelled"}
MATCH = dict(price={"RATE_ABOVE_PO", "RATE_OVER_PO"}, qty={"QTY_NOT_RECEIVED", "QTY_OVER_PO"},
             missing_po={"PO_NOT_FOUND", "PO_NONE_OPEN", "PO_MISSING", "NO_PO"},
             mismatch={"RATE_ABOVE_PO", "RATE_OVER_PO", "QTY_NOT_RECEIVED", "QTY_OVER_PO", "OVER_TOLERANCE", "OVER_BILLED", "PO_VENDOR_MISMATCH"},
             partial={"PARTIAL_BILLING", "LINE_NOT_ON_PO"})


def overview(runs, date_from=None, date_to=None, vendor=None):
    """The ERP dashboard's figures: invoices from Invoice Flow's own runs (received in the period), bills, POs and money
    from the Odoo mirror (bill date and order date in the period). vendor: an Odoo partner id."""
    vid = int(vendor) if vendor else None
    rs = [r for r in runs if _in(r["created_at"], date_from, date_to) and (not vid or r.get("vendor_id") == f"OD{vid}")]
    eff = lambda r: r.get("final_decision") or r.get("decision")
    done = [r for r in rs if r["status"] in ("done", "error")]
    bills = [b for b in _rows("account.move") if _in(b.get("invoice_date") or b.get("write_date"), date_from, date_to)
             and (not vid or (b.get("partner_id") or [None])[0] == vid)]
    sign = lambda b: -1 if b.get("move_type") == "in_refund" else 1
    posted = [b for b in bills if b.get("state") == "posted"]
    pos = [p for p in _rows("purchase.order") if _in(p.get("date_order"), date_from, date_to) and (not vid or (p.get("partner_id") or [None])[0] == vid)]
    live = [p for p in pos if p.get("state") != "cancel"]
    flags = lambda r: set(r.get("flags") or [])
    matched = [r for r in done if "THREE_WAY_OK" in flags(r) and not set(r.get("codes") or [])]
    return dict(
        invoices=dict(received=len(rs), processed=len(done), pending=sum(1 for r in rs if r["status"] not in ("done", "error"))
                      + sum(1 for r in done if eff(r) == "HOLD"),
                      approved=sum(1 for r in done if eff(r) == "APPROVE"), rejected=sum(1 for r in done if eff(r) == "REJECT"),
                      held=sum(1 for r in done if eff(r) == "HOLD"),
                      bills_draft=sum(1 for b in bills if b.get("state") == "draft"), bills_posted=len(posted),
                      bills_paid=sum(1 for b in posted if b.get("payment_state") in ("paid", "in_payment")),
                      bills_unpaid=sum(1 for b in posted if b.get("payment_state") in ("not_paid", "partial")),
                      credit_notes=sum(1 for b in bills if b.get("move_type") == "in_refund")),
        purchase_orders=dict(total=len(pos), draft=sum(1 for p in pos if p.get("state") in ("draft", "sent")),
                             pending_approval=sum(1 for p in pos if p.get("state") == "to approve"),
                             confirmed=sum(1 for p in pos if p.get("state") in ("purchase", "done")),
                             approved=sum(1 for p in pos if p.get("date_approve")),
                             partially_received=sum(1 for p in live if p.get("receipt_status") == "partial"),
                             fully_received=sum(1 for p in live if p.get("receipt_status") == "full"),
                             not_received=sum(1 for p in live if p.get("state") in ("purchase", "done") and p.get("receipt_status") == "pending"),
                             cancelled=sum(1 for p in pos if p.get("state") == "cancel"),
                             pending_sync=sum(1 for p in pos if p["_status"] in ("pending", "error", "conflict"))),
        money=dict(invoice_value=round(sum(sign(b) * (b.get("amount_total") or 0) for b in bills if b.get("state") != "cancel"), 2),
                   po_value=round(sum(p.get("amount_total") or 0 for p in live), 2),
                   pending_payment=round(sum(sign(b) * (b.get("amount_residual") or 0) for b in posted), 2),
                   paid=round(sum(sign(b) * ((b.get("amount_total") or 0) - (b.get("amount_residual") or 0)) for b in posted), 2),
                   outstanding_liabilities=round(sum(sign(b) * (b.get("amount_residual") or 0) for b in posted if b.get("payment_state") != "paid"), 2),
                   draft_bill_value=round(sum(sign(b) * (b.get("amount_total") or 0) for b in bills if b.get("state") == "draft"), 2),
                   tax=round(sum(sign(b) * (b.get("amount_tax") or 0) for b in bills if b.get("state") != "cancel"), 2),
                   po_tax=round(sum(p.get("amount_tax") or 0 for p in live), 2)),
        matching=dict(matched=len(matched),
                      partially_matched=sum(1 for r in done if flags(r) & MATCH["partial"] and r not in matched),
                      mismatched=sum(1 for r in done if flags(r) & MATCH["mismatch"]),
                      missing_po=sum(1 for r in done if flags(r) & MATCH["missing_po"]),
                      price_discrepancies=sum(1 for r in done if flags(r) & MATCH["price"]),
                      quantity_discrepancies=sum(1 for r in done if flags(r) & MATCH["qty"])),
        period=dict(date_from=date_from, date_to=date_to, vendor=vid),
        sync=status())


def status():
    """How fresh the mirror is, and what's waiting to go to Odoo."""
    q = db.outbox_list(500)
    return dict(last_pull=db.erp_state("last_pull"), last_full=db.erp_state("last_full"), last_webhook=db.erp_state("last_webhook"),
                queue=dict(pending=sum(1 for r in q if r["status"] in ("pending", "running")), failed=sum(1 for r in q if r["status"] == "error"),
                           conflicts=sum(1 for r in q if r["status"] == "conflict"), done=sum(1 for r in q if r["status"] == "done")),
                errors={m: db.erp_state(f"error:{m}") for m in MODELS if db.erp_state(f"error:{m}")},
                counts={m: len(db.erp_list(m)) for m in MODELS})


def po_detail(record_id, runs):
    rec = db.erp_get(record_id=record_id)
    if not rec or rec["model"] != "purchase.order":
        return None
    po = dict(json.loads(rec["data_json"] or "{}"), _id=rec["id"], _odoo_id=rec["odoo_id"], _status=rec["sync_status"], _error=rec["last_error"],
              _source=rec["source"], _synced=rec["synced_at"], _version=(rec["odoo_write_date"] or "").split("|")[0])
    oid = rec["odoo_id"]
    po["receipts"] = [p for p in _rows("stock.picking") if oid and (p.get("purchase_id") or [None])[0] == oid]
    po["bills"] = [b for b in _rows("account.move") if oid and b.get("_odoo_id") in (po.get("invoice_ids") or [])]
    po["invoices"] = [dict(id=r["id"], filename=r["filename"], invoice_number=r["invoice_number"], decision=r.get("final_decision") or r["decision"],
                           total=r["total"], created_at=r["created_at"]) for r in runs if po.get("name") and r.get("po_number") == po.get("name")]
    po["queue"] = [dict(id=q["id"], op=q["op"], status=q["status"], error=q["last_error"], at=q["created_at"]) for q in db.outbox_list(500)
                   if q["record_id"] == record_id]
    return po


# ================================================================== the loop --

def loop():
    """The background worker: the queue every 15 s, an incremental pull every minute, a full reconcile every 15 min."""
    db.outbox_reset_running()
    last_pull = last_full = 0.0
    while True:
        try:
            if odoo.enabled():
                run_due()
                now = time.time()
                if now - last_full > FULL_EVERY:
                    pull_all(full=True)
                    last_full = last_pull = now
                elif now - last_pull > PULL_EVERY:
                    pull_all()
                    last_pull = now
        except Exception:                                         # never let the worker die
            logger.exception("sync loop")
        time.sleep(QUEUE_EVERY)


def today():
    return date.today().isoformat()
