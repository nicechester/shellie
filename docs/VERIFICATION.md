# Manual Verification Playbook

This is the on-device acceptance checklist for Shellie (task 5.6). It
is a manual playbook: Chester runs each item against a real bot/device and
records the result. Nothing here is executed by an agent.

## 1. Preconditions & cautions

- [ ] **Never run a foreground instance while the service (launchd/systemd)
      is installed and running.** The `.shellie.lock` flock guard makes the
      second process print `다른 인스턴스가 실행 중입니다.` and exit(1)
      immediately. If that guard were somehow bypassed, two processes
      polling `getUpdates` with the same bot token race each other and
      Telegram returns 409 Conflict to the loser. Stop the service
      (`systemctl --user stop shellie` / `launchctl bootout
      gui/$(id -u) ...`) before starting a foreground instance for testing.
- [ ] **RSS measurement:** `ps -o rss= -p <pid>` (value is in KB; divide by
      1024 for MB). Take the reading after the process has been idle for at
      least a minute post-startup (steady state), not immediately after
      launch.
- [ ] **Latency measurement:** either (a) stopwatch from hitting "send" in
      Telegram to the reply appearing, or (b) compare the log timestamp of
      the incoming update to the log timestamp of the outgoing
      `sendMessage`/`execute_shell` result line.

## 2. Foreground smoke

- [ ] Start with a complete `.env` (`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`,
      `ALLOWED_USER_ID` all set): `PYTHONPATH=. python3 -m src.agent` prints
      startup log lines and begins polling without exiting.
      **Expected:** process stays up; log shows the web server starting
      (if `WEB_ENABLED` is true) and no "설정 필요" message.
