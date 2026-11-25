"""The platform view inside Invoice Flow, for platform administrators only (ADMIN_ACCOUNTS): every user, invoice, folder,
purchase order, vendor and activity row across all accounts, the same data the central sync puts in Odoo.

Access is decided here, by Invoice Flow, never by Odoo: every route depends on admin_user, which checks the signed-in
account against ADMIN_ACCOUNTS and answers 403 to everyone else (an ordinary account can't tell these routes exist from
its own data). Ordinary routes are unchanged and still see only their own account. Opening another person's invoice or
document is written to the admin audit log (who, what, whose, when), visible on the page and at /api/admin/audit.

GET  /api/admin/overview                platform figures (users, invoices, folders, POs, money, sync)
GET  /api/admin/users                   every account with its counts
GET  /api/admin/invoices?...            every invoice; filters: user, q, folder, vendor, po, approval, status, payment,
                                        match, date_from, date_to (invoice date), upload_from, upload_to
GET  /api/admin/invoices/{id}           one invoice: fields, checks, decision, trail, activity (audited)
GET  /api/admin/invoices/{id}/file      its original document (audited)
GET  /api/admin/folders                 users > folders > invoice counts
GET  /api/admin/pos · /vendors          every PO and vendor with its owner
GET  /api/admin/activity(.csv)?user=    the activity log of every account
GET  /api/admin/sync · POST /api/admin/sync   the central sync to Odoo: state, errors; run a round now
GET  /api/admin/audit                   what administrators opened
"""
import csv
import io
import json
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response

from . import auth, central_sync, config, db, odoo

router = APIRouter()


def admin_user(user: dict = Depends(auth.current_user)):
    if not central_sync.is_admin(user["username"]):
        raise HTTPException(403, "Only a platform administrator can see this.")
    return user


def _ctx():
    users = {u["id"]: u for u in db.all_users()}
    folders = {f["id"]: f for f in db.all_folders()}
    return users, folders


def _row(r, users, folders):
    d = central_sync.describe_run(r, users, folders)
    return dict(id=r["id"], user_id=r["user_id"], username=d["x_owner_name"], email=d["x_owner_email"] or None, folder_id=r.get("folder_id"),
                folder=d["x_folder_name"] or None, filename=r.get("filename"), uploaded_at=r.get("created_at"), decided_at=r.get("decided_at"),
                status=d["x_processing_status"], approval=d["x_approval"], decided_by=d["x_decided_by"], reason=d["x_reason"], match=d["x_match"],
                vendor=d["x_vendor_name"] or None, gstin=d["x_vendor_gstin"] or None, invoice_number=r.get("invoice_number"),
                invoice_date=r.get("invoice_date"), po_number=r.get("po_number"), net=r.get("net"), total=r.get("total"),
                duplicate=d["x_duplicate"], bill_id=r.get("odoo_bill_id"), bill_state=d["x_bill_state"] or None,
                payment=d["x_payment_state"] or None, codes=d["x_codes"] or "", extractor=r.get("extractor"))


def _rows():
    users, folders = _ctx()
    return [_row(r, users, folders) for r in db.all_runs()], users, folders


def _between(v, lo, hi):
    v = (v or "")[:10]
    return (not lo or (v and v >= lo)) and (not hi or (v and v <= hi))


@router.get("/api/admin/overview")
def overview(user: dict = Depends(admin_user)):
    rows, users, folders = _rows()
    count = lambda **kw: sum(1 for r in rows if all(r[k] == v for k, v in kw.items()))
    value = lambda rs: round(sum(r["total"] or 0 for r in rs), 2)
    pos = pos_list(user)["pos"]
    recent = {u for u in users if (users[u].get("last_login_at") or "") >= _days_ago(30)}
    return dict(
        users=dict(total=len(users), active_30d=len(recent), admins=sum(1 for u in users.values() if central_sync.is_admin(u["username"]))),
        invoices=dict(total=len(rows), processed=count(status="Processed"), pending=sum(1 for r in rows if r["approval"] in ("Pending", "On hold")),
                      approved=count(approval="Approved"), held=count(approval="On hold"), rejected=count(approval="Rejected"),
                      matched=count(match="Matched"), unmatched=sum(1 for r in rows if r["match"] in ("Mismatched", "PO not found", "Partly matched")),
                      failed=count(status="Failed"), duplicates=sum(1 for r in rows if r["duplicate"]),
                      bills_draft=count(bill_state="draft"), bills_posted=count(bill_state="posted")),
        folders=len(folders), pos=dict(total=len(pos), by_source={s: sum(1 for p in pos if p["source"] == s) for s in {p["source"] for p in pos}}),
        money=dict(invoice_value=value(rows), approved_value=value([r for r in rows if r["approval"] == "Approved"]),
                   held_value=value([r for r in rows if r["approval"] == "On hold"]),
                   paid_value=value([r for r in rows if r["payment"] in ("paid", "in_payment")]),
                   po_value=round(sum(p["total"] or 0 for p in pos), 2)),
        sync=central_sync.summary(), odoo=dict(odoo.info(), platform_menu=f"{odoo.info()['url']}/odoo") if odoo.enabled() else None)


