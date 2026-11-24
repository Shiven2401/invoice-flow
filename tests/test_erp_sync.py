"""The two-way sync (app/erp_sync.py, app/erp_api.py) against a stand-in Odoo that answers the same model/method calls
as the real one (search_read, read, create, write, button_confirm, message_post), keeps write_dates, and can go down,
or time out after doing a write. No network."""
import copy
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import db, erp_sync, main, odoo, odoo_trail
from test_accounts import clean, client, run_sample  # noqa: F401  (clean is an autouse fixture)


class FakeOdoo:
    M2O = {"partner_id": "res.partner", "order_id": "purchase.order", "product_id": "product.product", "purchase_id": "purchase.order",
           "picking_id": "stock.picking", "purchase_line_id": "purchase.order.line", "uom_id": "uom.uom", "categ_id": "product.category",
           "state_id": "res.country.state", "country_id": "res.country", "property_supplier_payment_term_id": "account.payment.term",
           "currency_id": "res.currency", "payment_term_id": "account.payment.term", "user_id": "res.users", "location_dest_id": "stock.location"}

    def __init__(self):
        self.db, self.seq, self.clock = {}, 0, datetime(2026, 11, 24, 10, 0, 0)
        self.down = self.timeout_after_write = False
        self.two_step, self.notes, self.calls = None, [], []
        v1 = self.add("res.partner", name="Sharma Office Supplies Pvt Ltd", vat="08AABCS1234F1Z5", supplier_rank=1, email="ap@sharma.test", city="Jodhpur")
        self.add("res.partner", name="Rathore IT Services LLP", vat="08AAFFR5678K1Z2", supplier_rank=1)
        p1 = self.add("product.product", name="A4 copier paper", default_code="PAP-A4", purchase_ok=True, type="consu", is_storable=True,
                      standard_price=280.0, supplier_taxes_id=[7], qty_available=0.0, incoming_qty=0.0, virtual_available=0.0, uom_id=1)
        self.add("product.product", name="Toner cartridge", default_code="TON-85A", purchase_ok=True, type="consu", is_storable=True, standard_price=1900.0,
                 supplier_taxes_id=[7], qty_available=5.0, incoming_qty=0.0, virtual_available=5.0, uom_id=1)
        self.add("account.tax", name="18% GST P", amount=18.0, type_tax_use="purchase", active=True)
        po = self.call("purchase.order", "create", vals_list=[dict(partner_id=v1, order_line=[[0, 0, dict(product_id=p1, name="A4 copier paper", product_qty=100, price_unit=280, tax_ids=[[6, 0, [7]]])]])])[0]
        self.call("purchase.order", "button_confirm", ids=[po])
        self.add("account.move", name="BILL/2026/0001", ref="S-1", move_type="in_invoice", state="posted", payment_state="not_paid", partner_id=v1,
                 invoice_date="2026-11-20", amount_untaxed=10000.0, amount_tax=1800.0, amount_total=11800.0, amount_residual=11800.0)
        self.add("account.move", name="BILL/2026/0002", ref="S-2", move_type="in_invoice", state="posted", payment_state="paid", partner_id=v1,
                 invoice_date="2026-11-21", amount_untaxed=5000.0, amount_tax=900.0, amount_total=5900.0, amount_residual=0.0)

    # ------------------------------------------------------------ storage --
    def tick(self):
        self.clock += timedelta(seconds=5)
        return self.clock.strftime("%Y-%m-%d %H:%M:%S")

    def add(self, model, **vals):
        self.seq += 1
        rec = dict(vals, id=self.seq, write_date=self.tick(), active=vals.get("active", True))
        self.db.setdefault(model, {})[self.seq] = rec
        return self.seq

    def touch(self, model, rid, **vals):
        self.db[model][rid].update(vals, write_date=self.tick())

    def out(self, model, rec, fields):
        row = {}
        for f in fields or rec.keys():
            v = rec.get(f, False)
            if f in self.M2O and isinstance(v, int) and not isinstance(v, bool):
                target = self.db.get(self.M2O[f], {}).get(v, {})
                v = [v, target.get("name", f"#{v}")]
            elif f in ("order_line", "picking_ids", "invoice_ids", "move_ids", "reconciled_bill_ids", "category_id", "supplier_taxes_id", "tax_ids") and not v:
                v = []
            row[f] = v
        return row

    def match(self, rec, dom):
        for f, op, val in dom:
            v = rec.get(f, False)
            if isinstance(v, list) and v and f in self.M2O:
                v = v[0]
            ok = {"=": lambda: v == val, "!=": lambda: v != val, "in": lambda: v in val, ">": lambda: v is not False and v > val,
                  ">=": lambda: v is not False and v >= val, "<": lambda: v is not False and v < val,
                  "=ilike": lambda: str(v).lower() == str(val).lower(), "ilike": lambda: str(val).lower() in str(v).lower()}[op]()
            if not ok:
                return False
        return True

    # ------------------------------------------------------------ the API --
    def call(self, model, method, **p):
        self.calls.append((model, method))
        if self.down:
            raise odoo.OdooUnreachable("Odoo didn't answer (ConnectTimeout)")
        table = self.db.setdefault(model, {})
        if method == "search_read":
            rows = [r for r in table.values() if r.get("active", True) and self.match(r, p.get("domain", []))]
            return [self.out(model, r, p.get("fields")) for r in rows[: p.get("limit") or None]]
        if method == "search_count":
            return sum(1 for r in table.values() if r.get("active", True) and self.match(r, p.get("domain", [])))
        if method == "read":
            return [self.out(model, table[i], p.get("fields")) for i in p["ids"] if i in table and table[i].get("active", True)]
        if method == "create":
            ids = [self._create(model, vals) for vals in p["vals_list"]]
            if self.timeout_after_write:
                self.timeout_after_write = False
                raise odoo.OdooUnreachable("Odoo didn't answer (ReadTimeout)")     # it did the write, the answer got lost
            return ids
        if method == "write":
            for i in p["ids"]:
                self._write(model, i, dict(p["vals"]))
            return True
        if method == "button_confirm":
            for i in p["ids"]:
                po = table[i]
                big = self.two_step is not None and po["amount_total"] >= self.two_step
                po.update(state="to approve" if big else "purchase", write_date=self.tick())
                if not big:
                    po["date_approve"] = po["write_date"]
                    self._picking(i)
            return True
        if method == "message_post":
            self.notes.append((model, p["ids"][0], p["body"]))
            return [len(self.notes)]
        if method == "fields_get":
            return {}
        raise AssertionError(f"unexpected call {model}.{method}")

    def _create(self, model, vals):
        vals = dict(vals)
        lines = vals.pop("order_line", None)
        if model == "purchase.order":
            n = sum(1 for _ in self.db.get(model, {})) + 1
            rid = self.add(model, name=f"P{n:05d}", state="draft", receipt_status=False, invoice_status="no", date_order=self.clock.strftime("%Y-%m-%d %H:%M:%S"),
                           picking_ids=[], invoice_ids=[], order_line=[], **vals)
            self._lines(rid, lines or [])
            return rid
        return self.add(model, **vals)

    def _write(self, model, rid, vals):
        lines = vals.pop("order_line", None)
        self.touch(model, rid, **vals)
        if lines is not None:
            self._lines(rid, lines)

    def _lines(self, po_id, cmds):
        po = self.db["purchase.order"][po_id]
        for cmd in cmds:
            if cmd[0] == 0:
                v = cmd[2]
                lid = self.add("purchase.order.line", order_id=po_id, name=v.get("name"), product_id=v["product_id"], product_qty=v["product_qty"],
                               price_unit=v.get("price_unit", 0.0), tax_ids=(v.get("tax_ids") or [[6, 0, []]])[0][2], qty_received=0.0, qty_invoiced=0.0,
                               discount=v.get("discount", 0.0), uom_id=1)
                po["order_line"].append(lid)
            elif cmd[0] == 1:
                v = dict(cmd[2])
                if "tax_ids" in v:
                    v["tax_ids"] = v["tax_ids"][0][2]
                self.touch("purchase.order.line", cmd[1], **v)
            elif cmd[0] == 2:
                po["order_line"].remove(cmd[1])
                del self.db["purchase.order.line"][cmd[1]]
        lines = [self.db["purchase.order.line"][i] for i in po["order_line"]]
        for ln in lines:
            ln["price_subtotal"] = ln["product_qty"] * ln["price_unit"]
        net = sum(ln["price_subtotal"] for ln in lines)
        po.update(amount_untaxed=net, amount_tax=round(net * 0.18, 2), amount_total=round(net * 1.18, 2), write_date=self.tick())

    def _picking(self, po_id):
        po = self.db["purchase.order"][po_id]
        pk = self.add("stock.picking", name=f"WH/IN/{po_id:05d}", state="assigned", picking_type_code="incoming", purchase_id=po_id,
                      partner_id=po["partner_id"], origin=po["name"], scheduled_date=po["write_date"], date_done=False, move_ids=[])
        for lid in po["order_line"]:
            ln = self.db["purchase.order.line"][lid]
            mv = self.add("stock.move", picking_id=pk, product_id=ln["product_id"], product_uom_qty=ln["product_qty"], quantity=0.0, state="assigned",
                          purchase_line_id=lid, date=po["write_date"])
            self.db["stock.picking"][pk]["move_ids"].append(mv)
            self.db["product.product"][ln["product_id"]]["incoming_qty"] += ln["product_qty"]
        po["picking_ids"].append(pk)
        po["receipt_status"] = "pending"

    def receive(self, po_name, qty):
        """Validate the PO's receipt for `qty` of each line, as Odoo Inventory does: received quantity and stock move;
        the PO's own write_date does not change."""
        po = next(p for p in self.db["purchase.order"].values() if p["name"] == po_name)
        pk = self.db["stock.picking"][po["picking_ids"][0]]
        for mv_id in pk["move_ids"]:
            mv = self.db["stock.move"][mv_id]
            self.touch("stock.move", mv_id, quantity=qty, state="done")
            self.touch("purchase.order.line", mv["purchase_line_id"], qty_received=qty)
            prod = self.db["product.product"][mv["product_id"]]
            prod["qty_available"] += qty
            prod["incoming_qty"] -= qty
        self.touch("stock.picking", pk["id"], state="done", date_done=self.clock.strftime("%Y-%m-%d %H:%M:%S"))
        po["receipt_status"] = "full" if all(self.db["purchase.order.line"][i]["qty_received"] >= self.db["purchase.order.line"][i]["product_qty"]
                                             for i in po["order_line"]) else "partial"

    def po(self, name):
        return next(p for p in self.db["purchase.order"].values() if p["name"] == name)


