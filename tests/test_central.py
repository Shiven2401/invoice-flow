"""The platform view: several independent accounts, the administrator's view of all of them in Invoice Flow (access
decided by Invoice Flow, audited), and the central copy in a stand-in Odoo (owner and folder kept, sent once, moves and
deletions followed, Odoo down or refusing a record, an answer lost after Odoo made the record)."""
import time

import pytest

from app import admin_api, central_sync, db, main, odoo
from test_accounts import clean, client, run_sample  # noqa: F401  (clean is an autouse fixture)

SAMPLES = ["01_happy_path_clean_exact.pdf", "11_unapproved_vendor.pdf"]


class FakeCentralOdoo:
    """Odoo's custom models as the sync uses them: search_read by x_key, create, write (archiving included)."""

    def __init__(self):
        self.recs, self.next_id, self.calls = {}, 1, []
        self.down = False
        self.refuse = None          # a function(model, vals) -> True to refuse that write
        self.lose_answer = False    # the next create happens in Odoo but its answer never arrives

    def __call__(self, model, method, **kw):
        if self.down:
            raise odoo.OdooUnreachable("Odoo didn't answer (ConnectTimeout)")
        self.calls.append((model, method))
        table = self.recs.setdefault(model, {})
        if method == "search_read":
            (field, op, value), = kw["domain"]
            hits = [dict(r, id=i) for i, r in table.items() if (r.get(field) in value if op == "in" else r.get(field) == value)]
            return hits[: kw.get("limit") or None]
        if method == "create":
            vals = kw["vals_list"][0]
            if self.refuse and self.refuse(model, vals):
                raise odoo.OdooError(f"Odoo rejected {model}.create (HTTP 422): bad value")
            rid, self.next_id = self.next_id, self.next_id + 1
            table[rid] = dict(vals)
            if self.lose_answer:
                self.lose_answer = False
                raise odoo.OdooUnreachable("Odoo didn't answer (ReadTimeout)")
            return [rid]
        if method == "write":
            vals = kw["vals"]
            if self.refuse and self.refuse(model, vals):
                raise odoo.OdooError(f"Odoo rejected {model}.write (HTTP 422): bad value")
            for rid in kw["ids"]:
                assert rid in table, f"{model} {rid} doesn't exist"
                table[rid].update(vals)
            return True
        raise AssertionError(f"unexpected call {model}.{method}")

    def all(self, model, active=None):
        return [dict(r, id=i) for i, r in self.recs.get(model, {}).items() if active is None or r.get("x_active") == active]

    def writes(self):
        return [c for c in self.calls if c[1] in ("create", "write") and c[0] != "x_if_platform"]


@pytest.fixture
def fake(monkeypatch):
    with db._lock:
        for table in ("central_sync", "activity", "admin_audit", "purchase_orders"):
            db.conn().execute(f"DELETE FROM {table}")
        db.conn().commit()
    f = FakeCentralOdoo()
    monkeypatch.setattr(odoo, "_call", f)
    monkeypatch.setattr(odoo, "enabled", lambda: True)
    monkeypatch.setenv("ODOO_ACCOUNTS", "nobody-is-connected")   # no account checks against Odoo: only the platform copy
    monkeypatch.setenv("CENTRAL_SYNC", "1")
    monkeypatch.setenv("CENTRAL_SITE", "test")
    monkeypatch.setenv("ADMIN_ACCOUNTS", "Shiven_24")
    monkeypatch.setattr(central_sync, "WRITE_PAUSE", 0)
    central_sync._state.update(backoff_until=0.0, backoff=central_sync.BACKOFF_START, failures=[])
    return f


def three_users():
    """Users A, B and C, each with a folder and their own invoices filed in it; plus the administrator."""
    out = {}
    for name in ("user_a", "user_b", "user_c"):
        c = client(name)
        folder = c.post("/api/folders", json=dict(name=f"{name} bills")).json()["id"]
        rids = [run_sample(c, s) for s in SAMPLES]
        for rid in rids:
            if (c.get(f"/api/runs/{rid}").json()["final_decision"] or c.get(f"/api/runs/{rid}").json()["decision"]) != "REJECT":
                assert c.post(f"/api/runs/{rid}/folder", json=dict(folder_id=folder)).status_code == 200
        out[name] = dict(client=c, folder=folder, runs=rids)
    out["admin"] = dict(client=client("Shiven_24"))
    return out


