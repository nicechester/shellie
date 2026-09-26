# Task List: Shellie

> 기준 문서: `dev-plan.md` (상세 내용·리스크 ID는 계획서 참조, 설계 수정안 A1은 dev-plan §6)
> 사용법: 완료 시 `[ ]` → `[x]`, 진행 중이면 항목 뒤에 `(진행 중)` 표기. 부분 완료·보류는 뒤에 짧은 메모를 남긴다.
>
> **언어 규칙: 모든 문서 산출물과 코드 주석·docstring은 영어로 작성한다** — `README.md`, `skills/README.md`, 수동 검증 시나리오(5.6), `.env.example` 주석, 그리고 `.py` 파일의 모든 주석/docstring. 단, 텔레그램 응답·웹 UI·로그 등 앱의 사용자 대면 **문자열 리터럴**은 한국어를 유지한다.

**진행 현황:** 전 Phase(0~9) 구현·문서화·최종 검토 완료 (2026-09-25, HTTP MCP·MD 스킬 포함). 남은 것 = ① Chester의 테스트 실행(162개) 및 수용 테스트(docs/VERIFICATION.md), ② 미결 결정 1건: OpenClaw 데이터 이관 여부

---

## 사전 결정 (확정)

- [x] D1. `ALLOWED_USER_ID` 미설정 시 동작 — **fail-closed 채택**: 유효값 없이는 polling 미시작 (D10 설정 필요 모드와 결합)
- [x] D2. 비밀값 보관 위치 — **`.env` 파일 채택**: stdlib 파서, plist에는 비밀값 없음
- [x] D3. 최소 지원 Python 버전 — **3.8 이상** (사용자 Mac이 3.8.2). 전 모듈 `from __future__ import annotations`, 3.9+ 전용 기능(dict `|` 병합, `str.removeprefix` 등) 사용 금지
- [x] D4. update offset 처리 — **파일 영속화(`.update_offset`), at-most-once 채택**
- [x] D5. 텔레그램 출력 포맷 — **HTML parse_mode 채택**: `& < >` 이스케이프, 셸 출력은 `<pre>`, LLM 마크다운은 HTML 변환
- [x] D6. 대화 맥락 — **최근 N턴 유지 + 유휴 자동 리셋 채택**: 기본 10턴/30분 (카탈로그 키로 런타임 조정 가능), `/reset`으로 수동 초기화
- [x] D7. 모델 체인 — **flash → pro → lite 채택** (카탈로그 키로 런타임 조정 가능)
- [x] D8. `skills/` 사용 방식 — **저장소만, LLM이 `execute_shell`로 실행 (a) 채택**
- [x] D9. 설정 영속화 — **`settings.json` 오버레이 채택**: 0600, 원자적 저장. `.env`는 읽기 전용 초기값, 앱은 절대 쓰지 않음
- [x] D10. 필수값 누락 시 — **설정 필요 모드 채택**: 웹만 기동·polling 미시작, 웹에서 채우면 무재시동 시작. 웹 비활성 시 exit(1)
- [x] D11. 웹 인증 — **로그인 없음 + Host/Origin/Sec-Fetch-Site/CSRF 토큰 필수 검사 채택** (R31/R32 방어)
- [x] D12. 플랫폼·서비스 계층 — **POSIX 전용 채택**: macOS(launchd) + Linux PC/라즈베리파이(systemd user unit), Windows 제외(기동 시 거부), 범용 wrapper 없음

---

## Phase 0: 스캐폴딩 — 완료

- [x] 0.1 디렉터리 구조 생성 `[junior]`
- [x] 0.2 빈 `__init__.py` 4개 생성 `[junior]`
- [x] 0.3 초기 `memory/MEMORY.md` 작성 `[junior]`
- [x] 0.4 `skills/README.md` + `sample_skill.py` placeholder (D8 반영) `[junior]`
- [x] 0.5 `.gitignore` 작성 `[junior]`
- [x] 0.6 `.env.example` 작성 `[junior]`
- [x] 0.7 `pyproject.toml` 작성 — 의존성 0, `requires-python >=3.8`(A2 반영 완료), requirements.txt 없음 `[junior]`
- [x] 0.1b `systemd/` 디렉터리 생성 (A2) `[junior]`
- [x] 0.5b `.gitignore`에 `.shellie.lock` 추가 (A2, R50 단일 인스턴스 락) `[junior]`

## Phase 1: 기반 유틸리티 + 설정 스토어 (A1)

