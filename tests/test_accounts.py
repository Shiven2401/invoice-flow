"""Logins, and proof that one account can't see or touch another's data. Real HTTP through the real app."""
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth, config, db, main, pipeline

SAMPLE = "01_happy_path_clean_exact.pdf"
UNKNOWN_VENDOR = "11_unapproved_vendor.pdf"
PUBLIC = {"/", "/api/login", "/api/signup", "/api/logout", "/api/me",  # /api/me does its own check
          "/api/auth/google", "/api/auth/google/callback"}               # signing in with Google happens before there's a session


@pytest.fixture(autouse=True)
def clean():
    auth._attempts.clear()
    with db._lock:
        for table in ("users", "sessions", "runs", "ledger", "vendors", "vendor_no_po", "vendor_aliases", "vendor_po_refs", "folders"):
            db.conn().execute(f"DELETE FROM {table}")
        db.conn().commit()


def client(name=None):
    c = TestClient(main.app)
    if name:
        assert c.post("/api/signup", json=dict(username=name, password="correct horse")).status_code == 200
    return c


def run_sample(c, name):
    rid = c.post("/api/runs/sample", json=dict(files=[name])).json()["runs"][0]["id"]
    for _ in range(600):
        if main.BUS[rid]["done"]:
            return rid
        time.sleep(0.02)
    raise TimeoutError(rid)


# ------------------------------------------------------------------- logging in --

def test_every_api_route_requires_a_login():
    routes = [r for r in main.app.routes if isinstance(r, APIRoute) and r.path not in PUBLIC]
    assert len(routes) > 15
    open_routes = [r.path for r in routes if auth.current_user not in [d.call for d in r.dependant.dependencies]]
    assert not open_routes


@pytest.mark.parametrize("method, path", [("get", "/api/runs"), ("get", "/api/stats"), ("get", "/api/pos"), ("get", "/api/export.xlsx"), ("get", "/api/me"),
                                          ("get", "/api/runs/abc/file"), ("post", "/api/reset")])
def test_logged_out_requests_are_refused(method, path):
    assert getattr(client(), method)(path).status_code == 401


def test_sign_in_page_itself_is_public():
    c = client()
    assert c.get("/").status_code == 200 and c.get("/static/app.js").status_code == 200


def test_login_logout_cycle():
    c = client("asha")
    assert c.get("/api/me").json() == dict(username="asha")
    c.post("/api/logout")
    assert c.get("/api/me").status_code == 401
    fresh = client()
    assert fresh.post("/api/login", json=dict(username="ASHA", password="correct horse")).status_code == 200  # names ignore case
    assert fresh.get("/api/me").status_code == 200


def test_logging_out_kills_the_session_on_the_server_too():
    c = client("asha")
    token = c.cookies[auth.COOKIE]
    c.post("/api/logout")
    thief = TestClient(main.app, cookies={auth.COOKIE: token})
    assert thief.get("/api/me").status_code == 401


def test_expired_session_is_refused():
    c = client("asha")
    with db._lock:
        db.conn().execute("UPDATE sessions SET expires_at=1")
        db.conn().commit()
    assert c.get("/api/me").status_code == 401


def test_wrong_password_and_unknown_user_look_the_same():
    client("asha")
    a = client().post("/api/login", json=dict(username="asha", password="wrong password"))
    b = client().post("/api/login", json=dict(username="nobody", password="wrong password"))
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_repeated_failures_lock_that_username_for_a_while():
    client("asha")
    for _ in range(5):
        client().post("/api/login", json=dict(username="asha", password="nope nope"))
    r = client().post("/api/login", json=dict(username="asha", password="correct horse"))
    assert r.status_code == 429


def test_names_and_passwords_are_validated():
    assert client().post("/api/signup", json=dict(username="ab", password="long enough")).status_code == 400
    assert client().post("/api/signup", json=dict(username="<script>", password="long enough")).status_code == 400
    assert client().post("/api/signup", json=dict(username="asha", password="short")).status_code == 400
    client("asha")
    assert client().post("/api/signup", json=dict(username="ASHA", password="long enough")).status_code == 409


def test_signup_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_SIGNUP", False)
    assert client().post("/api/signup", json=dict(username="asha", password="long enough")).status_code == 403


def test_nothing_secret_is_stored_in_the_clear():
    c = client("asha")
    with db._lock:
        stored = db.conn().execute("SELECT pw_hash FROM users").fetchone()[0]
        session = db.conn().execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert "correct horse" not in stored and stored.startswith("scrypt$")
    assert session != c.cookies[auth.COOKIE]