def _days_ago(n):
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=n)).isoformat(timespec="seconds")


@router.get("/api/admin/users")
def users_list(user: dict = Depends(admin_user)):
    rows, users, folders = _rows()
    out = []
    for u in users.values():
        mine = [r for r in rows if r["user_id"] == u["id"]]
        out.append(dict(id=u["id"], username=u["username"], email=u["username"] if "@" in u["username"] else None,
                        role="Platform admin" if central_sync.is_admin(u["username"]) else "User", login_method=u["login_method"],
                        created_at=u["created_at"], last_login_at=u.get("last_login_at"), odoo_connected=odoo.serves(u["username"]),
                        invoices=len(mine), folders=sum(1 for f in folders.values() if f["user_id"] == u["id"]),
                        pos=sum(1 for p in db.all_added_pos() if p["user_id"] == u["id"]),
                        pending=sum(1 for r in mine if r["approval"] in ("Pending", "On hold")), failed=sum(1 for r in mine if r["status"] == "Failed"),
                        approved=sum(1 for r in mine if r["approval"] == "Approved"), rejected=sum(1 for r in mine if r["approval"] == "Rejected"),
                        value=round(sum(r["total"] or 0 for r in mine), 2)))
    return dict(users=out)


@router.get("/api/admin/invoices")
def invoices(user: dict = Depends(admin_user), user_id: int | None = None, q: str = "", folder: int | None = None, vendor: str = "",
             po: str = "", approval: str = "", status: str = "", payment: str = "", match: str = "", date_from: str = "", date_to: str = "",
             upload_from: str = "", upload_to: str = ""):
    rows, _users, _folders = _rows()
    ql = q.strip().lower()
    keep = [r for r in rows
            if (user_id is None or r["user_id"] == user_id) and (folder is None or r["folder_id"] == folder)
            and (not vendor or vendor.lower() in (r["vendor"] or "").lower()) and (not po or po.lower() in (r["po_number"] or "").lower())
            and (not approval or r["approval"] == approval) and (not status or r["status"] == status) and (not match or r["match"] == match)
            and (not payment or (r["payment"] or "none") == payment)
            and _between(r["invoice_date"], date_from, date_to) and _between(r["uploaded_at"], upload_from, upload_to)
            and (not ql or any(ql in str(r[k] or "").lower() for k in ("invoice_number", "filename", "username", "email", "vendor", "id", "folder")))]
    return dict(invoices=keep, total=len(rows))


def _owned_run(run_id, admin, action):
    r = db.all_runs(run_id)
    if not r:
        raise HTTPException(404, "invoice not found")
    owner = db.username(r["user_id"])
    db.audit(admin["username"], action, f"invoice {run_id} ({r.get('filename')})", owner)
    return r, owner


@router.get("/api/admin/invoices/{run_id}")
def invoice_detail(run_id: str, user: dict = Depends(admin_user)):
    r, _owner = _owned_run(run_id, user, "opened invoice")
    users, folders = _ctx()
    row = _row(r, users, folders)
    res = json.loads(r.get("result_json") or "null") or {}
    full = db.get_run(run_id, r["user_id"]) or {}
    return dict(row, result=res, inputs=json.loads(r.get("inputs_json") or "{}"), trail=json.loads(full.get("odoo_trail") or "[]"),
                activity=[a for a in db.all_activity() if a["run_id"] == run_id],
                odoo_bill_url=odoo.link("bill", r["odoo_bill_id"]) if r.get("odoo_bill_id") and odoo.enabled() else None)


