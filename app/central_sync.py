"""The central copy of the whole platform in Odoo, for the platform administrator: every user, folder, vendor, purchase
order, invoice (with its document, extracted fields, checks and decision) and activity row, whoever owns it.

Invoice Flow stays the source of truth: accounts, sign-in, who owns which invoice and folder, and who may see what are
decided here, never read back from Odoo. Odoo gets a read-only copy (models in central_schema.py, set up by
central_setup.py), kept in step like this:

- A reconciler, not a set of hooks. Each round builds every record from the database, takes a fingerprint of it and
  sends only what changed since the fingerprint Odoo last got (table central_sync). Nothing can be missed: a change made
  while Odoo was down is simply still different next round.
- Event-driven: anything that changes what's shown (db.CHANGED, set by the database functions) wakes the loop within
  seconds; otherwise it reconciles every RECONCILE_EVERY seconds anyway.
- Idempotent, no duplicates: every Odoo record carries its key (x_key = "<site>:<kind>:<id>"); a record is found by its
  key before anything is created, so a retry after a lost answer updates instead of adding a second one.
- Retries: Odoo unreachable or rate-limiting -> the round stops and waits (15 s, doubling, at most 15 min). A record
  Odoo refuses is retried on its own with the same backoff, the others carry on.
- Deleted in Invoice Flow -> archived in Odoo (x_active false, "Deleted in Invoice Flow"), never deleted: history stays.
- Owner and folder ride on every invoice; moving it to another folder changes its fingerprint and is sent next round.

Settings: CENTRAL_SYNC=1 switches it on (needs the Odoo settings too). CENTRAL_SITE names this server in the keys
(default: the SITE_URL's host, else "local"), so a test server never overwrites the live server's records.
ADMIN_ACCOUNTS: the Invoice Flow accounts that are platform administrators (comma-separated).
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from loguru import logger

from . import config, db, odoo

RECONCILE_EVERY = 300        # seconds between full rounds when nothing nudges the loop
DEBOUNCE = 2.0               # a burst of changes (a folder upload) is one round
BACKOFF_START, BACKOFF_MAX = 15, 900
WRITE_PAUSE = 0.05           # between writes, so a first full push stays under Odoo Online's rate limit
KINDS = ["user", "folder", "vendor", "po", "invoice", "activity"]   # in this order: later kinds point at earlier ones
MODEL = dict(user="x_if_user", folder="x_if_folder", vendor="x_if_vendor", po="x_if_po", invoice="x_if_invoice", activity="x_if_activity")
LINKS = ("x_partner_id", "x_po_id", "x_bill_id")   # links to Odoo's own records: dropped and retried if Odoo no longer has them

_state = dict(backoff_until=0.0, backoff=BACKOFF_START, last_ok=None, last_error=None, running=False, failures=[], last_round=None,
              pending=0)
_round_lock = threading.Lock()


def enabled():
    return odoo.enabled() and os.getenv("CENTRAL_SYNC", "0").strip() == "1"


def site():
    s = os.getenv("CENTRAL_SITE", "").strip()
    if s:
        return s
    host = urlparse(os.getenv("SITE_URL", "")).hostname or ""
    return host.split(".")[0] if host else "local"


def admin_names():
    return {a.strip().lower() for a in os.getenv("ADMIN_ACCOUNTS", "").split(",") if a.strip()}


def is_admin(username):
    return bool(username) and username.strip().lower() in admin_names()


# ------------------------------------------------------------------------------------- building the records --

def _utc(s):
    """An app timestamp (server-local, naive) as Odoo wants a datetime: UTC, 'YYYY-MM-DD HH:MM:SS'. False if none."""
    if not s:
        return False
    try:
        return datetime.fromisoformat(str(s)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False


def _date(s):
    return s if isinstance(s, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else False


def _num(v):
    try:
        return round(float(v), 2) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


REF = "__ref__"   # a link to another synced record, resolved to its Odoo id when sent: {REF: (kind, local_id)}


def ref(kind, local_id):
    return {REF: [kind, str(local_id)]} if local_id not in (None, "") else False


MATCH_SETS = dict(mismatch={"RATE_ABOVE_PO", "RATE_OVER_PO", "QTY_NOT_RECEIVED", "QTY_OVER_PO", "OVER_TOLERANCE", "OVER_BILLED", "PO_VENDOR_MISMATCH"},
                  missing={"PO_NOT_FOUND", "PO_NONE_OPEN", "PO_MISSING"}, partial={"PARTIAL_BILLING", "LINE_NOT_ON_PO"})
DUPLICATES = {"DUPLICATE_EXACT", "INVOICE_NUMBER_REUSED", "DUPLICATE_SUSPECTED", "DUPLICATE_LIKELY", "ERP_DUPLICATE_BILL"}
FAILED = {"UNREADABLE", "NOT_AN_INVOICE"}
APPROVAL = {"APPROVE": "Approved", "HOLD": "On hold", "REJECT": "Rejected"}
PROCESSING = {"queued": "Processing", "running": "Processing", "review": "Waiting for review", "waiting": "Waiting (folder upload)"}


def describe_run(r, users, folders):
    """What the platform shows of one invoice: owner, folder, status, decision, matching, extracted fields, Odoo bill."""
    res = json.loads(r.get("result_json") or "null") or {}
    findings = res.get("findings") or []
    flags = {x.get("code") for x in findings}
    blocking = [x for x in findings if x.get("severity") in ("hold", "reject")]
    codes = {x.get("code") for x in blocking}
    done = r["status"] in ("done", "error")
    eff = r.get("final_decision") or r.get("decision")
    if r["status"] == "error" or flags & FAILED:
        processing = "Failed"
    else:
        processing = "Processed" if done else PROCESSING.get(r["status"], r["status"] or "")
    if not done:
        match = "Not checked yet"
    elif "THREE_WAY_OK" in flags and not codes:
        match = "Matched"
    elif flags & MATCH_SETS["mismatch"]:
        match = "Mismatched"
    elif flags & MATCH_SETS["missing"]:
        match = "PO not found"
    elif flags & MATCH_SETS["partial"]:
        match = "Partly matched"
    elif r.get("po_number"):
        match = "Matched"
    else:
        match = "No PO"
    user = users.get(r["user_id"]) or {}
    folder = folders.get(r.get("folder_id")) or {}
    fields = res.get("fields") or {}
    derived = res.get("derived") or {}
    status = r.get("odoo_status") or ""
    bill_state = "posted" if status in ("posted", "paid", "in_payment", "partial", "reversed") else status
    payment = status if status in ("paid", "in_payment", "partial", "reversed") else ("not_paid" if status == "posted" else "")
    owner = user.get("username") or ""
    vendor = res.get("vendor") or {}
    po = res.get("po") or {}
    decided_by = (owner if r.get("final_decision") else "Invoice Flow checks") if done and eff else ""
    reason = r.get("override_note") or (blocking[0].get("message") if blocking else ("All checks passed" if eff == "APPROVE" else ""))
    return dict(
        x_name=(r.get("invoice_number") or r.get("filename") or r["id"])[:200], x_run_id=r["id"],
        x_user_id=ref("user", r["user_id"]), x_owner_name=owner, x_owner_email=owner if "@" in owner else False, x_app_user_id=r["user_id"] or 0,
        x_folder_id=ref("folder", r.get("folder_id")), x_folder_name=folder.get("name") or False, x_app_folder_id=r.get("folder_id") or 0,
        x_filename=r.get("filename") or False, x_uploaded_at=_utc(r.get("created_at")), x_decided_at=_utc(r.get("decided_at")) if done else False,
        x_processing_status=processing, x_approval=APPROVAL.get(eff, "Pending") if done else "Pending",
        x_auto_decision=APPROVAL.get(r.get("decision"), "") if done else "", x_decided_by=decided_by, x_reason=(reason or "")[:2000],
        x_match=match, x_headline=res.get("headline") or False, x_codes=", ".join(sorted(c for c in codes if c)) or False,
        x_findings="\n".join(f"{x.get('severity', '').upper()} {x.get('code')}: {x.get('message')}" for x in findings)[:20000] or False,
        x_extracted=json.dumps(fields, indent=1, ensure_ascii=False)[:20000] if fields else False, x_extractor=r.get("extractor") or False,
        x_vendor_name=r.get("vendor_name") or fields.get("vendor_name") or False, x_vendor_gstin=fields.get("vendor_gstin") or False,
        x_vendor_ref=r.get("vendor_id") or False, x_partner_id=vendor.get("odoo_id") or False,
        x_invoice_number=r.get("invoice_number") or False, x_invoice_date=_date(r.get("invoice_date")), x_due_date=_date(fields.get("due_date")),
        x_po_number=r.get("po_number") or False, x_po_id=po.get("odoo_id") or False,
        x_net=_num(r.get("net") if r.get("net") is not None else derived.get("net")), x_tax=_num(derived.get("tax")), x_total=_num(r.get("total")),
        x_duplicate=bool(flags & DUPLICATES),
        x_bill_id=r.get("odoo_bill_id") if r.get("odoo_bill_id") and status != "deleted" else False,
        x_bill_state=bill_state or False, x_payment_state=payment or False)


def build():
    """{kind: {local_id: (vals, document path or None)}} for the whole platform, from the database."""
    users = {u["id"]: u for u in db.all_users()}
    folders = {f["id"]: f for f in db.all_folders()}
    connected = odoo.serves   # accounts whose invoices also become real draft bills in Odoo
    out = {k: {} for k in KINDS}
    for u in users.values():
        name = u["username"]
        out["user"][str(u["id"])] = (dict(
            x_name=name, x_app_user_id=u["id"], x_email=name if "@" in name else False, x_role="Platform admin" if is_admin(name) else "User",
            x_login_method=u["login_method"], x_created_at=_utc(u["created_at"]), x_last_login=_utc(u.get("last_login_at")),
            x_odoo_connected=bool(connected(name))), None)
    for f in folders.values():
        out["folder"][str(f["id"])] = (dict(x_name=f["name"], x_app_folder_id=f["id"], x_user_id=ref("user", f["user_id"]),
                                            x_owner_name=(users.get(f["user_id"]) or {}).get("username") or False,
                                            x_created_at=_utc(f["created_at"])), None)
    # vendors: the shared master list (no owner) and each account's approved ones, with their Odoo vendor when linked
    links = db.all_vendor_links()
    import csv
    with open(config.DATA_DIR / "vendors.csv", newline="", encoding="utf-8") as fh:
        master = list(csv.DictReader(fh))
    for v in master:
        link = links.get((0, v["vendor_id"])) or {}
        out["vendor"][f"0:{v['vendor_id']}"] = (dict(
            x_name=v["name"], x_vendor_ref=v["vendor_id"], x_user_id=False, x_owner_name="(shared master list)", x_gstin=v.get("gstin") or False,
            x_status=v.get("status") or "approved", x_approved_at=False, x_aliases=v.get("aliases") or False, x_note=False,
            x_partner_id=link.get("odoo_id") or False, x_link_status=link.get("status") or False), None)
    for v in db.all_added_vendors():
        link = links.get((v["user_id"], v["vendor_id"])) or {}
        out["vendor"][f"{v['user_id']}:{v['vendor_id']}"] = (dict(
            x_name=v["name"], x_vendor_ref=v["vendor_id"], x_user_id=ref("user", v["user_id"]),
            x_owner_name=(users.get(v["user_id"]) or {}).get("username") or False, x_gstin=v.get("gstin") or False,
            x_status=v.get("status") or "approved", x_approved_at=_utc(v.get("approved_at")), x_aliases=v.get("aliases") or False,
            x_note=v.get("note") or False, x_partner_id=link.get("odoo_id") or False,
            x_link_status=(link.get("status") or "") + (f": {link['detail']}" if link.get("detail") else "") or False), None)
    # purchase orders: the shared register, each account's own register, and the Odoo POs accounts raised in the app
    billed = db.all_billed()
    with open(config.DATA_DIR / "purchase_orders.csv", newline="", encoding="utf-8") as fh:
        master_pos = list(csv.DictReader(fh))
    vname = {v["vendor_id"]: v["name"] for v in master}
    for p in master_pos:
        net, rate = _num(p["net_amount"]), _num(p["gst_rate"])
        out["po"][f"0:{p['po_number']}"] = (dict(
            x_name=p["po_number"], x_user_id=False, x_owner_name="(shared master list)", x_source="Invoice Flow master list",
            x_vendor_ref=p["vendor_id"], x_vendor_name=vname.get(p["vendor_id"]) or p["vendor_id"], x_po_date=_date(p.get("po_date")),
            x_description=p.get("description") or False, x_net=net, x_gst_rate=rate, x_total=round(net * (1 + rate / 100), 2),
            x_status=p.get("status") or "open", x_receipt_status=False, x_billed=0.0, x_remaining=net, x_po_id=False, x_created_at=False), None)
    added_names = {(v["user_id"], v["vendor_id"]): v["name"] for v in db.all_added_vendors()}
    for p in db.all_added_pos():
        net, rate = _num(p["net_amount"]), _num(p["gst_rate"])
        b = round(billed.get((p["user_id"], p["po_number"]), 0.0), 2)
        out["po"][f"{p['user_id']}:{p['po_number']}"] = (dict(
            x_name=p["po_number"], x_user_id=ref("user", p["user_id"]), x_owner_name=(users.get(p["user_id"]) or {}).get("username") or False,
            x_source="Invoice Flow register", x_vendor_ref=p["vendor_id"],
            x_vendor_name=added_names.get((p["user_id"], p["vendor_id"])) or vname.get(p["vendor_id"]) or p["vendor_id"],
            x_po_date=_date(p.get("po_date")), x_description=p.get("description") or False, x_net=net, x_gst_rate=rate,
            x_total=round(net * (1 + rate / 100), 2), x_status=p.get("status") or "open", x_receipt_status=False, x_billed=b,
            x_remaining=round(net - b, 2), x_po_id=False, x_created_at=_utc(p.get("created_at"))), None)
    for rec in db.erp_created_by_app("purchase.order"):
        d = json.loads(rec["data_json"] or "{}")
        lines = d.get("lines") or []
        net = _num(d.get("amount_untaxed"))
        billed_net = round(sum(_num(ln.get("qty_invoiced")) * _num(ln.get("price_unit")) for ln in lines), 2)
        out["po"][f"odoo:{rec['id']}"] = (dict(
            x_name=d.get("name") or f"New PO (waiting for Odoo, #{rec['id']})", x_user_id=ref("user", rec["created_by"]),
            x_owner_name=(users.get(rec["created_by"]) or {}).get("username") or False, x_source="Odoo (raised in Invoice Flow)",
            x_vendor_ref=f"OD{(d.get('partner_id') or [0])[0]}" if d.get("partner_id") else False,
            x_vendor_name=(d.get("partner_id") or [None, False])[1] if isinstance(d.get("partner_id"), list) else False,
            x_po_date=_date((d.get("date_order") or "")[:10]), x_description=", ".join(str(ln.get("name") or "") for ln in lines)[:200] or False,
            x_net=net, x_gst_rate=0.0, x_total=_num(d.get("amount_total")), x_status=d.get("state") or rec["sync_status"] or False,
            x_receipt_status=d.get("receipt_status") or False, x_billed=billed_net, x_remaining=round(net - billed_net, 2),
            x_po_id=rec["odoo_id"] or False, x_created_at=_utc(rec.get("created_at"))), None)
    for r in db.all_runs():
        out["invoice"][r["id"]] = (describe_run(r, users, folders), r.get("file_path"))
    for a in db.all_activity():
        out["activity"][str(a["id"])] = (dict(
            x_name=f"{a.get('username') or ''} · {a.get('filename') or a.get('run_id')}"[:200], x_user_id=ref("user", a["user_id"]),
            x_username=a.get("username") or False, x_session_id=a.get("session_id") or False, x_invoice_id=ref("invoice", a.get("run_id")),
            x_run_id=a.get("run_id") or False, x_filename=a.get("filename") or False, x_invoice_number=a.get("invoice_number") or False,
            x_vendor=a.get("vendor") or False, x_decision=a.get("decision") or False, x_decided_by=a.get("decided_by") or False,
            x_reason=a.get("reason") or False, x_uploaded_at=_utc(a.get("uploaded_at")), x_decided_at=_utc(a.get("decided_at")),
            x_bill_ref=a.get("odoo_bill_id") or 0, x_sync_status=a.get("sync_status") or False), None)
    return out


def fingerprint(vals):
    return hashlib.sha1(json.dumps(vals, sort_keys=True, default=str).encode()).hexdigest()


def doc_fingerprint(path):
    try:
        st = os.stat(path)
        return f"{st.st_size}:{int(st.st_mtime)}"
    except (OSError, TypeError):
        return None


# ------------------------------------------------------------------------------------------- sending to Odoo --

def _fail(kind, local_id, row, error):
    attempts = (row or {}).get("attempts", 0) + 1
    db.central_set(kind, local_id, status="error", error=str(error)[:500], attempts=attempts,
                   next_try=time.time() + min(BACKOFF_START * 2 ** (attempts - 1), BACKOFF_MAX))
    _state["failures"].append(time.time())


def _resolve(vals, ids):
    """Links to other synced records become their Odoo ids. Returns (vals, missing): a link whose target isn't in Odoo
    yet is left empty and the record is sent again next round."""
    out, missing = {}, False
    for k, v in vals.items():
        if isinstance(v, dict) and REF in v:
            kind, lid = v[REF]
            oid = ids.get(kind, {}).get(lid)
            out[k] = oid or False
            missing = missing or not oid
        else:
            out[k] = v
    return out, missing


def _send(model, oid, vals):
    if oid:
        odoo._call(model, "write", ids=[oid], vals=vals)
        return oid
    return odoo._call(model, "create", vals_list=[vals])[0]


def push_kind(kind, records, ids, stamp):
    """Send one kind's changed records, archive the ones Invoice Flow no longer has. Returns (sent, failed, pending)."""
    model, key_of = MODEL[kind], lambda lid: f"{site()}:{kind}:{lid}"
    rows = db.central_rows(kind)
    sent = failed = pending = 0
    todo = []
    for lid, (vals, doc) in records.items():
        row = rows.get(lid)
        h, dh = fingerprint(vals), doc_fingerprint(doc) if doc else None
        due = not row or row["status"] != "error" or (row["next_try"] or 0) <= time.time()
        if row and row["hash"] == h and (row["doc_hash"] == dh or not doc) and row["status"] == "synced" and row["odoo_id"]:
            continue
        if not due:
            pending += 1
            continue
        todo.append((lid, vals, doc, h, dh, row))
    # one search for the Odoo ids of everything about to be sent without one: a record made in an earlier round whose
    # answer was lost, or by an earlier install, is updated, never made twice
    unknown = [key_of(lid) for lid, *_rest, row in todo if not (row and row["odoo_id"])]
    found = {}
    for i in range(0, len(unknown), 200):
        for r in odoo._call(model, "search_read", domain=[["x_key", "in", unknown[i:i + 200]]], fields=["id", "x_key"],
                            context={"active_test": False}):
            found[r["x_key"]] = r["id"]
    for lid, vals, doc, h, dh, row in todo:
        oid = (row or {}).get("odoo_id") or found.get(key_of(lid))
        out, missing = _resolve(vals, ids)
        out.update(x_key=key_of(lid), x_site=site(), x_last_sync=stamp, x_active=True, x_deleted_in_app=False)
        if doc and dh and (not row or row["doc_hash"] != dh or not row["odoo_id"]):
            try:
                with open(doc, "rb") as fh:
                    out["x_document"] = base64.b64encode(fh.read()).decode()
            except OSError:
                dh = None
        try:
            try:
                oid = _send(model, oid, out)
            except odoo.OdooUnreachable:
                raise
            except odoo.OdooError as e:
                # a bill, PO or vendor deleted in Odoo can't be linked any more: send the rest without those links
                if not any(k in out and out[k] for k in LINKS):
                    raise
                logger.warning("central sync: {} {} refused with its Odoo links ({}); sending it without them", kind, lid, e)
                oid = _send(model, oid, {k: (False if k in LINKS else v) for k, v in out.items()})
        except odoo.OdooUnreachable:
            raise
        except odoo.OdooError as e:
            _fail(kind, lid, row, e)
            failed += 1
            continue
        ids.setdefault(kind, {})[lid] = oid
        db.central_set(kind, lid, odoo_id=oid, hash=None if missing else h, doc_hash=dh if doc else None, status="synced", error=None,
                       attempts=0, next_try=0, synced_at=db.now())
        sent += 1
        pending += 1 if missing else 0
        time.sleep(WRITE_PAUSE)
    for lid, row in rows.items():
        if lid in records or row["status"] == "archived" or not row["odoo_id"]:
            continue
        try:
            odoo._call(model, "write", ids=[row["odoo_id"]], vals={"x_active": False, "x_deleted_in_app": True, "x_last_sync": stamp})
            db.central_set(kind, lid, status="archived", error=None, synced_at=db.now())
            sent += 1
        except odoo.OdooUnreachable:
            raise
        except odoo.OdooError as e:
            _fail(kind, lid, row, e)
            failed += 1
    return sent, failed, pending


