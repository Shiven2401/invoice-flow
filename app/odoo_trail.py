"""What Invoice Flow leaves behind in Odoo, for an account connected to it (odoo.py does the talking):

  1. Users      each account becomes a contact tagged "Invoice Flow User", on sign-up or its first sign-in.
  2. Approved   the draft bill gets the original PDF and an internal note: who processed it, the decision and why.
  3. Held or    no bill, so the note (and the PDF) go in the chatter of the invoice's PO; with no PO in Odoo, on the
     rejected  vendor's contact.
  4. Back       a bill posted or paid in Odoo shows as Posted or Paid in Invoice Flow.

None of this ever changes a decision: if Odoo doesn't take a note, the run says so and the invoice is decided as before.
"""
import html
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

from . import db, odoo, rules

WORD = {"APPROVE": "approved", "HOLD": "put on hold", "REJECT": "rejected"}


# --------------------------------------------------------------- 1. users --

_syncing, _tried = set(), {}
_lock = threading.Lock()


def sync_user(user_id):
    """Make (or find) the account's contact in Odoo and remember its id. Safe to call often: once synced it's a no-op,
    and a failed try waits 5 minutes before the next."""
    if not rules.uses_odoo(user_id):
        return None
    u = db.user_info(user_id)
    if not u or u["odoo_partner_id"]:
        return u and u["odoo_partner_id"]
    with _lock:
        if user_id in _syncing or time.time() - _tried.get(user_id, 0) < 300:
            return None
        _syncing.add(user_id)
        _tried[user_id] = time.time()
    try:
        google = (u["pw_hash"] or "").startswith("sso$google$")
        name = u["username"]
        details = (f"<p>Invoice Flow account <b>{html.escape(name)}</b>, signs in with {'Google' if google else 'a password'}.</p>"
                   f"<p>Account created {html.escape((u['created_at'] or '')[:16].replace('T', ' '))}; linked to Odoo "
                   f"{datetime.now():%Y-%m-%d %H:%M}. Invoices this person processes leave notes in the chatter of their "
                   f"bills and purchase orders.</p>")
        pid = odoo.upsert_contact(f"invoice-flow:{name.lower()}", name, details, email=name if "@" in name else None)
        db.set_odoo_partner(user_id, pid)
        logger.info("account {!r} is Odoo contact {}", name, pid)
        return pid
    except odoo.OdooError as e:
        logger.warning("couldn't add account {} to Odoo's contacts: {}", user_id, e)
        from . import erp_sync   # queued, so it's retried until Odoo takes it and shows in the sync log meanwhile
        erp_sync.enqueue("contact_sync", dict(user_id=user_id), f"contact:{user_id}", user_id, run_now=False)
        return None
    finally:
        with _lock:
            _syncing.discard(user_id)


def sync_user_later(user_id):
    """sync_user without making a sign-in wait for Odoo."""
    if rules.uses_odoo(user_id):
        threading.Thread(target=sync_user, args=(user_id,), daemon=True).start()


# ------------------------------------------------- 2 and 3. the audit trail --

def _reasons(result, decision):
    """The first reason as a sentence, and the rest as (label, message) pairs."""
    if decision == "APPROVE":
        lines = ((result.get("budget") or {}).get("lines")) or []
        first = "every check passed" + (f", including the 3-way match (PO, receipt, invoice) on {len(lines)} line{'s' * (len(lines) != 1)}" if lines else "")
        return first + ".", []
    blocking = [x for x in result.get("findings") or [] if x["severity"] in ("hold", "reject")]
    blocking.sort(key=lambda x: (rules.SEV[x["severity"]] * -1, rules.PRIORITY.index(x["code"]) if x["code"] in rules.PRIORITY else 99))
    if not blocking:
        return (result.get("headline") or "").rstrip(".") + ".", []
    msg = lambda x: (x.get("message") or rules.CODE_LABEL.get(x["code"], x["code"])).strip().rstrip(".") + "."
    return msg(blocking[0]), [(rules.CODE_LABEL.get(x["code"], x["code"]), msg(x)) for x in blocking[1:]]


def note_html(result, decision, user, filename, by_person=False, person_note="", target_label=""):
    f = result.get("fields") or {}
    inv = f.get("invoice_number") or filename
    first, more = _reasons(result, decision)
    e = html.escape
    who = (f"Decision recorded by <b>{e(user)}</b> after reviewing the hold" + (f": “{e(person_note)}”" if person_note else "") + "."
           if by_person else f"Decision recorded by <b>{e(user)}</b> (processed in Invoice Flow; decided by its checks).")
    facts = [x for x in [f"Vendor: {e(f.get('vendor_name') or '?')}" + (f" (GSTIN {e(f['vendor_gstin'])})" if f.get("vendor_gstin") else ""),
                         f"Total: {e(rules.inr(f.get('total')))}" if f.get("total") is not None else "",
                         f"PO: {e((result.get('po') or {}).get('po_number') or f.get('po_number') or 'none')}",
                         f"File: {e(filename)} (attached)"] if x]
    return (f"<p><b>Invoice {e(inv)} {WORD[decision]}</b>: {e(first)} {who}</p>"
            + (f"<ul>{''.join(f'<li>{e(label)}: {e(m)}</li>' for label, m in more)}</ul>" if more else "")
            + f"<p>{' · '.join(facts)} · {datetime.now():%d %b %Y %H:%M}</p>")