@router.get("/api/admin/invoices/{run_id}/file")
def invoice_file(run_id: str, user: dict = Depends(admin_user)):
    r, _owner = _owned_run(run_id, user, "opened document")
    if not r.get("file_path") or not Path(r["file_path"]).exists():
        raise HTTPException(404, "file not found")
    return FileResponse(r["file_path"], filename=r["filename"], content_disposition_type="inline")


@router.get("/api/admin/folders")
def folders_tree(user: dict = Depends(admin_user)):
    rows, users, folders = _rows()
    tree = []
    for u in users.values():
        mine = [r for r in rows if r["user_id"] == u["id"]]
        fs = [dict(id=f["id"], name=f["name"], created_at=f["created_at"], invoices=[dict(id=r["id"], name=r["invoice_number"] or r["filename"],
                                                                                         approval=r["approval"], total=r["total"]) for r in mine if r["folder_id"] == f["id"]])
              for f in folders.values() if f["user_id"] == u["id"]]
        tree.append(dict(user_id=u["id"], username=u["username"], folders=fs, unfiled=sum(1 for r in mine if not r["folder_id"]), invoices=len(mine)))
    return dict(users=tree)


@router.get("/api/admin/pos")
def pos_list(user: dict = Depends(admin_user)):
    built = central_sync.build()
    owner = {str(u["id"]): u["username"] for u in db.all_users()}
    out = []
    for lid, (v, _doc) in built["po"].items():
        u = v["x_user_id"]
        out.append(dict(id=lid, po_number=v["x_name"], owner=owner.get(u["__ref__"][1]) if u else None, source=v["x_source"], vendor=v["x_vendor_name"],
                        po_date=v["x_po_date"] or None, description=v["x_description"] or None, net=v["x_net"], total=v["x_total"], status=v["x_status"],
                        receipt=v["x_receipt_status"] or None, billed=v["x_billed"], remaining=v["x_remaining"],
                        odoo_url=odoo.link("po", v["x_po_id"]) if v["x_po_id"] else None))
    return dict(pos=out)


@router.get("/api/admin/vendors")
def vendors_list(user: dict = Depends(admin_user)):
    built = central_sync.build()
    owner = {str(u["id"]): u["username"] for u in db.all_users()}
    return dict(vendors=[dict(id=lid, name=v["x_name"], vendor_id=v["x_vendor_ref"], owner=owner.get(v["x_user_id"]["__ref__"][1]) if v["x_user_id"] else None,
                              gstin=v["x_gstin"] or None, status=v["x_status"], approved_at=v["x_approved_at"] or None,
                              odoo_url=odoo.link("vendor", v["x_partner_id"]) if v["x_partner_id"] else None, link=v["x_link_status"] or None)
                         for lid, (v, _d) in built["vendor"].items()])


ACTIVITY_COLS = ["username", "session_id", "filename", "run_id", "invoice_number", "vendor", "decision", "decided_by", "reason",
                 "uploaded_at", "decided_at", "odoo_bill_id", "sync_status"]


def _activity(user_id):
    return [{k: a[k] for k in ACTIVITY_COLS} | dict(user_id=a["user_id"]) for a in db.all_activity() if user_id is None or a["user_id"] == user_id]


@router.get("/api/admin/activity")
def activity(user: dict = Depends(admin_user), user_id: int | None = None):
    return dict(rows=_activity(user_id))


@router.get("/api/admin/activity.csv")
def activity_csv(user: dict = Depends(admin_user), user_id: int | None = None):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(ACTIVITY_COLS)
    for a in _activity(user_id):
        w.writerow([a[k] if a[k] is not None else "" for k in ACTIVITY_COLS])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="invoice-flow-platform-activity-{config.as_of()}.csv"'})


@router.get("/api/admin/sync")
def sync_state(user: dict = Depends(admin_user)):
    return central_sync.summary()


@router.post("/api/admin/sync")
def sync_now(user: dict = Depends(admin_user)):
    if not central_sync.enabled():
        raise HTTPException(400, "The central sync to Odoo is off on this server (CENTRAL_SYNC=1 switches it on).")
    threading.Thread(target=central_sync.run_round, kwargs=dict(force=True), daemon=True).start()
    return dict(ok=True, started=True)


@router.get("/api/admin/audit")
def audit(user: dict = Depends(admin_user)):
    return dict(rows=db.audit_rows())