- [x] 1.1 `config.py` 기반부 1차 구현 (`.env` 파서, 경로 상수) `[junior]`
- [x] 1.1b `config.py` A1 재작업 완료: 경로 상수+`parse_dotenv`(순수 함수, R40)+`settings.py` 재수출만 남김, import 부작용 제거 `[junior→senior가 1.4~1.5와 함께 수행]`
- [x] 1.2 `utils/http.py` 구현 — HTTPError 본문 보존, 전송 오류 status 0, 설정 비의존이라 A1 영향 없음. 3.9+ 전용 API 미사용 확인 완료(어노테이션만 사용, future import 있음) `[junior]`
- [x] 1.3 SSL 인증서 메모 → Phase 6.1로 이관. 단일 스레드 전제는 A1로 폐기(웹 스레드 추가) `[junior]`
- [x] 1.4 설정 카탈로그 완료: `src/agent/settings.py`에 `SettingSpec` 17키(SHELL_PATH 포함), 파서·검증기(`/etc/shells`)·정책 플래그·마스킹, SYSTEM_PROMPT 플랫폼 중립. 토큰/WEB_PORT/ALLOWED_USER_ID precheck는 슬롯만 두고 Phase 3.1·W.2에서 `register_precheck`로 연결 `[senior]`
- [x] 1.5 `SettingsStore` 완료: 3레이어, COW 읽기, 직렬화 update/unset(10단계, 전부 적용/거부), 원자적 0600 저장, 깨진 JSON 보존, 훅(잠금 밖), revision, 감사 로그. 참고: 카탈로그 키인데 미설정이면 `get()`은 None(KeyError는 카탈로그 밖 키 전용) `[senior]`
- [x] 1.6 `.gitignore` settings.json* 추가, `.env.example` 17키 주석 예시+override 우선순위 안내 `[junior]`

## Phase 2: 코어 엔진 (shell / memory / gemini) — 의존: Phase 1

- [x] 2.1 `core/memory.py` 구현 완료 — 여러 줄 입력을 공백으로 정규화해 한 줄 규칙 보장, 파일 끝 개행 여부 검사 후 append(R24) `[junior]`
- [x] 2.2 `core/shell.py` POSIX 셸 엔진 완료: 셸 결정 체인(무캐시), Popen argv+start_new_session, killpg TERM→2초→KILL+출력 회수, `kill_active()`, stdout/stderr 모두 보존+`[exit N]`, 비밀 키 제거(R40), OS별 PATH 보강(R48), 설정 호출 시점 읽기, `execution_environment()` 제공 `[senior]`
- [x] 2.3 `core/gemini.py` Fallback 엔진 완료: 트리거 = 429 / RESOURCE_EXHAUSTED / 5xx(500·502·503·504) / 전송 오류(status 0), API 키는 `x-goog-api-key` 헤더(R19), 호출 시작 시 snapshot, `ToolEntry` 레지스트리(동적 선언 콜러블, MCP 어댑터 대비), 스킬 자동 발견(ast.get_docstring), `[실행 환경]` 블록 주입(R44) `[senior]`
- [x] 2.4 `parse_response()` 완료: ParsedReply(text/function_calls/blocked/block_reason/finish_reason/raw_content), 전체 parts 순회(다중 FC·다중 text·thought 스킵), 빈 candidates·blockReason·finishReason 무예외 처리(R11·R12) `[senior]`

## Phase 3: 텔레그램 계층 (client / handlers) — 의존: Phase 1, 2

- [x] 3.1 `telegram/client.py` 완료: 호출 시점 토큰 URL, POLL_TIMEOUT_SEC+5 타임아웃, allowed_updates=["message"], 실패 시 `{"_status","_error"}` 반환(401/409 backoff용), get_me/delete_message, `register_prechecks()`(토큰 getMe·봇 자신 ID 금지 — __main__에서 호출), 토큰 마스킹 로깅 `[junior]`
- [x] 3.2 `split_message()` 완료: 4096 분할(개행 우선), `<pre>` 태그 연속성 유지, HTML 400 시 plain 재시도 폴백(R16) `[junior]`
- [x] 3.3 `handlers.py` 라우팅 완료: ALLOWED_USER_ID 매 update 읽기, 검증→설정 명령→Bypass→LLM 순서, `@botname` 제거·소문자 정확 일치, 미허가는 **무응답+경고 로그**(R6 결정), HTML 일관화(마크다운→HTML 변환기 포함) `[senior]`
- [x] 3.4 `/restart` 완료: 서비스 관리자 감지(INVOCATION_ID/XPC_SERVICE_NAME) 후 미검출 시 경고, offset은 __main__의 처리 전 커밋(at-most-once D4)에 위임 — 4.1에서 마무리(R3) `[senior]`
- [x] 3.5 FC 루프 완료: parse_response 사용, 다중 functionCall을 user-role 단일 턴으로 일괄 응답, FC_MAX_LOOPS/CONTEXT_TURNS/IDLE_RESET_MINUTES 메시지마다 읽기, 상한 도달 시 명시 안내(무보존), 차단 응답 처리, CONTEXT_TURNS=0 단발(R11~R13) `[senior]`
- [x] 3.6 설정 명령 완료: `/settings` `/get` `/set` `/unset`, 차단 키 P1(+비밀 키는 deleteMessage+재발급 권고, 카탈로그 secret 플래그 기반), difflib 제안, 이력 제외 `[senior]`
- [x] 3.7 사용법·도움말·응답 문구 3.6에 포함 구현 `[senior가 수행]`

