"""A stand-in Odysseus core for building a mod without Odysseus.

    python -m odysseus_mod.devcore                 # runs the mod from ./odysseus-mod.json
    python -m odysseus_mod.devcore --port 7900 --user alice
    python -m odysseus_mod.devcore --no-start --mod-port 8765   # a mod you started yourself

It does what the real core does for a mod, minus the real channels:

  - starts the mod's runtime.command with the same environment variables
    (ODYSSEUS_URL, ODYSSEUS_MOD_TOKEN, ODYSSEUS_MOD_ID, ODYSSEUS_MOD_BASE,
    MOD_PORT, MOD_DATA_DIR), restarting nothing: it is a dev tool;
  - serves the mod API at /api/mods/v1/... with the manifest's permissions
    enforced: notify and events are printed and listed, settings and storage
    are kept in .devcore/, llm/chat answers with a canned reply (or calls an
    OpenAI-compatible server if DEVCORE_LLM_URL is set), devices are fake;
  - proxies the mod at /mods/<id>/... with signed identity headers, as user
    --user, streaming the response through;
  - delivers events the mod emits back to its own webhook when it subscribed
    to them (so an emit/subscribe round trip can be tried alone);
  - shows a page at / with the mod's tabs and cards, a tool tester, and the
    notifications and events so far.

No WebSocket proxying: open the mod's own port for that while developing.
"""
from __future__ import annotations

import argparse
import fnmatch
import html
import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from odysseus_mod import CORE_USER, sign_identity

PERMS_FOR = {
    ("POST", "notify"): "notify", ("POST", "events"): "events.emit",
    ("GET", "events/subscriptions"): "events.subscribe", ("POST", "events/subscriptions"): "events.subscribe",
    ("DELETE", "events/subscriptions"): "events.subscribe", ("GET", "devices"): "devices.read",
    ("POST", "llm/chat"): "llm.call",
}
FAKE_DEVICES = [{"name": "dev-phone", "kind": "phone", "commands": ["notify", "open_url"], "online": True}]


