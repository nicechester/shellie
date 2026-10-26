# Shellie

A personal Telegram agent that runs on your own machine. Zero pip dependencies —
Python 3.8+ standard library only. Current version: **1.2.1**.

## 1. Overview

Shellie is a single-user Telegram bot that runs continuously on a
machine you own (a Mac, a Linux PC, or a Raspberry Pi) and gives you two ways
to interact with it:

- **Bypass track** — commands are handled directly, with no LLM call: shell
  (`!<cmd>`, `/sh`, `/shell`), memory (`/mem`), session (`/reset`, `/restart`,
  `/queue`, `/kill`), tasks (`/continue`, `/discard`), files (`/file`,
  `/browse`), schedules (`/schedule`), MCP login (`/mcp`), logs (`/systemlog`),
  and settings (`/settings`, `/get`, `/set`, `/unset`). Shell commands run in ~0.1s.
- **LLM track** — any other message goes to the LLM. The chain is
  `LLM_MODEL_CHAIN`, a mix of Gemini (`gemini/<model>`) and any
  OpenAI-compatible server (`openai/<model>`, for example OpenRouter, Groq,
  LM Studio or Ollama). The default chain is Gemini only. Entries are tried in
  order and the next one is used on 429, 5xx or transport errors. The model has
  these tools:
  - `execute_shell`, `append_memory`, `execute_web_search`
  - `send_file`, `view_file` (look at a workspace file, including images)
  - `manage_schedule` (add, change or remove scheduled prompts)
  - `manage_skill` (create, update or remove user skills)
  - `propose_mcp_server` (propose adding or removing an MCP server; applied only after you confirm)
  - every tool exposed by configured MCP servers

  The model loops up to `FC_MAX_LOOPS` times per message. On reaching the limit,
  or on a detected repeat loop, it makes one final tools-disabled call to summarize.
  With `FC_MAX_CONTINUATIONS` > 0 it can resume an unfinished task automatically;
  otherwise use `/continue`.
- **Long-term memory** — split into a core file, `memory/MEMORY.md`
  (user-curated, permanent), and per-day files `memory/YYYY-MM-DD.md` that
  the LLM appends to with `append_memory`. `/mem` and every LLM call inject
  the core file plus today's dated file into the system instruction; older
  dated files are not injected but can be found with `grep -ri "<keyword>"
  memory/`.
