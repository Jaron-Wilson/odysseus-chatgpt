"""odysseus_mod: a small helper for writing Odysseus mods. Standard library only.

    from odysseus_mod import ModApp, html

    app = ModApp()                      # reads the env the core gives a mod

    @app.get("/")
    def home(req):
        return html(f"<h1>Hello {req.user}</h1>")

    @app.tool("say_hi", "Say hi to someone", {"name": {"type": "string"}}, keywords=["hi"])
    def say_hi(req, args):
        return f"Hi {args.get('name', 'there')}"

    @app.on_event("*/print.finished")
    def finished(req, event):
        app.core.notify("Print done", event["data"].get("name", ""))

    app.run()

Under the core (ODYSSEUS_URL set) every request except /health must carry
a valid signed identity, and `req.user` is the signed-in Odysseus user.
Standalone (ODYSSEUS_URL unset) the app asks for a password instead
(MOD_PASSWORD, or one printed at start), and the core client keeps settings
and storage in MOD_DATA_DIR and prints notifications.

The contract this follows is docs/mods.md in the Odysseus repository.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

__all__ = ["ModApp", "CoreClient", "Request", "Response", "StreamResponse", "html", "json_response", "redirect",
           "sign_identity", "verify_identity", "CoreError", "SKEW_S"]
__version__ = "0.1.0"

SKEW_S = 300
CORE_USER = "odysseus-core"      # the identity of the core's own requests (tool list, events)


# ── Signed identity ──────────────────────────────────────────────────────

def sign_identity(token: str, mod_id: str, user: str, ts: Optional[int] = None) -> Dict[str, str]:
    """The headers the core adds to each request it forwards to a mod."""
    ts = int(ts if ts is not None else time.time())
    sig = hmac.new(token.encode(), f"v1\n{mod_id}\n{user}\n{ts}".encode(), hashlib.sha256).hexdigest()
    return {"X-Odysseus-User": user, "X-Odysseus-Mod": mod_id, "X-Odysseus-Ts": str(ts), "X-Odysseus-Sig": sig}


def verify_identity(token: str, headers, mod_id: Optional[str] = None, now: Optional[float] = None) -> Optional[str]:
    """The user named by valid identity headers, else None."""
    def h(name):
        return headers.get(name) or headers.get(name.lower())
    user, mid, ts, sig = h("X-Odysseus-User"), h("X-Odysseus-Mod"), h("X-Odysseus-Ts"), h("X-Odysseus-Sig")
    if not (token and user and mid and ts and sig) or (mod_id and mid != mod_id):
        return None
    try:
        t = int(ts)
    except ValueError:
        return None
    if abs((now if now is not None else time.time()) - t) > SKEW_S:
        return None
    want = sign_identity(token, mid, user, t)["X-Odysseus-Sig"]
    return user if hmac.compare_digest(want, sig) else None


# ── The core API ─────────────────────────────────────────────────────────

class CoreError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class CoreClient:
    """The mod API (/api/mods/v1). Standalone it falls back to local files
    and printing, so the same mod code runs with and without Odysseus."""

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, mod_id: Optional[str] = None,
                 data_dir: Optional[str] = None, timeout: float = 30):
        self.url = (url if url is not None else os.environ.get("ODYSSEUS_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("ODYSSEUS_MOD_TOKEN", "")
        self.mod_id = mod_id or os.environ.get("ODYSSEUS_MOD_ID", "")
        self.data_dir = data_dir or os.environ.get("MOD_DATA_DIR") or os.path.join(os.getcwd(), "data")
        self.timeout = timeout
        self._lock = threading.Lock()

    @property
    def standalone(self) -> bool:
        return not self.url

    def call(self, method: str, path: str, body: Any = None) -> Dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}/api/mods/v1/{path.lstrip('/')}", data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("detail") or e.reason
            except Exception:
                detail = e.reason
            raise CoreError(e.code, str(detail))

    # local fallbacks
    def _local(self, name: str) -> Dict[str, Any]:
        try:
            with open(os.path.join(self.data_dir, name), "r", encoding="utf-8") as f:
                v = json.load(f)
            return v if isinstance(v, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_local(self, name: str, data: Dict[str, Any]) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        p = os.path.join(self.data_dir, name)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(p + ".tmp", p)

    def me(self) -> Dict[str, Any]:
        if self.standalone:
            return {"id": self.mod_id, "permissions": [], "standalone": True}
        return self.call("GET", "me")

    def notify(self, title: str, body: str = "", level: str = "info", url: str = "", tag: str = "",
               user: str = "", image_url: str = "") -> Dict[str, Any]:
        """Send the user a notification; the core picks the channels."""
        if self.standalone:
            print(f"[notify:{level}] {title}{': ' + body if body else ''}", flush=True)
            return {"ok": True, "standalone": True}
        return self.call("POST", "notify", {"title": title, "body": body, "level": level, "url": url,
                                            "tag": tag, "user": user, "image_url": image_url})

    def emit(self, type: str, data: Any = None) -> Dict[str, Any]:
        """Publish an event (it must be listed in the manifest's events.emits)."""
        if self.standalone:
            return {"ok": True, "standalone": True, "delivered_to": []}
        return self.call("POST", "events", {"type": type, "data": data if data is not None else {}})

    def subscribe(self, pattern: str, path: Optional[str] = None) -> Dict[str, Any]:
        if self.standalone:
            return {"pattern": pattern, "standalone": True}
        return self.call("POST", "events/subscriptions", {"pattern": pattern, "path": path})

    def devices(self) -> List[Dict[str, Any]]:
        if self.standalone:
            return []
        return self.call("GET", "devices").get("devices", [])

    def device_command(self, name: str, command: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.standalone:
            raise CoreError(503, "no linked devices without Odysseus")
        return self.call("POST", f"devices/{urllib.request.quote(name)}/command",
                         {"command": command, "params": params or {}})

    def settings(self) -> Dict[str, Any]:
        if self.standalone:
            return self._local("settings.json")
        return self.call("GET", "settings").get("settings", {})

    def save_settings(self, values: Dict[str, Any]) -> None:
        if self.standalone:
            with self._lock:
                self._save_local("settings.json", values)
            return
        self.call("PUT", "settings", {"settings": values})

    def kv_get(self, key: str, default: Any = None) -> Any:
        if self.standalone:
            return self._local("kv.json").get(key, default)
        try:
            return self.call("GET", f"kv/{key}").get("value", default)
        except CoreError as e:
            if e.status == 404:
                return default
            raise

    def kv_put(self, key: str, value: Any) -> None:
        if self.standalone:
            with self._lock:
                d = self._local("kv.json")
                d[key] = value
                self._save_local("kv.json", d)
            return
        self.call("PUT", f"kv/{key}", {"value": value})

    def kv_delete(self, key: str) -> None:
        if self.standalone:
            with self._lock:
                d = self._local("kv.json")
                d.pop(key, None)
                self._save_local("kv.json", d)
            return
        self.call("DELETE", f"kv/{key}")

    def refresh_tools(self, wait: bool = False) -> Dict[str, Any]:
        """Tell the core the mod's tools changed (it was configured, a device
        showed up), so it reads GET /mod/tools again. By default the core
        does that in the background; wait=True waits for it and answers the
        new tool names (only from a server that handles requests in
        parallel, like ModApp's)."""
        if self.standalone:
            return {"ok": True, "standalone": True}
        return self.call("POST", "tools/refresh", {"wait": bool(wait)})

    def register_models(self, user: str, models: List[str], name: str = "") -> Dict[str, Any]:
        """Show this mod's models in `user`'s model picker (an llm.provider mod:
        one endpoint row, base URL mod://<id>/v1)."""
        if self.standalone:
            return {"ok": True, "standalone": True}
        return self.call("PUT", "llm/endpoint", {"user": user, "models": list(models), "name": name})

    def unregister_models(self, user: str) -> Dict[str, Any]:
        if self.standalone:
            return {"ok": True, "standalone": True}
        from urllib.parse import quote
        return self.call("DELETE", f"llm/endpoint?user={quote(user, safe='')}")

    @staticmethod
    def link(mod_id: str, tab: str, path: str = "") -> str:
        """A link that opens one of a mod's tabs inside Odysseus, optionally at
        a page under it: for a notification's `url`, a tool's output, or
        parent.postMessage({type: 'odysseus:open', link}) from a page."""
        from urllib.parse import quote
        return f"/#mod/{mod_id}/{tab}" + (f"?path={quote(path, safe='/')}" if path else "")

    def llm_chat(self, messages: List[Dict[str, str]], max_tokens: int = 1024, temperature: float = 0.3,
                 model_role: str = "utility", user: str = "") -> str:
        """Ask the user's configured model (their routing, their usage)."""
        if self.standalone:
            raise CoreError(503, "no model without Odysseus; set ODYSSEUS_URL or use your own")
        return self.call("POST", "llm/chat", {"messages": messages, "max_tokens": max_tokens,
                                              "temperature": temperature, "model_role": model_role,
                                              "user": user}).get("content", "")


# ── Requests and responses ───────────────────────────────────────────────

class Request:
    def __init__(self, handler: BaseHTTPRequestHandler, user: Optional[str], params: Dict[str, str]):
        parts = urlsplit(handler.path)
        self.method = handler.command
        self.path = parts.path
        self.query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.headers = handler.headers
        self.user = user
        self.params = params
        self.session_id = handler.headers.get("X-Odysseus-Session") or ""
        self.base_path = handler.headers.get("X-Forwarded-Prefix") or ""
        n = int(handler.headers.get("Content-Length") or 0)
        self.body = handler.rfile.read(n) if n else b""

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"{}")
        except json.JSONDecodeError:
            return {}

    def form(self) -> Dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.body.decode("utf-8", "replace")).items()}


