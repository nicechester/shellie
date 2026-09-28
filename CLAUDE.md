# Shellie — Project Context

Shellie (formerly "Free Secretary") is a zero-dependency personal Telegram agent:
Python 3.8+ **standard library only** (no pip), targeting macOS / Linux PC /
Raspberry Pi (POSIX only). It replaces OpenClaw. Goals: <30MB RSS, ~0.1s shell
bypass, 24/7 via launchd/systemd.

## Hard rules

1. **Do NOT build, run, or test** — no unittest runs, no `python3 -m src.agent`,
   no lint. Chester verifies on his machine. (Global policy; applies to subagents.)
2. **Stdlib only, Python 3.8 floor.** Every module starts with
   `from __future__ import annotations`. No 3.9+ APIs (no dict `|` merge,
   `str.removeprefix/removesuffix`, `functools.cache`, `zoneinfo`,
   `subprocess process_group=`, `match`). Use `typing.Dict/List/Optional` in
   runtime type expressions; `start_new_session=True` for process groups.
3. **Language:** all docs, code comments, and docstrings in **English**.
   App user-facing string literals (Telegram replies, web UI, logs) in **Korean**.
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
- `settings.py` — `SettingSpec` catalog (**21 keys**) + `SettingsStore`
  singleton: 3 layers (defaults < .env/os.environ read once at startup <
  settings.json overrides), copy-on-write lock-free reads, serialized
  all-or-nothing `update/unset` with per-key prechecks, atomic 0600 persist,
  `on_change` hooks run outside the lock, audit log. Secrets + ALLOWED_USER_ID
  are **web-only** (blocked for actor="telegram"). ⚠️ `SettingSpec.precheck`
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
  capped at 300s; `on_cooldown` callback for caller notification). 5xx same-model
  retry: exponential backoff from `_5XX_RETRY_BASE_SEC=10` (10s/20s/40s),
  `_5XX_MAX_RETRIES=3` attempts before falling back. RPM vs RPD detection:
  QuotaFailure.quotaId "PerDay" → RPD (fall back); otherwise RPM (sleep
  server-hinted delay and retry once, then fall back). `ToolEntry` registry:
  execute_shell (dynamic description) + append_memory + execute_web_search
  (google_search grounding via SEARCH_MODEL). Hybrid skills auto-discovery —
  `skills/*.py` (first docstring line) AND `skills/*.md` procedure docs (first
  non-empty line, `#` stripped, 80 chars; README.md and `_`-prefixed excluded);
  only the one-line summary goes into the system prompt, the LLM `cat`s the full
  .md on demand and follows its steps — `[execution environment]` block
  (OS/arch/shell/cwd), robust `parse_response()` (multi functionCall parts,
  thought-part skip, blocked/finishReason handling). Live retry state (calling /
  cooldown reason 429_rpm·5xx·transport·chain_cooldown, model, until, attempt)
  is published copy-on-write via `get_retry_status()` for display only; reset to
  idle in `call_gemini`'s finally.
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
  history) → bypass (`!`, `/sh`, `/mem`, `/reset` = context + queue drain,
  `/restart` = process exit, `/queue` = queue status + live Gemini retry/cooldown (remaining seconds, attempt), `/kill` = drain queue
  without history reset, `/systemlog [N]` = tail agent.log last N lines (default
  50, max 500); service-manager detection warns if none) → LLM track (FC loop:
  all functionCalls answered in one user-role turn, FC_MAX_LOOPS cap (1–50, default 15) + repeat-loop detection (3 identical call+result iterations) → tools-disabled wrap-up call (toolConfig NONE), turn preserved in history,
  CONTEXT_TURNS pairs + IDLE_RESET_MINUTES idle reset). Task queue: LLM messages
  serialized via `queue.Queue` + single worker thread; `_process_llm_item` is
  now a thin wrapper — all retry/cooldown logic lives in `call_gemini`. Output
  is Telegram HTML.
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
  `.update_offset` **before** process_update (at-most-once; prevents /restart
  loop), exponential backoff (1→60s) on poll failures incl. 401/409, token-change
  detection (bot id changed → offset reset).

## Key decisions

D1 fail-closed auth · D2 secrets in .env (600) · D3 Python ≥3.8 (Chester's Mac
is 3.8.2) · D4 offset file, at-most-once · D5 Telegram HTML · D6 context =
N turns + idle reset · D7 chain flash→pro→lite · D8 skills = hybrid: .py scripts
run via execute_shell (deterministic) + .md procedure docs the LLM reads and
follows · D9 settings.json overlay · D10 setup-required mode · D11 web =
no login + Host/Origin/CSRF · D12 launchd + systemd user unit, POSIX only.

## Commands

- Run: `./run.sh` or `PYTHONPATH=. python3 -m src.agent`
- Tests: `PYTHONPATH=. python3 -m unittest discover tests -v`
- Service install: `sh launchd/install.sh` (Darwin→launchd, Linux→systemd user
  unit; Linux boot-start needs `loginctl enable-linger $USER`)

## Status (2026-09-28)

Phases 0–9 all implemented. Recent changes:
- `GEMINI_TIMEOUT_SEC` default raised 60 → 120s.
- 5xx same-model retry base raised 1s → 10s (10s/20s/40s).
- Redundant flat-60s retry loop in `_process_llm_item` removed; `call_gemini`
  owns all retry/cooldown logic.
- Added `/kill`, `/systemlog [N]` bypass commands; `/reset` now also drains queue.
- `execute_web_search` tool added (google_search grounding via SEARCH_MODEL).
- Issue #13 open: OpenAI-compatible LLM backend support.

Outstanding:
1. Chester: run tests + acceptance playbook.
2. Open decision: whether to migrate OpenClaw data (memories/settings).