- **Runtime-editable settings** — all 25 settings can be changed without a
  restart, from the local web UI or from Telegram with `/set` and `/unset`
  (except the web-only keys, listed in [§7](#7-settings)). See [§7](#7-settings).
- **MCP servers (optional)** — remote tools over Streamable HTTP, with static
  headers or an OAuth 2.0 login. Setup walkthrough, including a Google Drive
  example: **[docs/mcp-setup.md](docs/mcp-setup.md)**. Summary in [§7](#mcp-servers-http).
- **Skills** — built-in and user skills the LLM can use. See [§8](#8-skills).

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
  `generativelanguage.googleapis.com`. Add the host of any OpenAI-compatible
  server you use, and of any MCP server you configure.
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

### Directories (config vs. workspace)

Shellie uses two separate directories:

- **`SHELLIE_HOME`** (default: `~/.shellie/`) — configuration and context:
  `settings.json`, memory files, user skills (`skills/`), MCP login tokens
  (`mcp_oauth.json`), schedules, the task file and the offset file
- **`SHELLIE_WORKSPACE`** (default: `~/workspace/`) — work products: job
  outputs, generated code, documents created by the LLM, and files you upload

You can configure both via environment variables or `.env` file:

```sh
# Via command line
SHELLIE_HOME=~/.config/shellie SHELLIE_WORKSPACE=~/my-work PYTHONPATH=. python3 -m src.agent

# Via .env file (read at startup)
echo "SHELLIE_HOME=/path/to/config" >> .env
echo "SHELLIE_WORKSPACE=/path/to/workspace" >> .env
PYTHONPATH=. python3 -m src.agent
```

This keeps configuration organized separately from the transient outputs of
work done by prompts, and both are outside the Shellie repository tree.

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
exit immediately — but even if that guard were bypassed, two processes polling
`getUpdates` with the same bot token race each other and Telegram returns 409
Conflict to one of them.

## 5. Service installation

**One-line install** (no checkout needed; macOS or Linux, Python 3.8+):

```sh
curl -fsSL https://raw.githubusercontent.com/nicechester/shellie/main/launchd/install.sh | sh
```

This downloads the latest release into `~/.local/share/shellie/shellie`,
installs the service, and starts it. To install a specific tag or branch:

```sh
curl -fsSL https://raw.githubusercontent.com/nicechester/shellie/main/launchd/install.sh | SHELLIE_REF=1.0.0 sh
```

Running the same command again upgrades in place and restarts the service.
Your `.env` and `settings.json` are kept across upgrades.

**From a checkout:**

```sh
sh launchd/install.sh
```

Either way, the script detects the OS (`uname -s`) and installs the correct
service (macOS → launchd, Linux → systemd user unit), and `chmod 600`s
`.env`/`settings.json` if present.

**Next steps.** Shellie starts in setup-required mode until
`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY` and `ALLOWED_USER_ID` are set. Fill
them in at `http://127.0.0.1:8321/`, or create `.env` in the project root
and restart the service.

**Uninstall:**

```sh
sh ~/.local/share/shellie/shellie/launchd/uninstall.sh   # one-line install
sh launchd/uninstall.sh                                  # from a checkout
```

This stops and removes the service. Your data in `~/.shellie` and
`~/workspace` is kept.

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
| `!<cmd>` / `/sh <cmd>` | Bypass (shell) | Runs `<cmd>` in the resolved shell (see `SHELL_PATH` in [§7](#7-settings)), no LLM call |
| `!!` / `/shell` | Bypass (shell mode) | Toggles shell mode: every following message runs as a shell command in one terminal message edited in place, with no LLM calls. `cd` persists within the session (shown in the terminal header); variables/exports do not. Requires a POSIX shell (sh/bash/zsh/dash/ksh); with other shells commands run in the last directory without tracking `cd`. Your command messages are deleted to keep the terminal visible. `/reset` also leaves shell mode. Not persisted across restarts. |
| `exit` *(in shell mode)* | Bypass (shell mode) | Leaves shell mode. Only special while shell mode is active; otherwise it goes to the LLM like any text. |
| `/mem` | Bypass | Prints long-term memory: core `MEMORY.md` plus today's dated file (`memory/YYYY-MM-DD.md`) |
| `/reset` | Bypass | Clears conversation history and the queue (context only, not `MEMORY.md`, not schedules) |
| `/kill` | Bypass | Drains the queue and discards the unfinished task, keeping conversation history |
| `/queue` | Bypass | Shows queue status, plus live Gemini retry or cooldown state |
| `/restart` | Bypass | Exits the process; the service manager restarts it. Warns if no service manager is detected. Use it to pick up **code** changes — setting changes already hot-apply |
| `/systemlog [N]` | Bypass | Last N log lines (default 50, max 500): from the journal under systemd, from `agent.log` under launchd |
| `/help` | Bypass | Shows the version and the command reference |
| `/settings`, `/env` | Bypass (settings) | Lists all 25 keys with current value (masked if secret), source, and apply timing |
| `/get KEY`, `/env KEY` | Bypass (settings) | Detail view for one key: value, source, default, constraint, apply timing |
| `/set KEY VALUE` | Bypass (settings) | Applies an override immediately (rejected for web-only keys) |
| `/unset KEY` | Bypass (settings) | Removes an override, falling back to the env/default layer |
| `/continue` | Bypass (task) | Resume the unfinished task (stopped, errored, or left over from a restart) |
| `/discard` | Bypass (task) | Drop the unfinished task without resuming |
| `/file <path>` | Bypass | Send a file from `SHELLIE_WORKSPACE` to you |
| `/browse [path]` | Bypass | List files in a directory with an inline-keyboard menu: tap a folder to open it, a file to download it |
| `/schedule` | Bypass (schedule) | List all schedules |
| `/schedule add <spec> <prompt>` | Bypass (schedule) | Add a new recurring or one-shot schedule |
| `/schedule rm <id>` | Bypass (schedule) | Remove a schedule |
| `/schedule on <id>` / `/schedule off <id>` / `/schedule run <id>` | Bypass (schedule) | Enable, disable, or run a schedule now |
| `/mcp` | Bypass (MCP) | Shows the login state of OAuth MCP servers |
| `/mcp auth <server>` | Bypass (MCP) | Sends the approval link for an OAuth server (valid 15 minutes) |
| `/mcp finish <server> <URL>` | Bypass (MCP) | Completes an OAuth login from the address you copied back. Shellie deletes that message, since it holds a one-time code |
| `/mcp logout <server>` | Bypass (MCP) | Forgets the stored login for a server |
| anything else | LLM | Sent with the tools listed in [§1](#1-overview). Function-calling loop up to `FC_MAX_LOOPS`, with up to `CONTEXT_TURNS` recent turns of history; older turns are compacted into a rolling summary |

Adding skills and MCP servers needs no command: ask in chat, for example
`add MCP server gdrive at https://drivemcp.googleapis.com/mcp/v1 with OAuth`,
or `create a skill that …`. MCP changes show a 🔌 proposal with ✅ Confirm and
✖ Cancel, and nothing changes until you confirm. Skill changes are announced
with 🧩.

### Attachments (multimodal uploads)

When you send files via Telegram, Shellie includes them in your next LLM message:

**Supported types:**
- **Images, PDFs, audio, and video** → sent to the model as base64 `inlineData` parts (processed directly).
- **Text and code files** → content is inlined as plain text in your message.

**Limits:**
- **5 MB total per message** (all attachments combined).
- **Up to 5 attachments** per message.
- **100 KB per text file** (larger text files are saved with a path note only, not inlined).

**No caption?** If you send a file without a message, Shellie replies with "What would you like me to do with it? I'll include it with your next message." — no LLM call. This reply is skipped for subsequent items in an album (media_group_id deduplication).

**Files already on disk:** Files already in the workspace can be viewed too — ask about a file by name and the model attaches it itself via `view_file`.

**History cleanup:** After each turn, attachment content is removed from conversation history and replaced with "[attachment removed from history: …]" placeholders to keep request sizes bounded. Re-send the file to ask about it again later.

### Scheduled prompts

Shellie can run recurring prompts on a schedule, without you needing to send a message:

**Spec grammar** (in local time, including DST; examples):
- `daily 09:00` or `daily 09:00,18:00` — every day at that time(s)
- `weekdays 08:30` or `weekdays 08:30,17:00` — Monday–Friday at that time(s)
- `weekly mon,thu 09:00` or `weekly mon,thu 09:00,14:00` — specific days and time(s)
- `hourly :15` — every hour at that minute (e.g. at :15 past each hour)
- `every 30m` / `every 2h` — every N minutes or hours, aligned to local midnight (min **15 minutes**, max 24 hours). `every 30m` fires at :00 and :30 of each hour; `every 2h` fires at 00:00, 02:00, 04:00, etc. Slots do not drift across restarts.
- `at 2026-10-05 14:30` — **one-shot, fires once** at that date and time (must be in the future), then shows as done.

**Schedule names:**
- Optional short label (≤60 chars) to help identify a schedule. The model sets these automatically on add to make future changes easier (e.g. "news briefing" instead of just "id 3").

**Constraints:**
- **Max 20 schedules** per bot instance.
- Schedules are stored in `~/.shellie/schedules.json`.
- Schedule **IDs are small integers, never reused** across the lifetime of the instance.
- The prompt must be **self-contained** — it runs with no conversation context or chat history.
- **Scheduled runs never replace an unfinished interactive task** — they run independently and post a ⏰ notice when starting.
- **History handling:** Daily/weekdays/weekly runs add their results to chat history (you can follow up with questions). Interval (every/hourly) runs are **not** added to history (protects your ongoing conversation context).
- Missed runs (>10 minutes late due to downtime, laptop sleep, restarts) are **skipped, never replayed**.
- At most one queued/running instance per schedule.
- **Quota note:** each run costs LLM requests; the 15-minute minimum floor protects free-tier daily limits.

**Management:**
- List, add, remove, enable, disable and run: see the `/schedule` commands in [§6](#6-telegram-commands), or ask the model in chat (it uses `manage_schedule`).
- Model-made schedule changes (via chat) are announced in chat with **🗓️** for visibility.
- `/reset` clears the conversation history and queue but **does not** affect schedules.

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

### 25-key reference

| Key | Default | Range / format | Telegram-editable | Apply timing |
|---|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | `^\d{5,16}:[A-Za-z0-9_-]{30,64}$` | No (web-only, secret) | next poll cycle |
| `GEMINI_API_KEY` | *(required)* | 20–128 chars, `[A-Za-z0-9._-]` | No (web-only, secret) | immediate |
| `ALLOWED_USER_ID` | *(required)* | integer 1 – 2^53−1, cannot equal the bot's own ID | No (web-only) | immediate |
| `LLM_MODEL_CHAIN` | `gemini/gemini-2.5-flash,gemini/gemini-2.5-pro,gemini/gemini-2.5-flash-lite` | Comma-separated `gemini/<model>` and `openai/<model>` entries, tried in order; no limit on count, no duplicates | Yes | immediate (next LLM call) |
| `GEMINI_TIMEOUT_SEC` | `120` | 5–300 | Yes | immediate |
| `GEMINI_FALLBACK_DELAY_SEC` | `1` | 0–10 | Yes | immediate |
| `GEMINI_RETRY_BASE_DELAY_SEC` | `60` | 5–300 | Yes | immediate |
| `GEMINI_MAX_RETRIES` | `2` | 0–5 | Yes | immediate |
| `SYSTEM_PROMPT` | language-adaptive default | 1–4000 chars, multi-line allowed | Yes | immediate |
| `FC_MAX_LOOPS` | `15` | 1–50 | Yes | immediate |
| `FC_MAX_CONTINUATIONS` | `2` | 0–5 | Yes | immediate |
| `CONTEXT_TURNS` | `10` | 0–50 (0 = single-shot, no history kept; older turns are compacted into a rolling summary) | Yes | immediate |
| `IDLE_RESET_MINUTES` | `1440` | 0–1440 (1440 = 24 h, 0 = disabled) | Yes | immediate |
| `SHELL_TIMEOUT_SEC` | `45` | 1–600 | Yes | immediate |
| `SHELL_OUTPUT_LIMIT` | `3000` | 200–20000 (characters) | Yes | immediate |
| `POLL_TIMEOUT_SEC` | `30` | 1–50 | Yes | next poll cycle |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | Yes | immediate (via hook) |
| `WEB_ENABLED` | `true` | bool (`true/false/on/off/1/0/yes/no/켜기/끄기`) | Yes | listener reconcile |
| `WEB_PORT` | `8321` | 1024–65535, must bind free (checked before switching) | Yes | listener reconcile |
| `SHELL_PATH` | `auto` | `auto`, or an absolute path that exists, is executable, and (if `/etc/shells` exists) is listed there | Yes | immediate (next shell command) |
| `MCP_SERVERS` | `""` (disabled) | JSON array; see [MCP servers](#mcp-servers-http) | No (web-only, secret) | immediate (next LLM call) |
| `SEARCH_MODEL` | `gemma-4-26b-a4b-it` | Single model name | Yes | immediate (next web search) |
| `OPENAI_API_BASE` | `https://api.openai.com/v1` | `http://` or `https://` URL (e.g. `http://localhost:11434/v1` for Ollama) | Yes | immediate (next LLM call) |
| `OPENAI_API_KEY` | `""` (none) | No whitespace, at most 512 chars; empty for local servers | No (web-only, secret) | immediate (next LLM call) |
| `OPENAI_TIMEOUT_SEC` | `120` | 5–300 | Yes | immediate (next LLM call) |

#### LLM fallback chain (`LLM_MODEL_CHAIN`)

Each entry is `<provider>/<model>`, where the provider is `gemini` or `openai`:

```
gemini/gemini-2.5-flash,gemini/gemini-2.5-pro,openai/gpt-4o-mini
```

- `gemini/` calls the Gemini API with `GEMINI_API_KEY`.
- `openai/` calls an OpenAI-compatible server at `OPENAI_API_BASE` with
  `OPENAI_API_KEY`. One `OPENAI_*` set covers OpenAI, OpenRouter, Groq, LM Studio
  and Ollama. Use the server's own model names: for example, on OpenRouter the
  entry is `openai/openai/gpt-4o-mini`, because the entry is split at the first `/`
  only. Groq's `openai/gpt-oss-120b` is entered as `openai/openai/gpt-oss-120b`.
- Any other provider prefix is rejected when the setting is saved.
- A fallback-eligible failure (429, 5xx, transport, and 404/408 on `openai/`) moves to the
  next entry. When a whole pass fails, the chain cools down and repeats, governed by
  `GEMINI_RETRY_BASE_DELAY_SEC` and `GEMINI_MAX_RETRIES`. Those timing settings apply to
  every provider in the chain. Other statuses (for example 401 or 403) fail immediately.
- An `openai/` entry with an empty `OPENAI_API_KEY` is skipped with a warning, unless
  `OPENAI_API_BASE` points at a local or LAN address (for example Ollama).
- Requests to OpenAI-compatible servers send a `Shellie/<version>` User-Agent. Some
  hosts behind Cloudflare reject Python's default User-Agent with 403.
- Conversations keep working when the chain switches provider mid-conversation.

Migration: `LLM_BACKEND`, `GEMINI_MODEL_CHAIN` and `OPENAI_MODEL_CHAIN` are read once
from `.env` and `settings.json` and folded into `LLM_MODEL_CHAIN`. With
`LLM_BACKEND=openai`, the result is the `openai/` entries from `OPENAI_MODEL_CHAIN`.
Otherwise it is the `gemini/` entries from `GEMINI_MODEL_CHAIN`. Once a layer sets
`LLM_MODEL_CHAIN`, its legacy keys are ignored.

`auto` resolution order (re-evaluated on every shell command, never cached):
`/bin/zsh` on macOS → `$SHELL` → the login shell from `pwd.getpwuid()` →
`/bin/sh`; each candidate must be an absolute, existing, executable file not
ending in `nologin`/`false`.

### MCP servers (HTTP)

`MCP_SERVERS` lets the LLM call tools on remote MCP (Model Context Protocol)
servers, alongside the built-in tools. The full walkthrough, including a Google
Drive example, is in **[docs/mcp-setup.md](docs/mcp-setup.md)**. This section is
the reference.

**Supported:** Streamable HTTP only (each call is a JSON POST to `url`), static
`headers`, OAuth 2.0 with PKCE, and JSON responses. SSE responses
(`text/event-stream`) and stdio servers are not supported; an SSE-only server is
logged and its tools are skipped.

Each entry is an object with `name`, `url`, and optional `headers` and `oauth`:

```json
[
  {
    "name": "svc",
    "url": "https://mcp.example.com/",
    "headers": {"Authorization": "Bearer <token>"}
  }
]
```

- `name`: 1–32 characters, letters, digits, `-` and `_`. Unique.
- Each remote tool is exposed to the LLM as `mcp_<server>_<tool>` (sanitized and
  truncated to 63 characters), with its description prefixed `[MCP:<server>]`.
- Tool lists are cached for 300 seconds. Changes apply on the next LLM call.

**OAuth (optional):** an `oauth` block runs the authorization-code flow with PKCE.
Shellie runs headless, so the login is copy-paste based:

```json
{
  "name": "bank",
  "url": "https://mcp.example.com/",
  "oauth": {
    "authorization_url": "https://auth.example.com/authorize",
    "token_url": "https://auth.example.com/token",
    "redirect_uri": "http://localhost:8787/callback",
    "client_id": "<client id>",
    "client_secret": "<optional, for confidential clients>",
    "scope": "<optional>"
  }
}
```

1. `/mcp auth bank` sends an approval link, valid 15 minutes.
2. Open the link on your own device and approve. The browser then fails to load
   `redirect_uri`. That's expected.
3. Copy the full address from the address bar and send `/mcp finish bank <URL>`.
   Shellie deletes that message, checks the state, and exchanges the code for tokens.

Rules:
- `authorization_url` and `token_url` must be `https://` (plain `http://` only for
  `localhost` and loopback addresses).
- Shellie asks for offline access, so the provider can issue a refresh token. The
  access token renews without another login. A login made before this was added
  needs `/mcp logout bank`, then `/mcp auth bank`.
- Tokens are stored in `~/.shellie/mcp_oauth.json` (0600). A stored login applies
  only while the server's `url`, `token_url` and `client_id` stay the same.
- If the provider rejects the refresh token (`invalid_grant`), the stored login is
  removed and you're asked to log in again.
- `redirect_uri` must be registered with the provider exactly as written.

**Adding a server:**

- **From chat:** ask in plain words. Shellie shows a 🔌 proposal with ✅ Confirm and
  ✖ Cancel. Nothing changes until you tap Confirm, and proposals expire after 10
  minutes. For an OAuth server, Confirm also sends the approval link. Chat can add
  or remove a server without headers or a `client_secret`. Each confirmed change is
  recorded in the settings audit log as actor `chat`.
- **From the web settings page:** required for static headers and for a
  `client_secret`, because `MCP_SERVERS` is a web-only secret. See
  [§7 Web UI](#web-ui) for remote access.

Because `MCP_SERVERS` can contain bearer tokens or client secrets, it is masked
everywhere, including `/settings` and `/get`, and it cannot be set with `/set`.

### Secrets policy

`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `ALLOWED_USER_ID`, `OPENAI_API_KEY` and
`MCP_SERVERS` can only be changed from the local web UI. A Telegram `/set`/`/unset`
attempt on any of them is rejected with a "🔒 web-only" reply. For the two actual
secrets (`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`) the bot additionally makes a
best-effort attempt to delete the triggering message and recommends
rotating the value, since it may already be visible in the chat history;
`ALLOWED_USER_ID` is blocked the same way but is not masked or deleted since
the ID itself is not sensitive. A `TELEGRAM_BOT_TOKEN` change is verified
with a live `getMe` call before being accepted; a successful change also
shows a one-time "new bot @username" notice on the web page (useful if you
pointed the bot at a different Telegram bot account).

Chat-driven MCP changes (`propose_mcp_server` and the Confirm button) never write
headers or a `client_secret`, so they stay within this rule.

### Web UI

The web UI listens on `127.0.0.1` only. The bind address is fixed in code and
can't be changed from settings. There is no login, so this is deliberate: anyone
on the network could otherwise change the bot token or API keys.

Open it on the same machine as `http://127.0.0.1:<WEB_PORT>/` — **not**
`localhost`, because the server binds IPv4 only, and some systems resolve
`localhost` to `::1` first.

**From another machine (for example, your laptop):** use an SSH tunnel and keep
the local port equal to `WEB_PORT`, because the server only accepts the Host
`127.0.0.1:<port>` or `localhost:<port>`:

```sh
ssh -L 8321:127.0.0.1:8321 <pi-user>@<pi-host>
```

Then open `http://127.0.0.1:8321` on your laptop while the tunnel is running.

Every request is checked against: Host header, Origin (same-origin only, if
present), `Sec-Fetch-Site` (`same-origin`/`none` only, if present), and — for
POSTs — a per-process CSRF token embedded in the form. Changing `ALLOWED_USER_ID`
requires an extra confirmation checkbox and, once applied, notifies both the old
and new Telegram user IDs.

### Lockout recovery

If a bad value locks you out (e.g. a mistyped `ALLOWED_USER_ID`, or
`WEB_ENABLED` turned off with the web UI as your only way back in): stop the
service, delete the offending key from `settings.json` (or delete the whole
file to fall back to `.env`/defaults), then restart the service. Because
`TELEGRAM_BOT_TOKEN`/`GEMINI_API_KEY`/`ALLOWED_USER_ID` can only be changed
from the web UI, Telegram commands alone can never lock you out of the bot.

### Task persistence

In-flight task state is persisted to `~/.shellie/tasks/current.json` (mode 0600): the file is created when an LLM task starts, updated on each wrap-up/continuation, and deleted when the task completes (or on `/reset`, `/kill`, `/discard`). On restart, the process does **not** auto-resume; instead it notifies you about the unfinished task and waits for `/continue` (resume) or `/discard` (drop). Task files expire after 3 days of inactivity and are deleted automatically.

## 8. Skills

Skills come in two places, and a user skill with the same name overrides a built-in one:

- **Built-in** — `skills/` in the repository. `.py` files are standalone scripts that
  the LLM runs through `execute_shell`. `.md` files are procedure documents the LLM
  reads and follows step by step. See [`skills/README.md`](skills/README.md) for the
  authoring convention.
- **User** — `~/.shellie/skills/`. Created or changed by the LLM with the
  `manage_skill` tool when you ask in chat (for example, "create a skill that …"),
  or by you directly in the folder.

Skills are auto-listed in the LLM's system prompt as
`- skills/<name>.py: <first docstring line>` or
`- skills/<name>.md: <first line>`. A new or changed skill is visible on the next message; there is no loader.

`manage_skill` rules:
- Names are lowercase letters, digits, `-` and `_`, ending in `.md` or `.py`.
- Built-in names can't be changed or removed.
- Content is limited to 20,000 characters, and must not be empty.
- Every change is announced in chat with 🧩.
- A `.py` skill runs through `execute_shell`, which the model already has. Only ask
  for skills you trust, and read them before running.

## 9. Security notes

- This bot executes arbitrary shell commands as your user — treat it as
  **RCE-equivalent access to this machine**. Keep `ALLOWED_USER_ID` correct;
  the process refuses to start polling without a valid value (fail-closed).
- `.env` and `settings.json` are created with `0600` permissions;
  `settings.json`'s permissions are checked and auto-corrected on every load
  if group/other-readable. MCP login tokens are kept in `mcp_oauth.json` (0600).
- `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` are stripped from the shell
  child's environment before every `execute_shell` call, so a plain `env` or
  `printenv` run by the LLM cannot exfiltrate them.
- Prompt injection via shell output, file contents, web search results, MCP tool
  output, or Drive documents is an inherent risk of an LLM that can call
  `execute_shell` without confirmation. Don't point this bot at untrusted
  repositories, files, or command output.
- MCP servers receive whatever the model sends them, including the arguments of
  each tool call. Add only servers you trust. Chat-proposed servers still need your
  Confirm tap, and the proposal shows the URL.
- OAuth: codes are single-use, the pending login is held in memory for 15 minutes,
  and the state is checked before the code is exchanged. The client secret must be
  entered in the web UI.

## 10. Development

### Layout

```
src/agent/
  __main__.py       entry point: POSIX guard, single-instance lock, polling loop, signal handlers
  config.py         path constants, .env parser (parse_dotenv, pure function)
  settings.py       25-key SettingSpec catalog + SettingsStore (layering, precheck, persistence)
  version.py        __version__ (keep in sync with pyproject.toml)
  core/
    shell.py        POSIX shell resolution + execution (timeout, process group, env scrubbing)
    memory.py       core MEMORY.md + per-day memory/YYYY-MM-DD.md read/append
    gemini.py       LLM call loop, model chain fallback, response parsing, tool registry
    openai.py       OpenAI-compatible backend (message conversion, attempt_model)
    llm.py          call_llm: walks the mixed LLM_MODEL_CHAIN across providers
    mcp.py          HTTP MCP client (Streamable HTTP only): tool discovery + invocation
    mcp_oauth.py    OAuth code+PKCE login for MCP servers (copy-paste, token store, refresh)
    chat_admin.py   skill create/update/remove and MCP add/remove proposals with confirmation
    scheduler.py    scheduled prompts: spec parsing, persistence, ticker thread
    tasks.py        task persistence for continuation across restarts
  telegram/
    client.py       Telegram Bot API calls (getUpdates, sendMessage, getMe, ...)
    handlers.py     command routing, settings and /mcp commands, function-calling loop, history
    files.py        attachments, uploads, /file and /browse helpers
  web/
    server.py       single-thread HTTPServer, security gate, routes
    page.py         HTML rendering (no external JS/CSS)
  utils/
    http.py         stdlib http_get/http_post/http_post_h helpers
launchd/            macOS launchd plist, install.sh and uninstall.sh (both OSes)
systemd/            Linux/Pi systemd user unit
skills/             built-in skills, auto-listed in the system prompt
memory/MEMORY.md    core long-term memory file (memory/YYYY-MM-DD.md holds per-day entries)
docs/mcp-setup.md   MCP server setup guide, with a Google Drive example
docs/gemini_429_observations.md   notes on Gemini 429 behavior
tests/              unittest suite
```

### Coding rules

- Standard library only — never add a pip dependency.
- Python 3.8 floor: every module starts with `from __future__ import
  annotations`. This only defers annotation evaluation — it does **not**
  make 3.9+ *runtime* syntax legal. Do not use, among others: dict `|`
  merge/union, `str.removeprefix`/`removesuffix`, `match` statements,
  `zoneinfo`, `functools.cache`, `subprocess.process_group=` (use
  `start_new_session=True` instead).
- Settings are read with `settings.get(KEY)` **at call time**, never at
  import time and never cached in a module-level variable — this is what
  makes hot-apply work.
- Comments, docstrings and user-facing strings (Telegram replies, web UI text,
  log messages) are in English.

### Tests

```sh
PYTHONPATH=. python3 -m unittest discover tests -v
```

Some shell tests spawn real short-lived subprocesses (`sleep`, `echo`) to
exercise timeout/process-group handling, so they are not fully hermetic and
take a few seconds.
