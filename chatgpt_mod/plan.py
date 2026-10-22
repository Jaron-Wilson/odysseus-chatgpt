"""Sign in with ChatGPT: use a ChatGPT Plus/Pro plan for OpenAI models.

This is OpenAI's official "ChatGPT plan usage" path for open-source and
locally hosted apps (https://developers.openai.com/siwc/token-sharing-open-source).
It used to live in the Odysseus core (src/chatgpt_plan.py); it is this
add-on now, and the core reaches it as the model endpoint mod://chatgpt/v1.

Flow, per the docs:

* one ``ext_agent_host_id`` (``urn:uuid:<v4>``) per install, persisted;
* browser authorization at auth.openai.com with PKCE (S256), ``state`` and
  ``nonce``, a loopback ``redirect_uri`` and ``client_id=dynamic_agent_client``
  on the first sign-in. The callback carries the issued ``client_id``, which
  is saved and used for every later sign-in and refresh;
* code exchange and refresh at the token endpoint (no client secret). The
  refresh token rotates, so both tokens are stored after every refresh;
* inference only through ``POST /v1/responses`` with ``store: false`` and
  ``stream: true``; the model list comes from ``GET /v1/models``.

Odysseus usually runs on a server reached over a private network, so the
loopback redirect lands on whatever device the browser is on and fails to
load. The user copies that address-bar URL back into the ChatGPT tab
(``complete_sign_in``). When the browser is on the server itself, a
short-lived listener on 127.0.0.1 catches the callback (``_LoopbackListener``).

Credentials are per Odysseus user (the signed X-Odysseus-User; ``local``
with sign-in turned off), in ``<MOD_DATA_DIR>/users/`` as 0600 JSON files
whose token values are Fernet-encrypted with this add-on's own key
(``<MOD_DATA_DIR>/.key``, 0600). The file names are the same as the core's
were (a hash of the user name), so the migration is a copy. Tokens are never
logged; ``redact`` scrubs them from error text.

Tests point the OpenAI addresses at a fake server with
CHATGPT_MOD_AUTH_ISSUER and CHATGPT_MOD_API_BASE.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

AUTH_ISSUER = (os.getenv("CHATGPT_MOD_AUTH_ISSUER") or "https://auth.openai.com").rstrip("/")
AUTHORIZE_URL = f"{AUTH_ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{AUTH_ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
API_BASE = (os.getenv("CHATGPT_MOD_API_BASE") or "https://api.openai.com/v1").rstrip("/")
RESPONSES_URL = f"{API_BASE}/responses"
MODELS_URL = f"{API_BASE}/models"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
AGENT_NAME = "Odysseus"
USAGE_SETTINGS_URL = "https://chatgpt.com/settings/usage"

LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = int(os.getenv("CHATGPT_MOD_LOOPBACK_PORT", "1455") or 1455)
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = f"http://{LOOPBACK_HOST}:{LOOPBACK_PORT}{CALLBACK_PATH}"

PENDING_TTL_SECONDS = 600
REFRESH_SKEW_SECONDS = 300
# Refresh-token errors that mean the token set is dead (errors-and-recovery).
_DEAD_REFRESH_CODES = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_reused"}

# The identities that mean "the one user of an install with sign-in off".
LOCAL_USERS = ("", "local")

NOT_SIGNED_IN = "Sign in with ChatGPT on the ChatGPT tab to use ChatGPT models."
SIGN_IN_AGAIN = "Your ChatGPT sign-in expired. Sign in again on the ChatGPT tab."
USAGE_LIMIT_MESSAGE = (
    "Your ChatGPT plan's usage limit was reached for now. "
    f"Check ChatGPT Settings > Usage ({USAGE_SETTINGS_URL}) or try again later."
)
_ERROR_MESSAGES = {
    "subscription_sharing_usage_limit_exceeded": USAGE_LIMIT_MESSAGE,
    "subscription_sharing_usage_unavailable": (
        "ChatGPT couldn't check your plan's usage right now. Try again in a moment."
    ),
    "subscription_sharing_user_not_eligible": (
        "This ChatGPT account or workspace isn't eligible to use its plan in other apps."
    ),
    "subscription_sharing_unsupported_capability": (
        "ChatGPT plan usage doesn't support a feature this request used."
    ),
    "subscription_sharing_invalid_user": SIGN_IN_AGAIN,
}


class ChatGPTPlanError(RuntimeError):
    """A ChatGPT plan failure whose message is safe to show the user."""

    status = 502


class NotSignedIn(ChatGPTPlanError):
    status = 401


class ReauthRequired(ChatGPTPlanError):
    status = 401


class SignInRejected(ChatGPTPlanError):
    status = 400


# What happens to the user's model endpoint in Odysseus (set by app.py, which
# calls the core; no-ops when standalone and in tests).
on_models: Callable[[str, List[str]], None] = lambda user, slugs: None
on_signed_out: Callable[[str], None] = lambda user: None


# ── Redaction ─────────────────────────────────────────────────────────────

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]*)?")
_KV_RE = re.compile(
    r"(?i)((?:access_token|refresh_token|id_token|code_verifier|code|authorization)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)"
    r"([^\s\"'&,}]+)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]{8,})")


def redact(text: Any, *secrets_: Optional[str]) -> str:
    """Scrub tokens from text that might end up in a log or an error."""
    out = "" if text is None else str(text)
    for value in secrets_:
        if value and len(value) >= 6:
            out = out.replace(value, "[redacted]")
    out = _JWT_RE.sub("[redacted]", out)
    out = _BEARER_RE.sub(lambda m: m.group(1) + "[redacted]", out)
    out = _KV_RE.sub(lambda m: m.group(1) + "[redacted]", out)
    return out


# ── Storage ───────────────────────────────────────────────────────────────

STORE_DIR: Optional[Path] = None  # tests point this at a temp dir


def _store_dir() -> Path:
    if STORE_DIR:
        return Path(STORE_DIR)
    return Path(os.environ.get("MOD_DATA_DIR") or os.path.join(os.getcwd(), "data"))


def user_key(user: Optional[str]) -> str:
    """The file name part for a user. "local" (sign-in off) is the core's old
    no-owner key, so credentials moved from the core keep working."""
    name = "" if (user or "") in LOCAL_USERS else user
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]


def _creds_path(user: Optional[str]) -> Path:
    return _store_dir() / "users" / f"{user_key(user)}.json"


def _registration_path(user: Optional[str]) -> Path:
    return _store_dir() / "users" / f"{user_key(user)}.client.json"


def write_private(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write JSON readable only by the owner (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("ChatGPT: unreadable credential file %s", path.name)
        return {}
    return data if isinstance(data, dict) else {}


def host_id() -> str:
    """This install's ext_agent_host_id, created once and kept."""
    path = _store_dir() / "host.json"
    data = _read_json(path)
    value = data.get("ext_agent_host_id")
    if isinstance(value, str) and value.startswith("urn:uuid:"):
        return value
    value = f"urn:uuid:{uuid.uuid4()}"
    write_private(path, {"ext_agent_host_id": value})
    return value


_TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")
_ENC_PREFIX = "enc:"
_fernet = None
_fernet_dir: Optional[str] = None


def _get_fernet():
    """This add-on's own Fernet key, made on first use (0600)."""
    global _fernet, _fernet_dir
    from cryptography.fernet import Fernet

    d = str(_store_dir())
    if _fernet is None or _fernet_dir != d:
        path = Path(d) / ".key"
        if path.exists():
            key = path.read_bytes().strip()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key)
        _fernet, _fernet_dir = Fernet(key), d
    return _fernet


def encrypt(value: str) -> str:
    if not value or value.startswith(_ENC_PREFIX):
        return value or ""
    return _ENC_PREFIX + _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    from cryptography.fernet import InvalidToken

    if not value or not value.startswith(_ENC_PREFIX):
        return value or ""
    try:
        return _get_fernet().decrypt(value[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("ChatGPT: a stored token could not be decrypted (wrong key or corrupt file)")
        return ""


def load_credentials(user: Optional[str]) -> Dict[str, Any]:
    raw = _read_json(_creds_path(user))
    if not raw:
        return {}
    out = dict(raw)
    for field in _TOKEN_FIELDS:
        out[field] = decrypt(raw.get(field) or "")
    return out


def save_credentials(user: Optional[str], creds: Dict[str, Any]) -> None:
    stored = dict(creds)
    for field in _TOKEN_FIELDS:
        stored[field] = encrypt(creds.get(field) or "")
    write_private(_creds_path(user), stored)


def load_registration(user: Optional[str]) -> Dict[str, Any]:
    return _read_json(_registration_path(user))


def save_registration(user: Optional[str], **fields: Any) -> None:
    reg = load_registration(user)
    reg.update({k: v for k, v in fields.items()})
    write_private(_registration_path(user), reg)


def delete_credentials(user: Optional[str]) -> bool:
    try:
        _creds_path(user).unlink()
        return True
    except FileNotFoundError:
        return False


# ── PKCE / authorize URL ──────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_code_verifier() -> str:
    return _b64url(secrets.token_bytes(48))  # 64 chars, within RFC 7636's 43..128


def code_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def build_authorize_url(*, client_id: str, state: str, nonce: str, challenge: str, host: str,
                        redirect_uri: str = REDIRECT_URI, login_hint: Optional[str] = None,
                        id_token_hint: Optional[str] = None) -> str:
    params = {
        "client_id": client_id,
        "ext_agent_host_id": host,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    if client_id == DYNAMIC_CLIENT_ID:
        params["agent_name_hint"] = AGENT_NAME
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    elif login_hint:
        params["login_hint"] = login_hint
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


_pending: Dict[str, Dict[str, Any]] = {}
_pending_lock = threading.Lock()


def _prune_pending(now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    for key in [k for k, v in _pending.items() if now - v["created"] > PENDING_TTL_SECONDS]:
        _pending.pop(key, None)


def start_sign_in(user: Optional[str], *, listen: bool = True) -> Dict[str, Any]:
    """Begin a sign-in: returns the authorize URL to open in a new tab."""
    reg = load_registration(user)
    client_id = reg.get("client_id") or DYNAMIC_CLIENT_ID
    verifier = new_code_verifier()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    creds = load_credentials(user)
    url = build_authorize_url(
        client_id=client_id, state=state, nonce=nonce, challenge=code_challenge(verifier), host=host_id(),
        login_hint=reg.get("email") if client_id != DYNAMIC_CLIENT_ID else None,
        id_token_hint=(creds.get("id_token") or None) if client_id != DYNAMIC_CLIENT_ID else None,
    )
    with _pending_lock:
        _prune_pending()
        # One live attempt per user: a new click replaces the old one.
        for key in [k for k, v in _pending.items() if v["user"] == (user or "")]:
            _pending.pop(key, None)
        _pending[state] = {"user": user or "", "verifier": verifier, "nonce": nonce, "client_id": client_id,
                           "redirect_uri": REDIRECT_URI, "created": time.time()}
    listening = _LISTENER.ensure_running() if listen and _listener_enabled() else False
    return {"authorize_url": url, "redirect_uri": REDIRECT_URI, "expires_in": PENDING_TTL_SECONDS,
            "listening": listening, "first_sign_in": client_id == DYNAMIC_CLIENT_ID}


def parse_callback(pasted: str) -> Dict[str, str]:
    """Pull code/state/client_id/scope/error out of a pasted redirect URL."""
    text = (pasted or "").strip().strip("<>\"'")
    if not text:
        raise SignInRejected("Paste the full address from the browser tab that failed to load.")
    query = urlparse(text).query if "://" in text else text.lstrip("?")
    if "#" in query:
        query = query.split("#", 1)[0]
    params = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items() if v}
    if not params:
        raise SignInRejected("That doesn't look like the sign-in redirect URL. Copy the whole address bar.")
    return params


def complete_sign_in(pasted_or_params: Any, *, user: Optional[str]) -> Dict[str, Any]:
    """Validate the callback's state, exchange the code and store the tokens."""
    params = pasted_or_params if isinstance(pasted_or_params, dict) else parse_callback(pasted_or_params)
    if params.get("error"):
        desc = params.get("error_description") or params["error"]
        if params["error"] == "access_denied":
            raise SignInRejected("Sign-in was cancelled or ChatGPT access was declined.")
        raise SignInRejected(f"ChatGPT sign-in failed: {redact(desc)}")
    state = params.get("state") or ""
    code = params.get("code") or ""
    if not state or not code:
        raise SignInRejected("The pasted URL is missing the code or state. Copy the whole address bar.")
    with _pending_lock:
        _prune_pending()
        pending = _pending.get(state)
        if pending is None or pending["user"] != (user or ""):
            raise SignInRejected("This sign-in link doesn't match a pending sign-in (it may have expired). "
                                 "Click Sign in with ChatGPT again.")
        _pending.pop(state, None)
    # The callback carries the issued client_id on a first registration; on
    # re-authorization it may be omitted, so keep the pending request's.
    client_id = params.get("client_id") or pending["client_id"]
    if client_id == DYNAMIC_CLIENT_ID:
        raise SignInRejected("ChatGPT didn't return a client id for this app. Try signing in again.")
    tokens = exchange_code(client_id=client_id, code=code, verifier=pending["verifier"],
                           redirect_uri=pending["redirect_uri"])
    claims = _jwt_claims(tokens.get("id_token") or "")
    if claims.get("nonce") and claims.get("nonce") != pending["nonce"]:
        raise SignInRejected("ChatGPT sign-in failed a security check (nonce mismatch). Try again.")
    scope = tokens.get("scope") or params.get("scope") or ""
    email = claims.get("email") or ""
    now = time.time()
    creds = _creds_from_token_response(tokens, now=now)
    creds.update({"client_id": client_id, "email": email, "scope": scope, "signed_in_at": int(now)})
    save_credentials(user, creds)
    save_registration(user, client_id=client_id, email=email, signed_out_reason="")
    return status(user)


def _creds_from_token_response(tokens: Dict[str, Any], *, now: float,
                               previous: Optional[Dict] = None) -> Dict[str, Any]:
    previous = previous or {}
    access = tokens.get("access_token") or ""
    if not access:
        raise ChatGPTPlanError("ChatGPT didn't return an access token.")
    try:
        expires_at = now + float(tokens.get("expires_in"))
    except (TypeError, ValueError):
        expires_at = float(_jwt_claims(access).get("exp") or now + 3600)
    try:
        earliest = float(tokens.get("earliest_refresh_at") or 0)
    except (TypeError, ValueError):
        earliest = 0.0
    out = dict(previous)
    out.update({
        "access_token": access,
        "refresh_token": tokens.get("refresh_token") or previous.get("refresh_token") or "",
        "id_token": tokens.get("id_token") or previous.get("id_token") or "",
        "expires_at": int(expires_at),
        "earliest_refresh_at": int(earliest),
    })
    if tokens.get("scope"):
        out["scope"] = tokens["scope"]
    return out


def _jwt_claims(token: str) -> Dict[str, Any]:
    # The ID token comes straight from the token endpoint over TLS (OIDC Core
    # 3.1.3.7), so its claims are read for identity (email) and the nonce.
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# ── Token endpoint ────────────────────────────────────────────────────────

def _oauth_error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or err.get("type") or "")
    return str(err or payload.get("code") or "")


_FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}


def exchange_code(*, client_id: str, code: str, verifier: str, redirect_uri: str,
                  timeout: float = 20.0) -> Dict[str, Any]:
    form = {"grant_type": "authorization_code", "client_id": client_id, "code": code, "code_verifier": verifier,
            "redirect_uri": redirect_uri, "resource": RESOURCE}
    try:
        response = httpx.post(TOKEN_URL, data=form, timeout=timeout, headers=_FORM_HEADERS)
    except httpx.HTTPError as exc:
        raise ChatGPTPlanError(f"Couldn't reach ChatGPT to finish sign-in: {redact(type(exc).__name__)}") from None
    if response.status_code != 200:
        code_ = _oauth_error_code(response)
        raise SignInRejected(
            f"ChatGPT rejected the sign-in code ({response.status_code}{', ' + redact(code_) if code_ else ''}). "
            "Click Sign in with ChatGPT and try again.")
    data = response.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ChatGPTPlanError("ChatGPT's token response was missing the access token.")
    return data


def _refresh_request(client_id: str, refresh_token: str, timeout: float = 20.0) -> Dict[str, Any]:
    form = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token,
            "resource": RESOURCE}
    try:
        response = httpx.post(TOKEN_URL, data=form, timeout=timeout, headers=_FORM_HEADERS)
    except httpx.HTTPError as exc:
        # Transient: keep the credentials (errors-and-recovery).
        raise ChatGPTPlanError(f"Couldn't reach ChatGPT to refresh the sign-in ({type(exc).__name__}).") from None
    if response.status_code == 200:
        data = response.json()
        if isinstance(data, dict) and data.get("access_token"):
            return data
        raise ChatGPTPlanError("ChatGPT's refresh response was missing the access token.")
    code_ = _oauth_error_code(response)
    if code_ in _DEAD_REFRESH_CODES or code_ == "invalid_client" or response.status_code in (400, 401):
        raise ReauthRequired(SIGN_IN_AGAIN)
    raise ChatGPTPlanError(f"ChatGPT couldn't refresh the sign-in right now (HTTP {response.status_code}).")


