"""The add-on end to end: Odysseus calls /mod/cap/llm.provider/v1/ signed for a
user, the add-on uses that user's ChatGPT tokens against a fake OpenAI, and
the sign-in pages keep the model picker up to date through the core's mod
API. Nothing leaves the machine."""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from chatgpt_mod import app as mod_app
from chatgpt_mod import plan
from odysseus_mod import sign_identity
from conftest import CORE, TOKEN  # the instance pytest loaded

ACCESS = "eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.c2lnbmF0dXJlLWFjY2Vzcw"
CLIENT = "oaiapp_issued_client_123"


def _jwt(claims):
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{seg}.c2ln"


class FakeOpenAI:
    def __init__(self):
        self.calls = []
        self.nonce = ""
        self.responses_status = 200
        self.events = [
            {"type": "response.output_text.delta", "delta": "Hi "},
            {"type": "response.output_text.delta", "delta": "there"},
            {"type": "response.completed", "response": {"usage": {"input_tokens": 7, "output_tokens": 2}}},
        ]
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _send(self, status, obj, ctype="application/json"):
                data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.calls.append({"path": self.path, "auth": self.headers.get("Authorization")})
                if self.path == "/v1/models":
                    self._send(200, {"models": [{"slug": "gpt-5.5", "visibility": "list"},
                                                {"slug": "hidden", "visibility": "hide"}]})
                else:
                    self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                if self.path == "/api/accounts/oauth/token":
                    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    outer.calls.append({"path": self.path, "form": form})
                    self._send(200, {"access_token": ACCESS, "refresh_token": "rt_refresh_secret_0001",
                                     "id_token": _jwt({"email": "a@example.com", "nonce": outer.nonce}),
                                     "expires_in": 3600, "scope": plan.SCOPE})
                    return
                body = json.loads(raw or b"{}")
                outer.calls.append({"path": self.path, "json": body, "auth": self.headers.get("Authorization")})
                if outer.responses_status != 200:
                    self._send(outer.responses_status, {"error": {
                        "code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for e in outer.events:
                    self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
                    self.wfile.flush()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"


@pytest.fixture
def world(tmp_path, monkeypatch):
    oa = FakeOpenAI()
    monkeypatch.setattr(plan, "STORE_DIR", tmp_path / "data")
    monkeypatch.setattr(plan, "TOKEN_URL", oa.base + "/api/accounts/oauth/token")
    monkeypatch.setattr(plan, "MODELS_URL", oa.base + "/v1/models")
    monkeypatch.setattr(plan, "RESPONSES_URL", oa.base + "/v1/responses")
    plan._pending.clear()
    CORE.calls.clear()
    srv = mod_app.app.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, oa
    srv.shutdown()
    oa.srv.shutdown()


def _as(user):
    return sign_identity(TOKEN, "chatgpt", user)


def _sign_in(base, oa, user="alice"):
    start = httpx.post(base + "/api/sign-in/start", json={}, headers=_as(user)).json()
    q = parse_qs(urlparse(start["authorize_url"]).query)
    oa.nonce = q["nonce"][0]
    cb = f"http://127.0.0.1:1455/auth/callback?code=AUTHCODE&state={q['state'][0]}&client_id={CLIENT}"
    return httpx.post(base + "/api/sign-in/complete", json={"redirect_url": cb}, headers=_as(user))


def test_requests_must_come_through_odysseus(world):
    base, _ = world
    assert httpx.get(base + "/health").status_code == 200
    assert httpx.get(base + "/").status_code == 401
    assert httpx.post(base + "/mod/cap/llm.provider/v1/chat/completions", json={}).status_code == 401
    assert httpx.get(base + "/", headers=_as("alice")).status_code == 200


def test_sign_in_registers_the_models_in_odysseus(world):
    base, oa = world
    r = _sign_in(base, oa)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["signed_in"] and out["email"] == "a@example.com" and out["models"] == ["gpt-5.5"]
    put = [c for c in CORE.calls if c["method"] == "PUT"][-1]
    assert put["path"] == "/api/mods/v1/llm/endpoint" and put["auth"] == f"Bearer {TOKEN}"
    assert put["body"] == {"user": "alice", "models": ["gpt-5.5"], "name": "ChatGPT"}
    # The tokens are this user's only.
    assert httpx.get(base + "/api/status", headers=_as("bob")).json()["signed_in"] is False
    r = httpx.post(base + "/api/sign-out", json={}, headers=_as("alice"))
    assert r.json()["signed_in"] is False
    dele = [c for c in CORE.calls if c["method"] == "DELETE"][-1]
    assert dele["path"] == "/api/mods/v1/llm/endpoint?user=alice"


def test_models_for_a_user_and_for_the_core(world):
    base, oa = world
    _sign_in(base, oa)
    got = httpx.get(base + "/mod/cap/llm.provider/v1/models", headers=_as("alice")).json()
    assert [m["id"] for m in got["data"]] == ["gpt-5.5"]
    core = httpx.get(base + "/mod/cap/llm.provider/v1/models", headers=_as("odysseus-core")).json()
    assert [m["id"] for m in core["data"]] == ["gpt-5.5"]


def test_streamed_chat_uses_the_users_token(world):
    base, oa = world
    _sign_in(base, oa)
    body = {"model": "gpt-5.5", "stream": True, "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]}
    with httpx.stream("POST", base + "/mod/cap/llm.provider/v1/chat/completions", json=body,
                      headers=_as("alice")) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        lines = [ln[6:] for ln in r.iter_lines() if ln.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    text = "".join((json.loads(x)["choices"][0]["delta"].get("content") or "")
                   for x in lines[:-1] if json.loads(x)["choices"])
    assert text == "Hi there"
    assert json.loads(lines[-2])["usage"]["prompt_tokens"] == 7
    sent = [c for c in oa.calls if c["path"] == "/v1/responses"][-1]
    assert sent["auth"] == f"Bearer {ACCESS}"
    assert sent["json"]["store"] is False and sent["json"]["stream"] is True
    assert sent["json"]["tools"][0]["name"] == "t"


def test_plain_chat_and_errors(world):
    base, oa = world
    url = base + "/mod/cap/llm.provider/v1/chat/completions"
    body = {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]}
    # Not signed in: an add_on error Odysseus shows as written.
    r = httpx.post(url, json=body, headers=_as("alice"))
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "add_on" and "ChatGPT tab" in r.json()["error"]["message"]
    r = httpx.post(url, json=body, headers=_as("odysseus-core"))
    assert r.status_code == 401
    _sign_in(base, oa)
    r = httpx.post(url, json=body, headers=_as("alice"))
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Hi there"
    oa.responses_status = 429
    r = httpx.post(url, json=body, headers=_as("alice"))
    assert r.status_code == 429 and "usage limit" in r.json()["error"]["message"]
    assert httpx.post(url, json={"messages": []}, headers=_as("alice")).status_code == 400


def test_local_is_the_no_owner_user():
    assert plan.user_key("local") == plan.user_key("") == plan.user_key(None)
    assert plan.user_key("alice") != plan.user_key("local")
