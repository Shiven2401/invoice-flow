"""Sign in with Google: the redirect, the callback's checks, and how Google accounts and password accounts stay apart.
Google itself is never called: the token exchange is replaced by a stub that returns a made-up ID token."""
import base64
import json
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, main

REDIRECT = "https://invoice.example/api/auth/google/callback"


@pytest.fixture(autouse=True)
def google(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "client-123.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "GOOGLE_REDIRECT_URI", REDIRECT)
    auth._attempts.clear()
    with db._lock:
        for table in ("users", "sessions"):
            db.conn().execute(f"DELETE FROM {table}")
        db.conn().commit()


def id_token(**claims):
    base = dict(iss="https://accounts.google.com", aud=config.GOOGLE_CLIENT_ID, exp=time.time() + 300, email="Asha@Example.com", email_verified=True)
    body = base64.urlsafe_b64encode(json.dumps(dict(base, **claims)).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.signature"


class Reply:
    def __init__(self, token):
        self.token = token

    def raise_for_status(self):
        pass

    def json(self):
        return dict(id_token=self.token)


def sign_in(monkeypatch, c=None, **claims):
    """Start at /api/auth/google, then come back to the callback as Google would. Returns (client, final response)."""
    c = c or TestClient(main.app)
    start = c.get("/api/auth/google", follow_redirects=False)
    assert start.status_code == 303
    state = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers["location"]).query)["state"][0]
    sent = {}
    monkeypatch.setattr(auth.requests, "post", lambda url, **kw: (sent.update(url=url, **kw), Reply(id_token(**claims)))[1])
    back = c.get(f"/api/auth/google/callback?code=abc&state={state}", follow_redirects=False)
    if sent:
        assert sent["url"] == auth.GOOGLE_TOKEN and sent["data"]["redirect_uri"] == REDIRECT and sent["data"]["code"] == "abc"
    return c, back


def test_button_only_when_configured(monkeypatch):
    assert "Continue with Google" in TestClient(main.app).get("/").text
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "")
    assert "Continue with Google" not in TestClient(main.app).get("/").text
    assert TestClient(main.app).get("/api/auth/google", follow_redirects=False).status_code == 404


def test_start_redirects_to_google_with_state():
    r = TestClient(main.app).get("/api/auth/google", follow_redirects=False)
    url = urllib.parse.urlparse(r.headers["location"])
    q = urllib.parse.parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert q["client_id"] == [config.GOOGLE_CLIENT_ID] and q["redirect_uri"] == [REDIRECT] and q["scope"] == ["openid email profile"]
    assert r.cookies.get(auth.STATE_COOKIE) == q["state"][0]


def test_first_sign_in_creates_an_account_and_logs_in(monkeypatch):
    c, back = sign_in(monkeypatch)
    assert back.status_code == 303 and back.headers["location"] == "/"
    assert c.get("/api/me").json() == dict(username="asha@example.com")
    assert db.find_user("asha@example.com")["pw_hash"].startswith(auth.GOOGLE_PREFIX)
    c2, _ = sign_in(monkeypatch)                           # the next sign-in is the same account
    assert c2.get("/api/me").json() == dict(username="asha@example.com")
    assert len(db.conn().execute("SELECT id FROM users").fetchall()) == 1


def test_google_account_cannot_log_in_with_a_password(monkeypatch):
    sign_in(monkeypatch)
    r = TestClient(main.app).post("/api/login", json=dict(username="asha@example.com", password="anything at all"))
    assert r.status_code == 401 and "Google" in r.json()["detail"]


def test_password_account_with_that_email_is_never_handed_to_google(monkeypatch):
    TestClient(main.app).post("/api/signup", json=dict(username="asha@example.com", password="correct horse"))
    c, back = sign_in(monkeypatch)
    assert "google_error=" in back.headers["location"] and "already+exists" in back.headers["location"].replace("%20", "+")
    assert c.get("/api/me").status_code == 401


def test_state_must_match():
    c = TestClient(main.app)
    c.get("/api/auth/google", follow_redirects=False)
    r = c.get("/api/auth/google/callback?code=abc&state=forged", follow_redirects=False)
    assert "google_error=" in r.headers["location"]
    assert c.get("/api/me").status_code == 401


@pytest.mark.parametrize("claims", [dict(aud="someone-else"), dict(iss="https://evil.example"), dict(exp=time.time() - 10),
                                    dict(email_verified=False), dict(email="")])
def test_bad_id_tokens_are_refused(monkeypatch, claims):
    c, back = sign_in(monkeypatch, **claims)
    assert "google_error=" in back.headers["location"]
    assert c.get("/api/me").status_code == 401


def test_error_is_shown_on_the_page():
    html = TestClient(main.app).get("/?google_error=" + urllib.parse.quote("<b>nope</b>")).text
    assert "&lt;b&gt;nope&lt;/b&gt;" in html and "<b>nope</b>" not in html
