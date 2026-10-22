"""The add-on runs under a fake Odysseus core in these tests (the env the core
gives a mod), so the signed identity is checked like in production. Nothing
leaves the machine: OpenAI is a local fake server too."""
import json
import os
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TOKEN = "odm_test_token_for_the_chatgpt_mod"


class FakeCore:
    """The core's mod API: records PUT/DELETE /api/mods/v1/llm/endpoint."""

    def __init__(self):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else None
                outer.calls.append({"method": self.command, "path": self.path, "body": body,
                                    "auth": self.headers.get("Authorization")})
                data = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_PUT = do_POST = do_DELETE = _go

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


CORE = FakeCore()
os.environ["ODYSSEUS_URL"] = CORE.url
os.environ["ODYSSEUS_MOD_TOKEN"] = TOKEN
os.environ["ODYSSEUS_MOD_ID"] = "chatgpt"
os.environ["MOD_DATA_DIR"] = tempfile.mkdtemp(prefix="chatgpt-mod-test-")
os.environ["CHATGPT_MOD_LOOPBACK_LISTENER"] = "0"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