@pytest.fixture
def fake(monkeypatch):
    f = FakeOdoo()
    monkeypatch.setattr(odoo, "_call", f.call)
    for k, v in dict(ODOO_URL="https://odoo.test", ODOO_DB="test", ODOO_API_KEY="key", ODOO_ACCOUNTS="", ODOO_APPROVERS="").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(odoo_trail, "_tried", {})
    monkeypatch.setattr(odoo_trail, "sync_user_later", lambda uid: None)
    with db._lock:
        for table in ("erp_records", "erp_outbox", "erp_state"):
            db.conn().execute(f"DELETE FROM {table}")
        db.conn().commit()
    return f


def mirror(model):
    return {r["ref"]: json.loads(r["data_json"]) for r in db.erp_list(model)}


# ------------------------------------------------------------------ Odoo -> Invoice Flow --

def test_full_pull_mirrors_every_model_and_the_api_serves_it(fake):
    out = erp_sync.pull_all(full=True)
    assert all("error" not in v for v in out.values()), out
    assert set(mirror("res.partner")) == {"Sharma Office Supplies Pvt Ltd", "Rathore IT Services LLP"}
    po = mirror("purchase.order")["P00001"]
    assert po["state"] == "purchase" and po["lines"][0]["product_qty"] == 100 and po["receipt_status"] == "pending"
    assert mirror("stock.picking")[f"WH/IN/{fake.po('P00001')['id']:05d}"]["moves"][0]["product_uom_qty"] == 100
    assert mirror("product.product")["PAP-A4"]["incoming_qty"] == 100
    c = client("asha")
    pos = c.get("/api/erp/purchase-orders").json()["purchase_orders"]
    assert [(p["name"], p["state_label"], p["_odoo_url"].endswith(f"/{p['_odoo_id']}")) for p in pos] == [("P00001", "Confirmed", True)]
    o = c.get("/api/erp/overview").json()
    assert o["purchase_orders"]["confirmed"] == 1 and o["purchase_orders"]["not_received"] == 1
    assert (o["invoices"]["bills_posted"], o["invoices"]["bills_paid"], o["invoices"]["bills_unpaid"]) == (2, 1, 1)
    assert (o["money"]["invoice_value"], o["money"]["pending_payment"], o["money"]["paid"]) == (17700.0, 11800.0, 5900.0)
    assert o["money"]["po_value"] == 33040.0                     # 100 x 280 + 18%


