# Shellie

A personal Telegram agent that runs on your own machine. Zero pip dependencies —
Python 3.8+ standard library only.

## 1. Overview

Shellie is a single-user Telegram bot that runs continuously on a
machine you own (a Mac, a Linux PC, or a Raspberry Pi) and gives you two ways
to interact with it:

- **Bypass track** — `!<cmd>`, `/sh <cmd>`, `/mem`, `/restart`, `/reset`, and
  the settings commands (`/settings`, `/get`, `/set`, `/unset`) are handled
  directly, with no LLM call. Shell commands run in ~0.1s.
- **Gemini LLM track** — any other message is sent to Gemini
  (`gemini-2.5-flash` → `gemini-2.5-pro` → `gemini-2.5-flash-lite` by default,
  falling back to the next model on 429/`RESOURCE_EXHAUSTED`/5xx/transport
  errors) with function calling. The model can call `execute_shell` and
  `append_memory`, looping up to `FC_MAX_LOOPS` times per message.
- **Long-term memory** — split into a core file, `memory/MEMORY.md`
  (user-curated, permanent), and per-day files `memory/YYYY-MM-DD.md` that
  the LLM appends to with `append_memory`. `/mem` and every LLM call inject
  the core file plus today's dated file into the system instruction; older
  dated files are not injected but can be found with `grep -ri "<keyword>"
  memory/`.
