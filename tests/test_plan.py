"""Sign in with ChatGPT (chatgpt_mod/plan.py): OpenAI's ChatGPT plan usage
flow for open-source apps, https://developers.openai.com/siwc/token-sharing-open-source.
Ported from the Odysseus core's tests when the feature became this add-on.

Everything here is mocked: no request leaves the machine.
"""

import base64
import hashlib
import json
import logging
import os
import stat
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from chatgpt_mod import plan as cgp

ACCESS = "eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.c2lnbmF0dXJlLWFjY2Vzcw"
REFRESH = "rt_refresh_secret_value_0001"
ISSUED_CLIENT = "oaiapp_issued_client_123"


def _jwt(claims):
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{seg}.c2ln"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Picker:
    """What the add-on told Odysseus's model picker (plan.on_models / on_signed_out)."""

    def __init__(self):
        self.rows = {}

    def models(self, user, slugs):
        self.rows[user] = list(slugs)

    def signed_out(self, user):
        self.rows.pop(user, None)


@pytest.fixture
def plan(tmp_path, monkeypatch):
    monkeypatch.setattr(cgp, "STORE_DIR", tmp_path / "chatgpt")
    monkeypatch.setenv("CHATGPT_MOD_LOOPBACK_LISTENER", "0")
    picker = Picker()
    monkeypatch.setattr(cgp, "on_models", picker.models)
    monkeypatch.setattr(cgp, "on_signed_out", picker.signed_out)
    cgp._pending.clear()
    cgp._refresh_locks.clear()
    cgp.picker = picker
    yield cgp
    cgp._pending.clear()


def _posts(monkeypatch, responder):
    calls = []

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        calls.append({"url": url, "data": dict(data or {}), "headers": headers})
        return responder(url, dict(data or {}))

    monkeypatch.setattr(cgp.httpx, "post", fake_post)
    return calls


def _token_body(nonce, email="jaron@example.com", access=ACCESS, refresh=REFRESH, **extra):
    body = {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": _jwt({"email": email, "nonce": nonce, "iss": "https://auth.openai.com"}),
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": cgp.SCOPE,
        "earliest_refresh_at": int(time.time()) + 1800,
    }
    body.update(extra)
    return body


def _sign_in(plan, monkeypatch, owner="alice", email="jaron@example.com"):
    start = plan.start_sign_in(owner)
    q = parse_qs(urlparse(start["authorize_url"]).query)
    state = q["state"][0]
    nonce = q["nonce"][0]
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(200, _token_body(nonce, email=email)))
    cb = f"http://127.0.0.1:1455/auth/callback?code=AUTHCODE&scope=openid&state={state}&client_id={ISSUED_CLIENT}"
    result = plan.complete_sign_in(cb, user=owner)
    return start, q, calls, result


# ── Authorize URL ────────────────────────────────────────────────────────