def test_a_po_made_or_changed_in_odoo_and_received_reaches_the_app(fake):
    erp_sync.pull_all(full=True)
    v2 = next(i for i, r in fake.db["res.partner"].items() if r["name"].startswith("Rathore"))
    new = fake.call("purchase.order", "create", vals_list=[dict(partner_id=v2, order_line=[[0, 0, dict(product_id=4, name="Toner", product_qty=3, price_unit=1900)]])])[0]
    erp_sync.pull_all()                                          # incremental: by write_date
    assert mirror("purchase.order")["P00002"]["state"] == "draft"
    fake.call("purchase.order", "write", ids=[new], vals={"partner_ref": "RIT-77"})
    erp_sync.pull_all()
    assert mirror("purchase.order")["P00002"]["partner_ref"] == "RIT-77"
    fake.receive("P00001", 80)                                   # 80 of 100 received in Odoo Inventory
    erp_sync.pull_all()
    po = mirror("purchase.order")["P00001"]
    assert po["lines"][0]["qty_received"] == 80 and po["receipt_status"] == "partial"
    assert mirror("product.product")["PAP-A4"]["qty_available"] == 80 and mirror("stock.picking")[f"WH/IN/{fake.po('P00001')['id']:05d}"]["state"] == "done"
    fake.db["purchase.order"][new]["active"] = False              # gone from Odoo: noticed by the full reconcile
    erp_sync.pull_all(full=True)
    assert "P00002" not in mirror("purchase.order") and db.erp_get("purchase.order", new)["sync_status"] == "deleted"