_refresh_locks: Dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock(user: Optional[str]) -> threading.Lock:
    key = user_key(user)
    with _refresh_locks_guard:
        lock = _refresh_locks.get(key)
        if lock is None:
            lock = _refresh_locks[key] = threading.Lock()
        return lock


def _needs_refresh(creds: Dict[str, Any], now: float) -> bool:
    expires_at = float(creds.get("expires_at") or 0)
    if now < expires_at - REFRESH_SKEW_SECONDS:
        return False
    earliest = float(creds.get("earliest_refresh_at") or 0)
    # Inside the skew window: refresh once OpenAI allows it, and always once
    # the access token is about to lapse.
    return now >= earliest or now >= expires_at - 30


def mark_signed_out(user: Optional[str], reason: str) -> None:
    delete_credentials(user)
    save_registration(user, signed_out_reason=reason)
    try:
        on_signed_out(user or "local")
    except Exception as exc:  # the endpoint row is a convenience, not state
        logger.debug("ChatGPT: endpoint removal after sign-out failed: %s", type(exc).__name__)


def get_access_token(user: Optional[str], *, now: Optional[float] = None) -> str:
    """A current access token for this user, refreshing first when due."""
    creds = load_credentials(user)
    if not creds.get("access_token"):
        raise NotSignedIn(NOT_SIGNED_IN)
    t = time.time() if now is None else now
    if not _needs_refresh(creds, t):
        return creds["access_token"]
    with _refresh_lock(user):
        creds = load_credentials(user)  # another request may have refreshed
        if not creds.get("access_token"):
            raise NotSignedIn(SIGN_IN_AGAIN)
        t = time.time() if now is None else now
        if not _needs_refresh(creds, t):
            return creds["access_token"]
        if not creds.get("refresh_token") or not creds.get("client_id"):
            mark_signed_out(user, "missing_refresh_token")
            raise ReauthRequired(SIGN_IN_AGAIN)
        try:
            refreshed = _refresh_request(creds["client_id"], creds["refresh_token"])
        except ReauthRequired:
            logger.info("ChatGPT: refresh token rejected; the user must sign in again")
            mark_signed_out(user, "refresh_failed")
            raise
        creds = _creds_from_token_response(refreshed, now=t, previous=creds)
        creds["last_refresh"] = int(t)
        save_credentials(user, creds)
        return creds["access_token"]


