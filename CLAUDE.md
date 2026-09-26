# Shellie — Project Context

Shellie (formerly "Free Secretary") is a zero-dependency personal Telegram agent:
Python 3.8+ **standard library only** (no pip), targeting macOS / Linux PC /
Raspberry Pi (POSIX only). It replaces OpenClaw. Goals: <30MB RSS, ~0.1s shell
bypass, 24/7 via launchd/systemd.

## Documents

- `design-req.md` — original baseline spec (Korean). **Historical — never edit.**
  Superseded in places by amendments; differences are recorded in dev-plan.
- `dev-plan.md` — development plan + amendment A1 (§6: runtime settings store,
  local web UI, Telegram settings commands) + A2 (§7: multi-platform). Risk
  register R1–R55 (annotated 해결됨/수용함).
- `tasks.md` — phase checklist, decision log D1–D12, current status. **Keep it
  updated as work progresses.**
- `docs/VERIFICATION.md` — manual acceptance playbook (Chester executes).

## Hard rules

1. **Do NOT build, run, or test** — no unittest runs, no `python3 -m src.agent`,
   no lint. Chester verifies on his machine. (Global policy; applies to subagents.)
2. **Stdlib only, Python 3.8 floor.** Every module starts with
   `from __future__ import annotations`. No 3.9+ APIs (no dict `|` merge,
   `str.removeprefix/removesuffix`, `functools.cache`, `zoneinfo`,
   `subprocess process_group=`, `match`). Use `typing.Dict/List/Optional` in
   runtime type expressions; `start_new_session=True` for process groups.
3. **Language:** all docs, code comments, and docstrings in **English**.
   App user-facing string literals (Telegram replies, web UI, logs) in **English**.
   Conversation with Chester in Korean.
4. **Settings are read at CALL time**, never at import time and never cached in
   module constants. Import-time side effects allowed: path constants +
   `os.makedirs` only (config.py). Final-review greps enforce this.
5. **Never log/render secrets.** TELEGRAM_BOT_TOKEN / GEMINI_API_KEY /
   MCP_SERVERS are masked everywhere (`mask_secret_value`, `mask_token`);
   Gemini key goes in the `x-goog-api-key` header, never URLs; child shells get
   an env with bot/API secrets stripped.

## Architecture (src/agent/)

- `config.py` — path constants (BASE_DIR, MEMORY_DIR/FILE, SKILLS_DIR,
  OFFSET_FILE, SETTINGS_FILE), pure `parse_dotenv()`, re-exports `settings`.
- `settings.py` — `SettingSpec` catalog (**20 keys**) + `SettingsStore`
  singleton: 3 layers (defaults < .env/os.environ read once at startup <
  settings.json overrides), copy-on-write lock-free reads, serialized
  all-or-nothing `update/unset` with per-key prechecks, atomic 0600 persist,
  `on_change` hooks run outside the lock, audit log. Secrets + ALLOWED_USER_ID
  are **web-only** (blocked for actor="telegram", P1). ⚠️ `SettingSpec.precheck`
  is shared global state on the CATALOG objects — tests must clean up with
  `addCleanup(register_precheck, key, None)`.
- `core/shell.py` — POSIX shell engine: resolution chain (SHELL_PATH `auto` →
  /bin/zsh on darwin → $SHELL → login shell → /bin/sh; `/etc/shells` + X_OK
  validated), `Popen([shell, "-c", cmd], start_new_session=True)`, timeout →
  killpg SIGTERM → 2s → SIGKILL, `kill_active()` for SIGTERM handler, per-OS
  PATH augmentation, output = stdout + `[stderr]` + `[exit N]`, truncation.
- `core/memory.py` — core `memory/MEMORY.md` (permanent, never auto-written) +
  dated files `memory/YYYY-MM-DD.md` (append_memory target, one-line entries).
  `read_memory()` injects **core + today only**; older memories are grep-searched
  on demand (hint injected into system prompt).
