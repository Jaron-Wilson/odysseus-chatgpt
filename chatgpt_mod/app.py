"""The ChatGPT add-on: Sign in with ChatGPT for Odysseus.

- The **ChatGPT** tab (``/``): each Odysseus user signs in to their own
  ChatGPT account (PKCE, with the paste-the-redirect fallback), refreshes
  their models and signs out.
- The model API (``/mod/cap/llm.provider/v1/``): an OpenAI-compatible
  ``models`` and ``chat/completions`` that Odysseus calls for the endpoint
  ``mod://chatgpt/v1``, signed for the chat's user, translated to the
  Responses API with that user's own tokens.

Run under Odysseus (it sets ODYSSEUS_URL), or standalone with
``python -m chatgpt_mod`` (asks for a password, one user: ``local``).
"""
from __future__ import annotations

import html as _html
import logging
from typing import Optional

import httpx

from chatgpt_mod import plan, translate
from odysseus_mod import CoreError, ModApp, StreamResponse, html, json_response

logger = logging.getLogger("chatgpt_mod")

MOD_ID = "chatgpt"
CAP = "/mod/cap/llm.provider/v1"
CORE_USER = "odysseus-core"
ENDPOINT_NAME = "ChatGPT"

app = ModApp(mod_id=MOD_ID)


def _user(req) -> Optional[str]:
    """Whose ChatGPT sign-in a request is for. The core's own calls (no user)
    have none."""
    u = req.user or ""
    return None if u == CORE_USER else u


# ── Odysseus's model picker ──────────────────────────────────────────────

def _register(user: str, slugs):
    try:
        app.core.register_models(user, slugs, ENDPOINT_NAME)
    except CoreError as e:
        logger.warning("Could not show the ChatGPT models in Odysseus: %s", e.detail)
    except Exception as e:
        logger.warning("Could not reach Odysseus to show the ChatGPT models: %s", type(e).__name__)


def _unregister(user: str):
    try:
        app.core.unregister_models(user)
    except Exception as e:
        logger.info("Could not remove the ChatGPT endpoint in Odysseus: %s", type(e).__name__)


plan.on_models = _register
plan.on_signed_out = _unregister


def _err(message: str, status: int, code: str = "") -> object:
    # type "add_on": Odysseus shows the message as written.
    return json_response({"error": {"type": "add_on", "message": message, "code": code}}, status)


def _plan_error(exc: Exception):
    if isinstance(exc, plan.ChatGPTPlanError):
        return _err(plan.redact(exc), exc.status)
    logger.error("ChatGPT request failed: %s", plan.redact(exc))
    return _err("ChatGPT hit an unexpected error.", 500)