def test_webhook_needs_the_token_and_fetches_the_record_from_odoo(fake):
    c = client("asha")
    assert c.post("/api/odoo/webhook?token=wrong", json={"_model": "purchase.order", "_id": 7}).status_code == 403
    po_id = fake.po("P00001")["id"]
    r = c.post(f"/api/odoo/webhook?token={odoo.webhook_token()}", json={"_model": "purchase.order", "_id": po_id, "state": "forged"})
    assert r.status_code == 200
    for _ in range(100):
        if db.erp_get("purchase.order", po_id):
            break
        time.sleep(0.02)
    assert json.loads(db.erp_get("purchase.order", po_id)["data_json"])["state"] == "purchase"   # Odoo's value, not the request's


# ------------------------------------------------------------------ Invoice Flow -> Odoo --

def new_po(c, **extra):
    vendor = next(v["_odoo_id"] for v in c.get("/api/erp/vendors").json()["vendors"] if v["name"].startswith("Sharma"))
    body = dict(vendor_id=vendor, partner_ref="SH-REQ-1", date_planned="2026-12-01", lines=[dict(product_id=3, qty=40, price=275, tax_ids=[7])], **extra)
    return c.post("/api/erp/purchase-orders", json=body)


def test_a_po_made_in_the_app_is_created_in_odoo_once_and_approved(fake):
    erp_sync.pull_all(full=True)
    c = client("asha")
    r = new_po(c, request_id="req-1")
    assert r.status_code == 200 and r.json()["queue"]["status"] == "done" and r.json()["ref"] == "P00002"
    po = fake.po("P00002")
    assert po["origin"] == f"Invoice Flow #{r.json()['id']}" and po["state"] == "draft" and po["partner_ref"] == "SH-REQ-1"
    assert any("Created in Invoice Flow by <b>asha</b>" in n[2] for n in fake.notes)
    assert new_po(c, request_id="req-1").json()["id"] == r.json()["id"]          # the same request twice: one PO
    assert sum(1 for p in fake.db["purchase.order"].values() if p.get("origin")) == 1
    a = c.post(f"/api/erp/purchase-orders/{r.json()['id']}/approve", json={})
    assert a.status_code == 200 and a.json()["state"] == "purchase" and po["state"] == "purchase" and po["picking_ids"]
    assert any("Approved in Invoice Flow by <b>asha</b>" in n[2] for n in fake.notes)
    assert c.post(f"/api/erp/purchase-orders/{r.json()['id']}/approve", json={}).status_code == 400   # already confirmed


