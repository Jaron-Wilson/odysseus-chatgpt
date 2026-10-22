# ChatGPT add-on for Odysseus

**Sign in with ChatGPT**: use OpenAI models on your ChatGPT Plus or Pro plan in Odysseus, with no API
key. This is OpenAI's official
[ChatGPT plan usage](https://developers.openai.com/siwc/token-sharing-open-source) path for open-source,
self-hosted apps. Usage counts against your plan's limits
([ChatGPT Settings > Usage](https://chatgpt.com/settings/usage)), not an API bill.

It used to be built into Odysseus (Settings > Add Models > Sign in with ChatGPT). It is an add-on now,
so a basic Odysseus install stays small: without it, Odysseus works with local models and API keys as
before, and Settings > Add Models points here.

## Install

1. In Odysseus: **Settings > Mods > Browse**, search for `chatgpt`, **Review and install**, approve.
   (Or **Install a mod** with `git@github.com:Jaron-Wilson/odysseus-chatgpt.git`.) It asks for no
   permissions. Setup makes a `.venv` with `httpx` and `cryptography`.
2. With more than one Odysseus user, set **Who can use it** to **All users** on its card in
   Settings > Mods, so everyone can sign in to their own ChatGPT account.
3. Open the **ChatGPT** tab (System group in the sidebar). The tutorial walks you through it.

## Use

- **Sign in with ChatGPT** opens ChatGPT in a new tab; sign in and approve Odysseus.
- ChatGPT then sends that tab to `http://127.0.0.1:1455/auth/callback`. When your browser is on the
  Odysseus server itself, that page finishes the sign-in. Anywhere else it fails to load, which is
  expected: copy the whole address from that tab and paste it under **Finish sign-in**.
- Your plan's models then show in the model picker under **ChatGPT** (an endpoint with the URL
  `mod://chatgpt/v1`), for chats, the agent, Company and anything else that picks a model.
  **Refresh models** reads the list again; **Sign out** deletes your tokens and the endpoint.
- System > Usage counts these calls as a subscription (`chatgpt-plan`), so subscription plans you
  priced before keep matching.

## Moving from the built-in sign-in

When the Odysseus core that no longer has Sign in with ChatGPT starts, it moves the old ChatGPT
endpoint rows and the chats on them to `mod://chatgpt/v1` by itself (same ids, owners and models),
so pickers, defaults and plans keep working. The sign-ins (tokens) are moved by this add-on, once,
with this command, run on the server as the user Odysseus runs as, after installing the add-on:

```
! cd ~/odysseus-data/mods/chatgpt/src && .venv/bin/python -m chatgpt_mod.migrate --core-data ~/odysseus-data --dry-run
! cd ~/odysseus-data/mods/chatgpt/src && .venv/bin/python -m chatgpt_mod.migrate --core-data ~/odysseus-data
```

(`~/odysseus-data` is the Odysseus data folder, `ODYSSEUS_DATA_DIR`; with `ODYSSEUS_MODS_DIR` set, the
add-on is in that folder instead.) It reads `<data>/chatgpt_plan/` and the core's key
`<data>/.app_key`, re-encrypts the tokens with this add-on's own key into `<data>/mods/chatgpt/data/`
(0600), keeps this install's ChatGPT host id and each user's issued client id, and prints only
counts. It never changes the core's files; once everyone's models work, `<data>/chatgpt_plan/` can be
deleted. Anyone it could not move just signs in again on the tab.

## How it works

- `chatgpt_mod/plan.py`: the sign-in (PKCE S256, `state` and `nonce`, the dynamic client on the first
  sign-in and the issued client after), token refresh (rotating refresh tokens, `earliest_refresh_at`,
  one refresh at a time per user), the model list, and storage: per user in
  `<MOD_DATA_DIR>/users/`, tokens Fernet-encrypted with `<MOD_DATA_DIR>/.key`. Tokens are never
  logged.
- `chatgpt_mod/translate.py`: Odysseus sends OpenAI Chat Completions; ChatGPT plan usage only takes
  the Responses API (`store: false`, `stream: true`). Requests (messages, images, tool calls and
  results, function tools) and the streamed answer (text, reasoning, tool calls, usage) are
  translated both ways.
- `chatgpt_mod/app.py`: the tab, and the API Odysseus calls at `/mod/cap/llm.provider/v1/`
  (`models`, `chat/completions`). Odysseus signs each call for the chat's user
  (`X-Odysseus-User`), and the add-on uses that user's own tokens. After a sign-in it tells Odysseus
  the user's models (`PUT /api/mods/v1/llm/endpoint`); after a sign-out it removes them.
- The manifest says `provides: ["llm.provider"]` and `llm_provider` (`usage_provider`
  `chatgpt-plan`, `billing` `subscription`, `tool_cap`: Odysseus keeps a request under OpenAI's 128
  function tools). See `docs/mods.md` in Odysseus, "Models from a mod".

## Develop

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```

The tests run the add-on under a fake core with a fake OpenAI; nothing leaves the machine.
`CHATGPT_MOD_AUTH_ISSUER`, `CHATGPT_MOD_API_BASE` and `CHATGPT_MOD_LOOPBACK_PORT` point it elsewhere;
`CHATGPT_MOD_LOOPBACK_LISTENER=0` turns the loopback listener off.

## License

AGPL-3.0, like Odysseus.