# ── The tab ──────────────────────────────────────────────────────────────

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ChatGPT</title>
<style>
body{{font:14px system-ui,sans-serif;margin:0;padding:20px;background:#111;color:#ddd;max-width:720px}}
h1{{font-size:18px;margin:0 0 6px}} .sub{{opacity:.7;margin:0 0 14px;line-height:1.45}}
a{{color:inherit}} code{{font-size:12px;background:#1c1c1c;padding:1px 4px;border-radius:4px}}
button{{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid #444;background:#222;color:#eee;cursor:pointer}}
button.primary{{background:#2d6a4f;border-color:#2d6a4f}} button:disabled{{opacity:.5;cursor:default}}
.row{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}}
input{{font:inherit;flex:1;min-width:240px;padding:6px 8px;border-radius:6px;border:1px solid #444;background:#181818;color:#eee}}
#paste{{display:none;border-top:1px solid #2a2a2a;margin-top:12px;padding-top:10px}} ol{{margin:0 0 8px 18px;padding:0;line-height:1.5}}
#msg{{min-height:18px;margin-top:8px}} .err{{color:#f77}} #models{{opacity:.8;margin-top:8px}}
</style></head><body>
<h1>Sign in with ChatGPT</h1>
<p class="sub">Use OpenAI models on your ChatGPT Plus or Pro plan in Odysseus, no API key needed. Each Odysseus user
signs in to their own ChatGPT account. Usage counts against your plan's limits: see
<a href="https://chatgpt.com/settings/usage" target="_blank" rel="noopener noreferrer">ChatGPT Settings &gt; Usage</a>.</p>
<div id="status">Checking sign-in...</div>
<div class="row">
  <button class="primary" id="signin">Sign in with ChatGPT</button>
  <button id="refresh" style="display:none">Refresh models</button>
  <button id="signout" style="display:none">Sign out</button>
</div>
<div id="paste">
  <ol>
    <li>In the ChatGPT tab that opened (<a id="authlink" href="#" target="_blank" rel="noopener noreferrer">open it again</a>), sign in and approve Odysseus.</li>
    <li>ChatGPT then sends that tab to <code id="redir">{redirect}</code>, which won't load when Odysseus runs on another machine. That's expected.</li>
    <li>Copy the whole address from that tab's address bar and paste it here.</li>
  </ol>
  <div class="row"><input id="url" placeholder="{redirect}?code=...&amp;state=..." autocomplete="off" spellcheck="false">
  <button class="primary" id="finish">Finish sign-in</button></div>
  <div class="sub" id="auto" style="display:none">If this browser is on the Odysseus server itself, sign-in finishes on its own.</div>
</div>
<div id="models"></div>
<div id="msg"></div>
<script>
// Relative URLs only: the page is served under /mods/chatgpt/~token/ by Odysseus.
const $ = (id) => document.getElementById(id);
let poll = null;
async function call(path, body) {{
  const r = await fetch(path, body === undefined ? {{}} : {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(body)}});
  let d = {{}}; try {{ d = await r.json(); }} catch (_) {{}}
  if (!r.ok) throw new Error((d.error && d.error.message) || ('HTTP ' + r.status));
  return d;
}}
function msg(t, err) {{ $('msg').textContent = t || ''; $('msg').className = err ? 'err' : ''; }}
function models(list) {{
  $('models').textContent = list && list.length ? 'In the model picker under "ChatGPT": ' + list.join(', ') : '';
}}
function render(s) {{
  $('status').textContent = s.signed_in
    ? ('Signed in' + (s.email ? ' as ' + s.email : '') + '.' + (s.plan_usage_enabled ? '' : ' This account did not grant plan usage.'))
    : (s.needs_sign_in_again ? 'Your ChatGPT sign-in expired. Sign in again.' : 'Not signed in.');
  $('signin').textContent = s.signed_in ? 'Sign in again' : 'Sign in with ChatGPT';
  $('refresh').style.display = s.signed_in ? '' : 'none';
  $('signout').style.display = s.signed_in ? '' : 'none';
  if (s.signed_in) {{ $('paste').style.display = 'none'; if (poll) {{ clearInterval(poll); poll = null; }} }}
  models(s.models);
}}
async function refresh() {{ try {{ render(await call('api/status')); }} catch (e) {{ msg(e.message, true); }} }}
$('signin').onclick = async () => {{
  msg('');
  try {{
    const s = await call('api/sign-in/start', {{}});
    $('authlink').href = s.authorize_url; $('redir').textContent = s.redirect_uri;
    $('paste').style.display = 'block'; $('auto').style.display = s.listening ? '' : 'none';
    window.open(s.authorize_url, '_blank', 'noopener');
    if (poll) clearInterval(poll);
    const until = Date.now() + 600000;
    poll = setInterval(() => {{ if (Date.now() > until) {{ clearInterval(poll); poll = null; }} else refresh(); }}, 3000);
  }} catch (e) {{ msg(e.message, true); }}
}};
$('finish').onclick = async () => {{
  msg('Finishing sign-in...');
  try {{
    const s = await call('api/sign-in/complete', {{redirect_url: $('url').value}});
    $('url').value = ''; render(s);
    msg(s.models_error ? ('Signed in, but the models did not load: ' + s.models_error) : 'Signed in. Your ChatGPT models are in the model picker.', !!s.models_error);
  }} catch (e) {{ msg(e.message, true); }}
}};
$('refresh').onclick = async () => {{
  msg('Refreshing...');
  try {{ const d = await call('api/models/refresh', {{}}); render(d.status); msg('Models refreshed.'); }} catch (e) {{ msg(e.message, true); }}
}};
$('signout').onclick = async () => {{
  if (!confirm('Sign out of ChatGPT here? Your ChatGPT models leave the model picker.')) return;
  try {{ render(await call('api/sign-out', {{}})); msg('Signed out.'); }} catch (e) {{ msg(e.message, true); }}
}};
refresh();
</script></body></html>"""


@app.get("/")
def home(req):
    return html(PAGE.format(redirect=_html.escape(plan.REDIRECT_URI)))


@app.get("/api/status")
def api_status(req):
    return plan.status(_user(req))


@app.post("/api/sign-in/start")
def api_start(req):
    try:
        return plan.start_sign_in(_user(req))
    except Exception as exc:
        return _plan_error(exc)


@app.post("/api/sign-in/complete")
def api_complete(req):
    user = _user(req)
    body = req.json() if isinstance(req.json(), dict) else {}
    try:
        result = plan.complete_sign_in(body.get("redirect_url") or "", user=user)
    except Exception as exc:
        return _plan_error(exc)
    try:
        result["models"] = [m["slug"] for m in plan.refresh_models(user)["models"]]
    except plan.ChatGPTPlanError as exc:
        result["models"] = []
        result["models_error"] = plan.redact(exc)
    return result


@app.post("/api/models/refresh")
def api_refresh(req):
    user = _user(req)
    try:
        out = plan.refresh_models(user)
    except Exception as exc:
        return _plan_error(exc)
    return {"models": out["models"], "status": plan.status(user)}


@app.post("/api/sign-out")
def api_sign_out(req):
    try:
        return plan.sign_out(_user(req))
    except Exception as exc:
        return _plan_error(exc)


# ── The model API Odysseus calls (llm.provider) ──────────────────────────

@app.get(CAP + "/models")
def v1_models(req):
    user = _user(req)
    slugs = plan.known_models(user)
    if not slugs and user is not None:
        try:
            slugs = [m["slug"] for m in plan.refresh_models(user)["models"]]
        except Exception:
            slugs = []
    return {"object": "list", "data": [{"id": s, "object": "model", "owned_by": "openai"} for s in slugs]}


def _open_stream(token: str, payload):
    """The upstream Responses stream (a context manager), opened."""
    client = httpx.Client(timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0))
    cm = client.stream("POST", plan.RESPONSES_URL, json=payload, headers=plan.headers_for(token))
    r = cm.__enter__()
    return client, cm, r


@app.post(CAP + "/chat/completions")
def v1_chat(req):
    user = _user(req)
    body = req.json()
    if not isinstance(body, dict) or not body.get("model") or not isinstance(body.get("messages"), list):
        return _err("A chat completion needs a model and messages.", 400)
    if user is None:
        return _err(plan.NOT_SIGNED_IN, 401, "not_signed_in")
    try:
        token = plan.get_access_token(user)
    except Exception as exc:
        return _plan_error(exc)
    payload = translate.build_payload(body)
    try:
        client, cm, r = _open_stream(token, payload)
    except httpx.HTTPError as exc:
        return _err(f"Couldn't reach ChatGPT ({type(exc).__name__}).", 502)
    if r.status_code != 200:
        raw = r.read().decode(errors="replace")
        cm.__exit__(None, None, None)
        client.close()
        e = plan.error_from_http(r.status_code, raw)
        return _err(e["message"], r.status_code, e["code"])
    t = translate.ChunkTranslator(body["model"])

    def events():
        try:
            for event in translate.iter_sse_events(r.iter_lines()):
                for chunk in t.feed(event):
                    yield chunk
                if t.done:
                    return
            for chunk in t.finish():
                yield chunk
        except httpx.HTTPError as exc:
            for chunk in t.feed({"type": "error", "error": {"message": f"connection lost ({type(exc).__name__})"}}):
                yield chunk
        finally:
            cm.__exit__(None, None, None)
            client.close()

    if body.get("stream"):
        return StreamResponse(events())
    for _ in events():
        pass
    if t.error and not t.emitted_text and not t.calls:
        return _err(t.error["message"], t.error["status"], t.error["code"])
    return translate.completion(t)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app.run()