def _target(result, decision, po_odoo_id=None, vendor_odoo_id=None):
    """Where the record goes: the bill if one was made, else the PO, else the vendor's contact. (model, id, label, url)."""
    bill = result.get("erp") or {}
    if decision == "APPROVE" and bill.get("bill_id"):
        return "account.move", bill["bill_id"], f"draft bill #{bill['bill_id']}", odoo.link("bill", bill["bill_id"])
    po = result.get("po") or {}
    po_id = po_odoo_id or po.get("odoo_id")
    if not po_id and po.get("po_number"):
        found = odoo.po_by_name(po["po_number"])
        po_id = found and found["id"]
    if po_id:
        return "purchase.order", po_id, f"purchase order {po.get('po_number') or po_id}", odoo.link("po", po_id)
    v = result.get("vendor") or {}
    vid = vendor_odoo_id or v.get("odoo_id")
    if vid:
        return "res.partner", vid, f"vendor {v.get('name') or vid}", odoo.link("vendor", vid)
    pid = odoo.inbox_contact()     # Odoo knows neither: the inbox for unknown vendors, so the invoice is still on record there
    return "res.partner", pid, "the Invoice Flow inbox (vendor not in Odoo)", odoo.link("contact", pid)


def record(run_id, user_id, result, decision, by_person=False, person_note="", po_odoo_id=None, vendor_odoo_id=None):
    """Leave this decision's record in Odoo and add it to result["odoo_trail"]. Returns the trail entry (never raises)."""
    row = db.get_run(run_id, user_id) or {}
    user, filename = db.username(user_id) or "unknown", row.get("filename") or "invoice"
    codes = sorted({x["code"] for x in result.get("findings") or [] if x["severity"] in ("hold", "reject")})
    trail = json.loads(row.get("odoo_trail") or "[]")            # its own column, so a re-check keeps the earlier notes
    entry = dict(at=db.now(), decision=decision, by=user, by_person=by_person, codes=codes)
    try:
        if "ERP_UNREACHABLE" in codes and not by_person:
            entry["error"] = "Odoo didn't answer, so nothing could be recorded there; checking again records it"
            return entry
        target = _target(result, decision, po_odoo_id, vendor_odoo_id)
        if not target:
            entry["error"] = "Odoo has neither this invoice's PO nor its vendor, so there was no record to note it on"
            return entry
        model, rec_id, label, url = target
        last = next((t for t in reversed(trail) if t.get("target")), None)
        if not by_person and last and last["decision"] == decision and last["codes"] == codes and last["target"]["model"] == model \
                and last["target"]["id"] == rec_id:
            entry = dict(last, repeated=True)                        # checked again, same answer: one note is enough
            return entry
        atts = []
        path = Path(row.get("file_path") or "")
        if path.is_file():
            atts.append(odoo.attach(model, rec_id, filename, path.read_bytes()))
        mid = odoo.note(model, rec_id, note_html(result, decision, user, filename, by_person, person_note, label), atts)
        entry.update(target=dict(model=model, id=rec_id, label=label, url=url), message_id=mid, attachment_ids=atts)
    except odoo.OdooError as e:
        entry["error"] = f"Odoo didn't take the note ({e})"
        logger.bind(run=run_id).warning("audit note not recorded in Odoo: {}", e)
    finally:
        if not entry.get("repeated"):
            trail.append(entry)
            db.update_run(run_id, odoo_trail=json.dumps(trail))
        result["odoo_trail"] = trail
    return entry


# ------------------------------------------------------- 4. back from Odoo --

def status_of(state, payment_state):
    if state == "cancel":
        return "cancel"
    if state != "posted":
        return "draft"
    return payment_state if payment_state in ("paid", "in_payment", "partial", "reversed") else "posted"


_last_refresh = {}


def refresh(user_id=None, every=15):
    """Read the state of this account's open bills (all accounts' when user_id is None) and store it. At most once
    every `every` seconds per account; Odoo being down just leaves the last known state."""
    key = user_id if user_id is not None else "*"
    if user_id is not None and not rules.uses_odoo(user_id):
        return 0
    if time.time() - _last_refresh.get(key, 0) < every:
        return 0
    _last_refresh[key] = time.time()
    rows = [r for r in db.open_bills(user_id) if user_id is not None or rules.uses_odoo(r[2])]
    if not rows:
        return 0
    try:
        states = odoo.bill_states({bill for _, bill, _ in rows})
    except odoo.OdooError as e:
        logger.warning("couldn't read bill states from Odoo: {}", e)
        return 0
    changed = 0
    for run_id, bill, owner in rows:
        new = status_of(*states[bill]) if bill in states else "deleted"
        old = (db.get_run(run_id, owner) or {}).get("odoo_status")
        if new != old:
            db.update_run(run_id, odoo_status=new)
            changed += 1
            logger.bind(run=run_id).info("Odoo bill #{} is now {}", bill, new)
    return changed


def poll_forever(seconds=60):
    """Keeps every connected account's bill states fresh, even with nobody looking at the Dashboard."""
    while True:
        time.sleep(seconds)
        try:
            refresh(None, every=0)
        except Exception:                                            # never let the poller die
            logger.exception("Odoo status poll failed")