def test_two_step_approval_stays_with_odoo(fake):
    fake.two_step = 5000                                          # Odoo: orders from Rs 5,000 need a purchase manager
    erp_sync.pull_all(full=True)
    c = client("asha")
    r = new_po(c, approve=True)
    detail = c.get(f"/api/erp/purchase-orders/{r.json()['id']}").json()
    assert detail["state"] == "to approve" and not detail.get("date_approve")   # never pushed past Odoo's own rule


def test_editing_a_po_and_the_conflict_when_odoo_changed_it_meanwhile(fake):
    erp_sync.pull_all(full=True)
    c = client("asha")
    rid = db.erp_get("purchase.order", fake.po("P00001")["id"])["id"]
    d = c.get(f"/api/erp/purchase-orders/{rid}").json()
    line = d["lines"][0]["id"]
    ok = c.patch(f"/api/erp/purchase-orders/{rid}", json=dict(base_version=d["_version"], lines=[dict(id=line, qty=120)], partner_ref="NEW-REF"))
    assert ok.status_code == 200 and ok.json()["queue"]["status"] == "done"
    assert fake.db["purchase.order.line"][line]["product_qty"] == 120 and fake.po("P00001")["partner_ref"] == "NEW-REF"
    stale = d["_version"]                                          # someone edits it in Odoo; the app's next edit is based on the old version
    fake.call("purchase.order", "write", ids=[fake.po("P00001")["id"]], vals={"partner_ref": "CHANGED-IN-ODOO"})
    bad = c.patch(f"/api/erp/purchase-orders/{rid}", json=dict(base_version=stale, lines=[dict(id=line, qty=1)]))
    assert bad.json()["queue"]["status"] == "conflict" and fake.db["purchase.order.line"][line]["product_qty"] == 120
    assert db.erp_get(record_id=rid)["sync_status"] == "conflict"
    assert json.loads(db.erp_get(record_id=rid)["data_json"])["partner_ref"] == "CHANGED-IN-ODOO"   # the mirror shows Odoo's version
    assert c.patch(f"/api/erp/purchase-orders/{rid}", json=dict(lines=[dict(id=line, qty=1)])).status_code == 400   # base_version required


