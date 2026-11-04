"""Accounts and login sessions, using only the standard library.

Passwords are stored as salted scrypt hashes. A session is a random token in an
HttpOnly cookie, and only its SHA-256 is stored, so a leaked database can't be
replayed as a login. Every /api route except the ones here depends on current_user.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import urllib.parse

import requests
from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from loguru import logger

from . import config, db

COOKIE = config.SESSION_COOKIE
USERNAME_RE = re.compile(r"[A-Za-z0-9._@+-]{3,64}")
SCRYPT = dict(n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024)

router = APIRouter()
_attempts = {}               # throttle key -> recent attempt times. In memory: the app runs as one process
_lock = threading.Lock()


def hash_password(password):
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, **SCRYPT).hex()}"


def verify_password(password, stored):
    _scheme, salt, digest = stored.split("$")
    return hmac.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT).hex(), digest)


_NO_SUCH_USER = hash_password(secrets.token_hex(16))  # checked for unknown names, so timing doesn't reveal which names exist


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _too_many(key, limit, window):
    with _lock:
        recent = [t for t in _attempts.get(key, []) if t > time.time() - window]
        _attempts[key] = recent
        return len(recent) >= limit


def _record(key):
    with _lock:
        _attempts.setdefault(key, []).append(time.time())


def _start_session(response, user_id):
    token = secrets.token_urlsafe(32)
    now_ts = time.time()
    db.add_session(_digest(token), user_id, now_ts + config.SESSION_DAYS * 86400, now_ts)
    response.set_cookie(COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=config.COOKIE_SECURE, path="/")


def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    user = db.session_user(_digest(token), time.time()) if token else None
    if not user:
        raise HTTPException(401, "Please log in.")
    return user


def _ip(request):
    return request.client.host if request.client else "unknown"


@router.post("/api/signup")
def signup(request: Request, response: Response, body: dict = Body(...)):
    if not config.ALLOW_SIGNUP:
        raise HTTPException(403, "New accounts are switched off.")
    key = f"signup:{_ip(request)}"
    if _too_many(key, 10, 3600):
        raise HTTPException(429, "Too many new accounts from this address. Try again later.")
    username, password = str(body.get("username") or "").strip(), str(body.get("password") or "")
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(400, "Usernames are 3 to 64 letters, numbers or . _ @ + -")
    if not 8 <= len(password) <= 200:
        raise HTTPException(400, "Passwords need at least 8 characters.")
    _record(key)
    user_id = db.create_user(username, hash_password(password))
    if user_id is None:
        raise HTTPException(409, "That username is taken.")
    _start_session(response, user_id)
    logger.info("account created: {!r}", username)
    return dict(ok=True, username=username)


@router.post("/api/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    username, password = str(body.get("username") or "").strip(), str(body.get("password") or "")[:200]
    keys = (f"name:{username.lower()}", 5), (f"ip:{_ip(request)}", 30)
    if any(_too_many(key, limit, 900) for key, limit in keys):
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    user = db.find_user(username)
    if user and user["pw_hash"].startswith(GOOGLE_PREFIX):
        raise HTTPException(401, "This account signs in with Google. Use Continue with Google.")
    matched = verify_password(password, user["pw_hash"] if user else _NO_SUCH_USER)
    if not user or not matched:
        for key, _limit in keys:
            _record(key)
        logger.warning("failed login for {!r}", username[:64])
        raise HTTPException(401, "Wrong username or password.")
    with _lock:
        _attempts.pop(keys[0][0], None)
    _start_session(response, user["id"])
    return dict(ok=True, username=user["username"])


# ------------------------------------------------------------------------------------ sign in with Google (OIDC) --
GOOGLE_PREFIX = "sso$google$"   # stored instead of a password hash: the account can only sign in with Google
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
# the sign-in's state rides in the session cookie itself ("gstate:..."), which no session matches, so it also works behind
# Firebase Hosting, which passes on only one cookie. Starting a Google sign-in therefore ends any current session.
STATE_PREFIX = "gstate:"


def google_enabled():
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET and config.GOOGLE_REDIRECT_URI)


def _back_with_error(message):
    response = RedirectResponse("/?google_error=" + urllib.parse.quote(message[:200]), status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/api/auth/google")
def google_start(request: Request):
    if not google_enabled():
        raise HTTPException(404, "Google sign-in isn't set up here.")
    if _too_many(f"google:{_ip(request)}", 30, 900):
        return _back_with_error("Too many sign-in attempts. Try again in 15 minutes.")
    _record(f"google:{_ip(request)}")
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(dict(client_id=config.GOOGLE_CLIENT_ID, redirect_uri=config.GOOGLE_REDIRECT_URI, response_type="code",
                                        scope="openid email profile", state=state, prompt="select_account"))
    response = RedirectResponse(f"{GOOGLE_AUTH}?{query}", status_code=303)
    # the state comes back on Google's redirect (a top-level GET, so a SameSite=Lax cookie is sent): it ties the answer to
    # this browser, so nobody can sign a visitor into the wrong account with a link of their own
    response.set_cookie(COOKIE, STATE_PREFIX + state, max_age=600, httponly=True, samesite="lax", secure=config.COOKIE_SECURE, path="/")
    return response


@router.get("/api/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not google_enabled():
        raise HTTPException(404, "Google sign-in isn't set up here.")
    cookie = request.cookies.get(COOKIE) or ""
    expected = cookie[len(STATE_PREFIX):] if cookie.startswith(STATE_PREFIX) else ""
    if error:
        return _back_with_error("Google sign-in was cancelled." if error == "access_denied" else f"Google sign-in failed ({error}).")
    if not code or not expected or not hmac.compare_digest(state, expected):
        return _back_with_error("That Google sign-in link has expired. Try again.")
    try:
        reply = requests.post(GOOGLE_TOKEN, timeout=15, data=dict(code=code, client_id=config.GOOGLE_CLIENT_ID, client_secret=config.GOOGLE_CLIENT_SECRET,
                                                                   redirect_uri=config.GOOGLE_REDIRECT_URI, grant_type="authorization_code"))
        reply.raise_for_status()
        # the ID token came straight from Google's token endpoint over TLS, authenticated with our client secret, so its
        # claims can be read without checking its signature (OpenID Connect Core 3.1.3.7); the claims are still checked
        payload = reply.json()["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception as e:
        logger.warning("Google sign-in: token exchange failed: {}", type(e).__name__)
        return _back_with_error("Couldn't complete the Google sign-in. Try again.")
    if claims.get("aud") != config.GOOGLE_CLIENT_ID or claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com") \
            or float(claims.get("exp", 0)) < time.time():
        logger.warning("Google sign-in: ID token refused (audience, issuer or expiry)")
        return _back_with_error("Couldn't complete the Google sign-in. Try again.")
    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True:
        return _back_with_error("Your Google account has no verified email address, so it can't be used here.")
    if not USERNAME_RE.fullmatch(email):
        return _back_with_error("This email address can't be used as an account name. Create an account with a username instead.")
    user = db.find_user(email)
    if user is None:
        if not config.ALLOW_SIGNUP:
            return _back_with_error("New accounts are switched off.")
        user_id = db.create_user(email, GOOGLE_PREFIX + secrets.token_hex(16))
        user = db.find_user(email)
        if user_id is not None:
            logger.info("account created with Google: {!r}", email)
    if not user["pw_hash"].startswith(GOOGLE_PREFIX):
        # a password account already has this name: never hand it to a Google login, or anyone could register a victim's
        # email as a username first and then read whatever that person uploads
        return _back_with_error(f"An account called {email} already exists with a password. Log in with its password instead.")
    response = RedirectResponse("/", status_code=303)
    _start_session(response, user["id"])  # replaces the state in the same cookie with the session
    return response


@router.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        db.delete_session(_digest(token))
    response.delete_cookie(COOKIE, path="/")
    return dict(ok=True)


@router.get("/api/me")
def me(request: Request):
    user = current_user(request)
    return dict(username=user["username"])