## Phase 3.5: 로컬 웹 인터페이스 (A1 신설) — 의존: Phase 1, 3.1

- [x] W.1 `web/server.py` 완료: 보안 게이트(Host→Origin→Sec-Fetch-Site, POST는 415→411→413→디코드→parse_qs→CSRF 순 — 토큰이 본문에 있어 CSRF는 파싱 후), PRG 303, 400 재렌더, 보안 헤더 전 응답 적용, 예외는 로그만·500 일반 페이지 `[senior]`
- [x] W.2 `WebServerManager` 완료: 지연 import(R39), 단일 스레드 HTTPServer+데몬 스레드 1개(A2-5), shutdown은 항상 별도 스레드, 멱등 reconcile, 포트 선기동 교체·실패 시 system actor 롤백(+현재 포트 bind-test 스킵으로 롤백 자기충돌 방지, R42), stop()(SIGTERM용), status_provider 주입점 `[senior]`
- [x] W.3 `web/page.py` 완료: 타입별 입력 매핑, html.escape 전면, 출처 배지, 배너/공지, 비밀 필드 빈 렌더+마스킹 힌트, "기본값으로"는 중첩 폼 대신 `formaction=/unset` 버튼(HTML 중첩 폼 불가 회피) `[senior가 수행]`
- [x] W.4 민감 키 흐름 완료: ALLOWED_USER_ID 확인 체크박스+변경 시 기존/새 ID 알림(P3 — 웹이 유일한 변경 주체이므로 호출부에서 이전 값 캡처, 백그라운드 스레드 발송), 토큰 getMe precheck는 client.register_prechecks와 충돌 없음, 변경 성공 시 새 봇 @username 일회성 공지(P4) `[senior]`

## Phase 4: 진입점과 서비스 등록 — 의존: Phase 3, 3.5

- [x] 4.1 `__main__.py` 완료: settings.load+D10 분기(설정 필요 모드 2초 폴링), LOG_LEVEL 훅, register_prechecks→manager.start 순서, offset을 **처리 전** 원자 커밋(at-most-once, R3·R4), 401/409 구분 로그+지수 backoff 최대 60초(R22), 토큰 변경 감지→봇 ID 다를 때만 offset 초기화(최초 기동은 기준값만 설정, R41), POSIX 가드+flock(R50)+SIGTERM/SIGINT 핸들러(R51·R52) `[senior]`
- [x] 4.2 `launchd/com.user.shellie.plist` 완료: 비밀값 없음(D9), PYTHONPATH·PYTHONUNBUFFERED·PATH, Umask=63(077), RunAtLoad+KeepAlive, USERNAME placeholder(install.sh가 치환) `[junior]`
- [x] 4.4 `systemd/shellie.service` 완료: Type=simple, Restart=always, RestartSec=2, StartLimit 60/10, UMask=0077, 비밀값·network-online 없음 `[junior]`
- [x] 4.3 `launchd/install.sh` 완료(실행 권한 있음): Darwin plist 치환→bootout(무시 가능)→bootstrap, Linux 유닛 복사(절대경로 치환)→daemon-reload→enable --now→enable-linger 안내(R46), 공통 chmod 600 `[junior]`

## Phase 5: 테스트 및 검증 — 의존: Phase 4

> 테스트 코드 작성까지만 에이전트 담당. 실행·판정은 Chester가 한다.