- `core/gemini.py` — model-chain fallback (429 / RESOURCE_EXHAUSTED / 5xx /
  transport → next model; snapshot of chain/key/timeouts at call start), plus
  whole-chain retry with cooldown (GEMINI_MAX_RETRIES passes, delay = server
  `retryDelay` hint or exponential backoff from GEMINI_RETRY_BASE_DELAY_SEC,
  capped at 300s; `on_cooldown` callback for caller notification),
  `ToolEntry` registry (execute_shell dynamic description + append_memory),
  hybrid skills auto-discovery — `skills/*.py` (first docstring line) AND
  `skills/*.md` procedure docs (first non-empty line, `#` stripped, 80 chars;
  README.md and `_`-prefixed excluded); only the one-line summary goes into the
  system prompt, the LLM `cat`s the full .md on demand and follows its steps —
  `[execution environment]` block (OS/arch/shell/cwd), robust `parse_response()`
  (multi functionCall parts, thought-part skip, blocked/finishReason handling).
- `core/mcp.py` — HTTP MCP client (Streamable HTTP JSON-RPC): initialize →
  tools/list → tools/call, Mcp-Session-Id support, 300s TTL cache invalidated by
  config fingerprint, tools exposed as `mcp_<server>_<tool>`. **No stdio, no
  OAuth, no SSE** (SSE servers skipped with a warning). Configured via
  MCP_SERVERS key (JSON array, secret/web-only).
- `telegram/client.py` — call-time token URLs, long-poll get_updates (failures
  return `{"_status", "_error"}` for backoff), `split_message` (4096, `<pre>`
  continuity, HTML→plain retry), get_me/delete_message, `register_prechecks()`.
- `telegram/handlers.py` — routing: auth (fail-closed; unauthorized = **silent**
  + warn log) → settings commands (`/settings /get /set /unset`, excluded from
  history) → bypass (`!`, `/sh`, `/mem`, `/reset` = context reset, `/restart` =
  process exit; service-manager detection warns if none) → LLM track (FC loop:
  all functionCalls answered in one user-role turn, FC_MAX_LOOPS cap,
  CONTEXT_TURNS pairs + IDLE_RESET_MINUTES idle reset). Output is Telegram HTML.
- `web/` — settings UI on 127.0.0.1 only (hard constant), single-thread
  HTTPServer in one daemon thread (lazy `http.server` import), security gate:
  Host → Origin → Sec-Fetch-Site → (POST: 415/411/413 → parse → CSRF), PRG.
  `WebServerManager.start()/reconcile()/stop()`; port change = start-new-first
  then swap, rollback via actor="system". `shutdown()` always from a separate
  thread. Setup-required mode (D10): missing required keys → web only, polling
  starts when filled. ⚠️ `_notice`/`_pending_old_values` are module globals —
  reset between tests.
- `__main__.py` — POSIX guard, `.shellie.lock` flock single-instance,
  SIGTERM/SIGINT handler (kill_active → web stop → exit 0), offset committed to
  `.update_offset` **before** process_update (at-most-once; fixes /restart loop
  R3), exponential backoff (1→60s) on poll failures incl. 401/409, token-change
  detection (bot id changed → offset reset).

## Key decisions (full log in tasks.md)

D1 fail-closed auth · D2 secrets in .env (600) · D3 Python ≥3.8 (Chester's Mac
is 3.8.2) · D4 offset file, at-most-once · D5 Telegram HTML · D6 context =
N turns + idle reset · D7 chain flash→pro→lite · D8 skills = hybrid: .py scripts
run via execute_shell (deterministic) + .md procedure docs the LLM reads and
follows · D9 settings.json overlay · D10 setup-required mode · D11 web =
no login + Host/Origin/CSRF · D12 launchd + systemd user unit, POSIX only.

## Commands

- Run (Chester only): `./run.sh` or `PYTHONPATH=. python3 -m src.agent`
- Tests (Chester only): `PYTHONPATH=. python3 -m unittest discover tests -v`
  (181 tests; some shell tests spawn real short-lived processes)
- Service install: `sh launchd/install.sh` (Darwin→launchd, Linux→systemd user
  unit; Linux boot-start needs `loginctl enable-linger $USER`)

## Status (2026-09-25)

Phases 0–9 all implemented, documented, final-reviewed. Outstanding:
1. Chester: run the 181 tests + docs/VERIFICATION.md acceptance playbook.
2. Open decision: whether to migrate OpenClaw data (memories/settings).
3. Note: project directory is still named `freesec`; renaming it to `shellie`
   is safe (paths are resolved dynamically) but breaks the current session cwd.