def status(user: Optional[str]) -> Dict[str, Any]:
    creds = load_credentials(user)
    reg = load_registration(user)
    signed_in = bool(creds.get("access_token") and creds.get("refresh_token"))
    scope = creds.get("scope") or ""
    out: Dict[str, Any] = {
        "signed_in": signed_in,
        "email": (creds.get("email") or reg.get("email") or "") if signed_in else "",
        "plan_usage_enabled": signed_in and (not scope or REQUIRED_SCOPE in scope.split()),
        "redirect_uri": REDIRECT_URI,
        "usage_url": USAGE_SETTINGS_URL,
        "registered": bool(reg.get("client_id")),
        "needs_sign_in_again": (not signed_in) and bool(reg.get("signed_out_reason")),
        "models": reg.get("models") or [],
    }
    if signed_in:
        out["expires_at"] = creds.get("expires_at")
    with _pending_lock:
        _prune_pending()
        out["pending"] = any(v["user"] == (user or "") for v in _pending.values())
    return out


def sign_out(user: Optional[str]) -> Dict[str, Any]:
    """Delete this user's tokens and their ChatGPT endpoint row."""
    with _refresh_lock(user):
        delete_credentials(user)
        save_registration(user, signed_out_reason="", models=[])
        with _pending_lock:
            for key in [k for k, v in _pending.items() if v["user"] == (user or "")]:
                _pending.pop(key, None)
    on_signed_out(user or "local")
    return status(user)