def test_every_account_s_invoices_reach_odoo_with_owner_and_folder(fake):
    u = three_users()
    r = central_sync.run_round()
    assert r["failed"] == 0 and not r.get("error"), r
    users = {x["x_name"]: x for x in fake.all("x_if_user")}
    assert set(users) == {"user_a", "user_b", "user_c", "Shiven_24"}
    assert users["Shiven_24"]["x_role"] == "Platform admin" and users["user_a"]["x_role"] == "User"
    invoices = fake.all("x_if_invoice")
    assert len(invoices) == 6
    folders = {f["id"]: f for f in fake.all("x_if_folder")}
    for name in ("user_a", "user_b", "user_c"):
        mine = [i for i in invoices if i["x_user_id"] == users[name]["id"]]
        assert len(mine) == 2 and all(i["x_owner_name"] == name for i in mine)
        assert {i["x_run_id"] for i in mine} == set(u[name]["runs"])
        filed = [i for i in mine if i["x_folder_id"]]
        assert filed and all(folders[i["x_folder_id"]]["x_name"] == f"{name} bills" and folders[i["x_folder_id"]]["x_user_id"] == users[name]["id"] for i in filed)
    one = invoices[0]
    assert one["x_document"] and one["x_filename"] and one["x_approval"] in ("Approved", "On hold", "Rejected")
    assert one["x_processing_status"] == "Processed" and one["x_extracted"] and one["x_key"].startswith("test:invoice:")
    assert {i["x_approval"] for i in invoices} >= {"Approved", "On hold"}
    assert fake.all("x_if_activity"), "the activity log goes too"
    assert fake.all("x_if_vendor") and fake.all("x_if_po"), "the shared master list's vendors and POs, with no owner"
    platform = fake.all("x_if_platform")
    assert len(platform) == 1 and platform[0]["x_sync_ok"] > 0 and platform[0]["x_sync_failed"] == 0


def test_nothing_is_sent_twice_and_a_folder_move_is_followed(fake):
    u = three_users()
    central_sync.run_round()
    n = len(fake.writes())
    central_sync.run_round()
    assert len(fake.writes()) == n, "an unchanged platform sends nothing"
    a = u["user_a"]
    other = a["client"].post("/api/folders", json=dict(name="Archive 2026")).json()["id"]
    rid = next(r for r in a["runs"] if a["client"].get(f"/api/runs/{r}").json()["folder_id"])
    a["client"].post(f"/api/runs/{rid}/folder", json=dict(folder_id=other))
    central_sync.run_round()
    inv = next(i for i in fake.all("x_if_invoice") if i["x_run_id"] == rid)
    folder = next(f for f in fake.all("x_if_folder") if f["id"] == inv["x_folder_id"])
    assert folder["x_name"] == "Archive 2026" and inv["x_owner_name"] == "user_a"
    assert len(fake.all("x_if_invoice")) == 6, "moved, not copied"


def test_an_invoice_deleted_in_the_app_is_archived_in_odoo_not_deleted(fake):
    u = three_users()
    central_sync.run_round()
    rid = u["user_b"]["runs"][0]
    assert u["user_b"]["client"].delete(f"/api/runs/{rid}").status_code == 200
    central_sync.run_round()
    inv = next(i for i in fake.all("x_if_invoice") if i["x_run_id"] == rid)
    assert inv["x_active"] is False and inv["x_deleted_in_app"] is True
    assert ("x_if_invoice", "unlink") not in fake.calls
    assert len(fake.all("x_if_invoice", active=True)) == 5


def test_odoo_down_loses_nothing_and_is_retried(fake):
    three_users()
    fake.down = True
    r = central_sync.run_round()
    assert r.get("error") and central_sync._state["backoff_until"] > time.time()
    assert central_sync.run_round()["skipped"].startswith("waiting")
    fake.down = False
    r = central_sync.run_round(force=True)
    assert not r.get("error") and len(fake.all("x_if_invoice")) == 6


def test_a_lost_answer_never_makes_a_second_record(fake):
    three_users()
    fake.lose_answer = True          # the first create happens in Odoo, but the app never hears back
    assert central_sync.run_round().get("error")
    central_sync.run_round(force=True)
    keys = [x["x_key"] for m in central_sync.MODEL.values() for x in fake.all(m)]
    assert len(keys) == len(set(keys)), "one Odoo record per Invoice Flow record"
    assert len(fake.all("x_if_user")) == 4


def test_a_refused_record_waits_alone_and_the_rest_go(fake):
    u = three_users()
    bad = u["user_c"]["runs"][0]
    fake.refuse = lambda model, vals: model == "x_if_invoice" and vals.get("x_run_id") == bad
    r = central_sync.run_round()
    assert r["failed"] == 1 and len(fake.all("x_if_invoice")) == 5
    s = central_sync.summary()
    assert s["failed"] == 1 and s["errors"][0]["id"] == bad
    fake.refuse = None
    central_sync.run_round()                                  # not due yet: backoff per record
    assert len(fake.all("x_if_invoice")) == 5
    with db._lock:
        db.conn().execute("UPDATE central_sync SET next_try=0 WHERE local_id=?", (bad,))
        db.conn().commit()
    central_sync.run_round()
    assert len(fake.all("x_if_invoice")) == 6 and central_sync.summary()["failed"] == 0