def summary():
    rows = db.central_all()
    day_ago = time.time() - 86400
    _state["failures"] = [t for t in _state["failures"] if t > day_ago]
    q = db.outbox_list(1000)
    runs = db.all_runs()
    return dict(
        enabled=enabled(), site=site(), synced=sum(1 for r in rows if r["status"] == "synced"), archived=sum(1 for r in rows if r["status"] == "archived"),
        failed=sum(1 for r in rows if r["status"] == "error"), pending=_state["pending"], last_ok=_state["last_ok"], last_error=_state["last_error"],
        last_round=_state["last_round"], api_failures_24h=len(_state["failures"]), waiting_until=_state["backoff_until"] if _state["backoff_until"] > time.time() else None,
        queue=dict(pending=sum(1 for r in q if r["status"] in ("pending", "running")), failed=sum(1 for r in q if r["status"] == "error"),
                   done=sum(1 for r in q if r["status"] == "done")),
        processing_errors=sum(1 for r in runs if r["status"] == "error"),
        errors=[dict(kind=r["kind"], id=r["local_id"], error=r["error"], attempts=r["attempts"]) for r in rows if r["status"] == "error"][:100])


def _push_status(stamp):
    s = summary()
    vals = dict(x_sync_ok=s["synced"], x_sync_failed=s["failed"], x_sync_pending=s["pending"], x_queue_pending=s["queue"]["pending"],
                x_queue_failed=s["queue"]["failed"], x_queue_done=s["queue"]["done"], x_api_failures=s["api_failures_24h"],
                x_last_sync_ok=_utc(s["last_ok"]) if s["last_ok"] else stamp, x_last_error=s["last_error"] or False,
                x_processing_errors=s["processing_errors"])
    rows = odoo._call("x_if_platform", "search_read", domain=[["x_key", "=", "platform"]], fields=["id"], limit=1, context={"active_test": False})
    if rows:
        odoo._call("x_if_platform", "write", ids=[rows[0]["id"]], vals=vals)
    else:
        odoo._call("x_if_platform", "create", vals_list=[dict(vals, x_name="Invoice Flow", x_key="platform", x_active=True)])


