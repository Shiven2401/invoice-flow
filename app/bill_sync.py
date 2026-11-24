"""Every invoice decided in Invoice Flow has one bill in Odoo, kept in line with the decision. Only ever draft or
cancelled: Invoice Flow never posts or pays a bill (a person does that in Odoo).

    Approved   draft bill. With a matched PO (the 3-way match): its lines linked to the PO lines, as before. Without a
               PO: the invoice's own lines, and the note "No PO, approved vendor, approved by <user>"
    Held       draft bill, "ON HOLD: <reason>". Never linked to PO lines, so Odoo's billed quantities (and with them the
               3-way match of the PO's later invoices) are untouched
    Rejected   the same bill, cancelled, with the reason
    Duplicate  (rejected as a resubmission of an earlier invoice) the original's bill is left as it is; a note on it
               records the resubmission

One bill per invoice: an invoice is its vendor (GSTIN, or the cleaned name) plus its number (table invoice_bills).
Uploaded again, or decided again (held, then approved), it updates the same bill: a cancelled one is set back to
draft, lines and note are rewritten. A bill already posted in Odoo is never changed; the decision is noted on it.
The original PDF is attached once. A vendor Odoo doesn't have gets no vendor record: the bill has no vendor and its
note names the vendor as read.
"""
import html
import json
from pathlib import Path

from loguru import logger

from . import db, extract, odoo, odoo_trail, rules

DUPLICATE = {"DUPLICATE_EXACT", "INVOICE_NUMBER_REUSED"}
STATE = {"APPROVE": "draft", "HOLD": "draft", "REJECT": "cancel"}


def invoice_key(fields, vendor=None):
    """vendor + invoice number, the same whoever uploads it and whatever was decided. None without a number."""
    num = extract.norm_alnum(fields.get("invoice_number"))
    if not num:
        return None
    gstin = ((vendor or {}).get("gstin") or fields.get("vendor_gstin") or "").replace(" ", "").upper()
    who = gstin or extract.norm_alnum(extract.clean_vendor_name((vendor or {}).get("name") or fields.get("vendor_name") or ""))
    return f"{who}|{num}" if who else None


def _reason(result, decision, user, by_person, person_note, po, linked=0):
    first, more = odoo_trail._reasons(result, decision)
    who = user if by_person else f"{user} (Invoice Flow checks)"
    if decision == "APPROVE":
        tail = f" Note: {person_note}" if person_note else ""
        if po and linked:
            return f"Approved in Invoice Flow by {who}: 3-way match with {po}." + tail
        if po:
            return f"Approved in Invoice Flow by {who} against {po} (checked in Invoice Flow; lines not linked to an Odoo PO)." + tail
        return f"No PO, approved vendor, approved by {who}." + tail
    label = "ON HOLD" if decision == "HOLD" else "REJECTED"
    reasons = "; ".join([first.rstrip(".")] + [m.rstrip(".") for _, m in more])
    return f"{label}: {reasons}." + (f" Decided by {user}" + (f": {person_note}" if person_note else "") + "." if by_person else "")


def _lines(result, decision, f):
    """Linked to the PO lines only for an approval with a matched PO; otherwise the invoice's own lines."""
    plan = result.get("erp_plan") or {}
    out = []
    for ln in plan.get("lines") or []:
        pl = ln.get("po_line")
        if pl and decision == "APPROVE":
            vals = {"product_id": pl["product_id"][0], "name": pl.get("name") or pl["product_id"][1], "quantity": ln["quantity"],
                    "price_unit": ln["price_unit"], "purchase_line_id": pl["id"]}
            if pl.get("tax_ids") is not None:
                vals["tax_ids"] = [[6, 0, pl["tax_ids"]]]
        else:
            vals = {"name": (pl and (pl.get("name") or pl["product_id"][1])) or ln.get("name") or "Invoice line",
                    "quantity": ln["quantity"], "price_unit": ln["price_unit"]}
        if ln.get("discount"):
            vals["discount"] = ln["discount"]
        out.append([0, 0, vals])
    if not out:
        amount = (result.get("derived") or {}).get("net") or f.get("subtotal") or f.get("total") or 0.0
        out.append([0, 0, {"name": f"Invoice {f.get('invoice_number') or ''}".strip(), "quantity": 1.0, "price_unit": float(amount)}])
    return out