- **Runtime-editable settings** — all 20 settings can be changed without a
  restart, either from a local web UI (`http://127.0.0.1:<WEB_PORT>/`) or via
  Telegram `/set`/`/unset` commands. See [§7](#7-settings).
- **MCP tools (optional)** — configure `MCP_SERVERS` to let the Gemini LLM
  also call tools exposed by remote HTTP MCP (Model Context Protocol)
  servers, alongside the built-in `execute_shell`/`append_memory` tools. See
  [MCP servers (HTTP)](#mcp-servers-http) in [§7](#7-settings).

**Design goals:** zero pip dependencies (Python 3.8+ standard library only),
steady-state RSS under 30MB, and 24/7 operation via the OS's own service
manager (no custom supervisor process).

**Supported platforms** (POSIX only — Windows is refused at startup):

| Platform | Python | Service manager |
|---|---|---|
| macOS | ≥ 3.8.2 | launchd |
| Linux PC | ≥ 3.8 | systemd (user unit) |
| Raspberry Pi OS Bullseye | 3.9 | systemd (user unit) |
| Raspberry Pi OS Bookworm | 3.11 | systemd (user unit) |

## 2. Requirements

- Python 3.8 or newer. No `pip install` of anything — the project has zero
  third-party dependencies (`pyproject.toml` declares `dependencies = []`).
- Working outbound HTTPS to `api.telegram.org` and
  `generativelanguage.googleapis.com`.
- **SSL certificates:** this only matters for python.org-distributed macOS
  builds, which ship without a root CA bundle — run
  `/Applications/Python 3.x/Install Certificates.command` once, or every
  HTTPS call will fail. Python installed via Apple's Command Line Tools and
  Python on Linux/Raspberry Pi OS use the system CA store and need no extra
  step.

## 3. Setup

1. Create a bot with **@BotFather** on Telegram and copy the bot token.
2. Get your own numeric Telegram user ID from **@userinfobot**.
3. Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN`,
   `GEMINI_API_KEY`, and `ALLOWED_USER_ID`. Restrict the file:
   `chmod 600 .env`.

**Alternative — no `.env` at all:** if you start the agent without the three
required values set anywhere, it enters **setup-required mode**: polling does
not start, but the local web UI comes up at `http://127.0.0.1:8321/` (the
default `WEB_PORT`) so you can fill them in from a browser. As soon as all
three required values are present, polling starts automatically — **no
restart needed**. If you also set `WEB_ENABLED=false` with required values
missing, the process exits immediately instead (there would be no way to
supply them).

## 4. Running

From the project root:

```sh
PYTHONPATH=. python3 -m src.agent
```

On startup the process: checks it is running on a POSIX OS (refuses
Windows), takes an exclusive `flock` on `.shellie.lock` (single-instance
guard), loads settings (`.env` + environment + `settings.json` overrides),
installs SIGTERM/SIGINT handlers, starts the web listener if enabled, and
then either enters setup-required mode or starts the Telegram polling loop.

**Warning: never run a foreground instance while the launchd/systemd service
is also installed and running.** The `flock` guard makes the second process
print "다른 인스턴스가 실행 중입니다." and exit(1) immediately — but even if
that guard were bypassed, two processes polling `getUpdates` with the same
bot token race each other and Telegram returns 409 Conflict to one of them.

## 5. Service installation

```sh
sh launchd/install.sh
```

This single script detects the OS (`uname -s`) and installs the correct
service for you (macOS → launchd, Linux → systemd user unit), and `chmod
600`s `.env`/`settings.json` if present.

> **Interpreter path:** both service files run `/usr/bin/python3` (the
> system/CLT Python). If your Python 3.8+ lives elsewhere (e.g. a
> python.org install at `/usr/local/bin/python3`), edit `ProgramArguments`
> in the plist or `ExecStart` in the systemd unit before running
> `install.sh` — the script substitutes the project path only, not the
> interpreter. Check yours with `which python3 && python3 --version`.

### macOS (launchd)

- Installed unit: `~/Library/LaunchAgents/com.user.shellie.plist`
  (rendered from `launchd/com.user.shellie.plist`, with the
  `/Users/USERNAME/shellie` placeholder replaced by your actual
  project path).
- Manual equivalents:
  ```sh
  launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.user.shellie.plist
  launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.user.shellie.plist
  launchctl kickstart -k "gui/$(id -u)/com.user.shellie"
  ```
- Logs: `agent.log` (stdout) and `agent_err.log` (stderr) in the project
  root (`StandardOutPath`/`StandardErrorPath`). No log rotation is
  configured — these files grow unbounded; rotate them yourself if needed.
- `KeepAlive` + `RunAtLoad` restart the process on any exit, including
  `/restart`. launchd's default restart throttle is about **10 seconds** —
  measure and record the actual value for your machine below.

### Linux PC / Raspberry Pi (systemd user unit)

- Installed unit: `~/.config/systemd/user/shellie.service` (rendered
  from `systemd/shellie.service`, with the `%h/shellie`
  placeholder replaced by your actual project path).
- Commands:
  ```sh
  systemctl --user daemon-reload
  systemctl --user enable --now shellie.service
  systemctl --user restart shellie.service
  systemctl --user stop shellie.service
  systemctl --user status shellie.service
  journalctl --user -u shellie -f
  ```
- `Restart=always` with `RestartSec=2` — restarts on any exit (including
  `/restart`'s `exit(0)`), throttled by `StartLimitIntervalSec=60` /
  `StartLimitBurst=10` (10 restarts per 60s before systemd gives up).
- **`loginctl enable-linger $USER` is required** for the service to start at
  boot without a login session, and is mandatory on a headless Raspberry Pi
  — without it, the unit only runs while you are logged in and stops when
  you log out. `install.sh` prints a reminder; it does not run this for
  you.

### Restart recovery time (fill in after measuring)

| OS / service manager | Measured `/restart` recovery time |
|---|---|
| macOS launchd | _(record actual seconds here)_ |
| Linux PC systemd (`RestartSec=2`) | _(record actual seconds here)_ |
| Raspberry Pi OS Bullseye systemd | _(record actual seconds here)_ |
| Raspberry Pi OS Bookworm systemd | _(record actual seconds here)_ |

## 6. Telegram commands

| Command | Track | Behavior |
|---|---|---|
| `!<cmd>` | Bypass (shell) | Runs `<cmd>` in the resolved shell (see `SHELL_PATH` in [§7](#7-settings)), no LLM call |
| `/sh <cmd>` | Bypass (shell) | Same as `!<cmd>`, explicit form |
| `/mem` | Bypass | Prints long-term memory: core `MEMORY.md` plus today's dated file (`memory/YYYY-MM-DD.md`) |
| `/reset` | Bypass | Clears the in-process conversation history (context only, not `MEMORY.md`) |
| `/restart` | Bypass | Exits the process; the service manager (launchd/systemd) restarts it. Warns if no service manager is detected. Now mainly useful for picking up **code** changes — setting changes already hot-apply |
| `/settings`, `/get` | Bypass (settings) | Lists all 20 keys with current value (masked if secret), source, and apply timing |
| `/get KEY` | Bypass (settings) | Detail view for one key: value, source, default, constraint, apply timing |
| `/set KEY VALUE` | Bypass (settings) | Applies an override immediately (rejected for web-only keys) |
| `/unset KEY` | Bypass (settings) | Removes an override, falling back to the env/default layer |
| anything else | LLM (Gemini) | Sent with tools `execute_shell` and `append_memory`; function-calling loop up to `FC_MAX_LOOPS` iterations, with up to `CONTEXT_TURNS` recent turns of history |

## 7. Settings

### Layers and hot-apply

Values are resolved from three layers, lowest to highest priority:

1. **default** — the catalog default in `src/agent/settings.py`.
2. **env** — `.env` merged with `os.environ` (environment variables already
   set win over `.env`), read **once at process startup**.
3. **override** — `settings.json`, written by the web UI or `/set`/`/unset`.

**Gotcha:** because `settings.json` overrides always win over `.env`, and
`.env` is only read at startup, editing `.env` while the process is running
does nothing until you restart — and even after a restart, if an override
for that key already exists in `settings.json`, your new `.env` value stays
shadowed until you `/unset` that key (or delete `settings.json`).

### 20-key reference

| Key | Default | Range / format | Telegram-editable | Apply timing |
|---|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | `^\d{5,16}:[A-Za-z0-9_-]{30,64}$` | No (web-only, secret) | next poll cycle |
| `GEMINI_API_KEY` | *(required)* | 20–128 chars, `[A-Za-z0-9._-]` | No (web-only, secret) | immediate |
| `ALLOWED_USER_ID` | *(required)* | integer 1 – 2^53−1, cannot equal the bot's own ID | No (web-only) | immediate |
| `GEMINI_MODEL_CHAIN` | `gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite` | 1–5 comma-separated model names, no duplicates | Yes | immediate |
| `GEMINI_TIMEOUT_SEC` | `60` | 5–300 | Yes | immediate |
| `GEMINI_FALLBACK_DELAY_SEC` | `1` | 0–10 | Yes | immediate |
| `GEMINI_RETRY_BASE_DELAY_SEC` | `60` | 5–300 | Yes | immediate |
| `GEMINI_MAX_RETRIES` | `2` | 0–5 | Yes | immediate |
| `SYSTEM_PROMPT` | platform-neutral Korean default | 1–4000 chars, multi-line allowed | Yes | immediate |
| `FC_MAX_LOOPS` | `5` | 1–10 | Yes | immediate |
| `CONTEXT_TURNS` | `10` | 0–50 (0 = single-shot, no history kept) | Yes | immediate |
| `IDLE_RESET_MINUTES` | `30` | 0–1440 (0 = disabled) | Yes | immediate |
| `SHELL_TIMEOUT_SEC` | `45` | 1–600 | Yes | immediate |
| `SHELL_OUTPUT_LIMIT` | `3000` | 200–20000 (characters) | Yes | immediate |
| `POLL_TIMEOUT_SEC` | `30` | 1–50 | Yes | next poll cycle |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | Yes | immediate (via hook) |
| `WEB_ENABLED` | `true` | bool (`true/false/on/off/1/0/yes/no/켜기/끄기`) | Yes | listener reconcile |
| `WEB_PORT` | `8321` | 1024–65535, must bind free (checked before switching) | Yes | listener reconcile |
| `SHELL_PATH` | `auto` | `auto`, or an absolute path that exists, is executable, and (if `/etc/shells` exists) is listed there | Yes | immediate (next shell command) |
| `MCP_SERVERS` | `""` (disabled) | JSON array: `name` (`[a-zA-Z0-9_-]{1,32}`, unique), `url` (`http://`/`https://`), optional `headers` (string→string map) | No (web-only, secret) | immediate (next LLM call) |

`auto` resolution order (re-evaluated on every shell command, never cached):
`/bin/zsh` on macOS → `$SHELL` → the login shell from `pwd.getpwuid()` →
`/bin/sh`; each candidate must be an absolute, existing, executable file not
ending in `nologin`/`false`.

### MCP servers (HTTP)

`MCP_SERVERS` lets the Gemini LLM call tools exposed by remote MCP (Model
Context Protocol) servers, in addition to the built-in `execute_shell` and
`append_memory` tools. It is JSON: an array of `{"name": ..., "url": ...,
"headers": ...}` objects, e.g.:

```json
[
  {
    "name": "svc",
    "url": "https://mcp.example.com/",
    "headers": {"Authorization": "Bearer <token>"}
  }
]
```

**Supported:** the Streamable HTTP transport (every call is a single JSON
POST to `url`), static per-server `headers` (e.g. bearer tokens), and JSON
(non-streaming) responses.

**Not supported (by design):** the stdio transport, OAuth flows, and SSE
(`text/event-stream`) responses — a server that answers a call with an
`text/event-stream` Content-Type is logged as unsupported and its tools are
skipped for that server (see `src/agent/core/mcp.py`).

Because the value can contain bearer tokens or other credentials in
`headers`, `MCP_SERVERS` is treated as a secret: it can only be set from the
local web UI (masked everywhere, including `/settings`/`/get`), the same as
`TELEGRAM_BOT_TOKEN`/`GEMINI_API_KEY`.

Each remote tool is exposed to Gemini as `mcp_<server>_<tool>` (sanitized to
`[a-zA-Z0-9_-]` and truncated to 63 characters), with its description
prefixed `[MCP:<server>]` so replies can be traced back to their source.
Failures degrade gracefully: an unreachable server, invalid config, or an
SSE-only server simply has its tools omitted from that request — the
built-in tools and the rest of the chat continue to work normally.

### Secrets policy

`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, and `ALLOWED_USER_ID` can only be
changed from the local web UI. A Telegram `/set`/`/unset` attempt on any of
them is rejected with a "🔒 web-only" reply. For the two actual secrets
(`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`) the bot additionally makes a
best-effort attempt to delete the triggering message and recommends
rotating the value, since it may already be visible in the chat history;
`ALLOWED_USER_ID` is blocked the same way but is not masked or deleted since
the ID itself is not sensitive. A `TELEGRAM_BOT_TOKEN` change is verified
with a live `getMe` call before being accepted; a successful change also
shows a one-time "new bot @username" notice on the web page (useful if you
pointed the bot at a different Telegram bot account).

### Web UI

Always use `http://127.0.0.1:<WEB_PORT>/` — **not** `localhost` — the server
binds IPv4 only, and some systems resolve `localhost` to `::1` first, which
would fail to connect. There is no login, but every request is checked
against: Host header (must be `127.0.0.1:<port>` or `localhost:<port>`),
Origin (same-origin only, if present), `Sec-Fetch-Site` (`same-origin`/`none`
only, if present), and — for POSTs — a per-process CSRF token embedded in
the form. Changing `ALLOWED_USER_ID` requires an extra confirmation
checkbox and, once applied, notifies both the old and new Telegram user IDs.

### Lockout recovery

If a bad value locks you out (e.g. a mistyped `ALLOWED_USER_ID`, or
`WEB_ENABLED` turned off with the web UI as your only way back in): stop the
service, delete the offending key from `settings.json` (or delete the whole
file to fall back to `.env`/defaults), then restart the service. Because
`TELEGRAM_BOT_TOKEN`/`GEMINI_API_KEY`/`ALLOWED_USER_ID` can only be changed
from the web UI, Telegram commands alone can never lock you out of the bot.

## 8. Skills

`skills/` is a hybrid library: `.py` files are standalone scripts the LLM
runs deterministically via its `execute_shell` tool, and `.md` files are
procedure documents the LLM reads on demand and follows step by step. See
[`skills/README.md`](skills/README.md) for the authoring convention for
both. Skills are auto-listed in the LLM's system prompt as
`- skills/<name>.py: <first docstring line>` or
`- skills/<name>.md: <first line>` — there is no loader.

## 9. Security notes

- This bot executes arbitrary shell commands as your user — treat it as
  **RCE-equivalent access to this machine**. Keep `ALLOWED_USER_ID` correct;
  the process refuses to start polling without a valid value (fail-closed).
- `.env` and `settings.json` are created with `0600` permissions;
  `settings.json`'s permissions are checked and auto-corrected on every load
  if group/other-readable.
- `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` are stripped from the shell
  child's environment before every `execute_shell` call, so a plain `env` or
  `printenv` run by the LLM cannot exfiltrate them.
- Prompt injection via shell output or file contents is an inherent risk of
  an LLM that can call `execute_shell` without confirmation — don't point
  this bot at untrusted repositories, files, or command output.

## 10. Development

### Layout

```
src/agent/
  __main__.py       entry point: POSIX guard, single-instance lock, polling loop, signal handlers
  config.py         path constants, .env parser (parse_dotenv, pure function)
  settings.py       18-key SettingSpec catalog + SettingsStore (layering, precheck, persistence)
  core/
    shell.py        POSIX shell resolution + execution (timeout, process group, env scrubbing)
    memory.py       core MEMORY.md + per-day memory/YYYY-MM-DD.md read/append
    gemini.py       Gemini HTTP calls, model fallback chain, response parsing, tool registry
    mcp.py          HTTP MCP client (Streamable HTTP only): tool discovery + invocation
  telegram/
    client.py       Telegram Bot API calls (getUpdates, sendMessage, getMe, ...)
    handlers.py     command routing, settings commands, function-calling loop, history
  web/
    server.py       single-thread HTTPServer, security gate, routes
    page.py         HTML rendering (no external JS/CSS)
  utils/
    http.py         stdlib http_get/http_post/http_post_h helpers
launchd/            macOS launchd plist + install.sh (installs for both OSes)
systemd/            Linux/Pi systemd user unit
skills/             standalone stdlib scripts, auto-listed in the system prompt
memory/MEMORY.md    core long-term memory file (memory/YYYY-MM-DD.md holds per-day entries)
tests/              unittest suite
docs/VERIFICATION.md manual acceptance/verification playbook
```

### Coding rules

- Standard library only — never add a pip dependency.
- Python 3.8 floor: every module starts with `from __future__ import
  annotations`. This only defers annotation evaluation — it does **not**
  make 3.9+ *runtime* syntax legal. Do not use, among others: dict `|`
  merge/union, `str.removeprefix`/`removesuffix`, `match` statements,
  `zoneinfo`, `functools.cache`, `subprocess.process_group=` (use
  `start_new_session=True` instead). See `dev-plan.md` §7 ("3.8 호환 코딩
  규칙") for the full list.
- Settings are read with `settings.get(KEY)` **at call time**, never at
  import time and never cached in a module-level variable — this is what
  makes hot-apply work.
- Comments and docstrings are in English. User-facing strings (Telegram
  replies, web UI text, log messages) are Korean by design — do not
  translate them.

### Tests

```sh
PYTHONPATH=. python3 -m unittest discover tests -v
```

Some shell tests spawn real short-lived subprocesses (`sleep`, `echo`) to
exercise timeout/process-group handling, so they are not fully hermetic and
take a few seconds.

See [`docs/VERIFICATION.md`](docs/VERIFICATION.md) for the manual, on-device
acceptance playbook, and `dev-plan.md` for the full design history, decision
log, and risk checklist.