# ── Models ────────────────────────────────────────────────────────────────

def fetch_models(access_token: str, timeout: float = 15.0) -> List[Dict[str, str]]:
    """List models the plan offers: ``visibility == "list"`` entries only."""
    try:
        response = httpx.get(MODELS_URL, timeout=timeout,
                             headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ChatGPTPlanError(f"Couldn't reach OpenAI for the model list ({type(exc).__name__}).") from None
    if response.status_code in (401, 403):
        raise ReauthRequired(SIGN_IN_AGAIN if response.status_code == 401
                             else "ChatGPT plan usage isn't available for this account.")
    if response.status_code != 200:
        raise ChatGPTPlanError(f"OpenAI returned HTTP {response.status_code} for the model list.")
    try:
        data = response.json()
    except Exception:
        raise ChatGPTPlanError("OpenAI's model list wasn't valid JSON.") from None
    return filter_models(data)


def filter_models(data: Any) -> List[Dict[str, str]]:
    entries: Iterable = []
    if isinstance(data, dict):
        entries = data.get("models") or data.get("data") or []
    elif isinstance(data, list):
        entries = data
    out: List[Dict[str, str]] = []
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip() or slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug.strip(), "display_name": str(item.get("display_name") or slug).strip()})
    return out


def refresh_models(user: Optional[str]) -> Dict[str, Any]:
    """Fetch the plan's models, remember them and show them in the user's picker."""
    token = get_access_token(user)
    models = fetch_models(token)
    slugs = [m["slug"] for m in models]
    save_registration(user, models=slugs)
    known = _read_json(_store_dir() / "models.json").get("models") or []
    write_private(_store_dir() / "models.json", {"models": sorted(set(known) | set(slugs))})
    on_models(user or "local", slugs)
    return {"models": models}