def sync(run_id, user_id, decision, result, by_person=False, person_note="", acting_user=None):
    """Create or update this invoice's bill for `decision`. Returns {bill_id, url, state, action, lines, linked}.
    Raises odoo.OdooError (OdooUnreachable when Odoo didn't answer: the queue tries again)."""
    row = db.get_run(run_id, user_id) or {}
    f = result.get("fields") or {}
    vendor = result.get("vendor") or {}
    user = acting_user or db.username(user_id) or "unknown"
    filename = row.get("filename") or "invoice"
    codes = {x["code"] for x in result.get("findings") or [] if x["severity"] in ("hold", "reject")}
    key = invoice_key(f, vendor) or f"run:{run_id}"
    link = db.bill_link(key)
    po = (result.get("po") or {}).get("po_number")
    e = html.escape

    if decision == "REJECT" and codes & DUPLICATE:          # a resubmission: the original invoice's bill stays as it is
        if link and link.get("odoo_bill_id") and link.get("run_id") != run_id:
            odoo.note("account.move", link["odoo_bill_id"], f"<p><b>Resubmission rejected</b>: the same invoice was uploaded again as "
                      f"{e(filename)} and rejected as a duplicate by Invoice Flow (for {e(user)}). This bill is unchanged.</p>")
            _record(run_id, user_id, link["odoo_bill_id"], link["state"], decision, "duplicate noted on the original's bill", by_person)
            return dict(bill_id=link["odoo_bill_id"], url=odoo.bill_url(link["odoo_bill_id"]), state=link["state"], action="duplicate-noted")
        key = f"run:{run_id}"                                  # the original never got a bill: this rejection gets its own
        link = db.bill_link(key)

    partner = vendor.get("odoo_id") or (result.get("erp_plan") or {}).get("partner_id") or _odoo_vendor(vendor, user_id)
    lines = _lines(result, decision, f)
    reason = _reason(result, decision, user, by_person, person_note, po, sum(1 for ln in lines if ln[2].get("purchase_line_id")))
    as_read = "" if partner else (f" Vendor (not in Odoo): {f.get('vendor_name') or 'unknown'}"
                                  + (f", GSTIN {f['vendor_gstin']}" if f.get("vendor_gstin") else "") + ".")
    narration = reason + as_read
    target = STATE[decision]
    move_type = (result.get("erp_plan") or {}).get("move_type") or ("in_refund" if f.get("doc_type") == "credit_note" else "in_invoice")
    vals = {"partner_id": partner or False, "ref": f.get("invoice_number") or filename, "narration": narration, "invoice_line_ids": lines}
    if f.get("invoice_date"):
        vals["invoice_date"] = f["invoice_date"]
    if f.get("due_date"):
        vals["invoice_date_due"] = f["due_date"]

    bill_id, state, action = (link or {}).get("odoo_bill_id"), None, "created"
    if not bill_id and partner and f.get("invoice_number"):  # entered in Odoo by hand, or made before the link table: reuse it
        found = odoo.find_bill(partner, f["invoice_number"], move_type)
        bill_id = found and found["id"]
    if bill_id:
        state = odoo.bill_state(bill_id)
        if state is None:
            bill_id = None                                     # deleted in Odoo: make it again
    if bill_id and state == "posted":                          # a person posted it: never changed from here
        odoo.note("account.move", bill_id, f"<p><b>Invoice Flow</b>: {e(reason)} The bill is already posted, so it was not changed.</p>")
        _record(run_id, user_id, bill_id, "posted", decision, "bill already posted in Odoo; not changed", by_person)
        db.set_bill_link(key, bill_id, "posted", decision, run_id)
        return dict(bill_id=bill_id, url=odoo.bill_url(bill_id), state="posted", action="posted-unchanged")
    if bill_id:
        if state == "cancel":
            odoo.set_bill_state(bill_id, "draft")
        odoo.write_bill(bill_id, dict(vals, invoice_line_ids=[[5, 0, 0]] + lines))   # [5]: clear the old lines, then the new ones
        action = "updated"
    else:
        bill_id = odoo.new_bill(dict(vals, move_type=move_type))
    if target == "cancel":
        odoo.set_bill_state(bill_id, "cancel")
    # the bill is what counts: if Odoo won't take the PDF or the note, the decision stands and the run says so
    attachment, extra_error = (link or {}).get("attachment_id"), None
    path = Path(row.get("file_path") or "")
    same = bool(link and link.get("decision") == decision and link.get("run_id") == run_id and link.get("odoo_bill_id") == bill_id)
    try:
        if not attachment and path.is_file():
            attachment = odoo.attach("account.move", bill_id, filename, path.read_bytes())
        if not same:                                           # the chatter says what was decided, by whom and why
            odoo.note("account.move", bill_id, odoo_trail.note_html(result, decision, user, filename, by_person, person_note)
                      + f"<p>{e(narration)}</p>", [attachment] if attachment and not (link or {}).get("attachment_id") else [])
    except odoo.OdooUnreachable:
        raise
    except odoo.OdooError as err:
        extra_error = f"Odoo didn't take the note or the PDF ({err})"
        logger.bind(run=run_id).warning("bill #{}: {}", bill_id, extra_error)
    db.set_bill_link(key, bill_id, target, decision, run_id, attachment)
    if not same or extra_error:
        _record(run_id, user_id, bill_id, target, decision, action, by_person, attachment, extra_error)
    linked = sum(1 for ln in lines if ln[2].get("purchase_line_id"))
    logger.bind(run=run_id).info("Odoo bill #{} {} ({}, {})", bill_id, action, decision, target)
    return dict(bill_id=bill_id, url=odoo.bill_url(bill_id), state=target, action=action, lines=len(lines), linked=linked,
                move_type=move_type, system="Odoo")