class Response:
    def __init__(self, body: Any = b"", status: int = 200, content_type: str = "text/plain; charset=utf-8",
                 headers: Optional[List[Tuple[str, str]]] = None):
        self.body = body if isinstance(body, (bytes, bytearray)) else str(body).encode()
        self.status = status
        self.content_type = content_type
        self.headers = list(headers or [])


class StreamResponse(Response):
    """A body sent as it is made (server-sent events): `chunks` yields bytes
    or str. The connection closes at the end (HTTP/1.0, no Content-Length)."""

    def __init__(self, chunks, status: int = 200, content_type: str = "text/event-stream",
                 headers: Optional[List[Tuple[str, str]]] = None):
        super().__init__(b"", status, content_type, headers)
        self.chunks = chunks


def html(text: str, status: int = 200) -> Response:
    return Response(text, status, "text/html; charset=utf-8")


def json_response(obj: Any, status: int = 200) -> Response:
    return Response(json.dumps(obj, default=str), status, "application/json")


def redirect(location: str, status: int = 303) -> Response:
    """Use a relative location ("./", "settings"): the mod is served under a prefix."""
    return Response(b"", status, headers=[("Location", location)])


def _as_response(v: Any) -> Response:
    if isinstance(v, Response):
        return v
    if isinstance(v, (dict, list)):
        return json_response(v)
    if v is None:
        return Response(b"", 204)
    return Response(str(v))