def tutorial_problems(manifest: Dict[str, Any], mod_dir: str = ".") -> List[str]:
    """What Odysseus would say about the manifest's `tutorial` (a rough copy
    of its checks; `python -m src.mods validate` is the real one). Every mod
    needs one: it is shown the first time someone opens the mod."""
    tut = manifest.get("tutorial")
    if not isinstance(tut, dict):
        return ["no `tutorial`: every mod needs one (shown the first time someone opens it), "
                "and Odysseus will not list it as Verified without one"]
    out = []
    steps = tut.get("steps")
    if steps is None and not tut.get("path"):
        out.append("`tutorial` needs `steps` or a `path`")
    for i, s in enumerate(steps or []):
        if not isinstance(s, dict) or not str(s.get("title") or "").strip() or not str(s.get("body") or "").strip():
            out.append(f"`tutorial.steps[{i}]` needs a `title` and a `body`")
        elif s.get("image") and not os.path.isfile(os.path.join(mod_dir, str(s["image"]))):
            out.append(f"`tutorial.steps[{i}].image` {s['image']!r} is not a file in the mod")
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DevCore:
    def __init__(self, mod_dir: str = ".", port: int = 0, user: str = "dev", mod_port: Optional[int] = None,
                 state_dir: Optional[str] = None):
        with open(os.path.join(mod_dir, "odysseus-mod.json"), "r", encoding="utf-8") as f:
            self.manifest = json.load(f)
        self.mod_dir = os.path.abspath(mod_dir)
        self.mod_id = self.manifest["id"]
        self.perms = set(self.manifest.get("permissions") or [])
        self.user = user
        self.token = "odm_dev_" + secrets.token_urlsafe(16)
        self.mod_port = mod_port or free_port()
        self.state_dir = state_dir or os.path.join(self.mod_dir, ".devcore")
        os.makedirs(self.state_dir, exist_ok=True)
        self.notifications: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.subs: List[Dict[str, str]] = [{"pattern": p, "path": (self.manifest.get("events") or {}).get(
            "webhook", "/mod/events")} for p in (self.manifest.get("events") or {}).get("subscribes", [])]
        self.tool_refreshes = 0
        self.tools_seen: List[str] = []
        self.warnings = tutorial_problems(self.manifest, mod_dir)
        for w in self.warnings:
            print(f"[devcore] warning: {w}", flush=True)
        self.proc: Optional[subprocess.Popen] = None
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    # ── state ──
    def _file(self, name):
        return os.path.join(self.state_dir, name)

    def _load(self, name) -> Dict[str, Any]:
        try:
            with open(self._file(name), "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, name, data):
        with open(self._file(name), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.update((self.manifest.get("runtime") or {}).get("env") or {})
        env.update({"ODYSSEUS_URL": self.url, "ODYSSEUS_MOD_TOKEN": self.token, "ODYSSEUS_MOD_ID": self.mod_id,
                    "ODYSSEUS_MOD_BASE": f"/mods/{self.mod_id}", "MOD_PORT": str(self.mod_port),
                    "MOD_HOST": "127.0.0.1", "MOD_DATA_DIR": os.path.join(self.state_dir, "data"),
                    "PYTHONUNBUFFERED": "1"})
        os.makedirs(env["MOD_DATA_DIR"], exist_ok=True)
        return env

    # ── the mod process ──
    def start_mod(self, wait: float = 20) -> None:
        cmd = list((self.manifest.get("runtime") or {}).get("command") or [])
        if not cmd:
            raise SystemExit("runtime.command is empty; is this a docker mod? Start it yourself with --no-start")
        if cmd[0] in ("python", "python3"):
            venv = os.path.join(self.mod_dir, ".venv", "bin", "python")
            cmd[0] = venv if os.path.exists(venv) else sys.executable
        self.proc = subprocess.Popen(cmd, cwd=self.mod_dir, env=self.env(), start_new_session=True)
        health = (self.manifest.get("runtime") or {}).get("health", "/health")
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"the mod exited with code {self.proc.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.mod_port}{health}", timeout=1) as r:
                    if r.status < 500:
                        return
            except Exception:
                time.sleep(0.2)
        raise SystemExit(f"the mod did not answer {health} within {wait:.0f}s")

    def stop_mod(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(5)
            except Exception:
                self.proc.kill()

    # ── calling the mod ──
    def mod_request(self, method: str, path: str, body: Optional[bytes] = None, user: Optional[str] = None,
                    headers: Optional[Dict[str, str]] = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.mod_port, timeout=120)
        h = dict(headers or {})
        h.update(sign_identity(self.token, self.mod_id, user or self.user))
        conn.request(method, path, body=body, headers=h)
        return conn, conn.getresponse()

    def call_tool(self, name: str, args: Dict[str, Any], user: Optional[str] = None) -> Dict[str, Any]:
        endpoint = ((self.manifest.get("tools") or {}).get("endpoint") or "/mod/tools").rstrip("/")
        conn, r = self.mod_request("POST", f"{endpoint}/{name}", json.dumps(args).encode(), user,
                                   {"Content-Type": "application/json"})
        try:
            return json.loads(r.read() or b"{}")
        finally:
            conn.close()

    def list_tools(self) -> List[Dict[str, Any]]:
        endpoint = (self.manifest.get("tools") or {}).get("endpoint") or "/mod/tools"
        conn, r = self.mod_request("GET", endpoint, user=CORE_USER)
        try:
            data = json.loads(r.read() or b"{}")
        finally:
            conn.close()
        return data.get("tools", data) if isinstance(data, dict) else data

    def _deliver(self, event: Dict[str, Any]) -> None:
        for s in self.subs:
            pat = s["pattern"]
            src, _, typ = pat.rpartition("/") if "/" in pat else ("*", "", pat)
            if fnmatch.fnmatchcase(event["source"], src or "*") and fnmatch.fnmatchcase(event["type"], typ):
                try:
                    conn, r = self.mod_request("POST", s["path"], json.dumps(event).encode(), CORE_USER,
                                               {"Content-Type": "application/json"})
                    r.read()
                    conn.close()
                except Exception as e:
                    print(f"[devcore] event delivery to {s['path']} failed: {e}", flush=True)

    # ── the mod API ──
    def api(self, method: str, path: str, body: Dict[str, Any]):
        perm = PERMS_FOR.get((method, path))
        if path.startswith("kv"):
            perm = "storage"
        if path.startswith("devices/") and path.endswith("/command"):
            perm = "devices.control"
        if perm and perm not in self.perms:
            return 403, {"detail": f"This mod was not granted the {perm} permission"}
        if path == "me":
            return 200, {"id": self.mod_id, "permissions": sorted(self.perms), "core_api": "0.1",
                         "base_path": f"/mods/{self.mod_id}", "devcore": True}
        if path == "notify":
            n = dict(body, ts=time.time())
            self.notifications.append(n)
            print(f"[devcore] notify ({body.get('level', 'info')}): {body.get('title')} {body.get('body', '')}",
                  flush=True)
            return 200, {"ok": True, "channels": {"devcore": "printed"}}
        if path == "events":
            etype = body.get("type", "")
            if etype not in ((self.manifest.get("events") or {}).get("emits") or []):
                return 400, {"detail": f"{etype!r} is not in this mod's events.emits"}
            ev = {"id": secrets.token_hex(8), "source": self.mod_id, "type": etype, "data": body.get("data") or {},
                  "ts": time.time()}
            self.events.append(ev)
            print(f"[devcore] event {etype}: {json.dumps(ev['data'])[:200]}", flush=True)
            threading.Thread(target=self._deliver, args=(ev,), daemon=True).start()
            return 200, {"ok": True, "id": ev["id"], "delivered_to": []}
        if path == "events/subscriptions":
            if method == "POST":
                self.subs.append({"pattern": body.get("pattern", ""),
                                  "path": body.get("path") or "/mod/events"})
                return 200, {"pattern": body.get("pattern"), "path": body.get("path") or ""}
            return 200, {"subscriptions": self.subs}
        if path == "tools/refresh" and method == "POST":
            if not self.manifest.get("tools"):
                return 400, {"detail": "This mod has no `tools` in its manifest"}
            self.tool_refreshes += 1

            def reread():
                try:
                    self.tools_seen = [t.get("function", {}).get("name", "") for t in self.list_tools()]
                except Exception as e:
                    print(f"[devcore] could not read the tool list: {e}", flush=True)

            if body.get("wait") is True:
                reread()
                return 200, {"ok": True, "tools": self.tools_seen}
            threading.Thread(target=reread, daemon=True).start()
            return 200, {"ok": True, "queued": True}
        if path == "devices":
            return 200, {"devices": FAKE_DEVICES}
        if path.startswith("devices/"):
            print(f"[devcore] device command {body}", flush=True)
            return 200, {"ok": True, "devcore": True}
        if path == "settings":
            if method == "PUT":
                self._save("settings.json", body.get("settings", body))
                return 200, {"ok": True}
            return 200, {"settings": self._load("settings.json")}
        if path == "kv":
            return 200, {"keys": sorted(self._load("kv.json"))}
        if path.startswith("kv/"):
            key = path[3:]
            kv = self._load("kv.json")
            if method == "PUT":
                kv[key] = body.get("value")
                self._save("kv.json", kv)
                return 200, {"ok": True}
            if method == "DELETE":
                existed = kv.pop(key, None) is not None
                self._save("kv.json", kv)
                return 200, {"ok": True, "existed": existed}
            if key not in kv:
                return 404, {"detail": "No such key"}
            return 200, {"key": key, "value": kv[key]}
        if path == "llm/chat":
            url = os.environ.get("DEVCORE_LLM_URL")
            if url:
                req = urllib.request.Request(url.rstrip("/") + "/chat/completions", method="POST",
                                             data=json.dumps({"model": os.environ.get("DEVCORE_LLM_MODEL", ""),
                                                              "messages": body.get("messages")}).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    out = json.loads(r.read())
                return 200, {"content": out["choices"][0]["message"]["content"], "model": out.get("model")}
            last = next((m.get("content", "") for m in reversed(body.get("messages") or []) if m.get("role") == "user"), "")
            return 200, {"content": f"(devcore has no model) You said: {last[:200]}", "model": "devcore"}
        return 404, {"detail": "Not found"}

    def index_page(self) -> str:
        ui = self.manifest.get("ui") or {}
        base = f"/mods/{self.mod_id}"
        links = "".join(f"<li><a href='{base}{html.escape(t.get('path', '/'))}'>{html.escape(t.get('title', t['id']))}"
                        "</a> (tab)</li>" for t in ui.get("tabs", []))
        cards = "".join(f"<div style='border:1px solid #444;border-radius:8px;margin:8px 0;width:360px'>"
                        f"<iframe src='{base}{html.escape(c.get('path', '/'))}' style='border:0;width:100%;"
                        f"height:{int(c.get('height', 180))}px' sandbox='allow-scripts allow-forms'></iframe></div>"
                        for c in ui.get("home_cards", []))
        notes = "".join(f"<li>[{html.escape(n.get('level', 'info'))}] {html.escape(str(n.get('title')))}: "
                        f"{html.escape(str(n.get('body', '')))}</li>" for n in reversed(self.notifications[-20:]))
        evs = "".join(f"<li>{html.escape(e['type'])} {html.escape(json.dumps(e['data'])[:120])}</li>"
                      for e in reversed(self.events[-20:]))
        return f"""<!doctype html><meta charset=utf-8><title>devcore: {html.escape(self.mod_id)}</title>
<body style="font:14px system-ui;background:#111;color:#ddd;padding:20px;max-width:900px">
<h1 style="font-size:18px">devcore for {html.escape(self.manifest.get('name', self.mod_id))}</h1>
<p>Signed in as <code>{html.escape(self.user)}</code>. Mod on port {self.mod_port}.</p>
<h2 style="font-size:15px">Pages</h2><ul>{links or '<li>No tabs</li>'}</ul>
<h2 style="font-size:15px">Home cards</h2>{cards or '<p>No cards</p>'}
<h2 style="font-size:15px">Try a tool</h2>
<form onsubmit="event.preventDefault();fetch('/devcore/tools/'+this.n.value,{{method:'POST',body:this.a.value}})
.then(r=>r.text()).then(t=>this.o.textContent=t)"><input name=n placeholder="tool name">
<input name=a value="{{}}" size=40><button>Call</button><pre name=o id=o></pre></form>
<h2 style="font-size:15px">Notifications</h2><ul>{notes or '<li>None</li>'}</ul>
<h2 style="font-size:15px">Events</h2><ul>{evs or '<li>None</li>'}</ul>"""

    def _handler(self):
        core = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, code, obj, ctype="application/json"):
                b = obj if isinstance(obj, bytes) else (obj.encode() if isinstance(obj, str) else json.dumps(obj).encode())
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _go(self):
                path = self.path
                if path.startswith("/api/mods/v1/"):
                    if self.headers.get("Authorization") != f"Bearer {core.token}":
                        return self._reply(401, {"detail": "Invalid or missing mod token"})
                    try:
                        body = json.loads(self._body() or b"{}")
                    except json.JSONDecodeError:
                        return self._reply(400, {"detail": "Expected JSON"})
                    sub = path[len("/api/mods/v1/"):].split("?", 1)[0]
                    code, out = core.api(self.command, sub, body if isinstance(body, dict) else {})
                    return self._reply(code, out)
                if path == "/" and self.command == "GET":
                    return self._reply(200, core.index_page(), "text/html; charset=utf-8")
                if path.startswith("/devcore/tools/") and self.command == "POST":
                    try:
                        args = json.loads(self._body() or b"{}")
                    except json.JSONDecodeError:
                        args = {}
                    return self._reply(200, core.call_tool(path.rsplit("/", 1)[-1], args))
                prefix = f"/mods/{core.mod_id}"
                if path == prefix:
                    self.send_response(307)
                    self.send_header("Location", prefix + "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path.startswith(prefix + "/"):
                    return self._proxy(path[len(prefix):], prefix)
                return self._reply(404, {"detail": "Not found"})

            def _proxy(self, upath, prefix):
                body = self._body() if self.command not in ("GET", "HEAD") else None
                keep = {k: v for k, v in self.headers.items()
                        if k.lower() not in ("host", "connection", "content-length", "authorization")
                        and not k.lower().startswith("x-odysseus")}
                keep["X-Forwarded-Prefix"] = prefix
                try:
                    conn, r = core.mod_request(self.command, upath, body, None, keep)
                except Exception as e:
                    return self._reply(502, {"detail": f"the mod did not answer: {e}"})
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    if k.lower() == "location" and v.startswith("/") and not v.startswith(prefix):
                        v = prefix + v
                    self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    while True:
                        chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    conn.close()

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _go

        return H

    def serve(self) -> threading.Thread:
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        return t

    def close(self) -> None:
        self.stop_mod()
        self.server.shutdown()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m odysseus_mod.devcore", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=".", help="the mod folder (with odysseus-mod.json)")
    ap.add_argument("--port", type=int, default=7900, help="devcore's port")
    ap.add_argument("--user", default="dev", help="the signed-in user the mod sees")
    ap.add_argument("--mod-port", type=int, default=0, help="the mod's port (default: a free one)")
    ap.add_argument("--no-start", action="store_true", help="do not start the mod; it is already running")
    a = ap.parse_args(argv)
    core = DevCore(a.dir, a.port, a.user, a.mod_port or None)
    core.serve()
    if a.no_start:
        print(f"Start your mod with:\n  ODYSSEUS_URL={core.url} ODYSSEUS_MOD_TOKEN={core.token} "
              f"ODYSSEUS_MOD_ID={core.mod_id} MOD_PORT={core.mod_port} <command>", flush=True)
    else:
        core.start_mod()
    print(f"devcore: open {core.url}/ (the mod at {core.url}/mods/{core.mod_id}/). Ctrl+C stops both.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        core.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