- [ ] Start with `.env` missing one or more required keys (setup-required
      mode). **Expected:** the web UI comes up at `http://127.0.0.1:8321/`
      (or your configured `WEB_PORT`), polling does **not** start, and the
      log shows `설정 필요: http://127.0.0.1:<port>/ (누락: ...)`. Fill in the
      missing value(s) from the web form and submit — **expected:** within
      ~2 seconds (the setup-required poll interval), polling starts with no
      process restart (check the log for `필수값 입력 완료, polling 시작`
      and confirm the PID hasn't changed).
- [ ] Start with required values missing **and** `WEB_ENABLED=false` (set in
      `.env`). **Expected:** the process prints the missing-keys message and
      the "웹 설정이 꺼져 있어 종료합니다." message, then exits(1) — no
      setup-required mode, no web server.

## 3. Shell bypass

- [ ] `!ls -la` from the allowed account returns the project directory
      listing with no Gemini call in the log.
- [ ] `/sh echo hi` returns `hi` the same way.
- [ ] Set `SHELL_OUTPUT_LIMIT` low (e.g. via `/set SHELL_OUTPUT_LIMIT 200`)
      and run a command producing long output. **Expected:** output is
      truncated at the configured character count with a trailing
      `... (출력이 길어 N자에서 잘림)` note.
- [ ] Set `SHELL_TIMEOUT_SEC` low first (e.g. `/set SHELL_TIMEOUT_SEC 3`),
      then run `!sleep 30`. **Expected:** a timeout message
      (`에러: 명령어 실행 시간 초과 (3초 제한).`) after ~3s (plus the ~2s
      SIGTERM grace period before SIGKILL if the process ignores TERM), the
      bot remains responsive to the next command, and no orphan `sleep`
      process remains (`pgrep sleep` shows nothing after the grace period).
- [ ] Send `!id` (or any shell command) from a Telegram account that is
      **not** `ALLOWED_USER_ID`. **Expected:** no reply at all in the chat;
      the agent's log shows a `미허가 접근: user_id=...` warning line
      (silent rejection, not an error reply — this is deliberate, so an
      unauthorized prober can't confirm the bot is alive).

## 4. Memory

- [ ] `/mem` immediately prints core `memory/MEMORY.md` plus today's
      `memory/<YYYY-MM-DD>.md` section (or `등록된 메모리가 없습니다.` if both
      are empty/missing) with no LLM call.
- [ ] Send a natural-language request that should trigger memory, e.g.
      "이거 기억해줘: 나는 아침형 인간이다." **Expected:** the LLM calls
      `append_memory`, the reply confirms it, and the entry lands in today's
      `memory/<YYYY-MM-DD>.md` file (not `MEMORY.md`); a follow-up `/mem`
      shows a new `- ...` line under a `## <today's date>` section.
- [ ] Seed a dated file for a previous day (e.g. copy today's dated file to
      yesterday's date) and confirm `/mem` does **not** include its
      content — only the core file and today's dated file are injected.
- [ ] Send a new message afterward that depends on the just-stored fact
      (e.g. ask what time you prefer to work). **Expected:** the reply
      reflects the newly stored memory — confirms the system instruction is
      rebuilt (via `read_memory()`) on every LLM call, not cached from
      process start.
- [ ] Ask the bot to recall something known to be in an older (non-today)
      dated file. **Expected:** since it is not injected into the system
      instruction, the bot uses `execute_shell` to run something like
      `grep -ri "<keyword>" memory/` (per the hint line in the system
      instruction) rather than answering from memory it doesn't have.

## 5. LLM track

- [ ] Send an ordinary conversational message (no shell/memory intent).
      **Expected:** a normal Gemini reply, HTML-rendered in Telegram
      (`<b>`/`<code>`/`<pre>` where the source had markdown), no shell
      execution in the log.
- [ ] Send a request that requires a shell action (e.g. "현재 디렉터리
      파일 목록 보여줘"). **Expected:** the log shows an `execute_shell`
      function call and its result folded into the final reply.
- [ ] Force a 429/`RESOURCE_EXHAUSTED` response from the first model in
      `GEMINI_MODEL_CHAIN` (e.g. by exhausting your quota, or temporarily
      setting `GEMINI_MODEL_CHAIN` to a chain with a deliberately-wrong
      first model name to trigger a fast failure and confirm the fallback
      path executes). **Expected:** a log line like `Gemini 모델 <model>
      호출 실패(429), 다음 모델로 전환`, then a successful reply from the
      next model in the chain, after waiting `GEMINI_FALLBACK_DELAY_SEC`.
- [ ] Send a message long enough to produce a reply over 4096 characters
      (e.g. ask for a very long explanation). **Expected:** the reply
      arrives as multiple Telegram messages (verify in `split_message`
      behavior: split on the last newline before the limit, `<pre>` tags
      re-opened/closed correctly across chunks), with no content dropped.

## 6. Context

- [ ] Have a two-turn conversation where the second message references the
      first ("그거 다시 설명해줘" after an earlier answer). **Expected:**
      the LLM's reply shows it retained the prior turn (up to
      `CONTEXT_TURNS` turns are kept).
- [ ] `/reset` clears the history. **Expected:** confirmation message
      `🔄 대화 맥락을 초기화했습니다.`, and a subsequent message that
      references the earlier conversation gets no context (the LLM has no
      memory of it).
- [ ] Set `IDLE_RESET_MINUTES=1`, send a message, wait over a minute without
      sending anything else, then send a message that would only make sense
      with prior context. **Expected:** the idle gap silently resets history
      before the new message is processed (same effect as `/reset`, but
      automatic — check `_last_activity`/`_history` behavior indirectly via
      the LLM's lack of context).

## 7. Settings hot-apply

- [ ] From the web UI, change `SHELL_TIMEOUT_SEC` (e.g. to 5) and save.
      **Expected:** the next `!sleep 10` times out at ~5s, not the old
      value — no restart needed.
- [ ] `/set GEMINI_MODEL_CHAIN gemini-2.5-flash-lite,gemini-2.5-flash` from
      Telegram. **Expected:** confirmation
      `✅ GEMINI_MODEL_CHAIN: <old> → <new> (적용: 즉시)`; the next LLM call
      log shows the new chain being tried in order; after a full
      restart (`/restart` or service restart), `/get GEMINI_MODEL_CHAIN`
      still shows the new value with source `override` (persisted in
      `settings.json`).
- [ ] Attempt `/set TELEGRAM_BOT_TOKEN 123:abc` and `/set GEMINI_API_KEY
      xyz` from Telegram. **Expected:** both rejected with a `🔒 ... 로컬
      웹에서만 변경할 수 있습니다.` reply, and the bot makes a best-effort
      `deleteMessage` call on your triggering message (confirm the message
      you sent disappears from the chat, or check the log for a
      `deleteMessage` call if Telegram's delete window has passed).
      Attempt `/set ALLOWED_USER_ID 1` too. **Expected:** same style of
      rejection, but **no** delete-message attempt (this key is web-only
      but not treated as a secret).
- [ ] CSRF/Host spot-checks against the web UI (replace `<port>` with your
      `WEB_PORT`, default 8321):
      ```sh
      # Wrong Host header -> expect 403
      curl -i -X POST "http://127.0.0.1:<port>/settings" \
           -H "Host: evil.example.com" \
           -H "Content-Type: application/x-www-form-urlencoded" \
           --data "v.SHELL_TIMEOUT_SEC=5"

      # Correct Host, but missing CSRF token -> expect 403
      curl -i -X POST "http://127.0.0.1:<port>/settings" \
           -H "Content-Type: application/x-www-form-urlencoded" \
           --data "v.SHELL_TIMEOUT_SEC=5"
      ```
      **Expected:** both return HTTP 403 with a short HTML error page; the
      log shows a `web request rejected code=403 reason=...` warning. A
      legitimate browser form submission (GET the page first, copy the
      `csrf` hidden field value, submit with the right Host) should succeed
      with a 303 redirect to `/?saved=N`.
- [ ] Change `WEB_PORT` from the web UI (e.g. 8321 → 8322). **Expected:** a
      new listener comes up on the new port first, then the old port stops
      accepting connections; `curl http://127.0.0.1:8321/` after the switch
      should fail to connect, while `http://127.0.0.1:8322/` serves the
      page. If the new port is already in use, **expected:** the change is
      rejected up front (precheck fails with "포트 사용 중").
- [ ] `/set WEB_ENABLED off` from Telegram, then confirm
      `http://127.0.0.1:<port>/` stops responding shortly after. Then
      `/set WEB_ENABLED on` from Telegram. **Expected:** the web listener
      comes back up on the same `WEB_PORT` without a process restart.
- [ ] Change `ALLOWED_USER_ID` from the web UI (with the confirmation
      checkbox checked). **Expected:** both the old and the new Telegram
      user ID receive a notification message (`⚠️ 허용 사용자가 로컬
      웹에서 변경되었습니다...` to the old ID, `✅ 이 계정이 허용
      사용자로 지정되었습니다.` to the new ID), and the old ID's commands
      are silently rejected afterward (per §3's unauthorized-account
      check).

## 8. Service layer per platform

Platform matrix (dev-plan §7 A2, task 5.6):

| Target | Python | Service manager |
|---|---|---|
| macOS | 3.8.2 | launchd |
| Linux PC x86_64 | ≥ 3.8 | systemd (user unit) |
| Raspberry Pi OS Bullseye | 3.9 | systemd (user unit) |
| Raspberry Pi OS Bookworm | 3.11 | systemd (user unit) |

For **each** target above:

- [ ] Install via `sh launchd/install.sh` (auto-detects OS). Confirm the
      rendered unit file has the correct absolute project path (no leftover
      `USERNAME`/`%h` placeholder).
- [ ] Kill the process (`kill <pid>` or `kill -9 <pid>`). **Expected:**
      the service manager restarts it automatically (KeepAlive / Restart=
      always) within its throttle window.
- [ ] Send `/restart`. **Expected:** process exits, service manager
      restarts it. **Record the recovery time** (time from exit to the bot
      responding again) — expected roughly 10s on launchd, 2s on systemd,
      but actual values vary by machine.
- [ ] Reboot the machine (or, on Linux, simulate by stopping/restarting the
      user systemd manager if a full reboot isn't practical). **Expected:**
      the bot comes back up automatically. On Linux/Pi this requires
      `loginctl enable-linger $USER` to have been run once beforehand —
      confirm it fails to auto-start without linger, and succeeds with it.
- [ ] `!echo $0` shows the shell resolved by `SHELL_PATH=auto` for that
      platform (`/bin/zsh` on macOS, otherwise `$SHELL` / login shell /
      `/bin/sh`).
- [ ] Start a second instance while the service is running (foreground
      `PYTHONPATH=. python3 -m src.agent`). **Expected:** immediate
      `다른 인스턴스가 실행 중입니다.` and exit(1), no Telegram 409.
- [ ] Send SIGTERM to the service (`systemctl --user stop shellie` /
      `launchctl bootout gui/$(id -u) ...`). **Expected:** a clean shutdown
      log line (`SIGTERM 수신, 정상 종료`), the web listener stops, and the
      process exits promptly (not killed after a timeout).
- [ ] Trigger a shell timeout (`!sleep 100`, with `SHELL_TIMEOUT_SEC` lower
      than 100) and then stop the service mid-timeout. **Expected:** no
      orphaned `sleep` process remains afterward — check with `pgrep sleep`
      (should return nothing once both the timeout's own SIGTERM/SIGKILL
      handling and the service manager's cgroup/process-group cleanup have
      run).

## 9. MCP servers (HTTP)

- [ ] Set up a small test MCP server that implements Streamable HTTP (JSON
      responses, not SSE) with at least one tool, e.g. an `echo` tool. Point
      `MCP_SERVERS` at it from the web UI, e.g.
      `[{"name":"test","url":"http://127.0.0.1:<port>/mcp"}]`. **Expected:**
      no restart needed; the next LLM call's function-declarations include
      `mcp_test_echo` (or your tool's name), and asking the bot to use it
      (e.g. "test 툴로 echo 해줘: hello") produces a reply built from the
      tool's result.
- [ ] Check `/settings` and `/get MCP_SERVERS`. **Expected:** the value is
      masked (`••••...`), never showing the raw JSON/headers in Telegram or
      the web UI's non-edit views, and the row shows "No (web-only, secret)"
      the same way `TELEGRAM_BOT_TOKEN`/`GEMINI_API_KEY` do.
- [ ] Break the configured server (e.g. change the `url` to an unreachable
      port, or stop the test server) and send another message that would use
      it. **Expected:** the chat still works normally with the built-in
      tools (`execute_shell`/`append_memory`) — the LLM call does not fail,
      it simply proceeds without the MCP tool; the log shows a warning
      (e.g. `MCP 서버 ... 초기화 실패` or `MCP 서버 ... 툴 목록 조회 실패`)
      rather than an exception.
- [ ] Point `MCP_SERVERS` at a server that answers with
      `Content-Type: text/event-stream` (SSE). **Expected:** the log shows
      `MCP 서버 <name>: SSE 응답은 지원하지 않습니다`, that server's tools
      are absent from the next LLM call's declarations, and the rest of the
      chat is unaffected.
- [ ] Set `MCP_SERVERS` to invalid JSON (or a JSON object instead of an
      array) directly via the web UI. **Expected:** the update is rejected
      with the constraint message (`JSON 배열: name(영숫자_-)·url(http/https)
      ·headers(선택)`) and the previous value is kept; a syntactically valid
      but semantically wrong entry (bad `name`/`url`, duplicate `name`, or a
      non-string `headers` value) is rejected the same way.

## 10. Metrics

- [ ] Steady-state RSS with `WEB_ENABLED=true`: record via
      `ps -o rss= -p <pid>` after ~1 minute idle. **Target: ≤ 30MB.**
- [ ] Steady-state RSS with `WEB_ENABLED=false`: same measurement.
      **Target: ≤ 30MB** (should be lower than the web-enabled figure).
      **Mandatory on Raspberry Pi**, where the 30MB budget is tightest.
- [ ] Bypass latency (`!echo hi` or similar): record round-trip time from
      §1's latency method. Expected order of magnitude: well under 1s.
- [ ] LLM latency per model in the chain (flash / pro / flash-lite):
      record one round-trip time for each, since `gemini-2.5-pro` is a
      "thinking" model and may be noticeably slower than the flash models.

| Metric | macOS | Linux PC | Pi Bullseye | Pi Bookworm |
|---|---|---|---|---|
| RSS, web on (MB) | | | | |
| RSS, web off (MB) | | | | |
| Bypass latency (s) | | | | |
| LLM latency: flash (s) | | | | |
| LLM latency: pro (s) | | | | |
| LLM latency: flash-lite (s) | | | | |

## 11. Network resilience

- [ ] Disconnect the network (Wi-Fi off / unplug Ethernet) for at least 2
      minutes while the process is running. **Expected:** `getUpdates`
      failures are logged (`getUpdates 실패: status=0` or similar) with an
      exponential backoff (starting at 1s, doubling up to a 60s cap) between
      retries — check CPU usage (`top`/`ps -o %cpu=`) stays near-idle, not
      pegged in a tight retry loop.
- [ ] Reconnect the network. **Expected:** polling recovers automatically
      within one backoff interval, with no restart needed and no missed
      messages sent while reconnected (messages sent while fully offline
      are, of course, not delivered until Telegram has them queued for the
      bot, which is standard Telegram behavior, not something this agent
      controls).
- [ ] Temporarily set an invalid `TELEGRAM_BOT_TOKEN` (simulating a revoked
      token) to trigger repeated 401s from `getUpdates`. **Expected:** the
      log shows `토큰이 유효하지 않습니다(401). 웹에서 수정하세요.` on a
      backoff loop, but the **web server stays reachable** the whole time
      (confirm `http://127.0.0.1:<port>/` still responds) so you can fix
      the token from the UI without needing shell/service access.