# ── The app ──────────────────────────────────────────────────────────────

class ModApp:
    """A tiny HTTP app with the mod contract built in: /health, the signed
    identity check, /mod/tools, /mod/events, and the standalone login."""

    def __init__(self, mod_id: Optional[str] = None, core: Optional[CoreClient] = None,
                 public_paths: Tuple[str, ...] = ("/health",)):
        self.core = core or CoreClient(mod_id=mod_id)
        self.mod_id = self.core.mod_id or mod_id or ""
        self.public_paths = public_paths
        self._routes: List[Tuple[str, re.Pattern, Callable]] = []
        self._tools: Dict[str, Tuple[Dict[str, Any], Callable]] = {}
        self._events: List[Tuple[str, Callable]] = []
        self._sessions: set = set()
        self.password = None if not self.core.standalone else (os.environ.get("MOD_PASSWORD") or "")
        self.server: Optional[ThreadingHTTPServer] = None

    # registration
    def route(self, method: str, pattern: str):
        rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

        def deco(fn):
            self._routes.append((method.upper(), rx, fn))
            return fn
        return deco

    def get(self, pattern: str):
        return self.route("GET", pattern)

    def post(self, pattern: str):
        return self.route("POST", pattern)

    def delete(self, pattern: str):
        return self.route("DELETE", pattern)

    def tool(self, name: str, description: str, properties: Optional[Dict[str, Any]] = None,
             required: Optional[List[str]] = None, keywords: Optional[List[str]] = None,
             controls_devices: bool = False, read_only: bool = False, asks_first: bool = False):
        """Register an agent tool. The handler gets (req, args) and returns
        text for the model, or {"output": text, "data": {...}}."""
        schema = {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []}},
            "x-odysseus": {"keywords": keywords or [], "controls_devices": controls_devices, "read_only": read_only,
                            "asks_first": asks_first}}

        def deco(fn):
            self._tools[name] = (schema, fn)
            return fn
        return deco

    def on_event(self, pattern: str):
        """Handle events matching "<mod>/<type glob>" or "<type glob>" (they must
        also be in the manifest's events.subscribes, or subscribed at run time)."""
        def deco(fn):
            self._events.append((pattern, fn))
            return fn
        return deco

    def tool_schemas(self) -> List[Dict[str, Any]]:
        return [s for s, _ in self._tools.values()]

    # dispatch
    def _identity(self, handler) -> Optional[str]:
        if not self.core.standalone:
            return verify_identity(self.core.token, handler.headers, self.mod_id or None)
        c = SimpleCookie(handler.headers.get("Cookie") or "")
        sid = c.get("mod_session")
        return "local" if sid and sid.value in self._sessions else None

    def _login(self, handler, req: Optional[Request]) -> Response:
        if req is not None and req.method == "POST":
            if self.password and hmac.compare_digest(req.form().get("password", ""), self.password):
                sid = secrets.token_urlsafe(24)
                self._sessions.add(sid)
                return Response(b"", 303, headers=[("Location", "./"),
                                                   ("Set-Cookie", f"mod_session={sid}; HttpOnly; SameSite=Lax; Path=/")])
        return html("<!doctype html><meta charset=utf-8><body style='font:14px system-ui;padding:24px'>"
                    "<form method=post action=login><input type=password name=password placeholder=Password "
                    "autofocus> <button>Sign in</button></form>", 401 if req is None else 200)

    def handle(self, handler: BaseHTTPRequestHandler) -> Response:
        path = urlsplit(handler.path).path
        if path == "/health" and handler.command in ("GET", "HEAD"):
            return json_response({"ok": True})
        user = self._identity(handler)
        if path == "/login" and self.core.standalone:
            return self._login(handler, Request(handler, None, {}))
        if user is None and path not in self.public_paths:
            if self.core.standalone:
                return self._login(handler, None)
            return json_response({"error": "requests must come through Odysseus"}, 401)
        if path == "/mod/tools" and handler.command == "GET":
            return json_response({"tools": self.tool_schemas()})
        if path.startswith("/mod/tools/") and handler.command == "POST":
            name = path[len("/mod/tools/"):]
            if name not in self._tools:
                return json_response({"error": f"no tool {name}"}, 404)
            req = Request(handler, user, {})
            args = req.json()
            out = self._tools[name][1](req, args if isinstance(args, dict) else {})
            return json_response(out if isinstance(out, dict) else {"output": "" if out is None else str(out)})
        if path == "/mod/events" and handler.command == "POST":
            req = Request(handler, user, {})
            ev = req.json()
            import fnmatch
            for pattern, fn in self._events:
                src_pat, _, type_pat = pattern.rpartition("/") if "/" in pattern else ("*", "", pattern)
                if fnmatch.fnmatchcase(ev.get("source", ""), src_pat or "*") and \
                        fnmatch.fnmatchcase(ev.get("type", ""), type_pat):
                    fn(req, ev)
            return json_response({"ok": True})
        for method, rx, fn in self._routes:
            m = rx.match(path)
            if m and (method == handler.command or (method == "GET" and handler.command == "HEAD")):
                return _as_response(fn(Request(handler, user, m.groupdict())))
        return json_response({"error": "not found"}, 404)

    def make_server(self, host: Optional[str] = None, port: Optional[int] = None) -> ThreadingHTTPServer:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"odysseus-mod/{__version__}"

            def log_message(self, fmt, *args):
                sys.stdout.write(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

            def _go(self):
                try:
                    resp = app.handle(self)
                except Exception:
                    traceback.print_exc()
                    resp = json_response({"error": "internal error"}, 500)
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.content_type)
                if not isinstance(resp, StreamResponse):
                    self.send_header("Content-Length", str(len(resp.body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in resp.headers:
                    self.send_header(k, v)
                self.end_headers()
                if self.command == "HEAD":
                    return
                if isinstance(resp, StreamResponse):
                    try:
                        for chunk in resp.chunks:
                            self.wfile.write(chunk.encode() if isinstance(chunk, str) else chunk)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # the core stopped reading (the user pressed Stop)
                    finally:
                        close = getattr(resp.chunks, "close", None)
                        if close:
                            close()
                    return
                self.wfile.write(resp.body)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _go

        host = host or os.environ.get("MOD_HOST", "127.0.0.1")
        port = int(port if port is not None else os.environ.get("MOD_PORT") or 8765)
        srv = ThreadingHTTPServer((host, port), Handler)
        srv.daemon_threads = True
        self.server = srv
        return srv

    def run(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        if self.core.standalone and not self.password:
            self.password = secrets.token_urlsafe(9)
            print(f"Standalone mode (no ODYSSEUS_URL). Password: {self.password}", flush=True)
        srv = self.make_server(host, port)
        h, p = srv.server_address[:2]
        print(f"{self.mod_id or 'mod'} listening on http://{h}:{p}/ "
              f"({'standalone' if self.core.standalone else 'under Odysseus'})", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