- [x] 5.1 `tests/` 구조 완료: unittest+mock, SettingsStore env/path 주입, 셸 경로 무하드코딩, 3.8 호환, 영어 주석. 총 **123개 테스트** (settings 37 + web 14 + handlers 22 + gemini 20 + shell 18 + memory 12) `[senior]`
- [x] 5.2 `tests/test_shell.py` 완료 (18개): 셸 결정 체인 mock, execute_shell의 ValueError→반환값 처리, 실제 실행(stdout/stderr/[exit]/잘라내기), 타임아웃+_active_pgid 정리 확인(<6초), kill_active, 비밀 env 제거 `[senior]`
- [x] 5.3 `tests/test_memory.py` 완료: 12개 테스트(읽기 3, 개행 처리 3, 정규화 2, 빈 입력 2, 반환값 2), MEMORY_FILE patch + TemporaryDirectory `[junior]`
- [x] 5.4 `tests/test_gemini.py` 완료 (20개): fallback 매트릭스(429/RESOURCE_EXHAUSTED/503/status 0), 400 즉시 예외+키 미노출, 헤더 계약, parse_response(다중 FC·thought 스킵·raw_content 동일성), 레지스트리, list_skills, system instruction 구성 `[senior]`
- [x] 5.5 `tests/test_handlers.py` 완료 (22개): 미허가 무응답, Bypass, /restart 순서+서비스 관리자 경고, 설정 명령(차단 키 비밀/비비밀 구분, difflib, 여러 줄 거부, 이력 제외), FC 루프(다중 호출 단일 user 턴, 상한, 차단), 이력 관리(N턴/0턴/유휴 리셋) `[senior]`
- [x] 5.6 `docs/VERIFICATION.md` 완료(영어): 10개 섹션 체크리스트 플레이북 — 사전 주의/측정법, 포그라운드, Bypass, 메모리, LLM, 맥락, 설정 핫 적용(curl 403 예시 포함), 플랫폼 매트릭스(기입식 표), 지표, 네트워크 복원력 `[senior]`
- [x] 5.7 `tests/test_settings.py` 완료 (37개): 브리핑 항목 전부 커버. ⚠️ 발견: `SettingSpec.precheck`는 CATALOG 공유 객체의 전역 가변 상태 — 프로덕션(싱글턴 store)은 무해하나 테스트는 반드시 `addCleanup(register_precheck, key, None)`으로 정리할 것. env 레이어의 카탈로그 외 키는 무경고 무시(override 파일과 달리 경고 없음) `[senior]`
- [x] 5.8 `tests/test_web.py` 완료 (14개): 실서버 포트 0 in-process, 보안 게이트 403 전부, PRG 303, 부분 무효 전체 거부, 411은 http.client 저수준 API로 재현, 413, ALLOWED_USER_ID 확인·알림 흐름. ⚠️ server.py의 `_notice`/`_pending_old_values`는 모듈 전역이라 테스트 간 리셋 필요 `[senior]`

## Phase 6: 문서화 — 의존: Phase 5

- [x] 6.1 `README.md` 완료(영어, 10개 섹션): 지원 OS·Python 버전 표(3.8+, SSL 인증서 R8은 python.org판 macOS만 해당), macOS launchd(bootstrap/bootout/kickstart) / Linux·Pi systemd(`--user`, enable-linger, journalctl), SHELL_PATH 설명, 재시작 지연 실측값, 3.8 호환 규칙, 명령어 목록, 보안 주의(RCE 수준 권한), 설정 섹션(웹 주소는 항상 127.0.0.1 표기 R53, 레이어·출처, /set 사용법, 웹 전용 키 정책, `.env` 무재시동 미반영·override 우선, 잠김 복구 절차 R43) `[junior]`
- [x] 6.2 `skills/README.md` 영어 재작성 완료: 규약, 무로더/execute_shell 실행, 자동 발견 메커니즘(`_` 접두 스킵, docstring 첫 줄), sample_skill 예시 `[senior가 수행]`
- [x] 6.3 최종 검토 완료: grep 감사 전 항목 통과(import 시점 설정 읽기 0건, 비밀값 노출 0건 — get_me 오류 마스킹 1건 후속 수정 완료, 3.9+ API 0건, 한국어 주석 0건), `.env.example` 영어 전환, dev-plan 리스크 58/60 주석(해결 47/수용 12), README 17키 표·명령 표·VERIFICATION 예시 모두 코드와 일치, README에 인터프리터 경로 주의 추가. **미결 2건은 진행 현황 참조** `[senior]`

## Phase 7: 후속 개선 (2026-09-25 추가)

- [x] 7.1 `run.sh` 완료(실행 권한 있음): `sh run.sh` 또는 `./run.sh`로 포그라운드 실행 `[junior]`
- [x] 7.2 메모리 날짜별 분리 완료(R24 해결): `append_memory` → `memory/YYYY-MM-DD.md`(MEMORY.md에는 더 이상 안 씀), `read_memory()` = 코어 + 오늘만(카탈로그 17키 유지), system prompt에 grep 검색 힌트 주입, `/mem` 라벨 갱신, 테스트 재작성(16개)·README·VERIFICATION·dev-plan R24 반영 `[senior]`

