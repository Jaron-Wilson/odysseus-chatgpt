"""Move the ChatGPT sign-ins from the Odysseus core into this add-on.

Before the add-on, Odysseus kept them in ``<data dir>/chatgpt_plan/``:
``host.json`` (this install's ext_agent_host_id) and, per user,
``users/<hash>.json`` (tokens, Fernet-encrypted with the core's app key
``<data dir>/.app_key``) and ``users/<hash>.client.json`` (the client id
OpenAI issued, the email). This copies them into the add-on's data folder
(``<data dir>/mods/chatgpt/data/``), re-encrypting the tokens with the
add-on's own key, so nobody has to sign in again. The endpoint rows and
chats are moved by the core itself (core/database.py, on start).

Nothing is printed but counts; the source is only read. Run it as the user
Odysseus runs as, from the installed add-on's folder:

    cd ~/odysseus-data/mods/chatgpt/src
    .venv/bin/python -m chatgpt_mod.migrate --core-data ~/odysseus-data

Add ``--dry-run`` to see what it would do, ``--force`` to overwrite sign-ins
the add-on already has.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict

from chatgpt_mod import plan

_TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")


def _core_fernet(core_data: Path):
    from cryptography.fernet import Fernet
    key = (core_data / ".app_key").read_bytes().strip()
    return Fernet(key)


def _core_decrypt(fernet, value: str) -> str:
    if not value or not value.startswith("enc:"):
        return value or ""
    return fernet.decrypt(value[4:].encode("ascii")).decode("utf-8")


def migrate(core_data: Path, mod_data: Path, *, dry_run: bool = False, force: bool = False) -> Dict[str, int]:
    src = core_data / "chatgpt_plan"
    out = {"users": 0, "registrations": 0, "skipped": 0, "unreadable": 0, "host": 0}
    if not src.is_dir():
        return out
    plan.STORE_DIR = mod_data
    host = src / "host.json"
    if host.exists() and (force or not (mod_data / "host.json").exists()):
        data = json.loads(host.read_text(encoding="utf-8"))
        if isinstance(data.get("ext_agent_host_id"), str):
            out["host"] = 1
            if not dry_run:
                plan.write_private(mod_data / "host.json", {"ext_agent_host_id": data["ext_agent_host_id"]})
    users = src / "users"
    fernet = None
    for f in sorted(users.glob("*.json")) if users.is_dir() else []:
        dest = mod_data / "users" / f.name
        if dest.exists() and not force:
            out["skipped"] += 1
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out["unreadable"] += 1
            continue
        if not isinstance(data, dict):
            out["unreadable"] += 1
            continue
        if f.name.endswith(".client.json"):
            out["registrations"] += 1
            if not dry_run:
                plan.write_private(dest, data)
            continue
        if fernet is None:
            fernet = _core_fernet(core_data)
        try:
            plain = {k: _core_decrypt(fernet, data.get(k) or "") for k in _TOKEN_FIELDS}
        except Exception:
            out["unreadable"] += 1  # a rotated app key: that user signs in again
            continue
        out["users"] += 1
        if not dry_run:
            stored = dict(data)
            for k in _TOKEN_FIELDS:
                stored[k] = plan.encrypt(plain[k])
            plan.write_private(dest, stored)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Move Odysseus's ChatGPT sign-ins into the ChatGPT add-on.")
    ap.add_argument("--core-data", required=True, help="Odysseus's data folder (ODYSSEUS_DATA_DIR)")
    ap.add_argument("--mod-data", help="the add-on's data folder (default: <core data>/mods/chatgpt/data)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite sign-ins the add-on already has")
    a = ap.parse_args(argv)
    core = Path(os.path.expanduser(a.core_data)).resolve()
    mod = Path(os.path.expanduser(a.mod_data)).resolve() if a.mod_data else core / "mods" / "chatgpt" / "data"
    if not (core / "chatgpt_plan").is_dir():
        print(f"Nothing to move: {core}/chatgpt_plan does not exist.")
        return 0
    n = migrate(core, mod, dry_run=a.dry_run, force=a.force)
    verb = "Would move" if a.dry_run else "Moved"
    print(f"{verb} {n['users']} ChatGPT sign-in(s) and {n['registrations']} client registration(s) "
          f"to {mod}" + (" (and this install's host id)" if n["host"] else "") + ".")
    if n["skipped"]:
        print(f"Skipped {n['skipped']} the add-on already has (use --force to overwrite).")
    if n["unreadable"]:
        print(f"{n['unreadable']} could not be read; those users sign in again on the ChatGPT tab.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