def test_odoo_down_queues_the_write_and_a_lost_answer_never_duplicates(fake):
    erp_sync.pull_all(full=True)
    c = client("asha")
    fake.down = True
    r = new_po(c)
    assert r.status_code == 200 and r.json()["queue"]["status"] == "pending" and "queued" in r.json()["message"]
    assert db.erp_get(record_id=r.json()["id"])["sync_status"] == "pending"
    assert any(p["_status"] == "pending" for p in c.get("/api/erp/purchase-orders").json()["purchase_orders"])   # shown while it waits
    fake.down, fake.timeout_after_write = False, True               # Odoo is back, but the first answer gets lost after the write
    db.outbox_finish(r.json()["queue"]["id"], "pending", next_try_at=0)
    erp_sync.run_due()                                               # the create happens, the reply is lost: still pending
    db.outbox_finish(r.json()["queue"]["id"], "pending", next_try_at=0)
    erp_sync.run_due()                                               # the retry finds it by its origin instead of making another
    assert sum(1 for p in fake.db["purchase.order"].values() if p.get("origin")) == 1
    assert db.outbox_get(r.json()["queue"]["id"])["status"] == "done" and db.erp_get(record_id=r.json()["id"])["odoo_id"]


def test_a_refused_write_is_retried_then_waits_for_a_person(fake, monkeypatch):
    erp_sync.pull_all(full=True)
    c = client("asha")
    real = fake.call

    def refuse(model, method, **p):
        if method == "create" and model == "purchase.order":
            raise odoo.OdooError("Odoo rejected purchase.order.create (HTTP 422): vendor is archived")
        return real(model, method, **p)
    monkeypatch.setattr(odoo, "_call", refuse)
    r = new_po(c)
    op = r.json()["queue"]["id"]
    while db.outbox_get(op)["status"] == "pending":         # the backoff, skipped: tried three times in all
        db.outbox_finish(op, "pending", error=db.outbox_get(op)["last_error"], next_try_at=0)
        erp_sync.run_due()
    assert db.outbox_get(op)["status"] == "error" and "archived" in db.outbox_get(op)["last_error"]
    log = c.get("/api/erp/sync").json()
    assert log["entries"][0]["status"] == "error" and log["entries"][0]["by"] == "asha" and log["queue"]["failed"] == 1   # kept, with who asked
    monkeypatch.setattr(odoo, "_call", real)
    assert c.post(f"/api/erp/sync/{op}/retry").json()["status"] == "done"


def test_vendors_and_products_are_created_once(fake):
    erp_sync.pull_all(full=True)
    c = client("asha")
    v = c.post("/api/erp/vendors", json=dict(name="Kaveri Engineering Works", vat="33AABCK1234L1Z5", email="ap@kaveri.test", city="Chennai"))
    assert v.status_code == 200 and v.json()["queue"]["status"] == "done"
    assert any(r["name"] == "Kaveri Engineering Works" and r["supplier_rank"] == 1 for r in fake.db["res.partner"].values())
    again = c.post("/api/erp/vendors", json=dict(name="kaveri engineering works"))
    assert again.status_code == 409                                   # already a vendor: never a second one
    assert c.post("/api/erp/vendors", json=dict(name="X", vat="NOTAGSTIN")).status_code == 400
    p = c.post("/api/erp/products", json=dict(name="Stapler heavy-duty", default_code="STP-HD", standard_price=450, tax_ids=[7]))
    assert p.status_code == 200 and any(r.get("default_code") == "STP-HD" and r["is_storable"] for r in fake.db["product.product"].values())
    assert c.post("/api/erp/products", json=dict(name="Other", default_code="STP-HD")).status_code == 409


