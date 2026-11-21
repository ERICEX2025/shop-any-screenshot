"""Optional "Sign in with Shop": OAuth device flow (RFC 8628) against Shop's public client. No key, no secret.

Docs: https://shop.dev/docs/authentication (device flow) and https://shop.dev/docs/sign-in. The public client ID below is
the one Shop publishes for a shopper's own agent or a prototype; an app that is distributed needs its own registered client.
The shopper opens a link on Shop's own site and approves. The token lives in this process's memory only: it is never
written to disk, logged or sent to the page. With it, searches use the shopper's saved preferences and Shopify's
checkout page can offer their Shop Pay details. This app still never calls complete_checkout.
"""
import threading
import time

import httpx

from . import shopify

AUTH = "https://accounts.shop.app"
CLIENT_ID = "5c733ab2-1903-400a-891e-7ba20c09e2a3"   # Shop's public client, published in the docs
_http = httpx.Client(timeout=30)
_lock = threading.Lock()
_s = {"state": "signed_out", "email": None, "link": None, "code": None, "expires": 0, "error": None, "refresh": None, "run": 0}


def status():
    with _lock:
        if _s["state"] == "signed_in" and shopify._token is None:   # the token expired and shopify.call dropped it
            _s.update(state="signed_out", email=None)
        return {k: _s[k] for k in ("state", "email", "link", "code", "error")} | {
            "expires_in": max(0, int(_s["expires"] - time.time())) if _s["state"] == "pending" else None}


def start():
    """Ask Shop for a device code and return the link the shopper approves on. A background thread waits for approval."""
    r = _http.post(AUTH + "/oauth/device", data={
        "client_id": CLIENT_ID, "resource": "https://mcp.shop.com", "scope": "openid email personal_agent",
        "device_name": "ShelfCart"})
    r.raise_for_status()
    d = r.json()
    with _lock:
        _s["run"] += 1
        run = _s["run"]
        _s.update(state="pending", error=None, email=None, code=d.get("user_code"),
                  link=d.get("verification_uri_complete") or d.get("verification_uri"),
                  expires=time.time() + int(d.get("expires_in", 600)))
    threading.Thread(target=_poll, args=(run, d["device_code"], int(d.get("interval") or 5), _s["expires"]), daemon=True).start()
    return status()


def _poll(run, device_code, interval, end):
    while time.time() < end:
        time.sleep(interval)
        with _lock:
            if _s["run"] != run or _s["state"] != "pending":
                return
        try:
            j = _http.post(AUTH + "/oauth/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": CLIENT_ID,
                "device_code": device_code}).json()
        except Exception:
            continue
        err = j.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        with _lock:
            if _s["run"] != run:
                return
            if j.get("access_token"):
                shopify.set_token(j["access_token"])
                _s.update(state="signed_in", refresh=j.get("refresh_token"), link=None, code=None)
            else:
                _s.update(state="signed_out", link=None, code=None,
                          error="Shop did not sign you in (" + str(err or "no token")[:60] + ").")
                return
        try:   # the (possibly masked) email, for display only
            acct = shopify.call("get_account", {}, need_auth=True)
            with _lock:
                _s["email"] = ((acct.get("buyer") or {}).get("email") or "")[:80] or None
        except Exception:
            pass
        return
    with _lock:
        if _s["run"] == run and _s["state"] == "pending":
            _s.update(state="signed_out", link=None, code=None, error="The sign-in link expired.")


def signout():
    with _lock:
        _s["run"] += 1
        tok = _s["refresh"] or shopify._token
        _s.update(state="signed_out", email=None, link=None, code=None, error=None, refresh=None)
    shopify.set_token(None)
    if tok:
        try:
            _http.post(AUTH + "/oauth/revoke", data={"token": tok, "client_id": CLIENT_ID})
        except Exception:
            pass
    return status()