def known_models(user: Optional[str]) -> List[str]:
    """The user's models from their last refresh; for the core itself (no
    user), every model any user's plan listed."""
    if user is None:
        return list(_read_json(_store_dir() / "models.json").get("models") or [])
    return list(load_registration(user).get("models") or [])


def headers_for(access_token: Optional[str]) -> Dict[str, str]:
    h = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


def friendly_error(code: Optional[str], message: Optional[str] = None, status: Optional[int] = None) -> str:
    if code and code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[code]
    if status == 401:
        return SIGN_IN_AGAIN
    if status == 403:
        return "ChatGPT plan usage was blocked for this request" + (f": {redact(message)}" if message else ".")
    if status == 429:
        return USAGE_LIMIT_MESSAGE
    if status == 503:
        return "ChatGPT plan usage is temporarily unavailable. Try again in a moment."
    detail = redact(message) if message else ""
    return "ChatGPT request failed" + (f": {detail}" if detail else ".")


def error_from_http(status_code: int, raw: str) -> Dict[str, Any]:
    """(friendly message, code) of an upstream HTTP error."""
    code, message = "", ""
    try:
        body = json.loads(raw) if raw else {}
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "")
            message = str(err.get("message") or "")
        elif isinstance(err, str):
            message = err
    except Exception:
        message = (raw or "")[:200]
    return {"message": friendly_error(code, message, status_code), "code": code}