def test_only_approvers_change_purchasing(fake, monkeypatch):
    erp_sync.pull_all(full=True)
    monkeypatch.setenv("ODOO_APPROVERS", "boss")
    c = client("asha")
    assert c.get("/api/erp/purchase-orders").status_code == 200       # anyone connected can look
    assert new_po(c).status_code == 403 and c.post("/api/erp/vendors", json=dict(name="Y Co")).status_code == 403
    assert c.get("/api/erp/status").json()["approver"] is False


def test_accounts_not_connected_get_nothing(fake, monkeypatch):
    monkeypatch.setenv("ODOO_ACCOUNTS", "someone-else")
    assert client("asha").get("/api/erp/overview").status_code == 404


# ------------------------------------------------------------------ invoices --

def test_a_bill_is_never_made_twice_for_the_same_invoice(fake):
    first = odoo.create_draft_bill(1, "INV-9", "2026-11-24", [dict(name="Paper", quantity=1, price_unit=100)])
    again = odoo.create_draft_bill(1, "INV-9", "2026-11-24", [dict(name="Paper", quantity=1, price_unit=100)])
    assert first == again and sum(1 for b in fake.db["account.move"].values() if b.get("ref") == "INV-9") == 1
    refund = odoo.create_draft_bill(1, "CN-1", "2026-11-24", [dict(name="Return", quantity=1, price_unit=50, discount=10)], move_type="in_refund",
                                    due_date="2026-12-24")
    assert fake.db["account.move"][refund]["move_type"] == "in_refund" and fake.db["account.move"][refund]["invoice_date_due"] == "2026-12-24"


def test_an_invoice_held_because_odoo_was_down_is_checked_again_when_it_answers(monkeypatch):
    """With the function-level stand-in of test_odoo: Odoo down holds the invoice and queues a re-check; once Odoo
    answers, the queue re-runs the checks and the invoice is approved, with its bill."""
    import importlib.util   # tests/test_odoo.py by its path: a test_odoo.py at the repo root is the guide's live Step 1 script
    spec = importlib.util.spec_from_file_location("tests_test_odoo", Path(__file__).with_name("test_odoo.py"))
    t_odoo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t_odoo)
    Funcs, ODOO_CASES, run, wait = t_odoo.FakeOdoo, t_odoo.ODOO_CASES, t_odoo.run, t_odoo.wait
    f = Funcs()
    f.contacts, f.attachments, f.notes, f.refuse_notes = [], [], [], False
    for name in ("vendors", "purchase_orders", "bill_exists", "create_draft_bill", "po_by_name", "upsert_contact", "attach", "note", "bill_states"):
        monkeypatch.setattr(odoo, name, getattr(f, name))
    monkeypatch.setattr(odoo, "_call", lambda *a, **k: (_ for _ in ()).throw(odoo.OdooUnreachable("down")) if f.down else 0)
    for k, v in dict(ODOO_URL="https://odoo.test", ODOO_DB="test", ODOO_API_KEY="key", ODOO_ACCOUNTS="").items():
        monkeypatch.setenv(k, v)
    with db._lock:
        db.conn().execute("DELETE FROM erp_outbox")
        db.conn().commit()
    c = client("asha")
    f.down = True
    r = run(c, ODOO_CASES[0])
    assert r["decision"] == "HOLD"
    (op,) = [q for q in db.outbox_list() if q["op"] == "run_recheck"]
    erp_sync.run_due()                                                # still down: stays queued, backs off
    assert db.outbox_get(op["id"])["status"] == "pending" and db.outbox_get(op["id"])["attempts"] == 0 or db.outbox_get(op["id"])["status"] == "pending"
    f.down = False
    db.outbox_finish(op["id"], "pending", next_try_at=0)
    erp_sync.run_due()
    wait(r["id"])
    again = c.get(f"/api/runs/{r['id']}").json()
    assert again["decision"] == "APPROVE", [x["message"] for x in again["result"]["findings"] if x["severity"] != "info"]
    assert db.outbox_get(op["id"])["status"] == "done" and again["decision"] == "APPROVE" and again["result"]["erp"]["bill_id"] == 1
