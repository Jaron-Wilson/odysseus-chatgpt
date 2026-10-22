"""Moving the sign-ins from the core (chatgpt_mod/migrate.py), against a fake
core data folder made the way the core made it: tokens Fernet-encrypted
with <data>/.app_key, "enc:"-prefixed, files named by the user hash."""
import json
import os
import stat

import pytest
from cryptography.fernet import Fernet

from chatgpt_mod import migrate, plan

ACCESS = "eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.c2lnbmF0dXJlLWFjY2Vzcw"
REFRESH = "rt_refresh_secret_value_0001"


def _core(tmp_path, users=("alice", "")):
    core = tmp_path / "odysseus-data"
    (core / "chatgpt_plan" / "users").mkdir(parents=True)
    key = Fernet.generate_key()
    (core / ".app_key").write_bytes(key)
    f = Fernet(key)
    enc = lambda v: "enc:" + f.encrypt(v.encode()).decode()  # noqa: E731
    (core / "chatgpt_plan" / "host.json").write_text(json.dumps({"ext_agent_host_id": "urn:uuid:1234"}))
    for u in users:
        k = plan.user_key(u)
        (core / "chatgpt_plan" / "users" / f"{k}.json").write_text(json.dumps({
            "access_token": enc(ACCESS + u), "refresh_token": enc(REFRESH + u), "id_token": "",
            "client_id": "oaiapp_1", "email": f"{u or 'local'}@example.com", "expires_at": 9999999999}))
        (core / "chatgpt_plan" / "users" / f"{k}.client.json").write_text(json.dumps(
            {"client_id": "oaiapp_1", "email": f"{u or 'local'}@example.com"}))
    return core


@pytest.fixture(autouse=True)
def _restore_store(monkeypatch):
    monkeypatch.setattr(plan, "STORE_DIR", None)


def test_moves_sign_ins_reencrypted_without_printing_secrets(tmp_path, capsys):
    core = _core(tmp_path)
    rc = migrate.main(["--core-data", str(core)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "Moved 2 ChatGPT sign-in(s) and 2 client registration(s)" in printed
    assert ACCESS not in printed and REFRESH not in printed
    mod = core / "mods" / "chatgpt" / "data"
    plan.STORE_DIR = mod
    # "local" is the core's no-owner user.
    assert plan.load_credentials("local")["refresh_token"] == REFRESH
    assert plan.get_access_token("alice") == ACCESS + "alice"
    assert plan.host_id() == "urn:uuid:1234"
    assert plan.status("alice")["registered"] is True
    for f in (mod / "users").iterdir():
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
        assert ACCESS not in f.read_text() and REFRESH not in f.read_text()
    assert stat.S_IMODE(os.stat(mod / ".key").st_mode) == 0o600
    # The core's key is not the add-on's.
    assert (mod / ".key").read_bytes() != (core / ".app_key").read_bytes()


def test_dry_run_and_existing_sign_ins(tmp_path, capsys):
    core = _core(tmp_path, users=("alice",))
    migrate.main(["--core-data", str(core), "--dry-run"])
    assert "Would move 1" in capsys.readouterr().out
    assert not (core / "mods").exists()
    migrate.main(["--core-data", str(core)])
    capsys.readouterr()
    migrate.main(["--core-data", str(core)])
    assert "Skipped 2" in capsys.readouterr().out


def test_a_rotated_core_key_is_reported_not_crashed(tmp_path, capsys):
    core = _core(tmp_path, users=("alice",))
    (core / ".app_key").write_bytes(Fernet.generate_key())
    migrate.main(["--core-data", str(core)])
    out = capsys.readouterr().out
    assert "1 could not be read" in out and "Moved 0 ChatGPT sign-in(s)" in out


def test_nothing_to_move(tmp_path, capsys):
    assert migrate.main(["--core-data", str(tmp_path)]) == 0
    assert "Nothing to move" in capsys.readouterr().out