def run_round(force=False):
    """One reconcile: build, compare, send what changed. Returns a summary dict. Safe to call from several threads."""
    if not enabled():
        return dict(skipped="central sync is off (CENTRAL_SYNC=1 and the Odoo settings switch it on)")
    if not force and _state["backoff_until"] > time.time():
        return dict(skipped="waiting after Odoo didn't answer")
    if not _round_lock.acquire(blocking=False):
        return dict(skipped="a round is already running")
    _state["running"] = True
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    totals = dict(sent=0, failed=0, pending=0)
    try:
        records = build()
        ids = {k: {lid: r["odoo_id"] for lid, r in db.central_rows(k).items() if r["odoo_id"] and r["status"] != "archived"} for k in KINDS}
        for kind in KINDS:
            s, f, p = push_kind(kind, records[kind], ids, stamp)
            totals["sent"] += s
            totals["failed"] += f
            totals["pending"] += p
        _state.update(pending=totals["pending"], last_ok=db.now(), backoff=BACKOFF_START, backoff_until=0.0,
                      last_error=None if not totals["failed"] else _state["last_error"] or "some records failed: see the sync errors")
        _push_status(stamp)
        if totals["sent"] or totals["failed"]:
            logger.info("central sync: {} sent, {} failed, {} waiting", totals["sent"], totals["failed"], totals["pending"])
    except odoo.OdooUnreachable as e:
        _state["failures"].append(time.time())
        _state.update(last_error=f"Odoo unreachable: {e}", backoff_until=time.time() + _state["backoff"],
                      backoff=min(_state["backoff"] * 2, BACKOFF_MAX))
        logger.warning("central sync: Odoo didn't answer ({}); trying again in {} s", e, int(_state["backoff_until"] - time.time()))
        totals["error"] = str(e)
    except odoo.OdooError as e:
        _state["failures"].append(time.time())
        _state["last_error"] = str(e)[:500]
        _state.update(backoff_until=time.time() + _state["backoff"], backoff=min(_state["backoff"] * 2, BACKOFF_MAX))
        logger.warning("central sync: {}", e)
        totals["error"] = str(e)
    finally:
        _state["running"] = False
        _state["last_round"] = db.now()
        _round_lock.release()
    return totals


def loop():
    """Background: a round at start (everything not yet in Odoo goes now), then on every change and every few minutes."""
    db.CHANGED.set()
    while True:
        db.CHANGED.wait(timeout=RECONCILE_EVERY)
        time.sleep(DEBOUNCE)
        db.CHANGED.clear()
        try:
            run_round()
        except Exception:                                     # never let the worker die
            logger.exception("central sync loop")
        wait = _state["backoff_until"] - time.time()
        if wait > 0:
            time.sleep(min(wait, BACKOFF_MAX))