## Phase 8: HTTP MCP 클라이언트 (2026-09-25 착수 — Post-v1 항목 승격)

- [x] 8.1 `core/mcp.py` 완료(332줄): initialize(2024-11-05)→notifications/initialized→tools/list→tools/call, Mcp-Session-Id 캡처·에코, 300초 TTL 캐시(설정 변경 시 fingerprint로 무효화), 이름 매핑 테이블(재분해 없음), SSE 서버는 TTL당 1회만 시도 후 스킵, 헤더 값 로그 금지 `[senior]`
- [x] 8.2 `MCP_SERVERS` 키 완료(카탈로그 18키): JSON 배열 검증(name 정규식·중복·url·headers), secret·웹 전용·즉시 적용 `[senior]`
- [x] 8.3 `gemini.py` 통합 완료: build_tools_schema 병합(예외 시 내장 툴만), run_tool→mcp.try_run 폴백 `[senior]`
- [x] 8.4 `http_post_h()` 추가 완료: 응답 헤더(소문자 키) 반환, http_post는 래퍼로 재구성(시그니처 불변) `[senior]`
- [x] 8.5 테스트·문서 완료: test_mcp.py 20개 + test_settings.py 경계 9개 추가(총 156개), README 18키 표+MCP 섹션, VERIFICATION §9 MCP(이후 절 번호 재조정) `[senior]`

제외(변경 없음): stdio MCP, OAuth, SSE 스트리밍 파싱

---

## Phase 9: MD 기반 스킬 (2026-09-25 착수)

- [x] 9.1 하이브리드 스킬 완료: `.md` 스캔(첫 비공백 줄 `#` 제거·80자 요약, README.md·`_` 접두 제외, 파일명 정렬 유지), system prompt 힌트 갱신(.py 실행 / .md는 cat 후 절차 수행) `[senior]`
- [x] 9.2 규약·샘플·테스트 완료: skills/README.md "Markdown skills" 섹션, sample_skill.md, test_gemini 6개 추가(총 162개), README §8 하이브리드 설명 `[senior]`

## 수용 테스트 (Definition of Done — 사용자 실행)

- [ ] 포그라운드 구동: `python3 -m src.agent` 정상 기동(Mac 3.8.2 포함), 무의존성 확인
- [ ] 설정 필요 모드: 필수값 없이 기동 → 웹만 뜸 → 웹에서 입력 → 무재시동 polling 시작
- [ ] 셸 Bypass: `!ls -la` / `/sh echo hi` 즉시 응답(LLM 호출 없음), 잘라내기, 타임아웃, 미허가 계정 차단
- [ ] 메모리: `/mem` 즉시 출력, "기억해줘" → `append_memory` → 반영 확인
- [ ] 재시작: `/restart` 후 자동 복구, 같은 `/restart` 재처리 없음(R3), 복구 시간 실측
- [ ] LLM 트랙: 대화·셸 툴 응답, 429 모델 전환 로그, 비정상 응답 무크래시, 4096자 분할 전송
- [ ] 대화 맥락: 연속 대화에서 직전 턴 참조 가능, 30분 유휴 후 자동 리셋, `/reset` 동작
- [ ] 설정: 웹·`/set` 변경이 재시동 없이 다음 사용에 반영, 재시동 후 유지, 차단 키 Telegram 거부(+메시지 삭제 시도), CSRF/Host 위반 403, 포트 변경·웹 on/off 무중단, 토큰 변경 후 정상 polling (상세: dev-plan §5.9)
- [ ] 서비스: 재부팅 후 자동 기동, kill 후 복구, 네트워크 단절 시 busy-loop 없음 (macOS launchd + Linux/Pi systemd)
- [ ] 플랫폼: macOS(3.8.2)·Linux·Pi 각각 기동/kill 복구/재부팅 자동 기동(linger)/`/restart` 복구 시간 기록, `!echo $0` 셸 확인, 타임아웃 시 손자 프로세스 잔존 없음, 이중 인스턴스 거부, SIGTERM 정상 종료 (상세: dev-plan §7 "5.10 플랫폼")
- [ ] 지표: RSS ≤ 30MB(웹 켬/끔 비교, 라즈베리파이 필수), Bypass·LLM 지연 기록
- [ ] 문서: README 완비, dev-plan §4·§6.6 리스크 전 항목 "해결됨"/"수용함(사유)" 표기