def test_session_cookie_is_locked_down(monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    r = TestClient(main.app, base_url="https://testserver").post("/api/signup", json=dict(username="asha", password="correct horse"))
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie


# ------------------------------------------------------------------- separation --

def test_each_login_has_its_own_dashboard():
    a, b = client("asha"), client("ben")
    run_sample(a, SAMPLE)
    assert a.get("/api/stats").json()["total"] == 1
    assert b.get("/api/stats").json()["total"] == 0
    assert b.get("/api/runs").json()["runs"] == []


@pytest.mark.parametrize("method, path, body", [
    ("get", "/api/runs/{id}", None), ("get", "/api/runs/{id}/file", None), ("get", "/api/runs/{id}/preview", None), ("get", "/api/runs/{id}/stream", None),
    ("post", "/api/runs/{id}/override", dict(decision="REJECT")), ("post", "/api/runs/{id}/approve-vendor", {}), ("post", "/api/runs/{id}/treat-as-invoice", None),
])
def test_another_accounts_run_is_invisible(method, path, body):
    a, b = client("asha"), client("ben")
    rid = run_sample(a, UNKNOWN_VENDOR)
    kwargs = dict(json=body) if body is not None else {}
    assert getattr(b, method)(path.format(id=rid), **kwargs).status_code == 404
    assert a.get(f"/api/runs/{rid}").json()["final_decision"] is None


def test_duplicate_check_stays_within_an_account():
    a, b = client("asha"), client("ben")
    run_sample(a, SAMPLE)
    rid = run_sample(b, SAMPLE)  # the same file, but it's the first time ben has sent it
    assert b.get(f"/api/runs/{rid}").json()["decision"] == "APPROVE"
    again = run_sample(a, SAMPLE)
    assert a.get(f"/api/runs/{again}").json()["decision"] == "REJECT"


def test_po_billing_stays_within_an_account():
    a, b = client("asha"), client("ben")
    run_sample(a, SAMPLE)
    billed = lambda c: next(p for p in c.get("/api/pos").json()["pos"] if p["po_number"] == "PO-2026-1001")["billed"]
    assert billed(a) == 100000.0 and billed(b) == 0
    assert a.get("/api/pos").json()["pos"][0]["invoices"] and not b.get("/api/pos").json()["pos"][0]["invoices"]


def test_approving_a_vendor_only_affects_that_account():
    a, b = client("asha"), client("ben")
    ra = run_sample(a, UNKNOWN_VENDOR)
    vendor_id = a.post(f"/api/runs/{ra}/approve-vendor", json={}).json()["vendor_id"]
    for _ in range(300):
        if main.BUS[ra]["done"]:
            break
        time.sleep(0.02)
    ids = lambda c: {v["vendor_id"] for v in c.get("/api/pos").json()["vendors"]}
    assert vendor_id in ids(a) and vendor_id not in ids(b)
    rb = run_sample(b, UNKNOWN_VENDOR)
    assert "VENDOR_UNKNOWN" in {x["code"] for x in b.get(f"/api/runs/{rb}").json()["result"]["findings"]}
    assert b.delete(f"/api/vendors/{vendor_id}").status_code == 404  # same id can't reach asha's approval
    assert vendor_id in ids(a)


def test_two_accounts_can_each_approve_a_vendor_with_the_same_new_id():
    a, b = client("asha"), client("ben")
    ids = []
    for c in (a, b):
        rid = run_sample(c, UNKNOWN_VENDOR)
        ids.append(c.post(f"/api/runs/{rid}/approve-vendor", json={}).json()["vendor_id"])
        for _ in range(300):
            if main.BUS[rid]["done"]:
                break
            time.sleep(0.02)
    assert ids == ["V006", "V006"]


def test_reset_only_clears_your_own_account():
    a, b = client("asha"), client("ben")
    ra, rb = run_sample(a, SAMPLE), run_sample(b, SAMPLE)
    path_a, path_b = (Path(db.get_run(ra, db.run_owner(ra))["file_path"]), Path(db.get_run(rb, db.run_owner(rb))["file_path"]))
    assert b.post("/api/reset").status_code == 200
    assert b.get("/api/stats").json()["total"] == 0 and a.get("/api/stats").json()["total"] == 1
    assert path_a.exists() and not path_b.exists()


def test_excel_report_only_has_your_invoices():
    import zipfile
    from io import BytesIO
    a, b = client("asha"), client("ben")
    run_sample(a, SAMPLE)
    z = zipfile.ZipFile(BytesIO(b.get("/api/export.xlsx").content))
    assert SAMPLE not in z.read("xl/sharedStrings.xml").decode()


# ------------------------------------------------------------- review before checks --

def upload(c, name=SAMPLE):
    rid = c.post("/api/runs", files=dict(files=(name, (ROOT / "samples/synthetic" / name).read_bytes()))).json()["runs"][0]["id"]
    wait(rid)
    return rid


def wait(rid):
    for _ in range(600):
        if main.BUS[rid]["done"]:
            return
        time.sleep(0.02)
    raise TimeoutError(rid)


def test_an_upload_stops_after_extraction_for_review():
    c = client("asha")
    rid = upload(c)
    run = c.get(f"/api/runs/{rid}").json()
    assert run["status"] == "review" and run["decision"] is None and run["result"] is None
    assert [e["stage"] for e in run["events"] if e["status"] != "running"] == ["ingest", "read", "extract"]
    assert c.get("/api/stats").json()["total"] == 0  # not on the Dashboard until it's decided


def test_continuing_runs_the_checks_without_reading_again(monkeypatch):
    from app import extract
    c = client("asha")
    rid = upload(c)
    monkeypatch.setattr(extract, "read_document", lambda *a: (_ for _ in ()).throw(AssertionError("read twice")))
    assert c.post(f"/api/runs/{rid}/continue", json=dict(inputs=dict(note="checked against the PO"))).status_code == 200
    wait(rid)
    run = c.get(f"/api/runs/{rid}").json()
    assert run["status"] == "done" and run["decision"] == "APPROVE" and run["inputs"]["note"] == "checked against the PO"
    seqs = [e["seq"] for e in run["events"]]
    assert seqs == sorted(seqs) and run["events"][0]["stage"] == "ingest" and run["events"][-1]["stage"] == "decision"


def test_details_added_at_review_count_like_typed_values():
    c = client("asha")
    rid = upload(c)
    c.post(f"/api/runs/{rid}/continue", json=dict(inputs=dict(total="1.00")))  # contradicts the document
    wait(rid)
    run = c.get(f"/api/runs/{rid}").json()
    assert run["decision"] == "HOLD" and "USER_INPUT_CONFLICT" in {x["code"] for x in run["result"]["findings"]}


def test_review_input_is_validated_and_the_run_stays_waiting():
    c = client("asha")
    rid = upload(c)
    r = c.post(f"/api/runs/{rid}/continue", json=dict(inputs=dict(invoice_date="someday")))
    assert r.status_code == 400 and c.get(f"/api/runs/{rid}").json()["status"] == "review"


def test_continue_only_once_and_only_your_own():
    a, b = client("asha"), client("ben")
    rid = upload(a)
    assert b.post(f"/api/runs/{rid}/continue", json=dict(inputs={})).status_code == 404
    assert a.post(f"/api/runs/{rid}/continue", json=dict(inputs={})).status_code == 200
    wait(rid)
    assert a.post(f"/api/runs/{rid}/continue", json=dict(inputs={})).status_code == 409


def test_review_survives_a_restart():
    c = client("asha")
    rid = upload(c)
    main.BUS.pop(rid)
    assert db.fail_interrupted() == 0  # waiting for a person is not an interrupted run
    assert c.post(f"/api/runs/{rid}/continue", json=dict(inputs={})).status_code == 200
    wait(rid)
    assert c.get(f"/api/runs/{rid}").json()["decision"] == "APPROVE"


def test_review_knows_whether_the_vendor_is_approved():
    c = client("asha")
    status = lambda name: next(e for e in c.get(f"/api/runs/{upload(c, name)}").json()["events"] if e["stage"] == "extract" and e["status"] != "running")["detail"]["vendor_check"]
    assert status(SAMPLE) == dict(status="approved", name="Sharma Office Supplies Pvt Ltd")
    assert status(UNKNOWN_VENDOR)["status"] == "unknown"


def test_a_new_vendor_can_be_approved_at_review():
    c = client("asha")
    rid = upload(c, UNKNOWN_VENDOR)
    assert c.post(f"/api/runs/{rid}/continue", json=dict(inputs={}, approve_vendor=dict(note="called them"))).status_code == 200
    wait(rid)
    run = c.get(f"/api/runs/{rid}").json()
    assert "VENDOR_UNKNOWN" not in {x["code"] for x in run["result"]["findings"]} and run["vendor_id"] == "V006"
    added = next(v for v in c.get("/api/pos").json()["vendors"] if v["vendor_id"] == "V006")
    assert added["note"] == "called them" and added["run_id"] == rid


def test_approval_at_review_cannot_get_round_the_vendor_list():
    c = client("asha")
    rid = upload(c, UNKNOWN_VENDOR)
    r = c.post(f"/api/runs/{rid}/continue", json=dict(inputs=dict(vendor_name="Kiran Courier Services"), approve_vendor={}))
    assert r.status_code == 400 and "Kiran" in r.json()["detail"]
    assert c.get(f"/api/runs/{rid}").json()["status"] == "review"  # nothing ran, nothing was added
    assert not [v for v in c.get("/api/pos").json()["vendors"] if v["source"] == "approved in app"]
    assert c.post(f"/api/runs/{rid}/continue", json=dict(inputs={}, approve_vendor="yes")).status_code == 400


def test_cancelling_at_review_discards_the_document():
    c = client("asha")
    rid = upload(c)
    path = Path(db.get_run(rid, db.run_owner(rid))["file_path"])
    assert c.post(f"/api/runs/{rid}/cancel").json() == dict(ok=True, stopping=False)
    assert c.get(f"/api/runs/{rid}").status_code == 404 and not path.exists()


def test_cancelling_while_reading_stops_at_the_next_stage(monkeypatch):
    monkeypatch.setattr(config, "STAGE_DELAY", 0.3)
    c = client("asha")
    rid = c.post("/api/runs/sample", json=dict(files=[SAMPLE])).json()["runs"][0]["id"]
    path = Path(db.get_run(rid, db.run_owner(rid))["file_path"])
    for _ in range(200):
        if c.get(f"/api/runs/{rid}").json()["status"] == "running":
            break
        time.sleep(0.02)
    assert c.post(f"/api/runs/{rid}/cancel").json() == dict(ok=True, stopping=True)
    wait(rid)
    assert c.get(f"/api/runs/{rid}").status_code == 404 and not path.exists()
    assert c.get("/api/stats").json()["total"] == 0


def test_cancelling_a_queued_document_leaves_the_others_alone(monkeypatch):
    monkeypatch.setattr(config, "STAGE_DELAY", 0.2)
    c = client("asha")
    first, second = [r["id"] for r in c.post("/api/runs/sample", json=dict(files=[SAMPLE, UNKNOWN_VENDOR])).json()["runs"]]
    assert c.get(f"/api/runs/{second}").json()["status"] == "queued"
    assert c.post(f"/api/runs/{second}/cancel").json() == dict(ok=True, stopping=False)
    assert c.get(f"/api/runs/{second}").status_code == 404
    wait(first)
    assert c.get(f"/api/runs/{first}").json()["decision"] == "APPROVE"


def test_a_decided_invoice_cannot_be_cancelled_and_only_yours_can_be():
    a, b = client("asha"), client("ben")
    rid = run_sample(a, SAMPLE)
    assert a.post(f"/api/runs/{rid}/cancel").status_code == 409
    waiting = upload(a)
    assert b.post(f"/api/runs/{waiting}/cancel").status_code == 404
    assert a.get(f"/api/runs/{waiting}").json()["status"] == "review"


def test_an_unreadable_upload_does_not_wait_for_review(tmp_path):
    from PIL import Image
    c = client("asha")
    path = tmp_path / "blank.png"
    Image.new("RGB", (600, 400), (200, 200, 200)).save(path)
    rid = c.post("/api/runs", files=dict(files=("blank.png", path.read_bytes()))).json()["runs"][0]["id"]
    wait(rid)
    run = c.get(f"/api/runs/{rid}").json()
    assert run["status"] == "done" and run["decision"] == "REJECT"


# ----------------------------------------------------------------------- folders --

def folder(c, name):
    r = c.post("/api/folders", json=dict(name=name))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_folders_are_created_listed_and_counted():
    c = client("asha")
    fid = folder(c, "  Team   lunches ")
    rid = run_sample(c, SAMPLE)
    assert c.post(f"/api/runs/{rid}/folder", json=dict(folder_id=fid)).status_code == 200
    assert c.get("/api/folders").json()["folders"] == [dict(id=fid, name="Team lunches", created_at=c.get("/api/folders").json()["folders"][0]["created_at"], count=1)]
    assert c.get(f"/api/runs/{rid}").json()["folder_id"] == fid
    assert c.post(f"/api/runs/{rid}/folder", json=dict(folder_id=None)).status_code == 200  # taken out again
    assert c.get("/api/folders").json()["folders"][0]["count"] == 0


@pytest.mark.parametrize("name, status", [("", 400), ("x" * 61, 400), ("team LUNCHES", 409)])
def test_folder_names_are_checked(name, status):
    c = client("asha")
    folder(c, "Team lunches")
    assert c.post("/api/folders", json=dict(name=name)).status_code == status


def test_each_folder_has_its_own_dashboard_and_report():
    import zipfile
    from io import BytesIO
    c = client("asha")
    lunches, office = folder(c, "Lunches"), folder(c, "Office")
    a, b = run_sample(c, SAMPLE), run_sample(c, UNKNOWN_VENDOR)
    c.post(f"/api/runs/{a}/folder", json=dict(folder_id=office))
    c.post(f"/api/runs/{b}/folder", json=dict(folder_id=lunches))
    counts = lambda f: c.get("/api/stats", params=dict(folder=f)).json()["counts"]
    assert counts(office) == dict(APPROVE=1, HOLD=0) and counts(lunches) == dict(APPROVE=0, HOLD=1)
    assert c.get("/api/stats").json()["total"] == 2
    r = c.get("/api/export.xlsx", params=dict(folder=office))
    assert "invoice-flow-report-office-" in r.headers["content-disposition"]
    strings = zipfile.ZipFile(BytesIO(r.content)).read("xl/sharedStrings.xml").decode()
    assert SAMPLE in strings and UNKNOWN_VENDOR not in strings
    assert c.get("/api/stats", params=dict(folder="abc")).status_code == 422


def test_resetting_a_folder_removes_only_its_invoices():
    c = client("asha")
    keep, clear = folder(c, "Keep"), folder(c, "Clear")
    kept, cleared, loose = run_sample(c, UNKNOWN_VENDOR), run_sample(c, SAMPLE), run_sample(c, "07_near_miss_within_tolerance.pdf")
    c.post(f"/api/runs/{kept}/folder", json=dict(folder_id=keep))
    c.post(f"/api/runs/{cleared}/folder", json=dict(folder_id=clear))
    path = Path(db.get_run(cleared, db.run_owner(cleared))["file_path"])
    billed = lambda: next(p for p in c.get("/api/pos").json()["pos"] if p["po_number"] == "PO-2026-1001")["billed"]
    assert billed() == 100000.0
    assert c.post(f"/api/folders/{clear}/reset").json()["removed"] == [cleared]
    ids = {r["id"] for r in c.get("/api/runs").json()["runs"]}
    assert ids == {kept, loose} and not path.exists() and billed() == 0  # its PO posting went with it
    assert {f["name"] for f in c.get("/api/folders").json()["folders"]} == {"Keep", "Clear"}  # the folder stays, empty


def test_folders_are_private_to_their_account():
    a, b = client("asha"), client("ben")
    theirs = folder(a, "Asha's")
    mine = run_sample(b, SAMPLE)
    assert b.post(f"/api/runs/{mine}/folder", json=dict(folder_id=theirs)).status_code == 404
    assert b.post(f"/api/folders/{theirs}/reset").status_code == 404
    assert b.get("/api/folders").json()["folders"] == []
    rid = run_sample(a, SAMPLE)
    assert b.post(f"/api/runs/{rid}/folder", json=dict(folder_id=None)).status_code == 404


def test_a_rejected_invoice_cannot_be_filed():
    c = client("asha")
    fid = folder(c, "Office")
    run_sample(c, SAMPLE)
    duplicate = run_sample(c, SAMPLE)
    assert c.get(f"/api/runs/{duplicate}").json()["decision"] == "REJECT"
    r = c.post(f"/api/runs/{duplicate}/folder", json=dict(folder_id=fid))
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    assert c.get("/api/folders").json()["folders"][0]["count"] == 0


def test_rejecting_a_filed_invoice_takes_it_out_of_its_folder():
    c = client("asha")
    fid = folder(c, "Office")
    held = run_sample(c, UNKNOWN_VENDOR)
    assert c.post(f"/api/runs/{held}/folder", json=dict(folder_id=fid)).status_code == 200
    c.post(f"/api/runs/{held}/override", json=dict(decision="REJECT"))
    assert c.get(f"/api/runs/{held}").json()["folder_id"] is None


def test_a_recheck_that_rejects_takes_it_out_of_its_folder():
    c = client("asha")
    fid = folder(c, "Office")
    run_sample(c, SAMPLE)
    duplicate = run_sample(c, SAMPLE)
    db.update_run(duplicate, folder_id=fid)  # as if it had been filed before turning out to be a duplicate
    main._requeue(db.get_run(duplicate, db.run_owner(duplicate)))
    for _ in range(300):
        if main.BUS[duplicate]["done"]:
            break
        time.sleep(0.02)
    run = c.get(f"/api/runs/{duplicate}").json()
    assert run["decision"] == "REJECT" and run["folder_id"] is None


def test_only_decided_invoices_can_be_filed():
    c = client("asha")
    fid = folder(c, "Lunches")
    rid = upload(c)  # waiting for review
    assert c.post(f"/api/runs/{rid}/folder", json=dict(folder_id=fid)).status_code == 404


def test_removing_from_history_deletes_for_good():
    c = client("asha")
    fid = folder(c, "Office")
    rid = run_sample(c, SAMPLE)
    c.post(f"/api/runs/{rid}/folder", json=dict(folder_id=fid))
    path = Path(db.get_run(rid, db.run_owner(rid))["file_path"])
    assert c.delete(f"/api/runs/{rid}").status_code == 200
    assert c.get(f"/api/runs/{rid}").status_code == 404 and not path.exists()
    assert c.get("/api/stats").json()["total"] == 0 and c.get("/api/folders").json()["folders"][0]["count"] == 0
    billed = next(p for p in c.get("/api/pos").json()["pos"] if p["po_number"] == "PO-2026-1001")["billed"]
    assert billed == 0  # its PO posting went too


def test_a_deleted_invoice_uploaded_again_is_new_not_a_duplicate():
    c = client("asha")
    first = run_sample(c, SAMPLE)
    c.delete(f"/api/runs/{first}")
    again = run_sample(c, SAMPLE)
    run = c.get(f"/api/runs/{again}").json()
    assert run["decision"] == "APPROVE" and "DUPLICATE_EXACT" not in {x["code"] for x in run["result"]["findings"]}


def test_only_your_own_finished_invoices_can_be_deleted():
    a, b = client("asha"), client("ben")
    rid = run_sample(a, SAMPLE)
    assert b.delete(f"/api/runs/{rid}").status_code == 404
    assert a.get("/api/stats").json()["total"] == 1
    with db._lock:
        db.conn().execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
        db.conn().commit()
    assert a.delete(f"/api/runs/{rid}").status_code == 409


def test_invoices_hidden_by_the_old_version_are_deleted_on_startup():
    c = client("asha")
    rid = run_sample(c, SAMPLE)
    db.update_run(rid, hidden=1)
    assert [run_id for run_id, _path in db.purge_hidden()] == [rid]
    assert c.get(f"/api/runs/{rid}").status_code == 404
    assert c.get(f"/api/runs/{run_sample(c, SAMPLE)}").json()["decision"] == "APPROVE"


def test_folder_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_FOLDERS_PER_USER", 1)
    c = client("asha")
    folder(c, "One")
    assert c.post("/api/folders", json=dict(name="Two")).status_code == 429


# ------------------------------------------------------------- approved vendor list --

def vendor_file(c):
    import zipfile
    from io import BytesIO
    r = c.get("/api/vendors.xlsx")
    assert r.status_code == 200 and "approved-vendors-" in r.headers["content-disposition"]
    z = zipfile.ZipFile(BytesIO(r.content))
    return z.read("xl/workbook.xml").decode(), z.read("xl/sharedStrings.xml").decode(), z.read("xl/worksheets/sheet1.xml").decode()


def test_approved_vendor_file_has_one_sheet_and_no_blocked_vendors():
    book, strings, _rows = vendor_file(client("asha"))
    assert book.count("<sheet ") == 1 and "Approved vendors" in book
    assert "Sharma Office Supplies Pvt Ltd" in strings and "Kiran Courier Services" not in strings


def test_approved_vendor_file_counts_only_that_accounts_invoices():
    a, b = client("asha"), client("ben")
    run_sample(a, SAMPLE)
    assert 'r="G2"><v>1</v>' in vendor_file(a)[2]   # V001: one approved invoice
    assert 'r="G2"><v>0</v>' in vendor_file(b)[2]


def test_vendor_approved_in_the_app_joins_only_that_accounts_file():
    a, b = client("asha"), client("ben")
    rid = run_sample(a, UNKNOWN_VENDOR)
    a.post(f"/api/runs/{rid}/approve-vendor", json=dict(note="=HYPERLINK(1)"))
    for _ in range(300):
        if main.BUS[rid]["done"]:
            break
        time.sleep(0.02)
    strings_a, rows_a = vendor_file(a)[1:]
    assert "Apex Stationers" in strings_a and "=HYPERLINK(1)" in strings_a and "<f>" not in rows_a
    assert "Apex Stationers" not in vendor_file(b)[1]


# ----------------------------------------------------------------------- periods --

def test_period_filter_covers_figures_and_report():
    import zipfile
    from io import BytesIO
    c = client("asha")
    october, september = run_sample(c, SAMPLE), run_sample(c, UNKNOWN_VENDOR)
    db.update_run(october, invoice_date="2026-10-12")
    db.update_run(september, invoice_date="2025-09-03")
    total = lambda period: c.get("/api/stats", params=dict(period=period)).json()["total"]
    assert (total(""), total("2026"), total("2026-10"), total("2025-09"), total("2026-09")) == (2, 1, 1, 1, 0)
    r = c.get("/api/export.xlsx", params=dict(period="2026-10"))
    assert r.status_code == 200 and "invoice-flow-report-2026-10.xlsx" in r.headers["content-disposition"]
    strings = zipfile.ZipFile(BytesIO(r.content)).read("xl/sharedStrings.xml").decode()
    assert SAMPLE in strings and UNKNOWN_VENDOR not in strings


def test_invoice_with_no_date_only_counts_under_all_time():
    c = client("asha")
    rid = run_sample(c, SAMPLE)
    db.update_run(rid, invoice_date=None)
    assert c.get("/api/stats").json()["total"] == 1
    assert c.get("/api/stats", params=dict(period="2026")).json()["total"] == 0


@pytest.mark.parametrize("period", ["26", "2026-1", "2026-10-01", "x' OR 1=1"])
def test_bad_period_is_refused(period):
    c = client("asha")
    assert c.get("/api/stats", params=dict(period=period)).status_code == 400
    assert c.get("/api/export.xlsx", params=dict(period=period)).status_code == 400


# ------------------------------------------------------------------------ limits --

def test_oversized_upload_is_refused_before_it_is_read(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    c = client("asha")
    r = c.post("/api/runs", files=dict(files=("big.pdf", b"x" * (2 * 1024 * 1024))))
    assert r.status_code == 413 and "1 MB" in r.json()["detail"]


def test_one_account_cannot_fill_the_shared_queue(monkeypatch):
    monkeypatch.setattr(config, "MAX_QUEUED_PER_USER", 2)  # the 2 waiting ones count; the first, at Review, does not
    c = client("asha")
    assert c.post("/api/runs/sample", json=dict(files=[SAMPLE] * 4)).status_code == 429


def test_each_account_has_a_document_limit(monkeypatch):
    c = client("asha")
    run_sample(c, SAMPLE)
    monkeypatch.setattr(config, "MAX_RUNS_PER_USER", 1)
    assert c.post("/api/runs/sample", json=dict(files=[SAMPLE])).status_code == 429


def test_a_hostile_filename_cannot_choose_where_the_file_is_stored():
    c = client("asha")
    r = c.post("/api/runs", files=dict(files=("../../evil.pdf", b"not a pdf")))
    rid = r.json()["runs"][0]["id"]
    for _ in range(300):
        if main.BUS[rid]["done"]:
            break
        time.sleep(0.02)
    stored = Path(db.get_run(rid, db.run_owner(rid))["file_path"])
    assert stored.parent == config.UPLOAD_DIR and stored.name == f"{rid}.pdf"


def test_security_headers_are_on_every_response():
    c = client()
    for path in ("/", "/api/me"):
        h = c.get(path).headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and "default-src 'self'" in h["content-security-policy"]
    assert "no-store" in c.get("/api/me").headers["cache-control"]


# --------------------------------------------------------------------- upgrading --

def test_first_account_inherits_history_from_before_logins():
    with db._lock:
        db.conn().execute("INSERT INTO runs(id,status,filename,file_path) VALUES('legacy','done','old.pdf','x')")
        db.conn().commit()
    a, b = client("asha"), client("ben")
    ids = lambda c: [r["id"] for r in c.get("/api/runs").json()["runs"]]
    assert ids(a) == ["legacy"] and ids(b) == []


def test_old_database_upgrades_in_place():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE runs(id TEXT PRIMARY KEY, inputs_json TEXT);
    CREATE TABLE ledger(id INTEGER PRIMARY KEY, po_number TEXT);
    CREATE TABLE vendors(vendor_id TEXT PRIMARY KEY, name TEXT, aliases TEXT, gstin TEXT, status TEXT, approved_at TEXT, note TEXT, run_id TEXT);
    INSERT INTO vendors(vendor_id,name) VALUES('V006','Old Vendor');
    """)
    db.init(c)
    for table in ("runs", "ledger", "vendors"):
        assert "user_id" in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    assert c.execute("SELECT name FROM vendors").fetchone()[0] == "Old Vendor"


def test_runs_interrupted_by_a_restart_are_parked_for_a_person():
    c = client("asha")
    with db._lock:
        db.conn().execute("INSERT INTO runs(id,status,filename,file_path,user_id) VALUES('stuck','running','a.pdf','x',?)", (db.find_user("asha")["id"],))
        db.conn().commit()
    assert db.fail_interrupted() == 1
    assert c.get("/api/runs/stuck").json()["status"] == "error"


# --------------------------------------------- vendor and PO answers on the Decision page, vendor folders --

def bill(tmp_path, vendor, gstin, number, items, po=None):
    """A simple machine-readable invoice: [(description, qty, rate)], 18% GST."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    path = tmp_path / f"{number.replace('/', '_')}.pdf"
    net = sum(q * r for _, q, r in items)
    lines = [f"{vendor}    TAX INVOICE", "Jodhpur, Rajasthan 342001", f"GSTIN: {gstin}", f"Invoice No: {number}", "Invoice Date: 28-Oct-2026"]
    lines += [f"PO Ref: {po}"] if po else []
    lines += ["S.No Description HSN/SAC Qty Rate Amount"]
    lines += [f"{i} {d} 94033000 {q} {r:,.2f} {q * r:,.2f}" for i, (d, q, r) in enumerate(items, 1)]
    lines += [f"Sub Total {net:,.2f}", f"CGST @ 9% {net * 0.09:,.2f}", f"SGST @ 9% {net * 0.09:,.2f}", f"Grand Total Rs. {net * 1.18:,.2f}"]
    c = canvas.Canvas(str(path), pagesize=A4)
    for i, l in enumerate(lines):
        c.drawString(50, 800 - i * 18, l)
    c.save()
    return path


def checked(c, path):
    """Upload, accept the fields as read, wait for the decision."""
    rid = c.post("/api/runs", files=dict(files=(path.name, path.read_bytes()))).json()["runs"][0]["id"]
    wait(rid)
    assert c.post(f"/api/runs/{rid}/continue", json=dict(inputs={})).status_code == 200
    wait(rid)
    return c.get(f"/api/runs/{rid}").json()


codes = lambda run: {x["code"] for x in run["result"]["findings"]}


def test_a_new_vendor_and_its_po_question_are_settled_on_the_decision_page():
    c = client("asha")
    rid = run_sample(c, UNKNOWN_VENDOR)
    run = c.get(f"/api/runs/{rid}").json()
    assert run["decision"] == "HOLD" and "VENDOR_UNKNOWN" in codes(run)
    assert c.post(f"/api/runs/{rid}/po", json=dict(no_po=True)).status_code == 400  # no PO hold yet: the vendor comes first
    c.post(f"/api/runs/{rid}/approve-vendor", json=dict(note="called them"))
    wait(rid)
    run = c.get(f"/api/runs/{rid}").json()
    assert run["decision"] == "HOLD" and codes(run) & {"PO_NONE_OPEN", "PO_AMBIGUOUS"} and run["po_options"] == []
    assert c.post(f"/api/runs/{rid}/po", json=dict(po_number="PO-2026-1001")).status_code == 400  # another vendor's PO
    assert client("ben").post(f"/api/runs/{rid}/po", json=dict(no_po=True)).status_code == 404
    assert c.post(f"/api/runs/{rid}/po", json=dict(no_po=True)).status_code == 200
    wait(rid)
    run = c.get(f"/api/runs/{rid}").json()
    assert run["decision"] == "APPROVE" and "PO_NOT_NEEDED" in codes(run)
    assert c.post(f"/api/runs/{rid}/po", json=dict(no_po=True)).status_code == 400  # nothing left to answer


def test_vendor_approval_can_say_no_po_in_one_step():
    c = client("asha")
    rid = run_sample(c, UNKNOWN_VENDOR)
    c.post(f"/api/runs/{rid}/approve-vendor", json=dict(no_po=True))
    wait(rid)
    assert c.get(f"/api/runs/{rid}").json()["decision"] == "APPROVE"


def test_the_vendors_next_bill_is_approved_and_filed_with_the_earlier_ones(tmp_path):
    c = client("asha")
    rid = run_sample(c, UNKNOWN_VENDOR)
    c.post(f"/api/runs/{rid}/approve-vendor", json=dict(no_po=True))
    wait(rid)
    apex = folder(c, "Apex")
    c.post(f"/api/runs/{rid}/folder", json=dict(folder_id=apex))
    run = checked(c, bill(tmp_path, "Apex Stationers & Co", "08AAPFA7777Q1ZK", "AS/2026/802", [("Office chairs", 4, 3000)]))
    assert run["decision"] == "APPROVE" and "PO_NOT_NEEDED" in codes(run)
    assert run["folder_id"] == apex and run["result"]["auto_folder"] == dict(id=apex, name="Apex", why="vendor")
    other = checked(c, ROOT / "samples/synthetic" / SAMPLE)  # a different vendor stays out of it
    assert other["decision"] == "APPROVE" and other["folder_id"] is None
    assert next(f for f in c.get("/api/folders").json()["folders"] if f["id"] == apex)["count"] == 2


def test_a_held_bill_is_filed_with_its_vendor_once_a_person_approves_it(tmp_path):
    c = client("asha")
    first = run_sample(c, UNKNOWN_VENDOR)
    c.post(f"/api/runs/{first}/approve-vendor", json=dict(no_po=True))
    wait(first)
    apex = folder(c, "Apex")
    c.post(f"/api/runs/{first}/folder", json=dict(folder_id=apex))
    run = checked(c, bill(tmp_path, "Apex Stationers & Co", "08AAPFA7777Q1ZK", "AS/2026/803", [("Desks", 2, 5000)]))
    rid = run["id"]
    db.update_run(rid, decision="HOLD", folder_id=None)  # as if a check had held it, so it wasn't filed
    assert c.post(f"/api/runs/{rid}/override", json=dict(decision="APPROVE")).json()["folder"]["id"] == apex
    rejected = checked(c, bill(tmp_path, "Apex Stationers & Co", "08AAPFA7777Q1ZK", "AS/2026/804", [("Shelves", 3, 2000)]))
    db.update_run(rejected["id"], decision="HOLD", folder_id=None)
    c.post(f"/api/runs/{rejected['id']}/override", json=dict(decision="REJECT"))
    assert c.get(f"/api/runs/{rejected['id']}").json()["folder_id"] is None


def test_a_bill_citing_an_unknown_po_is_matched_to_the_right_one(tmp_path):
    c = client("asha")
    items = [("A4 copier paper", 150, 280), ("Toner cartridge", 20, 1900), ("Whiteboard markers", 50, 400)]
    run = checked(c, bill(tmp_path, "Sharma Office Supplies Pvt Ltd", "08AABCS1234F1Z5", "SOS/26-27/0499", items, po="PO-2026-9999"))
    rid = run["id"]
    assert run["decision"] == "HOLD" and "PO_NOT_FOUND" in codes(run)
    assert "PO-2026-1001" in {p["po_number"] for p in run["po_options"]}
    assert c.post(f"/api/runs/{rid}/po", json=dict(no_po=True)).status_code == 400  # it cites a PO, so it needs the right one
    assert c.post(f"/api/runs/{rid}/po", json=dict(po_number="PO-2026-1001")).status_code == 200
    wait(rid)
    run = c.get(f"/api/runs/{rid}").json()
    assert run["decision"] == "APPROVE" and run["po_number"] == "PO-2026-1001" and "PO_ASSIGNED" in codes(run)
    assert next(p for p in c.get("/api/pos").json()["pos"] if p["po_number"] == "PO-2026-1001")["billed"] == 100000.0


def test_no_po_answers_belong_to_one_account_and_go_with_the_vendor():
    a, b = client("asha"), client("ben")
    rid = run_sample(a, UNKNOWN_VENDOR)
    vendor_id = a.post(f"/api/runs/{rid}/approve-vendor", json=dict(no_po=True)).json()["vendor_id"]
    wait(rid)
    assert db.no_po_vendors(db.run_owner(rid)) and not db.no_po_vendors(db.find_user("ben")["id"])
    a.delete(f"/api/vendors/{vendor_id}")
    assert not db.no_po_vendors(db.run_owner(rid))


# ------------------------------------------- a person's approval teaches the checks about the vendor and its PO --

SHARMA_ITEMS = [("A4 copier paper", 150, 280), ("Toner cartridge", 20, 1900), ("Whiteboard markers", 50, 400)]  # = PO-2026-1001


def approve(c, rid):
    r = c.post(f"/api/runs/{rid}/override", json=dict(decision="APPROVE"))
    assert r.status_code == 200, r.text
    return r.json()


def test_approving_an_unknown_vendor_adds_it_and_its_next_bill_is_approved(tmp_path):
    c = client("asha")
    rid = run_sample(c, UNKNOWN_VENDOR)
    learned = approve(c, rid)["learned"]
    assert len(learned) == 2 and "approved vendors" in learned[0] and "purchase orders" in learned[1]
    assert c.get(f"/api/runs/{rid}").json()["result"]["learned"] == learned  # shown on the invoice afterwards
    run = checked(c, bill(tmp_path, "Apex Stationers & Co", "08AAPFA7777Q1ZK", "AS/2026/900", [("Office chairs", 4, 3000)]))
    assert run["decision"] == "APPROVE" and "PO_NOT_NEEDED" in codes(run)


def test_an_ambiguous_name_becomes_another_name_for_the_vendor_it_resembles(tmp_path):
    c = client("asha")
    run = checked(c, bill(tmp_path, "Sharma Office Mart", "", "SOM/101", [("Printer cartridges", 10, 500)]))
    assert run["decision"] == "HOLD" and "VENDOR_AMBIGUOUS" in codes(run)
    learned = approve(c, run["id"])["learned"]
    assert learned == ["'Sharma Office Mart' is now known as another name for Sharma Office Supplies Pvt Ltd, so its bills match that vendor."]
    assert c.get(f"/api/runs/{run['id']}").json()["vendor_id"] == "V001"
    again = checked(c, bill(tmp_path, "Sharma Office Mart", "", "SOM/102", SHARMA_ITEMS, po="PO-2026-1001"))
    assert again["decision"] == "APPROVE" and again["vendor_id"] == "V001" and again["po_number"] == "PO-2026-1001"
    assert not db.no_po_vendors(db.run_owner(run["id"]))  # Sharma uses POs, so that isn't switched off


def test_an_accepted_po_number_is_accepted_on_the_vendors_next_bill(tmp_path):
    c = client("asha")
    first = checked(c, bill(tmp_path, "Sharma Office Supplies Pvt Ltd", "08AABCS1234F1Z5", "SOS/9001", [("Chairs", 2, 4000)], po="PO-OLD-77"))
    assert first["decision"] == "HOLD" and "PO_NOT_FOUND" in codes(first)
    assert approve(c, first["id"])["learned"] == ["PO number PO-OLD-77 is accepted for this vendor from now on."]
    second = checked(c, bill(tmp_path, "Sharma Office Supplies Pvt Ltd", "08AABCS1234F1Z5", "SOS/9002", [("Desk", 1, 6000)], po="PO-OLD-77"))
    assert second["decision"] == "APPROVE" and "PO_REF_ACCEPTED" in codes(second)
    other = checked(c, bill(tmp_path, "Sharma Office Supplies Pvt Ltd", "08AABCS1234F1Z5", "SOS/9003", [("Desk", 1, 6000)], po="PO-OLD-78"))
    assert "PO_NOT_FOUND" in codes(other)  # only that number


def test_fraud_signals_are_never_learned(tmp_path):
    c = client("asha")
    run = checked(c, bill(tmp_path, "Sharma Office Supplies Pvt Ltd", "08AABCS9999F1Z5", "SOS/7001", SHARMA_ITEMS, po="PO-2026-1001"))
    assert run["decision"] == "HOLD" and "VENDOR_GSTIN_MISMATCH" in codes(run)
    assert approve(c, run["id"])["learned"] == []
    again = checked(c, bill(tmp_path, "Sharma Office Supplies Pvt Ltd", "08AABCS9999F1Z5", "SOS/7002", [("Pens", 10, 50)]))
    assert again["decision"] == "HOLD" and "VENDOR_GSTIN_MISMATCH" in codes(again)
    kiran = checked(c, bill(tmp_path, "Kiran Parcel Services", "", "KC/1", [("Courier", 10, 100)]))  # resembles blocked Kiran Courier Services
    assert kiran["decision"] == "HOLD" and "VENDOR_AMBIGUOUS" in codes(kiran)
    assert approve(c, kiran["id"])["learned"] == []
    assert "VENDOR_AMBIGUOUS" in codes(checked(c, bill(tmp_path, "Kiran Parcel Services", "", "KC/2", [("Courier", 5, 100)])))


def test_rejecting_learns_nothing_and_learning_stays_in_the_account(tmp_path):
    a, b = client("asha"), client("ben")
    rid = run_sample(a, UNKNOWN_VENDOR)
    a.post(f"/api/runs/{rid}/override", json=dict(decision="REJECT"))
    assert not db.added_vendors(db.run_owner(rid))
    rid = run_sample(a, UNKNOWN_VENDOR)
    db.update_run(rid, folder_id=None)
    approve(a, rid)
    assert db.added_vendors(db.run_owner(rid)) and not db.added_vendors(db.find_user("ben")["id"])


# ------------------------------------------------- a folder of invoices: one review, the rest checked straight through --

NEXORA = ("Nexora Office Solutions Pvt. Ltd.", "36AABCN4521K1ZP")


def nexora_folder(tmp_path, n=3):
    pos = ["PO-2026-0488", "PO-2026-0488", "PO-2026-0452", "PO-2026-0417"]
    return [bill(tmp_path, *NEXORA, f"NX/2026/{1250 + i}", [(f"Office furniture lot {i}", 2 + i, 5000)], po=pos[i]) for i in range(n)]


def upload_folder(c, paths):
    r = c.post("/api/runs", data=dict(batch="1"), files=[("files", (p.name, p.read_bytes())) for p in paths])
    assert r.status_code == 200, r.text
    runs = r.json()["runs"]
    wait(runs[0]["id"])
    return runs


def status(c, rid):
    return c.get(f"/api/runs/{rid}").json()


def test_a_folder_stops_only_at_its_first_invoice_and_its_answers_cover_the_rest(tmp_path):
    c = client("asha")
    runs = upload_folder(c, nexora_folder(tmp_path, 4))
    first, rest = runs[0]["id"], [r["id"] for r in runs[1:]]
    assert len({r["batch"] for r in runs}) == 1 and [r["waiting"] for r in runs] == [False, True, True, True]
    assert status(c, first)["status"] == "review" and {status(c, r)["status"] for r in rest} == {"waiting"}
    po = status(c, first)["events"][-1]["detail"]["po_check"]
    assert po == dict(reference="PO-2026-0488", known=False)
    d = c.post(f"/api/runs/{first}/continue", json=dict(inputs={}, approve_vendor=dict(note="checked GSTIN"), batch_po="accept")).json()
    assert [r["id"] for r in d["batch"]] == rest
    for rid in [first] + rest:
        wait(rid)
        run = status(c, rid)
        assert run["decision"] == "APPROVE", (run["filename"], codes(run))
        assert "PO_REF_ACCEPTED" in codes(run)
    assert [e["stage"] for e in status(c, rest[0])["events"] if e["status"] != "running"][:3] == ["ingest", "read", "extract"]  # no stop at Review
    later = checked(c, bill(tmp_path, *NEXORA, "NX/2026/1300", [("Desk lamp", 4, 900)], po="PO-2026-0452"))
    assert later["decision"] == "APPROVE"  # the vendor and its PO numbers are remembered after the folder too


def test_without_answers_the_rest_are_checked_and_held_for_the_same_reasons(tmp_path):
    c = client("asha")
    runs = upload_folder(c, nexora_folder(tmp_path))
    c.post(f"/api/runs/{runs[0]['id']}/continue", json=dict(inputs={}))
    for r in runs:
        wait(r["id"])
        assert status(c, r["id"])["decision"] == "HOLD" and {"VENDOR_UNKNOWN", "PO_NOT_FOUND"} <= codes(status(c, r["id"]))


def test_the_folders_po_answer_never_covers_another_vendor(tmp_path):
    c = client("asha")
    paths = nexora_folder(tmp_path, 2) + [bill(tmp_path, "Sharma Office Supplies Pvt Ltd", "08AABCS1234F1Z5", "SOS/5555", [("Chairs", 2, 4000)], po="PO-OLD-1")]
    runs = upload_folder(c, paths)
    c.post(f"/api/runs/{runs[0]['id']}/continue", json=dict(inputs={}, approve_vendor={}, batch_po="accept"))
    for r in runs:
        wait(r["id"])
    assert status(c, runs[1]["id"])["decision"] == "APPROVE"
    sharma = status(c, runs[2]["id"])
    assert sharma["decision"] == "HOLD" and "PO_NOT_FOUND" in codes(sharma)


def test_a_po_answer_needs_the_vendor_and_a_folder(tmp_path):
    c = client("asha")
    runs = upload_folder(c, nexora_folder(tmp_path))
    r = c.post(f"/api/runs/{runs[0]['id']}/continue", json=dict(inputs={}, batch_po="accept"))
    assert r.status_code == 400 and "vendor" in r.json()["detail"]
    assert status(c, runs[0]["id"])["status"] == "review" and status(c, runs[1]["id"])["status"] == "waiting"  # nothing moved
    assert c.post(f"/api/runs/{runs[0]['id']}/continue", json=dict(inputs={}, batch_po="maybe")).status_code == 400
    single = upload(c)
    assert c.post(f"/api/runs/{single}/continue", json=dict(inputs={}, batch_po="no_po")).status_code == 400


def test_if_the_first_invoice_is_cancelled_the_next_one_asks(tmp_path):
    c = client("asha")
    runs = upload_folder(c, nexora_folder(tmp_path))
    assert c.post(f"/api/runs/{runs[0]['id']}/cancel").json()["stopping"] is False
    wait(runs[1]["id"])
    assert status(c, runs[1]["id"])["status"] == "review" and status(c, runs[2]["id"])["status"] == "waiting"


def test_if_the_first_file_is_not_an_invoice_the_next_one_asks(tmp_path):
    from PIL import Image
    blank = tmp_path / "0_blank.png"
    Image.new("RGB", (600, 400), (120, 160, 90)).save(blank)
    c = client("asha")
    runs = upload_folder(c, [blank] + nexora_folder(tmp_path, 2))
    assert status(c, runs[0]["id"])["decision"] == "REJECT"
    wait(runs[1]["id"])
    assert status(c, runs[1]["id"])["status"] == "review" and status(c, runs[2]["id"])["status"] == "waiting"


def test_filing_the_first_invoice_files_the_rest_that_passed(tmp_path):
    c = client("asha")
    runs = upload_folder(c, nexora_folder(tmp_path))
    c.post(f"/api/runs/{runs[0]['id']}/continue", json=dict(inputs={}, approve_vendor={}, batch_po="accept"))
    for r in runs:
        wait(r["id"])
    fid = folder(c, "Nexora")
    assert sorted(c.post(f"/api/runs/{runs[0]['id']}/folder", json=dict(folder_id=fid)).json()["filed"]) == sorted(r["id"] for r in runs[1:])
    assert next(f for f in c.get("/api/folders").json()["folders"] if f["id"] == fid)["count"] == 3


def test_waiting_invoices_count_against_the_queue_limit(tmp_path, monkeypatch):
    c = client("asha")
    monkeypatch.setattr(config, "MAX_QUEUED_PER_USER", 3)
    upload_folder(c, nexora_folder(tmp_path, 3))  # its first is at Review, which doesn't count; the 2 waiting do
    two = [ROOT / "samples/synthetic" / SAMPLE, ROOT / "samples/synthetic" / UNKNOWN_VENDOR]
    r = c.post("/api/runs", data=dict(batch="1"), files=[("files", (p.name, p.read_bytes())) for p in two])
    assert r.status_code == 429


def test_a_folder_left_waiting_by_a_restart_carries_on(tmp_path):
    c = client("asha")
    runs = upload_folder(c, nexora_folder(tmp_path))
    db.update_run(runs[0]["id"], status="error", decision="HOLD")  # as fail_interrupted leaves it
    main._resume_stuck_batches()
    wait(runs[1]["id"])
    assert status(c, runs[1]["id"])["status"] == "review"


def test_approve_all_vendors_adds_each_new_vendor_but_still_holds_lookalikes(tmp_path):
    c = client("asha")
    paths = nexora_folder(tmp_path, 2) + [
        bill(tmp_path, "Apex Stationers & Co", "08AAPFA7777Q1ZK", "AS/77", [("Files", 10, 300)], po="AP-55"),
        bill(tmp_path, "Kiran Parcel Services", "", "KP/9", [("Courier", 5, 100)])]  # resembles blocked Kiran Courier Services
    runs = upload_folder(c, paths)
    d = c.post(f"/api/runs/{runs[0]['id']}/continue", json=dict(inputs={}, approve_all_vendors=True, batch_po="accept"))
    assert d.status_code == 200, d.text  # no separate vendor tick needed: this one's vendor is new too
    for r in runs:
        wait(r["id"])
    nexora, apex, kiran = status(c, runs[0]["id"]), status(c, runs[2]["id"]), status(c, runs[3]["id"])
    assert nexora["decision"] == "APPROVE" and "VENDOR_ADDED" in codes(nexora)
    assert status(c, runs[1]["id"])["decision"] == "APPROVE" and "VENDOR_ADDED" not in codes(status(c, runs[1]["id"]))  # added once
    assert apex["decision"] == "APPROVE" and {"VENDOR_ADDED", "PO_REF_ACCEPTED"} <= codes(apex)  # the PO answer covers it too
    assert kiran["decision"] == "HOLD" and "VENDOR_AMBIGUOUS" in codes(kiran)
    names = {v["name"] for v in c.get("/api/pos").json()["vendors"] if v["source"] == "approved in app"}
    assert names == {"Nexora Office Solutions Pvt. Ltd.", "Apex Stationers & Co"}


def test_without_approve_all_only_the_first_invoices_vendor_is_approved(tmp_path):
    c = client("asha")
    runs = upload_folder(c, nexora_folder(tmp_path, 1) + [bill(tmp_path, "Apex Stationers & Co", "08AAPFA7777Q1ZK", "AS/78", [("Files", 10, 300)])])
    c.post(f"/api/runs/{runs[0]['id']}/continue", json=dict(inputs={}, approve_vendor={}, batch_po="accept"))
    for r in runs:
        wait(r["id"])
    assert "VENDOR_UNKNOWN" in codes(status(c, runs[1]["id"]))
    single = upload(c, UNKNOWN_VENDOR)
    assert c.post(f"/api/runs/{single}/continue", json=dict(inputs={}, approve_all_vendors=True)).status_code == 400


def test_all_of_a_folder_upload_can_be_saved_to_one_folder(tmp_path):
    c = client("asha")
    paths = nexora_folder(tmp_path, 2) + [ROOT / "samples/synthetic" / SAMPLE, ROOT / "samples/synthetic" / SAMPLE]  # the 2nd copy is rejected
    runs = upload_folder(c, paths)
    c.post(f"/api/runs/{runs[0]['id']}/continue", json=dict(inputs={}, approve_vendor={}, batch_po="accept"))
    for r in runs:
        wait(r["id"])
    assert status(c, runs[3]["id"])["decision"] == "REJECT"
    fid = folder(c, "November")
    d = c.post(f"/api/batches/{runs[0]['batch']}/folder", json=dict(folder_id=fid)).json()
    assert sorted(d["filed"]) == sorted(r["id"] for r in runs[:3]) and d["rejected"] == 1 and d["unfinished"] == 0
    assert next(f for f in c.get("/api/folders").json()["folders"] if f["id"] == fid)["count"] == 3
    other = client("ben")
    assert other.post(f"/api/batches/{runs[0]['batch']}/folder", json=dict(folder_id=folder(other, "Mine"))).status_code == 404
    assert c.post(f"/api/batches/{runs[0]['batch']}/folder", json=dict(folder_id=9999)).status_code == 404


# ---------------------------------------------- the Approvals step: answers given before anything is uploaded --

def upload_with(c, paths, **approvals):
    data = dict(approvals=__import__("json").dumps(approvals))
    if len(paths) > 1:
        data["batch"] = "1"
    r = c.post("/api/runs", data=data, files=[("files", (p.name, p.read_bytes())) for p in paths])
    assert r.status_code == 200, r.text
    runs = r.json()["runs"]
    wait(runs[0]["id"])  # the first stops at Review; any others wait for it
    return runs


def review_first(c, runs):
    """Submit the first invoice's review as read, then wait for every invoice in the upload to be decided."""
    assert c.post(f"/api/runs/{runs[0]['id']}/continue", json=dict(inputs={})).status_code == 200
    for x in runs:
        wait(x["id"])


def test_with_approvals_the_rest_wait_then_go_through_one_at_a_time_into_the_folder(tmp_path, monkeypatch):
    c = client("asha")
    fid = folder(c, "November")
    decided, finish = [], pipeline._finish
    monkeypatch.setattr(pipeline, "_finish", lambda run_id, *a: decided.append(run_id) or finish(run_id, *a))
    paths = nexora_folder(tmp_path, 2) + [bill(tmp_path, "Apex Stationers & Co", "08AAPFA7777Q1ZK", "AS/80", [("Files", 10, 300)])]  # no PO at all
    runs = upload_with(c, paths, vendors=True, po=True, folder_id=fid)
    assert [r["review"] for r in runs] == [True, False, False] and [r["waiting"] for r in runs] == [False, True, True]
    assert status(c, runs[0]["id"])["status"] == "review"
    assert {status(c, r["id"])["status"] for r in runs[1:]} == {"waiting"}  # nothing else starts while the first is at Review
    review_first(c, runs)
    assert decided == [r["id"] for r in runs]  # the first is decided before the next one is even read
    first = status(c, runs[0]["id"])
    assert first["decision"] == "APPROVE" and {"VENDOR_ADDED", "PO_APPROVED"} <= codes(first) and first["folder_id"] == fid
    second, apex = status(c, runs[1]["id"]), status(c, runs[2]["id"])
    assert second["decision"] == "APPROVE" and "VENDOR_ADDED" not in codes(second)  # Nexora was added by the first, already decided
    assert apex["decision"] == "APPROVE" and {"VENDOR_ADDED", "PO_APPROVED"} <= codes(apex)
    assert second["folder_id"] == apex["folder_id"] == fid
    assert next(f for f in c.get("/api/folders").json()["folders"] if f["id"] == fid)["count"] == 3
    assert not db.accepted_po_refs(db.run_owner(runs[0]["id"])) and not db.no_po_vendors(db.run_owner(runs[0]["id"]))  # for this upload only


def test_approvals_left_unticked_change_nothing(tmp_path):
    c = client("asha")
    runs = upload_with(c, nexora_folder(tmp_path, 2), vendors=False, po=False, folder_id=None)
    review_first(c, runs)
    rest = status(c, runs[1]["id"])
    assert rest["decision"] == "HOLD" and {"VENDOR_UNKNOWN", "PO_NOT_FOUND"} <= codes(rest) and rest["folder_id"] is None


def test_approved_po_references_still_hold_another_vendors_po_and_check_a_known_ones_balance(tmp_path):
    c = client("asha")
    other = bill(tmp_path, "Sharma Office Supplies Pvt Ltd", "08AABCS1234F1Z5", "SOS/601", [("Paper", 10, 280)], po="PO-2026-1002")  # Rathore's PO
    over = bill(tmp_path, "Sharma Office Supplies Pvt Ltd", "08AABCS1234F1Z5", "SOS/602", [("Paper", 400, 280)], po="PO-2026-1001")  # 112,000 > 100,000
    runs = upload_with(c, [other, over], vendors=True, po=True, folder_id=None)
    review_first(c, runs)
    assert "PO_VENDOR_MISMATCH" in codes(status(c, runs[0]["id"])) and status(c, runs[0]["id"])["decision"] == "HOLD"
    assert status(c, runs[1]["id"])["decision"] in ("HOLD", "REJECT") and "PO_APPROVED" not in codes(status(c, runs[1]["id"]))


def test_a_single_file_takes_approvals_too_and_rejected_ones_are_never_filed(tmp_path):
    c = client("asha")
    fid = folder(c, "Mine")
    one = upload_with(c, [ROOT / "samples/synthetic" / UNKNOWN_VENDOR], vendors=True, po=True, folder_id=fid)[0]
    c.post(f"/api/runs/{one['id']}/continue", json=dict(inputs={}))
    wait(one["id"])
    run = status(c, one["id"])
    assert run["decision"] == "APPROVE" and run["folder_id"] == fid
    run_sample(c, SAMPLE)
    dup = upload_with(c, [ROOT / "samples/synthetic" / SAMPLE], vendors=False, po=False, folder_id=fid)[0]
    c.post(f"/api/runs/{dup['id']}/continue", json=dict(inputs={}))
    wait(dup["id"])
    assert status(c, dup["id"])["decision"] == "REJECT" and status(c, dup["id"])["folder_id"] is None


def test_approvals_are_checked(tmp_path):
    c = client("asha")
    p = [ROOT / "samples/synthetic" / SAMPLE]
    assert c.post("/api/runs", data=dict(approvals='{"folder_id": 999}'), files=[("files", (p[0].name, p[0].read_bytes()))]).status_code == 404
    assert c.post("/api/runs", data=dict(approvals='"yes"'), files=[("files", (p[0].name, p[0].read_bytes()))]).status_code == 400
    theirs = folder(client("ben"), "Ben's")
    assert c.post("/api/runs", data=dict(approvals=f'{{"folder_id": {theirs}}}'), files=[("files", (p[0].name, p[0].read_bytes()))]).status_code == 404


# ---------------------------------------------------------------------------------------- Analytics tab --

def test_analytics_figures(tmp_path):
    c = client("asha")
    ok = run_sample(c, SAMPLE)                                   # approved by the system
    dup = run_sample(c, SAMPLE)                                  # rejected: an exact duplicate
    held = run_sample(c, UNKNOWN_VENDOR)                         # held for an unknown vendor, then approved by a person
    c.post(f"/api/runs/{held}/override", json=dict(decision="APPROVE"))
    waiting = checked(c, bill(tmp_path, "Sharma Office Mart", "", "SOM/1", [("Pens", 10, 50)]))  # still held: ambiguous vendor
    runs = upload_with(c, nexora_folder(tmp_path, 3), vendors=True, po=True, folder_id=None)   # a batch of three
    review_first(c, runs)
    a = c.get("/api/analytics").json()
    assert a["total"] == 7  # 4 single files and the batch of 3
    assert a["outcome"]["REJECT"]["count"] == 1 and a["outcome"]["HOLD"]["count"] == 1 and a["outcome"]["APPROVE"]["count"] == 5
    assert a["outcome"]["APPROVE"]["value"] == round(sum(status(c, r)["total"] for r in [ok, held] + [x["id"] for x in runs]), 2)
    assert sum(x["count"] for x in a["hold_ages"]) == 1 and a["hold_ages"][0]["label"] == "Under a day"
    assert "matrix" not in a and "stops" not in a  # those two charts were removed
    assert {"code": "DUPLICATE_EXACT", "count": 1} in a["flags"]["call"]
    assert all(f["code"] in __import__("app.rules", fromlist=["x"]).FLAG_KIND["resend"] for f in a["flags"]["resend"])
    sharma = next(v for v in a["vendors"] if v["name"] == "Sharma Office Supplies Pvt Ltd")
    assert (sharma["invoices"], sharma["stopped"], sharma["hold_rate"], sharma["top_reason"]) == (2, 1, 50, "DUPLICATE_EXACT")
    batch = next(b for b in a["batches"] if b["batch"])
    assert batch["invoices"] == 3 and batch["cleared"] == 3 and batch["cleared_pct"] == 100
    single = a["batches"][-1]
    assert single["batch"] is None and single["invoices"] == 4
    assert dict(a["spend"]) and a["undated_spend"] >= 0


def test_analytics_follow_the_folder_and_period_and_stay_private(tmp_path):
    c = client("asha")
    fid = folder(c, "Office")
    rid = run_sample(c, SAMPLE)
    run_sample(c, UNKNOWN_VENDOR)
    c.post(f"/api/runs/{rid}/folder", json=dict(folder_id=fid))
    assert c.get("/api/analytics", params=dict(folder=fid)).json()["total"] == 1
    assert c.get("/api/analytics", params=dict(period="1999")).json()["total"] == 0
    assert c.get("/api/analytics", params=dict(period="bad")).status_code == 400
    assert client("ben").get("/api/analytics").json()["total"] == 0


# ------------------------------------------------------------------------------------------- ZIP uploads --

def zip_of(tmp_path, members, name="invoices.zip"):
    """members: {name inside the ZIP: bytes or a file path}."""
    import zipfile
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for inner, data in members.items():
            z.writestr(inner, data if isinstance(data, bytes) else Path(data).read_bytes())
    return path


def test_a_zip_is_unpacked_into_its_invoices_and_checked_one_at_a_time(tmp_path, monkeypatch):
    c = client("asha")
    bills = nexora_folder(tmp_path, 3)
    z = zip_of(tmp_path, {"nov/NX_10.pdf": bills[2], "nov/NX_2.pdf": bills[1], "NX_1.pdf": bills[0], "__MACOSX/nov/._NX_2.pdf": b"junk",
                          ".DS_Store": b"junk", "readme.txt": b"hello", "inner.zip": b"PK not unpacked"})
    decided, finish = [], pipeline._finish
    monkeypatch.setattr(pipeline, "_finish", lambda run_id, *a: decided.append(run_id) or finish(run_id, *a))
    runs = upload_with(c, [z], vendors=True, po=True, folder_id=None)
    assert [r["filename"] for r in runs] == ["NX_2.pdf", "NX_10.pdf", "NX_1.pdf"]  # path order, numbers natural (2 before 10), junk skipped
    assert len({r["batch"] for r in runs}) == 1 and [r["waiting"] for r in runs] == [False, True, True]
    review_first(c, runs)
    assert decided == [r["id"] for r in runs] and all(status(c, r["id"])["decision"] == "APPROVE" for r in runs)


def test_a_zip_can_go_with_other_files_and_a_one_invoice_zip_is_a_single_file(tmp_path):
    c = client("asha")
    bills = nexora_folder(tmp_path, 3)
    runs = upload_with(c, [bills[0], zip_of(tmp_path, {"a.pdf": bills[1], "b.pdf": bills[2]})], vendors=False, po=False, folder_id=None)
    assert len(runs) == 3 and runs[0]["batch"] and [r["waiting"] for r in runs] == [False, True, True]
    one = upload_with(c, [zip_of(tmp_path, {"only.pdf": ROOT / "samples/synthetic" / SAMPLE}, "one.zip")], vendors=False, po=False, folder_id=None)
    assert len(one) == 1 and one[0]["batch"] is None and status(c, one[0]["id"])["status"] == "review"


def test_names_inside_a_zip_cannot_choose_where_files_are_written(tmp_path):
    c = client("asha")
    z = zip_of(tmp_path, {"../../evil.pdf": ROOT / "samples/synthetic" / SAMPLE, "C:/Windows/x.pdf": ROOT / "samples/synthetic" / SAMPLE})
    runs = upload_with(c, [z], vendors=False, po=False, folder_id=None)
    assert sorted(r["filename"] for r in runs) == ["evil.pdf", "x.pdf"]
    for r in runs:
        stored = Path(db.get_run(r["id"], db.run_owner(r["id"]))["file_path"]).resolve()
        assert stored.parent == config.UPLOAD_DIR.resolve()
    assert not (config.UPLOAD_DIR.parent.parent / "evil.pdf").exists()


@pytest.mark.parametrize("members, message", [
    ({"readme.txt": b"no invoices"}, "has no PDFs or photos"),
    (None, "isn't a readable ZIP"),
])
def test_bad_zips_are_refused(tmp_path, members, message):
    c = client("asha")
    path = zip_of(tmp_path, members) if members else tmp_path / "fake.zip"
    if members is None:
        path.write_bytes(b"this is not a zip")
    r = c.post("/api/runs", files=dict(files=(path.name, path.read_bytes())))
    assert r.status_code == 400 and message in r.json()["detail"]
    assert c.get("/api/runs").json()["runs"] == []


def test_a_zip_bomb_is_stopped_while_unpacking(tmp_path, monkeypatch):
    c = client("asha")
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    z = zip_of(tmp_path, {"big.pdf": b"%PDF-" + b"\0" * (3 * 1024 * 1024)})  # 3 MB unpacked, a few KB packed
    assert z.stat().st_size < 100_000
    r = c.post("/api/runs", files=dict(files=(z.name, z.read_bytes())))
    assert r.status_code == 400 and "more than the size allowed" in r.json()["detail"]
    assert not list(config.UPLOAD_DIR.glob("_tmp_*"))  # nothing left behind


def test_a_password_protected_zip_is_refused(tmp_path):
    import zipfile
    path = zip_of(tmp_path, {"a.pdf": ROOT / "samples/synthetic" / SAMPLE})
    data = bytearray(path.read_bytes())
    for sig in (b"PK\x03\x04", b"PK\x01\x02"):  # set the "encrypted" flag in both headers, as a protected archive has
        i = data.find(sig)
        off = i + (6 if sig == b"PK\x03\x04" else 8)
        data[off] |= 0x1
    r = client("asha").post("/api/runs", files=dict(files=("locked.zip", bytes(data))))
    assert r.status_code == 400 and "password-protected" in r.json()["detail"]


def test_there_is_no_limit_on_documents_unless_one_is_set(tmp_path):
    assert config.MAX_QUEUED_PER_USER == 0 and config.MAX_RUNS_PER_USER == 0  # the defaults: no limit
    c = client("asha")
    paths = [bill(tmp_path, "Apex Stationers & Co", "08AAPFA7777Q1ZK", f"AS/{900 + i}", [("Files", 1 + i, 100)]) for i in range(30)]
    z = zip_of(tmp_path, {p.name: p for p in paths}, "thirty.zip")
    runs = upload_with(c, [z], vendors=False, po=False, folder_id=None)
    assert len(runs) == 30 and sum(r["waiting"] for r in runs) == 29   # more than the old limit of 25, all accepted