def test_a_bill_deleted_in_odoo_does_not_block_the_invoice(fake):
    u = three_users()
    rid = u["user_a"]["runs"][0]
    db.update_run(rid, odoo_bill_id=999, odoo_status="draft")
    fake.refuse = lambda model, vals: model == "x_if_invoice" and vals.get("x_bill_id") == 999
    r = central_sync.run_round()
    assert r["failed"] == 0
    inv = next(i for i in fake.all("x_if_invoice") if i["x_run_id"] == rid)
    assert inv["x_bill_id"] is False


def test_only_the_platform_administrator_sees_everyone(fake):
    u = three_users()
    a = u["user_a"]["client"]
    for path in ("/api/admin/overview", "/api/admin/users", "/api/admin/invoices", "/api/admin/folders", "/api/admin/pos",
                 "/api/admin/vendors", "/api/admin/activity", "/api/admin/activity.csv", "/api/admin/sync", "/api/admin/audit",
                 f"/api/admin/invoices/{u['user_b']['runs'][0]}", f"/api/admin/invoices/{u['user_b']['runs'][0]}/file"):
        assert a.get(path).status_code == 403, path
    assert a.post("/api/admin/sync").status_code == 403
    # an ordinary account still sees only its own invoices, and can't open another's
    assert {r["id"] for r in a.get("/api/runs").json()["runs"]} == set(u["user_a"]["runs"])
    assert a.get(f"/api/runs/{u['user_b']['runs'][0]}").status_code == 404
    assert a.get("/api/config").json()["admin"] is False

    admin = u["admin"]["client"]
    assert admin.get("/api/config").json()["admin"] is True
    inv = admin.get("/api/admin/invoices").json()
    assert inv["total"] == 6 and {r["username"] for r in inv["invoices"]} == {"user_a", "user_b", "user_c"}
    uid_b = next(x["id"] for x in admin.get("/api/admin/users").json()["users"] if x["username"] == "user_b")
    only_b = admin.get(f"/api/admin/invoices?user_id={uid_b}").json()["invoices"]
    assert len(only_b) == 2 and {r["id"] for r in only_b} == set(u["user_b"]["runs"])
    assert all(r["folder"] == "user_b bills" for r in admin.get(f"/api/admin/invoices?folder={u['user_b']['folder']}").json()["invoices"])
    assert admin.get("/api/admin/invoices?approval=Approved").json()["invoices"]
    tree = {t["username"]: t for t in admin.get("/api/admin/folders").json()["users"]}
    assert tree["user_c"]["folders"][0]["name"] == "user_c bills" and tree["user_c"]["invoices"] == 2
    users = {x["username"]: x for x in admin.get("/api/admin/users").json()["users"]}
    assert users["user_a"]["invoices"] == 2 and users["user_a"]["folders"] == 1 and users["Shiven_24"]["role"] == "Platform admin"
    o = admin.get("/api/admin/overview").json()
    assert o["users"]["total"] == 4 and o["invoices"]["total"] == 6 and o["folders"] == 3
    # the administrator opening someone's invoice and document is audited
    rid = u["user_b"]["runs"][0]
    assert admin.get(f"/api/admin/invoices/{rid}").json()["username"] == "user_b"
    assert admin.get(f"/api/admin/invoices/{rid}/file").status_code == 200
    log = admin.get("/api/admin/audit").json()["rows"]
    assert {(x["admin"], x["action"], x["owner"]) for x in log} >= {("Shiven_24", "opened invoice", "user_b"), ("Shiven_24", "opened document", "user_b")}
    assert "user_c" in admin.get("/api/admin/activity.csv").text
    # the administrator's own invoice list is still their own (none): the platform view is separate
    assert admin.get("/api/runs").json()["runs"] == []


def test_admin_accounts_come_from_the_setting_not_the_code(fake, monkeypatch):
    three_users()
    monkeypatch.setenv("ADMIN_ACCOUNTS", "user_c")
    c = client()
    c.post("/api/login", json=dict(username="user_c", password="correct horse"))
    assert c.get("/api/admin/invoices").json()["total"] == 6
    s = client()
    s.post("/api/login", json=dict(username="Shiven_24", password="correct horse"))
    assert s.get("/api/admin/invoices").status_code == 403


def test_central_sync_is_off_unless_switched_on(fake, monkeypatch):
    monkeypatch.setenv("CENTRAL_SYNC", "0")
    assert "off" in central_sync.run_round()["skipped"]
    assert fake.calls == []