def _odoo_vendor(vendor, user_id):
    """The Odoo id of the invoice's vendor when the result doesn't carry it: an Odoo vendor ("OD<id>"), or an app vendor
    linked to Odoo since (vendor_odoo). None for a vendor Odoo doesn't have, or no vendor."""
    vid = (vendor or {}).get("vendor_id") or ""
    if (vendor or {}).get("status") not in ("approved", "blocked", None) or not vid:
        return None
    if vid.startswith("OD") and vid[2:].isdigit():
        return int(vid[2:])
    link = db.vendor_links(user_id).get(vid)
    return link.get("odoo_id") if link else None


def _record(run_id, user_id, bill_id, state, decision, action, by_person, attachment=None, error=None):
    """The run, its trail (shown in Invoice Flow) and the activity log all point at the bill."""
    db.update_run(run_id, odoo_bill_id=bill_id, odoo_status=state)
    row = db.get_run(run_id, user_id) or {}
    trail = json.loads(row.get("odoo_trail") or "[]")
    trail.append(dict(at=db.now(), decision=decision, by=db.username(user_id), by_person=by_person, codes=[], action=action,
                      target=dict(model="account.move", id=bill_id, label=f"bill #{bill_id} ({state})", url=odoo.bill_url(bill_id)),
                      attachment_ids=[attachment] if attachment else [], **({"error": error} if error else {})))
    db.update_run(run_id, odoo_trail=json.dumps(trail))
    db.activity_for_run(run_id, odoo_bill_id=bill_id, sync_status=f"synced: bill {state}")


def preview_bill(fields, vendor, decision, codes, key_links):
    """What sync would do, without calling Odoo for writes: (action, state). key_links: {invoice_key: link}."""
    key = invoice_key(fields, vendor)
    if decision == "REJECT" and codes & DUPLICATE:
        return ("note on the original's bill", "note") if key and key in key_links else ("own cancelled bill", "cancel")
    link = key_links.get(key) if key else None
    if link and link.get("state") == "posted":
        return "note only (bill already posted)", "posted"
    return ("update bill #%s" % link["odoo_bill_id"] if link else "create"), STATE[decision]   # "(new)": made earlier in the same backfill


def vendor_plan(name, gstin, status, odoo_vendors):
    """For the dry run and for linking: ('match', odoo_id, how) / ('create', None, why) / ('skip', None, why)."""
    if status == "blocked":
        return "skip", None, "blocked in Invoice Flow"
    gstin = (gstin or "").replace(" ", "").upper()
    if not gstin:
        return "skip", None, "no GSTIN: flagged, not sent to Odoo"
    for v in odoo_vendors:
        if (v.get("vat") or "").upper() == gstin:
            return "match", v["id"], "same GSTIN"
    clean = rules.norm_name(extract.clean_vendor_name(name))   # labels off, case, punctuation and Pvt/Ltd/LLP aside
    for v in odoo_vendors:
        if clean and rules.norm_name(v["name"]) == clean:
            return "match", v["id"], "same name"
    return "create", None, "approved in Invoice Flow, not in Odoo"