# ── Optional loopback listener ────────────────────────────────────────────

def _listener_enabled() -> bool:
    return os.getenv("CHATGPT_MOD_LOOPBACK_LISTENER", "1").strip().lower() not in ("0", "false", "no", "off")


_DONE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Odysseus</title>"
    "<body style='font-family:system-ui;padding:40px'><h2>{title}</h2><p>{body}</p></body>"
)


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # the query string holds the code
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
        with _pending_lock:
            pending = _pending.get(params.get("state") or "")
        if pending is None:
            title, body, code = ("Sign-in not recognized",
                                 "Go back to Odysseus and paste this page's address into the ChatGPT tab.", 400)
        else:
            user = pending["user"] or None
            try:
                complete_sign_in(params, user=user)
                try:
                    refresh_models(user)
                except Exception as exc:
                    logger.info("ChatGPT: model refresh after sign-in failed: %s", redact(exc))
                title, body, code = ("Signed in to ChatGPT", "You can close this tab and return to Odysseus.", 200)
            except ChatGPTPlanError as exc:
                title, body, code = ("Sign-in didn't finish", redact(exc), 400)
            except Exception:
                logger.exception("ChatGPT: loopback sign-in failed")
                title, body, code = ("Sign-in didn't finish", "Something went wrong. Try again from Odysseus.", 500)
        page = _DONE_PAGE.format(title=title, body=body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


class _LoopbackListener:
    """Serves 127.0.0.1:<port>/auth/callback while a sign-in is pending."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._server: Optional[HTTPServer] = None
        self._deadline = 0.0

    def ensure_running(self, port: Optional[int] = None) -> bool:
        with self._lock:
            self._deadline = time.time() + PENDING_TTL_SECONDS
            if self._server is not None:
                return True
            try:
                server = HTTPServer((LOOPBACK_HOST, LOOPBACK_PORT if port is None else port), _CallbackHandler)
            except OSError:
                # Port busy (e.g. a Codex CLI login). The paste path still works.
                return False
            server.timeout = 1.0
            self._server = server
        threading.Thread(target=self._serve, args=(server,), name="chatgpt-callback", daemon=True).start()
        return True

    def _serve(self, server: HTTPServer) -> None:
        try:
            while True:
                with self._lock:
                    if self._server is not server:
                        break
                    with _pending_lock:
                        _prune_pending()
                        idle = not _pending
                    if time.time() > self._deadline or idle:
                        self._server = None
                        break
                server.handle_request()
        finally:
            server.server_close()

    def stop(self) -> None:
        with self._lock:
            self._server = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> Optional[int]:
        server = self._server
        return server.server_address[1] if server else None


_LISTENER = _LoopbackListener()
