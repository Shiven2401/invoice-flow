"""The Create account tab: username, password and confirm password, then signed straight in. The ☰ folders menu is on the page."""
from fastapi.testclient import TestClient

from app import auth, config, db, main


def setup_function():
    auth._attempts.clear()
    with db._lock:
        for table in ("users", "sessions"):
            db.conn().execute(f"DELETE FROM {table}")
        db.conn().commit()


def test_matching_passwords_create_the_account_and_sign_in():
    c = TestClient(main.app)
    r = c.post("/api/signup", json=dict(username="meera", password="correct horse", confirm="correct horse"))
    assert r.status_code == 200
    assert c.get("/api/me").json() == dict(username="meera")       # signed in straight away


def test_passwords_that_differ_are_refused_and_nothing_is_created():
    c = TestClient(main.app)
    r = c.post("/api/signup", json=dict(username="meera", password="correct horse", confirm="correct hose"))
    assert r.status_code == 400 and r.json()["detail"] == "The passwords don't match."
    assert db.find_user("meera") is None and c.get("/api/me").status_code == 401


def test_the_api_alone_still_works_without_a_confirmation():
    r = TestClient(main.app).post("/api/signup", json=dict(username="meera", password="correct horse"))
    assert r.status_code == 200


def test_the_page_has_both_tabs_and_the_folders_menu(monkeypatch):
    html = TestClient(main.app).get("/").text
    for part in ('id="tab-login"', 'id="tab-signup"', 'id="signup-form"', 'name="confirm"', 'id="menu"', 'id="side-folders"'):
        assert part in html, part
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "id")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "GOOGLE_REDIRECT_URI", "https://x/api/auth/google/callback")
    html = TestClient(main.app).get("/").text
    assert html.index("Continue with Google") < html.index('id="tab-login"')   # above both tabs