def test_first_sign_in_builds_the_documented_authorize_url(plan):
    start = plan.start_sign_in("alice")
    parsed = urlparse(start["authorize_url"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://auth.openai.com/api/accounts/authorize"
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert q["client_id"] == "dynamic_agent_client"
    assert q["agent_name_hint"] == "Odysseus"
    assert q["ext_agent_host_id"].startswith("urn:uuid:")
    assert q["response_type"] == "code"
    assert q["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert q["scope"] == "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
    assert q["resource"] == "https://api.openai.com/v1"
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 32 and len(q["nonce"]) >= 32 and q["state"] != q["nonce"]
    # PKCE: the challenge is base64url(SHA-256(verifier)) without padding.
    verifier = plan._pending[q["state"]]["verifier"]
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected
    assert start["first_sign_in"] is True


def test_host_id_is_created_once_and_kept(plan):
    first = plan.host_id()
    assert plan.host_id() == first
    q1 = parse_qs(urlparse(plan.start_sign_in("alice")["authorize_url"]).query)
    q2 = parse_qs(urlparse(plan.start_sign_in("bob")["authorize_url"]).query)
    assert q1["ext_agent_host_id"] == q2["ext_agent_host_id"] == [first]


def test_later_sign_ins_use_the_issued_client_id_and_hints(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    again = plan.start_sign_in("alice")
    q = {k: v[0] for k, v in parse_qs(urlparse(again["authorize_url"]).query).items()}
    assert q["client_id"] == ISSUED_CLIENT
    assert "agent_name_hint" not in q
    assert q.get("id_token_hint") or q.get("login_hint")
    assert again["first_sign_in"] is False
    # Another Odysseus user still registers their own client.
    other = parse_qs(urlparse(plan.start_sign_in("bob")["authorize_url"]).query)
    assert other["client_id"] == ["dynamic_agent_client"]


# ── Pasted redirect URL ──────────────────────────────────────────────────

def test_pasted_url_exchanges_the_code_with_the_documented_form(plan, monkeypatch):
    start, q, calls, result = _sign_in(plan, monkeypatch)
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://auth.openai.com/api/accounts/oauth/token"
    verifier_challenge = q["code_challenge"][0]
    form = call["data"]
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == ISSUED_CLIENT
    assert form["code"] == "AUTHCODE"
    assert form["redirect_uri"] == q["redirect_uri"][0]
    assert form["resource"] == "https://api.openai.com/v1"
    assert "client_secret" not in form
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == verifier_challenge
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert result["signed_in"] is True and result["email"] == "jaron@example.com"
    assert result["plan_usage_enabled"] is True


def test_a_wrong_state_is_rejected_without_calling_openai(plan, monkeypatch):
    plan.start_sign_in("alice")
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(200, {}))
    with pytest.raises(cgp.SignInRejected):
        plan.complete_sign_in("http://127.0.0.1:1455/auth/callback?code=X&state=not-the-state", user="alice")
    assert calls == []
    assert plan.status("alice")["signed_in"] is False


def test_another_users_state_is_rejected(plan, monkeypatch):
    start = plan.start_sign_in("alice")
    state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(200, {}))
    with pytest.raises(cgp.SignInRejected):
        plan.complete_sign_in(f"http://127.0.0.1:1455/auth/callback?code=X&state={state}", user="bob")
    assert calls == []


def test_a_state_can_only_be_used_once_and_expires(plan, monkeypatch):
    start, q, calls, _ = _sign_in(plan, monkeypatch)
    with pytest.raises(cgp.SignInRejected):
        plan.complete_sign_in(f"http://127.0.0.1:1455/auth/callback?code=AUTHCODE&state={q['state'][0]}",
                              user="alice")
    start = plan.start_sign_in("carol")
    state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]
    plan._pending[state]["created"] -= plan.PENDING_TTL_SECONDS + 1
    with pytest.raises(cgp.SignInRejected):
        plan.complete_sign_in(f"?code=X&state={state}", user="carol")


def test_declined_consent_and_garbage_are_explained(plan):
    plan.start_sign_in("alice")
    with pytest.raises(cgp.SignInRejected, match="cancelled|declined"):
        plan.complete_sign_in("http://127.0.0.1:1455/auth/callback?error=access_denied&state=x", user="alice")
    with pytest.raises(cgp.SignInRejected):
        plan.complete_sign_in("   ", user="alice")


def test_reauthorization_without_client_id_in_callback_keeps_the_saved_one(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    start = plan.start_sign_in("alice")
    q = parse_qs(urlparse(start["authorize_url"]).query)
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(200, _token_body(q["nonce"][0])))
    plan.complete_sign_in(f"http://127.0.0.1:1455/auth/callback?code=C2&state={q['state'][0]}", user="alice")
    assert calls[0]["data"]["client_id"] == ISSUED_CLIENT


def test_a_mismatched_nonce_is_rejected(plan, monkeypatch):
    start = plan.start_sign_in("alice")
    state = parse_qs(urlparse(start["authorize_url"]).query)["state"][0]
    _posts(monkeypatch, lambda url, data: FakeResponse(200, _token_body("some-other-nonce")))
    with pytest.raises(cgp.SignInRejected, match="nonce"):
        plan.complete_sign_in(f"?code=C&state={state}&client_id={ISSUED_CLIENT}", user="alice")
    assert plan.status("alice")["signed_in"] is False


def test_missing_plan_scope_keeps_sign_in_but_flags_plan_usage_off(plan, monkeypatch):
    start = plan.start_sign_in("alice")
    q = parse_qs(urlparse(start["authorize_url"]).query)
    body = _token_body(q["nonce"][0], scope="openid profile email offline_access")
    _posts(monkeypatch, lambda url, data: FakeResponse(200, body))
    st = plan.complete_sign_in(f"?code=C&state={q['state'][0]}&client_id={ISSUED_CLIENT}", user="alice")
    assert st["signed_in"] is True and st["plan_usage_enabled"] is False


# ── Storage ──────────────────────────────────────────────────────────────

def test_tokens_are_stored_owner_only_and_encrypted(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    path = plan._creds_path("alice")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    raw = path.read_text()
    assert ACCESS not in raw and REFRESH not in raw
    assert plan.load_credentials("alice")["refresh_token"] == REFRESH
    for f in path.parent.iterdir():
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert plan.load_credentials("bob") == {}


def test_tokens_never_reach_the_logs(plan, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _sign_in(plan, monkeypatch)
    creds = plan.load_credentials("alice")
    creds["expires_at"] = int(time.time()) + 10
    creds["earliest_refresh_at"] = 0
    plan.save_credentials("alice", creds)
    _posts(monkeypatch, lambda url, data: FakeResponse(400, {"error": "invalid_grant"}))
    with pytest.raises(cgp.ReauthRequired):
        plan.get_access_token("alice")
    text = caplog.text
    for secret in (ACCESS, REFRESH, "AUTHCODE"):
        assert secret not in text


def test_redact_scrubs_tokens_from_error_text():
    msg = f"failed Authorization: Bearer {ACCESS} refresh_token={REFRESH} code=abc123"
    out = cgp.redact(msg, REFRESH)
    assert ACCESS not in out and REFRESH not in out and "abc123" not in out


# ── Refresh ──────────────────────────────────────────────────────────────

def _expiring(plan, owner="alice", *, in_seconds=60, earliest=0):
    creds = plan.load_credentials(owner)
    creds["expires_at"] = int(time.time()) + in_seconds
    creds["earliest_refresh_at"] = earliest
    plan.save_credentials(owner, creds)


def test_a_fresh_token_is_used_without_refreshing(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(500, {}))
    assert plan.get_access_token("alice") == ACCESS
    assert calls == []


def test_refresh_before_expiry_rotates_the_refresh_token(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    _expiring(plan)
    new_access = _jwt({"exp": int(time.time()) + 3600, "n": 2})
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(200, {
        "access_token": new_access, "refresh_token": "rt_rotated_0002", "expires_in": 3600,
        "earliest_refresh_at": int(time.time()) + 1800,
    }))
    assert plan.get_access_token("alice") == new_access
    form = calls[0]["data"]
    assert calls[0]["url"] == "https://auth.openai.com/api/accounts/oauth/token"
    assert form == {"grant_type": "refresh_token", "client_id": ISSUED_CLIENT,
                    "refresh_token": REFRESH, "resource": "https://api.openai.com/v1"}
    creds = plan.load_credentials("alice")
    assert creds["refresh_token"] == "rt_rotated_0002"
    assert creds["access_token"] == new_access
    assert creds["expires_at"] > time.time() + 3000


def test_earliest_refresh_at_is_respected_while_the_token_is_still_valid(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    _expiring(plan, in_seconds=200, earliest=int(time.time()) + 120)
    calls = _posts(monkeypatch, lambda url, data: FakeResponse(500, {}))
    assert plan.get_access_token("alice") == ACCESS
    assert calls == []


def test_concurrent_requests_refresh_once(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    _expiring(plan)
    new_access = _jwt({"exp": int(time.time()) + 3600, "n": 3})
    counter = {"n": 0}

    def responder(url, data):
        counter["n"] += 1
        time.sleep(0.2)
        return FakeResponse(200, {"access_token": new_access, "refresh_token": "rt_rotated_0003",
                                  "expires_in": 3600})

    _posts(monkeypatch, responder)
    results, errors = [], []

    def worker():
        try:
            results.append(plan.get_access_token("alice"))
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert counter["n"] == 1
    assert results == [new_access] * 8


@pytest.mark.parametrize("code", ["invalid_grant", "refresh_token_reused", "token_expired", "invalid_refresh_token"])
def test_a_dead_refresh_token_signs_the_user_out(plan, monkeypatch, code):
    _sign_in(plan, monkeypatch)
    plan.picker.models("alice", ["gpt-5.5"])
    _expiring(plan)
    _posts(monkeypatch, lambda url, data: FakeResponse(400, {"error": code}))
    with pytest.raises(cgp.ReauthRequired, match="Sign in again"):
        plan.get_access_token("alice")
    st = plan.status("alice")
    assert st["signed_in"] is False and st["needs_sign_in_again"] is True
    assert not plan._creds_path("alice").exists()
    assert "alice" not in plan.picker.rows
    with pytest.raises(cgp.NotSignedIn):
        plan.get_access_token("alice")


def test_a_network_failure_during_refresh_keeps_the_credentials(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    _expiring(plan)

    def boom(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(cgp.httpx, "post", boom)
    with pytest.raises(cgp.ChatGPTPlanError) as info:
        plan.get_access_token("alice")
    assert not isinstance(info.value, cgp.ReauthRequired)
    assert plan.status("alice")["signed_in"] is True


# ── Models ───────────────────────────────────────────────────────────────

def test_the_model_list_keeps_only_visibility_list(plan, monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"], seen["headers"] = url, headers
        return FakeResponse(200, {"models": [
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list"},
            {"slug": "gpt-5.5-mini", "display_name": "GPT-5.5 mini", "visibility": "list"},
            {"slug": "internal-x", "display_name": "Hidden", "visibility": "hide"},
            {"slug": "no-visibility"},
            {"display_name": "no slug", "visibility": "list"},
        ]})

    monkeypatch.setattr(cgp.httpx, "get", fake_get)
    models = plan.fetch_models(ACCESS)
    assert [m["slug"] for m in models] == ["gpt-5.5", "gpt-5.5-mini"]
    assert models[0]["display_name"] == "GPT-5.5"
    assert seen["url"] == "https://api.openai.com/v1/models"
    assert seen["headers"]["Authorization"] == f"Bearer {ACCESS}"


def test_refresh_models_shows_them_in_the_picker(plan, monkeypatch):
    _sign_in(plan, monkeypatch)
    monkeypatch.setattr(cgp.httpx, "get", lambda url, headers=None, timeout=None: FakeResponse(
        200, {"models": [{"slug": "gpt-5.5", "visibility": "list"}]}))
    out = plan.refresh_models("alice")
    assert [m["slug"] for m in out["models"]] == ["gpt-5.5"]
    assert plan.picker.rows == {"alice": ["gpt-5.5"]}
    assert plan.known_models("alice") == ["gpt-5.5"] and plan.known_models(None) == ["gpt-5.5"]


