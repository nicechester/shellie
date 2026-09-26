# 개발 계획서: Shellie — 경량 텔레그램 에이전트 (shellie)

> 기준 문서: `/Users/chester.kim/workspace/trashcan/shellie/design-req.md`
> 이 문서는 계획서입니다. 코드는 포함하지 않으며, 설계 명세 §4의 초안 코드를 기준선으로 삼습니다.
>
> **⚠️ 수정안 병합 공지:** §6의 수정안 A1(런타임 설정 스토어·로컬 웹 UI·텔레그램 설정 명령)과 §7의 수정안 A2(멀티플랫폼: macOS·Linux PC·Raspberry Pi)가 §1~§5와 충돌하는 부분에서는 **수정안이 우선**합니다. A2가 A1과 충돌하는 부분(웹 서버 클래스, SYSTEM_PROMPT 기본값)에서는 A2가 우선합니다.

---

## 1. 목표 및 범위 요약

이 프로젝트(Shellie)는 OpenClaw를 완전히 대체할 macOS용 텔레그램 에이전트를 만드는 것입니다. **Python 3 표준 라이브러리만** 사용하고 외부 의존성은 0%이며, `pip install`이 필요 없습니다. 에이전트는 세 가지 트랙으로 동작합니다. (1) `!`, `/sh`, `/mem`, `/restart`는 LLM을 거치지 않고 바로 처리하는 Bypass 트랙입니다. (2) 일반 대화는 Gemini(`2.5-pro → 2.5-flash → 2.5-flash-lite`, 429가 오면 다음 모델로 전환)와 Function Calling(`execute_shell`, `append_memory`, 최대 5회 루프)으로 처리합니다. (3) 장기 기억은 `MEMORY.md` 파일에 저장합니다. 목표 지표는 네 가지입니다. **상주 메모리(RSS) 30MB 이하**, **Bypass 응답 0.1초 이내 / LLM 응답 1~2초 이내**, **`launchd`(KeepAlive) 기반 24/7 자동 복구**, 그리고 오래된 Mac에서도 안정적으로 동작하는 것입니다. 범위는 명세 §2 디렉터리 구조의 모든 파일, 로컬 검증, 서비스 등록 절차까지입니다. 명세에 없는 기능(스킬 자동 로딩, 멀티턴 대화 기억 등)은 §4의 "결정 필요 사항"으로 따로 정리했습니다.

---

## 2. 사전 결정 사항 (Phase 1 착수 전에 확정)

아래 항목은 여러 파일의 구현 방향을 바꿉니다. 구현을 시작하기 전에 결정해야 합니다. 자세한 근거는 §4 리스크 체크리스트의 해당 ID에 있습니다.

| # | 결정 항목 | 선택지 | 관련 리스크 |
|---|---|---|---|
| D1 | `ALLOWED_USER_ID`가 없거나 0일 때 동작 | (a) 초안처럼 fail-open, 즉 모두 허용 / (b) **fail-closed, 즉 기동 거부(권장)** | R1 |
| D2 | 비밀값 보관 위치 | (a) plist `EnvironmentVariables`에 평문 / (b) `.env` 파일을 stdlib로 파싱 / (c) macOS Keychain(`security` CLI) | R20, R21 |
| D3 | 최소 지원 Python 버전 | 대상 Mac의 `/usr/bin/python3` 실제 버전 확인 필요(3.8 이하라면 타입 힌트 처리 필요) | R2 |
| D4 | 텔레그램 update offset 처리 방식 | (a) 메모리에만 보관(초안) / (b) **파일에 저장하고 처리 전에 커밋, at-most-once(권장)** | R3, R4 |
| D5 | 텔레그램 출력 포맷 | (a) plain text / (b) HTML + 이스케이프 / (c) MarkdownV2 + 이스케이프 | R16 |
| D6 | 대화 맥락 | (a) 메시지마다 단발 호출(초안) / (b) 최근 N턴을 메모리에 보관 | R13 |
| D7 | 모델 체인 순서와 모델명 | 현재 사용 가능한 모델명을 확인해야 함. 지연 목표를 지키려면 flash 우선도 검토 | R9, R10 |
| D8 | `skills/` 사용 방식 | (a) 저장소 역할만 하고 LLM이 `execute_shell`로 실행 / (b) 로더 구현(범위 확대) | R26 |

---

## 3. 단계별 구현 계획

### 모듈 의존 관계

```
config ─┬─> core/memory ─┐
        ├─> core/shell ──┤
        │                ├─> core/gemini ─┐
utils/http ──────────────┘                ├─> telegram/handlers ─> __main__
        └─> telegram/client ──────────────┘
```

`utils/http`는 `config`에 의존하지 않습니다. 나머지 모듈은 import 시점에 모두 `config`를 로드합니다.

---

### Phase 0: 스캐폴딩

**의존:** 없음

| # | 작업 | 파일 | 담당 |
|---|---|---|---|
| 0.1 | 디렉터리 구조 생성: `launchd/`, `memory/`, `skills/`, `src/agent/{core,telegram,utils}/` | 디렉터리 | [junior] |
| 0.2 | 빈 `__init__.py` 4개 생성 | `src/agent/__init__.py`, `src/agent/core/__init__.py`, `src/agent/telegram/__init__.py`, `src/agent/utils/__init__.py` | [junior] |
| 0.3 | 초기 `MEMORY.md` 작성(헤더와 기본 규칙 몇 줄, 형식은 `- 항목`) | `memory/MEMORY.md` | [junior] |
| 0.4 | `skills/README.md` 작성, `sample_skill.py`는 표준 라이브러리만 쓰는 hello 수준 placeholder. 명세에 내용 정의가 없으므로 D8에 따름 | `skills/README.md`, `skills/sample_skill.py` | [junior] |
| 0.5 | `.gitignore` 작성: `.env`, `*.log`, `__pycache__/`, `*.pyc`, offset 상태 파일(D4를 (b)로 정할 경우) | `.gitignore` | [junior] |
| 0.6 | `.env.example`은 명세 §4.1 그대로 작성 | `.env.example` | [junior] |
| 0.7 | `pyproject.toml`/`requirements.txt` 포함 여부 결정. 무의존성 원칙상 `requirements.txt`는 **만들지 않는 것**을 권장. PEP 518을 준수한다고 적혀 있으니, 원하면 의존성 없는 최소 `pyproject.toml`만 둠(R27) | (선택) `pyproject.toml` | [junior] |

---

### Phase 1: 기반 유틸리티 (config / http)

**의존:** Phase 0

| # | 작업 | 파일 | 담당 |
|---|---|---|---|
| 1.1 | 명세 §4.2를 기준으로 `config.py` 구현. D1에 따라 `ALLOWED_USER_ID` 필수 검증과 숫자 파싱 실패 처리 추가(R1, R5). D2가 (b)라면 stdlib `.env` 파서(`KEY=VALUE`, 주석/빈 줄 무시, 이미 설정된 환경변수는 덮어쓰지 않음)를 여기에 포함 | `src/agent/config.py` | [junior] (D2가 (c) Keychain이면 [senior]) |
| 1.2 | 명세 §4.3을 기준으로 `http.py` 구현. D3에 따라 필요하면 파일 상단에 `from __future__ import annotations` 추가(R2). `http_get`의 HTTPError 분기에서 본문을 버리지 않도록 할지 검토(R7) | `src/agent/utils/http.py` | [junior] |
| 1.3 | 스레드 안전성 검토는 필요 없음(단일 스레드). SSL 인증서 확인 방법을 README에 기록할 항목으로 메모만 해 둠(R8) | — | [junior] |

---

### Phase 2: 코어 엔진 (shell / memory / gemini)

**의존:** Phase 1 (`config`, `http`)

| # | 작업 | 파일 | 담당 |
|---|---|---|---|
| 2.1 | 명세 §4.4를 그대로 옮겨 `memory.py` 구현. 개행 처리 확인: 파일 끝에 개행이 없을 때도 `\n- ` 접두가 맞게 붙는지, 여러 줄 입력이 한 줄 규칙을 깨지 않는지(R24) | `src/agent/core/memory.py` | [junior] |
| 2.2 | `shell.py` 강화 구현. 초안을 기준으로 다음을 반영: stdout과 stderr를 모두 보존하고 종료 코드 표기, `stdin=DEVNULL`, 디코딩 오류에 `errors="replace"`, 타임아웃 시 프로세스 그룹 전체 종료(자식 프로세스 잔존 방지), launchd의 최소 PATH 보완(Homebrew 경로 등), 3000자 잘라내기 유지(R14, R15, R17, R18) | `src/agent/core/shell.py` | [senior] |
| 2.3 | `gemini.py` Fallback 엔진 강화. 429뿐 아니라 응답 본문의 `error.status == "RESOURCE_EXHAUSTED"`도 트리거로 인식(명세 §3.3 요구사항인데 초안에서 빠져 있음). 5xx/503(과부하)와 전송 오류(합성 500)를 fallback 대상에 넣을지 결정. API 키를 URL 쿼리 대신 `x-goog-api-key` 헤더로 보내는 방안 검토. D7 모델 체인 반영(R9~R12, R19) | `src/agent/core/gemini.py` | [senior] |
| 2.4 | Gemini 응답 파싱 헬퍼 설계. `candidates`가 없거나 빈 배열인 경우, `promptFeedback.blockReason`, `finishReason`(SAFETY/MAX_TOKENS), `parts`가 없는 경우, 여러 개의 `functionCall` part, 여러 text part 이어 붙이기, thought part 무시 등을 안전하게 추출. 위치는 `gemini.py` 안(handlers의 루프에서 사용)(R11, R12) | `src/agent/core/gemini.py` | [senior] |

---

### Phase 3: 텔레그램 계층 (client / handlers)

**의존:** Phase 1(`http`, `config`), Phase 2(모든 core)

| # | 작업 | 파일 | 담당 |
|---|---|---|---|
| 3.1 | 명세 §4.7을 기준으로 `client.py` 구현. `getUpdates`에 `allowed_updates=["message"]` 추가 검토. `send_message`의 실패 응답(`ok: false`)을 로깅. D5 포맷 결정 반영 | `src/agent/telegram/client.py` | [junior] |
| 3.2 | `client.py`에 4096자 분할 전송 헬퍼 추가(LLM 응답과 `/mem`이 길어질 수 있음)(R16) | `src/agent/telegram/client.py` | [junior] |
| 3.3 | `handlers.py` 라우팅 구현. 사용자 검증(D1, 거부 시 응답할지 조용히 무시할지 결정하고 로깅), Bypass 트랙(`!`, `/sh `, `/mem`, `/restart`, `/reset`)의 명령 판별 정확성 확인(`/sh`만 입력한 경우, `/mem@botname` 같은 그룹 멘션 형식), 출력 포맷(D5) 일관화(R1, R6, R16) | `src/agent/telegram/handlers.py` | [senior] |
| 3.4 | `/restart` 처리 수정. 종료 전에 offset을 Telegram에 확정하거나 파일에 저장해서 **같은 `/restart` update가 다시 전달되어 무한 재시작하는 문제를 막음**. 이 작업은 `__main__.py`의 offset 관리와 함께 설계해야 함(R3) | `src/agent/telegram/handlers.py`, `src/agent/__main__.py` | [senior] |
| 3.5 | Function Calling 루프 재작성. Phase 2.4 파싱 헬퍼 사용, 한 턴에 여러 개의 `functionCall`이 오면 모두 응답, `functionResponse` 턴의 role 값(`"function"`과 현재 문서 기준 `"user"` 중 무엇인지) 확인, 모델 응답 content(thought signature 포함)를 수정하지 않고 되돌려 보내기, 5회에 도달하면 명확한 안내 메시지, 루프 도중 fallback으로 모델이 바뀌었을 때의 동작 확인, D6 대화 맥락 반영(R11~R13, R25) | `src/agent/telegram/handlers.py` | [senior] |

---

### Phase 4: 진입점과 launchd 연결

**의존:** Phase 3

| # | 작업 | 파일 | 담당 |
|---|---|---|---|
| 4.1 | `__main__.py` 메인 루프 강화. `get_updates`가 실패(빈 dict 반환)했을 때도 backoff sleep을 넣어 **네트워크 단절 시 CPU busy-loop를 막음**. 401(토큰 오류), 409(다른 인스턴스 polling 중 또는 webhook 설정됨) 처리. offset 영속화(D4)와 커밋 시점(처리 전 또는 후) 구현. `logging` 출력 대상(stdout/stderr) 정리(R3, R4, R6, R22) | `src/agent/__main__.py` | [senior] |
| 4.2 | 명세 §4.10을 기준으로 plist 작성. D2에 따라 비밀값 항목 제거 또는 유지. `PYTHONUNBUFFERED=1` 추가, 필요하면 `PATH` 추가, `ThrottleInterval` 명시 검토. 사용자 경로 placeholder는 ASCII로 표기(R20, R22, R23) | `launchd/com.user.shellie.plist` | [junior] |
| 4.3 | (선택) 설치 보조 스크립트. plist의 경로 치환, `chmod 600`, `launchctl bootstrap gui/$(id -u) …` / `bootout` / `kickstart -k` 절차. 셸 스크립트 1개(R21, R23) | (선택) `launchd/install.sh` | [junior] |

---

### Phase 5: 로컬 테스트 및 검증

**의존:** Phase 4
**참고:** 조직 정책상 에이전트는 테스트 코드와 체크리스트만 작성하고 **직접 실행하지 않습니다.** 실행과 판정은 사용자(Chester)가 합니다.

| # | 작업 | 파일 | 담당 |
|---|---|---|---|
| 5.1 | `unittest`와 `unittest.mock`(stdlib)으로 테스트 구조 구성. `config`의 import 부작용(`sys.exit`) 때문에 테스트 setUp에서 환경변수를 먼저 주입하는 방식 정의(R5) | `tests/__init__.py`, `tests/…` | [senior] |
| 5.2 | `shell.py` 단위 테스트: 정상 출력, stderr만 있는 경우, 출력 없음, 3000자 잘라내기, 타임아웃(짧은 값으로 주입), 비 UTF-8 출력 | `tests/test_shell.py` | [junior] (5.1 구조 확정 후) |
| 5.3 | `memory.py` 단위 테스트: 파일 없음, 빈 파일, append 형식(임시 디렉터리 사용) | `tests/test_memory.py` | [junior] |
| 5.4 | `gemini.py` 테스트: `http_post`를 mock 처리하고 429 → 다음 모델, 본문 `RESOURCE_EXHAUSTED`, 모든 모델 429, 비정상 응답 구조(candidates 없음/빈 배열/parts 없음) 확인 | `tests/test_gemini.py` | [senior] |
| 5.5 | `handlers.py` 테스트: 미허가 사용자, Bypass 판별, `/restart` 시 offset 확정 여부, function-call 루프(단일/다중 호출, 5회 상한, 알 수 없는 함수) | `tests/test_handlers.py` | [senior] |
| 5.6 | 실기기 수동 검증 시나리오 문서화: 포그라운드 실행 → launchd 등록 → §5 수용 기준 항목. **포그라운드 인스턴스와 launchd 인스턴스를 동시에 띄우지 말 것**(409 Conflict) 주의 명시. RSS 측정 방법(`ps -o rss= -p <pid>`), 지연 측정 방법 정의(R28, R29) | `README.md`의 검증 섹션 | [senior] |

---

### Phase 6: 문서화

**의존:** Phase 5

| # | 작업 | 파일 | 담당 |
|---|---|---|---|
| 6.1 | README 작성: 개요, 요구 Python 버전(D3), 설치/환경변수(D2), 실행, launchd 등록/해제/재시작(`bootstrap`/`bootout`/`kickstart`), 로그 위치와 정리, 명령어 목록, 보안 주의사항(RCE 수준 권한이라는 점) | `README.md` | [junior] |
| 6.2 | `skills/README.md`에 스킬 작성 규약 확정(D8) | `skills/README.md` | [junior] |
| 6.3 | 문서와 코드의 동작 일치 최종 검토(명령어, 경로, 환경변수명) | — | [senior] |

---

## 4. 리스크 및 미결 사항 체크리스트 (구현자 확인용, 수정 지시가 아님)

### 보안

- [x] **R1 (치명적): `ALLOWED_USER_ID` fail-open.** `config.py`는 기본값을 `0`으로 두고, `handlers.py`는 `ALLOWED_USER_ID != 0 and …` 조건으로 검사합니다. 그래서 환경변수가 빠지면 **봇을 찾은 누구나 Mac에서 임의의 셸을 실행할 수 있습니다.** 명세 §3.1은 "검증"을 요구하지만 초안 코드는 토큰과 키만 검사합니다. — **해결됨:** ALLOWED_USER_ID를 required 카탈로그 키로 만들고(D1 fail-closed), 값이 없으면 D10 설정 필요 모드로 polling 자체를 막음
- [x] **R6:** 미허가 사용자에게 "⛔ 권한이 없습니다"로 응답하면 봇이 살아 있다는 사실이 드러납니다. 조용히 무시하고 로그만 남길지 결정해야 합니다. 거부 시도 로깅도 없습니다. `from`이 없는 update(채널 포스트 등)도 처리해야 합니다. — **해결됨:** handlers.py가 무응답+`미허가 접근` 경고 로그만 남기고, `from`/`chat` 없는 update는 조용히 무시
- [x] **R19:** Gemini API 키가 URL 쿼리스트링에 들어갑니다. 예외 메시지나 프록시 로그로 노출될 수 있으니 헤더 방식(`x-goog-api-key`)을 검토해야 합니다. — **해결됨:** gemini.py가 키를 `x-goog-api-key` 헤더로만 전송, URL에는 모델명만 포함
- [x] **R20:** plist `EnvironmentVariables`에 토큰과 키가 평문으로 들어갑니다. `~/Library/LaunchAgents`의 기본 권한은 644입니다. `.env`를 쓰거나 Keychain을 쓰는 방식과 비교해 결정해야 합니다(D2). — **해결됨:** D2 채택(.env), plist/systemd 유닛에는 비밀값 없음(확인 완료)
- [x] **R21:** `.env.example`은 있지만 **`.env`를 로드하는 코드가 없습니다.** 명세 §5는 `export` 방식을 전제합니다. `.env`를 쓰려면 stdlib 파서가 필요합니다. — **해결됨:** `config.parse_dotenv()` stdlib 파서 구현, SettingsStore.load()에서 사용
- [x] **R25:** LLM이 `execute_shell`을 확인 없이 실행합니다. 셸 출력이나 파일 내용에 섞인 프롬프트 인젝션이 파괴적인 명령으로 이어질 수 있습니다. 위험 명령 확인 절차를 둘지는 정책 결정 사항입니다. 또한 `append_memory`로 저장된 내용은 이후 모든 system prompt에 영구 주입됩니다. — **수용함:** 개인용 단일 사용자 봇의 설계상 위험으로 확인 절차 없이 진행, README §9 보안 주의사항에 명시

### 정확성과 안정성

- [x] **R3 (치명적): `/restart` 무한 재시작 루프.** offset은 메모리에서만 증가하고, Telegram은 *다음* `getUpdates(offset=…)` 호출이 있어야 update를 확인 처리합니다. `sys.exit(0)` 후 재기동하면 offset이 `None`이므로 **같은 `/restart`를 다시 받아 또 종료합니다.** — **해결됨:** `__main__.py`가 `process_update()` 실행 **전**에 offset을 파일에 원자적으로 커밋(at-most-once, D4)
- [x] **R4:** R3과 같은 원리로, 처리 도중 크래시가 나면 재기동 후 같은 셸 명령이 **다시 실행됩니다**(비멱등). at-most-once와 at-least-once 중 선택해야 합니다(D4). — **해결됨:** R3과 동일한 처리 전 offset 커밋으로 at-most-once 채택
- [x] **R22:** 네트워크가 끊기면 `http_get`이 곧바로 `({"error":…}, 500)`을 반환하고, `get_updates`는 `{}`를 반환합니다. 예외가 아니므로 메인 루프의 `sleep(2)`를 타지 않아 **busy-loop로 CPU와 로그가 폭증합니다.** 401/409도 마찬가지입니다. — **해결됨:** `get_updates` 실패를 `_status`로 명시 반환, 폴링 루프가 1s→60s 지수 backoff로 재시도
- [x] **R7:** `http_get`은 HTTPError의 본문을 버리고 `{}`만 반환해서 409/401의 원인을 진단할 수 없습니다. 실제 HTTP 500과 전송 오류를 나타내는 합성 500이 구분되지 않습니다. — **해결됨:** HTTPError 본문을 읽어 JSON 파싱 시도 후 반환, 전송 오류는 status 0으로 구분
- [x] **R5:** `config.py`가 import 시점에 `sys.exit(1)`을 실행하고, `int()`에 숫자가 아닌 값이 들어오면 ValueError로 즉시 크래시합니다. 이 부작용 때문에 테스트하기 어렵습니다. KeepAlive 환경에서는 설정 오류가 10초 간격 재시작 루프가 됩니다. — **해결됨:** import 시점 부작용 제거, `settings.load()`가 무효 필수 키 목록을 반환(크래시 없음)
- [x] **R14:** `shell.py`는 `stdout if stdout else stderr` 구조라서 둘 다 있으면 stderr가 사라지고, 종료 코드도 표시되지 않습니다. — **해결됨:** `_combine_streams()`가 stdout+stderr 모두 보존, 비정상 종료 시 `[exit N]` 표시
- [x] **R15:** `shell=True`에서 타임아웃이 나면 zsh만 종료되고 자식 프로세스는 남을 수 있습니다. stdin을 지정하지 않아서 포그라운드 테스트 중 대화형 명령이 TTY 입력을 기다리며 멈출 수 있습니다. — **해결됨:** `start_new_session=True`+`killpg` TERM→KILL로 프로세스 그룹 전체 종료, `stdin=DEVNULL`
- [x] **R17:** `text=True`에서 비 UTF-8 출력이 나오면 UnicodeDecodeError가 발생하고, 일반적인 "에러 발생"으로 뭉개집니다. — **해결됨:** `encoding="utf-8", errors="replace"`로 디코딩 오류 방지
- [x] **R18:** launchd 환경의 PATH는 `/usr/bin:/bin:/usr/sbin:/sbin` 수준이라 Homebrew 명령을 찾지 못합니다. 포그라운드 테스트와 결과가 달라집니다. — **해결됨:** R48로 범위 확장, `_augmented_path()`가 OS별 디렉터리를 PATH에 자동 보강
- [x] **R24:** `append_memory`에 여러 줄 내용이 들어오면 "한 줄 단위" 규칙이 깨집니다. MEMORY.md가 무한히 커지면 매 호출마다 토큰 비용이 늘어납니다. — **해결됨:** 한 줄 정규화 + 날짜별 파일 분리, 코어+오늘만 주입, 과거 기억은 grep 검색

### Gemini API

- [x] **R9:** 명세 §3.3의 `RESOURCE_EXHAUSTED` 트리거가 초안에 구현되어 있지 않습니다(HTTP 429만 검사). 503(overloaded)이나 404(모델 폐기)가 오면 fallback 없이 곧바로 예외가 납니다. — **해결됨:** `_should_fallback()`이 429/RESOURCE_EXHAUSTED/5xx/전송오류(status 0)를 모두 트리거로 인식(400/404는 설정 오류로 간주해 즉시 예외 유지)
- [x] **R10:** 모델명 `gemini-2.5-*`이 현재도 유효한지 확인해야 합니다. **pro를 먼저 호출하는 순서는 "LLM 1~2초" 지연 목표와 충돌할 가능성이 큽니다**(thinking 모델). 한 번의 호출이 최악의 경우 3모델 × 60초 타임아웃에 5회 루프까지 겹칠 수 있습니다. — **수용함:** flash를 1순위로 채택(D7)해 지연 목표와의 충돌을 줄이고, `GEMINI_MODEL_CHAIN`을 런타임에 수정 가능하게 해 모델명 유효성은 실제 호출 시 fallback으로 대응
- [x] **R11:** 응답 파싱이 취약합니다. `candidates: []`이면 `[0]`에서 IndexError가 납니다. `promptFeedback.blockReason`, `finishReason=SAFETY/MAX_TOKENS`로 `parts`가 없는 경우를 처리하지 않습니다. `parts[0]`만 검사해서, 앞에 text나 thought가 있고 뒤에 functionCall이 오면 놓칩니다. 최종 응답도 `parts[0]`의 text만 사용합니다. — **해결됨:** `parse_response()`가 빈 candidates·blockReason·finishReason·parts 부재를 모두 무예외 처리하고 전체 parts를 순회
- [x] **R12:** 병렬 function call(여러 `functionCall` part)에 첫 번째만 응답하면 API 오류나 잘못된 동작이 생길 수 있습니다. `functionResponse`의 role(`"function"`과 `"user"`)을 현재 문서로 확인해야 합니다. 루프 중에 fallback으로 모델이 바뀌면 thought signature 호환성도 확인해야 합니다. 5회 상한에 도달했을 때 "처리가 완료되었습니다"로 표시되는 것은 오해를 부릅니다. — **해결됨:** 모든 functionCall을 user-role 단일 턴으로 일괄 응답(결정), raw_content 그대로 왕복, 상한 도달 시 명확한 한도 안내 메시지로 교체
- [x] **R13:** 메시지마다 단발 호출이라 이전 대화 맥락이 없습니다. 명세가 의도한 동작인지 불명확합니다(D6). — **해결됨:** D6 채택(최근 N턴 유지+유휴 자동 리셋), `CONTEXT_TURNS`/`IDLE_RESET_MINUTES`로 런타임 조정, `/reset`

### 텔레그램

- [x] **R16:** `parse_mode`를 지정하지 않은 채 ``` 와 `**`를 보내면 기호가 그대로 보입니다. Markdown을 켜면 셸 출력의 특수문자 때문에 400 오류가 날 수 있으므로 이스케이프가 필요합니다. LLM 응답과 `/mem`이 4096자를 넘으면 전송이 실패하는데, `send_message`는 결과를 확인하지 않아 **조용히 실패합니다.** — **해결됨:** D5(HTML+이스케이프) 채택, `split_message()`로 4096자 분할, `send_message`가 각 청크 결과를 확인하고 400 시 plain 재시도
- [x] 단일 스레드 구조입니다. 45초짜리 셸 명령이나 긴 LLM 루프가 실행되는 동안 다른 메시지를 처리하지 못하고, `/restart`로도 중단할 수 없습니다. 이 구조를 수용할지 확인이 필요합니다. — **수용함:** 단일 사용자 봇 설계상 의도된 제약(웹 UI는 별도 스레드로 항상 병렬 동작), README/dev-plan에 문서화
- [x] `send_chat_action`을 먼저 동기 호출하므로 Bypass 경로에 HTTP 왕복 1회가 더해집니다. 0.1초 목표에 영향이 있습니다. — **수용함:** HTTP 왕복 1회 정도의 오버헤드로 수용, 실측은 docs/VERIFICATION.md §9 Bypass latency 항목에서 측정
- [x] `edited_message`와 사진 등 텍스트가 아닌 메시지는 무시됩니다. 의도한 동작인지 확인이 필요합니다. — **수용함:** 텍스트 명령 기반 봇으로 설계 범위 확정, `allowed_updates=["message"]`로 edited_message 자체를 수신하지 않고 텍스트 없는 message는 조용히 무시

### 플랫폼, 환경, 운영

- [x] **R2 (오래된 Mac 대상이라 치명적):** `tuple[dict, int]` 같은 내장 제네릭 어노테이션은 **Python 3.8 이하에서 함수 정의 시점에 TypeError**를 냅니다. 오래된 macOS의 `/usr/bin/python3`는 CLT shim이거나 3.7/3.8일 수 있습니다. 최소 버전을 정해야 합니다(D3). — **해결됨:** D3(3.8 이상) 채택, 전 모듈 `from __future__ import annotations`, 3.9+ API 미사용을 6.3 grep으로 확인
- [x] **R8:** python.org 배포판 Python은 SSL 루트 인증서를 따로 설치해야 합니다(`Install Certificates.command`). 설치하지 않으면 모든 HTTPS 호출이 실패합니다. 사용할 인터프리터 경로를 plist와 일치시켜야 합니다. — **수용함:** 코드로 해결 불가능한 배포 환경 이슈, README §2 Requirements에 python.org 빌드에만 해당한다고 명시(Apple CLT/Linux/Pi는 시스템 CA 사용)
- [x] **R23:** `launchctl load`는 deprecated입니다(`bootstrap`/`bootout`/`kickstart` 권장). **launchd의 기본 재시작 throttle은 약 10초라서 "1초 내 복구" 주장과 맞지 않습니다.** 실제 동작을 측정해야 합니다. plist 경로 placeholder(`사용자계정명`)는 한글이라 치환 실수 위험이 있습니다. — **수용함:** install.sh는 `bootstrap`/`bootout`만 사용, placeholder를 ASCII(`USERNAME`)로 교체, README에 launchd ~10초/systemd RestartSec=2 실측 기재란 마련
- [x] 로그 관련: `logging.basicConfig`는 stderr로 출력하므로 INFO 로그가 모두 `agent_err.log`에 쌓입니다. stdout 버퍼링 때문에 로그가 늦게 기록될 수 있습니다(`PYTHONUNBUFFERED`). 로그 로테이션이 없어 파일이 계속 커집니다. 로그에 사용자 메시지와 API 오류 본문이 남습니다. — **해결됨/수용함:** `basicConfig(stream=sys.stdout)`로 INFO를 agent.log로, plist/유닛에 `PYTHONUNBUFFERED=1`; macOS 로그 로테이션 부재는 README에 명시하고 수용(Linux/Pi는 journald가 처리), 토큰/키는 로그 출력 전 마스킹
- [x] **R26:** `skills/` 디렉터리는 명세에 있지만 로딩이나 호출 메커니즘이 정의되어 있지 않습니다(D8). `sample_skill.py`의 내용도 정의되지 않았습니다. — **해결됨:** D8(a) 채택, 로더 없이 LLM이 `execute_shell`로 실행, docstring 기반 자동 발견/목록화 구현, `sample_skill.py` 작성
- [x] **R27:** 무의존성 원칙상 `requirements.txt`는 필요 없습니다. 명세는 "PEP 518 준수"라고 하지만 `pyproject.toml`이 디렉터리 구조에 없습니다. 포함 여부를 정해야 합니다. — **해결됨:** `pyproject.toml` 포함(`dependencies = []`, `requires-python = ">=3.8"`), `requirements.txt`는 두지 않음
- [x] **R28:** 포그라운드 테스트 인스턴스와 launchd 인스턴스가 동시에 polling하면 409 Conflict가 발생합니다. — **해결됨:** R50으로 확장, `fcntl.flock` 단일 인스턴스 락으로 두 번째 프로세스를 기동 시점에 차단
- [x] **R29:** 지표 측정 기준이 정의되어 있지 않습니다. "0.1초"가 내부 처리 시간인지 텔레그램 왕복 시간인지, "30MB"가 RSS인지 등을 정해야 합니다. — **해결됨:** docs/VERIFICATION.md §1(측정법: `ps -o rss=`, 왕복시간 스톱워치/로그타임스탬프)과 §9(지표 표)로 기준 명시
- [ ] OpenClaw에서 데이터(기존 기억, 설정)를 옮기는 작업이 필요한지 명세에 언급이 없습니다.

---

## 5. 완료 기준 (Definition of Done)

명세 §5를 바탕으로 정리했고, §2의 결정 사항과 §4의 치명적 리스크 확인 항목을 더했습니다. 실행과 판정은 사용자가 합니다.

### 5.1 포그라운드 구동 (§5-1)
- [ ] 환경변수(또는 D2 방식)를 설정한 뒤 `python3 -m src.agent`가 기동 로그를 출력하고 계속 polling한다.
- [ ] 필수 환경변수가 빠지면 명확한 오류와 함께 종료한다. `ALLOWED_USER_ID`도 포함한다(D1이 fail-closed인 경우).
- [ ] 선택한 최소 Python 버전(D3)의 대상 Mac `/usr/bin/python3`에서 import 오류 없이 기동한다.
- [ ] 외부 패키지를 설치하지 않은 깨끗한 환경에서 동작한다(무의존성).

### 5.2 셸 Bypass (`!ls -la`, `/sh …`)
- [ ] `!ls -la`에 프로젝트 루트 목록이 LLM 호출 없이 반환된다. 로그에 Gemini 호출이 없어야 한다.
- [ ] `/sh echo hi` 형식도 동작한다.
- [ ] 3000자를 넘는 출력은 잘라내고 안내 문구를 붙인다.
- [ ] 45초를 넘는 명령은 타임아웃 메시지를 반환하고 봇이 계속 응답한다.
- [ ] 허가되지 않은 계정에서 보낸 `!` 명령은 실행되지 않는다.

### 5.3 메모리 (`/mem`)
- [ ] `/mem`이 `MEMORY.md` 내용을 LLM 호출 없이 출력한다.
- [ ] 자연어로 "기억해줘"라고 요청해 `append_memory`가 실행된 뒤, `/mem`에 새 `- 항목`이 보인다.
- [ ] 다음 LLM 호출에 새로 저장된 기억이 반영된다(system instruction 동기화).

### 5.4 재시작 (`/restart`, `/reset`)
- [ ] `/restart` 후 프로세스가 종료되고, launchd가 다시 기동한다. 실측한 복구 시간을 기록한다(R23).
- [ ] **재기동 후 같은 `/restart`를 다시 처리하지 않는다**(무한 재시작 없음, R3).

### 5.5 LLM 트랙
- [ ] 일반 대화에 응답한다. 셸 작업이 필요한 요청은 `execute_shell`을 거쳐 결과를 포함해 응답한다.
- [ ] 429를 인위적으로 발생시키면(mock 또는 테스트) 다음 모델로 전환되고, 로그에 전환이 기록된다.
- [ ] 비정상 응답(차단, 빈 candidates)이 와도 크래시 없이 안내 메시지를 보낸다.
- [ ] 4096자를 넘는 응답도 누락 없이 전달된다(분할 전송 또는 D5 방침).

### 5.6 launchd 등록 (§5-2)
- [ ] plist가 `~/Library/LaunchAgents/`에 설치되고, `launchctl`(bootstrap 또는 load)로 등록된다.
- [ ] 로그인이나 재부팅 후 자동으로 기동한다(RunAtLoad).
- [ ] 프로세스를 강제 종료(`kill`)해도 자동으로 복구된다(KeepAlive).
- [ ] 네트워크를 끊었다가 복구하는 동안 CPU busy-loop가 없고(R22), 복구 후 정상 응답한다.
- [ ] 비밀값 보관 방식이 D2 결정과 일치하고, 파일 권한이 적절하다.

### 5.7 지표
- [ ] 안정 상태 RSS가 30MB 이하다(`ps -o rss=` 측정값 기록).
- [ ] Bypass 처리 지연을 측정 기준(R29)에 따라 기록한다.
- [ ] LLM 1회 응답 지연을 모델별로 기록한다.

### 5.8 문서
- [ ] README에 설치, 실행, 서비스 관리, 보안 주의사항, 요구 Python 버전이 기재되어 있다.
- [ ] §4 체크리스트의 각 항목이 "해결됨" 또는 "수용함(사유)" 중 하나로 표시되어 있다.

### 5.9 설정 변경 (수정안 A1)
- [ ] 웹에서 `SHELL_TIMEOUT_SEC`와 `SHELL_OUTPUT_LIMIT`를 바꾸면 재시동 없이 다음 `!` 명령에 반영된다.
- [ ] `/set GEMINI_MODEL_CHAIN …` 값이 다음 LLM 호출 로그에 반영되고, 재시동 후에도 유지된다.
- [ ] `/set GEMINI_API_KEY …`, `/set ALLOWED_USER_ID …`, `/set TELEGRAM_BOT_TOKEN …`는 거부되고, 비밀 키 메시지는 삭제를 시도한다.
- [ ] 웹 폼에서 한 필드라도 무효면 아무것도 적용되지 않는다.
- [ ] 다른 출처의 폼 POST, 잘못된 Host, CSRF 없는 요청은 403이다.
- [ ] `WEB_PORT`를 바꾸면 새 주소로 접속되고 기존 포트는 닫힌다. 사용 중인 포트는 거부된다.
- [ ] `ALLOWED_USER_ID`를 바꾸면 기존 계정과 새 계정 모두에 알림이 간다.
- [ ] 필수값 없이 기동하면 설정 필요 모드로 뜨고, 웹에서 값을 입력하면 재시동 없이 polling이 시작된다.
- [ ] `settings.json` 권한이 0600이고 로그에 비밀값 원문이 없다.

---

## 6. 설계 수정안 A1: 런타임 설정 스토어 · 로컬 웹 설정 · 텔레그램 설정 명령

> 신규 요구사항: "모든 세팅은 로컬 웹 인터페이스와 텔레그램 커맨드로 수정할 수 있고, 수정하면 재시동 없이 바로 적용된다."
> 이 절이 §2·§3·§4와 충돌하는 부분에서는 이 절이 우선한다.

### 6.0 신규 사전 결정 (§2 표에 추가)

| # | 결정 항목 | 채택 | 관련 리스크 |
|---|---|---|---|
| D9 | 수정한 설정의 영속화 위치 | **`settings.json` 오버레이(0600, 원자적 저장)**. `.env`는 사람이 쓰는 읽기 전용 초기값, 앱은 절대 쓰지 않음. 이미 설정된 환경변수가 `.env`보다 우선하는 규칙 유지 | R33, R38 |
| D10 | 필수값(토큰·키·허용 ID) 누락 시 | **설정 필요 모드**: polling은 시작하지 않고 웹만 기동, 웹에서 채우면 무재시동으로 polling 시작. 웹 비활성 시에는 exit(1). fail-closed(D1)는 유지 — 유효한 ALLOWED_USER_ID 없이는 polling이 시작되지 않음 | R1, R5, R35 |
| D11 | 웹 UI 인증 | **로그인 없음 + Host/Origin/Sec-Fetch-Site/CSRF 토큰 필수 검사**. localhost 바인딩만으로는 브라우저 경유 공격(CSRF/DNS rebinding)을 막지 못하므로 검사 필수 | R31, R32, R34 |

### 6.1 설정 스토어 재설계

**레이어와 우선순위** (낮음 → 높음): ① default(카탈로그 기본값) → ② env(`.env` 위에 `os.environ`을 덮은 값, 카탈로그 키만) → ③ override(`settings.json`). 유효값은 최상위 레이어. 모든 레이어가 같은 검증기를 통과해야 하며, 무효 값은 경고 후 그 레이어만 무시(크래시 없음 → R5 해결). `.env`/`os.environ`은 기동 시 1회만 읽고, **`.env` 값을 `os.environ`에 쓰지 않는다**(비밀값 자식 셸 상속 차단, R40).

**`settings.json`**: `BASE_DIR/settings.json`, `{"version":1,"values":{...}}`, override만 저장. 쓰기는 `settings.json.tmp`를 `os.open(0o600)`으로 생성 → write/fsync → `os.replace` 원자 교체. 실패 시 메모리 불변 + 오류 반환. 깨진 JSON은 `settings.json.corrupt-<epoch>`로 보존 후 무시. 기동 시 권한에 group/other 비트가 있으면 경고 + chmod 600.

**`SettingsStore` API** (config.py 내 싱글턴 `settings`): `load()`(명시적 1회 호출, 무효 필수 키 목록 반환) / `get(key)`(잠금 없는 COW 읽기) / `snapshot()` / `update(changes, actor)`(web·telegram·system, 전부 적용 또는 전부 거부) / `unset(key, actor)` / `rows(mask=True)` / `on_change(key, fn)` / `revision`.

**update 순서**: 키 정규화(모르는 키는 difflib 제안) → actor 정책 검사(스토어에서도 재검사) → 파싱·검증 → no-op 제외 → 키별 precheck(getMe, 포트 바인드 등) → 원자 저장 → 참조 교체·revision 증가 → 잠금 해제 → 훅 실행(잠금 밖, 예외 격리) → 감사 로그(비밀 마스킹).

**스레드 안전성**: 읽기는 Copy-on-write로 잠금 없음(polling 스레드 비차단). 쓰기는 단일 `threading.Lock`으로 precheck~커밋 직렬화. 훅은 잠금 밖 실행(데드락 방지).

**마스킹**: 비밀값은 `••••`+마지막 4자(12자 미만은 `••••`만), 봇 토큰은 `<봇ID>:••••abcd`. 원문은 로그·HTML·텔레그램 응답 어디에도 노출 금지.

**설정 읽기 규칙(전 모듈, 6.3에서 grep 검증)**: `settings.get(...)`을 **호출 시점에** 부른다. 모듈 전역에서 설정값 읽기, 값 자체 import 금지. 경로 상수(`BASE_DIR`, `MEMORY_FILE`, `SKILLS_DIR`, `OFFSET_FILE`, `SETTINGS_FILE`)는 설정이 아닌 상수.

**기존 config.py 처리**: `parse_dotenv(path) -> dict`로 변경(os.environ 비변경), 모듈 전역 비밀 상수·import 시점 검증·`sys.exit` 삭제, 경로 상수·makedirs 유지 + `SETTINGS_FILE` 추가. `utils/http.py`는 영향 없음.

### 6.2 설정 카탈로그 (16키)

적용 시점: **즉시**=다음 사용, **폴링**=다음 polling 사이클, **리스너**=웹 리스너 자동 재구성.

| # | 키 | 타입/검증 | 기본값 | 분류 | Telegram | 적용 |
|---|---|---|---|---|---|---|
| 1 | `TELEGRAM_BOT_TOKEN` | str, `^\d{5,16}:[A-Za-z0-9_-]{30,64}$`, precheck getMe(10초) | 없음(필수) | 비밀 | **차단** | 폴링 |
| 2 | `GEMINI_API_KEY` | str 20~128자 `^[A-Za-z0-9_-]+$` | 없음(필수) | 비밀 | **차단** | 즉시 |
| 3 | `ALLOWED_USER_ID` | int 1~2^53−1, 봇 자신 ID 금지 | 없음(필수) | 보안 | **차단** | 즉시 |
| 4 | `GEMINI_MODEL_CHAIN` | 콤마 리스트 1~5개, 중복 불가 | `gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite` | 튜너블 | 허용 | 즉시 |
| 5 | `GEMINI_TIMEOUT_SEC` | int 5~300 | 60 | 튜너블 | 허용 | 즉시 |
| 6 | `GEMINI_FALLBACK_DELAY_SEC` | int 0~10 | 1 | 튜너블 | 허용 | 즉시 |
| 7 | `SYSTEM_PROMPT` | str 1~4000자, 여러 줄 허용 | 명세 §4.6 기본 문구 | 튜너블 | 허용 | 즉시 |
| 8 | `FC_MAX_LOOPS` | int 1~10 | 5 | 튜너블 | 허용 | 즉시 |
| 9 | `CONTEXT_TURNS` | int 0~50 (0=단발) | 10 | 튜너블 | 허용 | 즉시 |
| 10 | `IDLE_RESET_MINUTES` | int 0~1440 (0=끔) | 30 | 튜너블 | 허용 | 즉시 |
| 11 | `SHELL_TIMEOUT_SEC` | int 1~600 | 45 | 튜너블 | 허용 | 즉시 |
| 12 | `SHELL_OUTPUT_LIMIT` | int 200~20000 | 3000 | 튜너블 | 허용 | 즉시 |
| 13 | `POLL_TIMEOUT_SEC` | int 1~50 | 30 | 튜너블 | 허용 | 폴링 |
| 14 | `LOG_LEVEL` | enum DEBUG/INFO/WARNING/ERROR | INFO | 튜너블 | 허용 | 즉시(훅) |
| 15 | `WEB_ENABLED` | bool | true | 운영 | 허용 | 리스너 |
| 16 | `WEB_PORT` | int 1024~65535, precheck 바인드 테스트 | 8321 | 운영 | 허용 | 리스너 |

상수로 고정(설정 불가): 웹 바인드 주소 127.0.0.1, 경로들, 요청 본문 상한 64KB, 텔레그램 4096자 분할 크기. bool 파싱: `true/false/on/off/1/0/yes/no/켜기/끄기`.

**보안 정책**:
- **P1**: 비밀값(1,2)과 `ALLOWED_USER_ID`(3)는 **로컬 웹에서만** 수정 가능. 텔레그램 시도 시 거부 + 비밀 키는 원 메시지 `deleteMessage`(best-effort) + 재발급 권고.
- **P2**: 텔레그램으로 바꿀 수 있는 어떤 값도 `/set`·`/settings` 자체를 끊을 수 없다(Bypass는 Gemini 설정에 비의존).
- **P3**: `ALLOWED_USER_ID` 변경(웹)은 확인 체크박스 필수 + 커밋 후 기존/새 ID 양쪽에 알림(조용한 탈취 방지).
- **P4**: 토큰 변경(웹)은 getMe 실패 시 거부(잘못된 토큰 잠김 방지), 성공 시 새 봇 @username 표시.
- **P5**: `unset`/`update`로 필수 키를 무효 상태로 만들 수 없다.
- **P6**: LLM에게 설정 변경 tool 없음. 설정 명령 메시지는 대화 이력에 제외.

### 6.3 로컬 웹 인터페이스

- 새 패키지 `src/agent/web/` (`__init__.py`, `server.py`, `page.py`). `ThreadingHTTPServer(("127.0.0.1", port))`, 데몬 스레드, **바인드 주소는 127.0.0.1 코드 상수 고정**. `WEB_ENABLED=true`일 때만 지연 import(R39). Handler `timeout=10`, 본문 로그 금지.
- 라우트: `GET /`(설정 페이지, `?saved=N` 표시) / `POST /settings`(일괄 저장, 성공 시 303 PRG, 실패 시 400 재렌더) / `POST /unset` / 그 외 404·405.
- 폼: 값 필드 `v.<KEY>`, bool은 select(켜기/끄기), 비밀 필드는 `type=password` 항상 빈 값(빈 제출=변경 없음), 바뀐 값만 update.
- **필수 검사(실패 시 403)**: ① Host = `127.0.0.1:<포트>`/`localhost:<포트>`(GET·POST 모두, DNS rebinding 방어) ② Origin 있으면 same-origin만, `null` 거부 ③ Sec-Fetch-Site 있으면 `same-origin`/`none`만 ④ CSRF 토큰(`secrets.token_urlsafe(32)`, `hmac.compare_digest`) ⑤ 본문: urlencoded만(415), Content-Length 필수(411), 64KB 초과 413, `parse_qs(max_num_fields=100)` ⑥ 응답 헤더: CSP `default-src 'none'`, X-Frame-Options DENY, Cache-Control no-store, Referrer-Policy no-referrer, nosniff ⑦ 핸들러 예외는 500 일반 페이지.
- **핫 적용 한계**: 진행 중 셸 명령·Gemini 호출 1회는 시작 시점 snapshot 값 사용. 토큰 변경은 다음 폴링 사이클부터, **봇 ID가 달라지면 offset 초기화 + 파일 삭제**(R41). `WEB_PORT` 변경은 새 서버 선기동 → 성공 시 구 서버 종료, 실패 시 system actor로 롤백(R42). `WEB_ENABLED` 끄기는 안내 페이지 후 지연 종료, 텔레그램 `/set WEB_ENABLED on`으로 재기동.
- **WebServerManager**: 자체 Lock, 멱등 `reconcile()`, `WEB_ENABLED`/`WEB_PORT` 훅으로 등록.
- **설정 필요 모드(D10)**: 필수값 없으면 웹만 기동 + 로그 안내, 메인 스레드가 2초마다 확인 후 polling 시작, 페이지에 배너.
- 페이지: 외부 리소스·JS 없음, 인라인 CSS. 행 = 라벨/입력/출처 배지(default·env·override)/적용 시점/제약/"기본값으로" 버튼. `html.escape(quote=True)`. 하단에 revision·기동 시각·폴링 상태.

### 6.4 텔레그램 설정 명령 (Bypass 트랙, LLM 0%)

라우팅: 사용자 검증 직후, 다른 Bypass보다 앞. 대화 이력 제외. 출력은 HTML(D5) 이스케이프.

파싱: 첫 공백까지 명령 토큰(`@botname` 제거, 소문자 정확 일치), 키는 대문자 정규화, `/set`은 `split(maxsplit=2)` — 값은 키 뒤 나머지 전체(내부 공백 보존). 줄바꿈 값은 `SYSTEM_PROMPT`만 허용. 빈 값 불가(기본값 복원은 `/unset`).

| 명령 | 동작 |
|---|---|
| `/settings`, `/get` | 전체 목록: `<pre>`로 `KEY = 값 [출처] [🔒웹 전용] (적용 시점)`, 비밀 마스킹, 60자 초과 `…` |
| `/get KEY` | 단일 키 상세(현재값·출처·기본값·제약·적용 시점·설명·수정 가능 여부) |
| `/set` / `/set KEY` | 사용법 + Telegram 수정 가능 키 목록 |
| `/set KEY VALUE` | `update(actor="telegram")` → `✅ KEY: 이전값 → 새값 (적용: …)` / 같으면 `ℹ️ 변경 없음` |
| `/unset KEY` | override 제거 → `✅ KEY: override 제거됨 → 현재 값 (출처)` |

`/reset`은 D6의 대화 초기화 명령이므로 override 제거는 **`/unset`**으로 명명. 오류 응답: 모르는 키(difflib 제안), 검증 실패(사유+제약+현재값), 차단 키(`🔒 로컬 웹에서만` + 비밀 키는 deleteMessage+재발급 권고), 저장 실패. 메시지 처리는 계속 단일 스레드(긴 셸 명령 중 `/set` 대기, 웹은 병렬 동작).

### 6.5 계획 변경 요약

- 설정 스토어는 별도 Phase로 두지 않고 **Phase 1에 편입**(1.1 재작업 + 1.4 카탈로그 + 1.5 스토어 + 1.6 부속 파일). 웹 서버는 **신규 Phase 3.5**(client의 getMe에 의존).
- 모듈 의존 관계 교체: 모든 모듈은 `settings`를 **호출 시점**에 읽고, import 부작용은 경로 상수와 makedirs뿐. `web/server`는 Phase 3.5로 `__main__`에 연결.
- 세부 작업 변경·추가는 tasks.md에 반영(1.1b·1.4~1.6, 2.2·2.3 변경, 3.1·3.3·3.5 변경, 3.6·3.7 신규, Phase 3.5 W.1~W.4, 4.1·4.3 변경, 5.1·5.5·5.6 변경, 5.7·5.8 신규, 6.1·6.3 변경).

### 6.6 신규 리스크 (§4에 추가, R30~R43)

- [x] **R30**: 웹/폴링 스레드 동시 접근 — COW 읽기 + 단일 쓰기 Lock + 훅 잠금 밖 실행. offset·대화 이력은 메인 스레드만 소유. — **해결됨:** SettingsStore가 COW 딕셔너리 교체+`threading.Lock` 직렬화 쓰기로 구현, offset/`_history`는 메인 스레드만 접근
- [x] **R31 (치명적)**: 브라우저 경유 CSRF — 임의 사이트가 localhost로 폼 POST 가능, `ALLOWED_USER_ID` 변경 시 원격 셸 탈취. CSRF 토큰+Origin+Sec-Fetch-Site 필수, ALLOWED_USER_ID는 확인 체크+알림. — **해결됨:** `secrets.token_urlsafe(32)`+`hmac.compare_digest` CSRF 토큰, Origin/Sec-Fetch-Site 검사, ALLOWED_USER_ID 변경 확인 체크박스+구/신 계정 알림 구현
- [x] **R32**: DNS rebinding — GET·POST 모두 Host 엄격 검사. — **해결됨:** `_security_gate()`가 `do_GET`/`do_POST` 모두에서 Host 헤더를 엄격 검사
- [x] **R33**: `settings.json` 평문 비밀값 — `os.open(0o600)`, 기동 시 권한 점검, `.gitignore` 필수. `.env`와 같은 보호 수준. — **해결됨:** `os.open(...,0o600)` 원자적 저장, load 시 권한 검사+자동 chmod, `.gitignore`에 `settings.json*` 포함
- [x] **R34**: 같은 호스트 다른 로컬 사용자/프로세스의 접근 — 단일 사용자 전제로 수용, 필요 시 `WEB_ENABLED=false`. — **수용함:** 단일 사용자 개인 기기 전제, 필요 시 `WEB_ENABLED=false`로 끌 수 있음(README에 명시)
- [x] **R35**: `/set`으로 비밀값 전송 시 Telegram 클라우드에 잔존 — 거부+deleteMessage best-effort+재발급 권고. — **해결됨:** `_blocked_key_reply()`가 비밀 키에 한해 거부+`deleteMessage` best-effort+재발급 권고 메시지 구현
- [x] **R36**: LLM이 셸로 settings.json/.env 읽기·로컬 웹 조작 가능 — R25 수준 권한 내이므로 신규 상승은 아님, 인젝션 경로로 기록. — **수용함:** R25와 동일한 권한 수준(신규 권한 상승 아님), README §9 보안 주의사항에 인젝션 경로로 기록
- [x] **R37**: 핫 적용은 부분적(진행 중 작업은 이전 값) — 적용 시점을 UI·응답에 명시. — **해결됨:** 카탈로그 `apply_timing`을 웹 UI 행, `/settings`·`/get`·`/set` 응답에 모두 표시
- [x] **R38**: override가 `.env`보다 우선해 ".env 고쳤는데 반영 안 됨" 혼란 — 출처 배지+기본값 버튼+README 명시. 실행 중 수동 편집은 다음 쓰기 때 덮어씀(mtime 경고만). — **해결됨:** 웹 UI 출처 배지+"기본값으로" 버튼, README §7 "Gotcha"에 우선순위와 잠김 복구 절차 명시
- [x] **R39**: 웹 서버 RSS 증가 — 지연 import, 웹 on/off RSS 비교 측정(5.6). — **해결됨:** `http.server`는 웹 서버 기동 시점에만 지연 import, docs/VERIFICATION.md §9에 web on/off RSS 비교 항목
- [x] **R40 (높음)**: `.env`값의 os.environ 주입 → 자식 셸에 비밀 상속, `env` 실행만으로 Gemini 컨텍스트로 유출 — 파서를 순수 함수로(1.1b), shell 자식 env에서 비밀 키 제거(2.2). — **해결됨:** `parse_dotenv()`는 `os.environ`을 건드리지 않는 순수 함수, `shell._child_env()`가 자식 프로세스 env에서 두 비밀 키를 제거(테스트로 확인)
- [x] **R41**: 토큰을 다른 봇으로 변경 시 기존 offset 부정합 — 봇 ID 변경 감지 시 offset 초기화. 새 봇에 webhook 있으면 409(4.1 처리). — **해결됨:** `_handle_token_change()`가 `getMe`로 봇 ID 변경을 감지해 offset 파일을 삭제, 409는 폴링 루프에서 로그+backoff로 처리
- [x] **R42**: 포트 변경 경쟁 상황 — 새 서버 선기동, 실패 시 롤백, 응답 전송 후 지연 종료. — **해결됨:** `_on_port_change()`가 새 포트 선기동 후 구 서버 종료, 바인드 실패 시 system actor로 이전 포트로 롤백
- [x] **R43 (잠김 시나리오)**: ①웹 끈 상태에서 토큰 폐기 ②웹에서 ALLOWED_USER_ID 오타 후 웹 끔 — 복구: 서비스 중지 → settings.json에서 키 삭제 → 재기동(README 문서화). 텔레그램만으로는 잠김 불가(P1·P2). — **해결됨:** README §7 "Lockout recovery"에 복구 절차 문서화, 비밀값/ALLOWED_USER_ID는 웹 전용(P1)이라 텔레그램만으로는 잠김 불가

### 6.7 범위 밖

웹 바인드 주소 설정화·HTTPS·원격 접근 / Keychain(D2 유지) / `.env`·`settings.json` 파일 감시 자동 리로드 / 경로 상수 런타임 변경 / LLM용 설정 변경 tool / 설정 변경 이력 UI(감사 로그 파일만). `utils/http.py`(1.2)와 `memory.py`(2.1)는 영향 없음.

---

## 7. 설계 수정안 A2: 멀티플랫폼 지원 (macOS · Linux PC · Raspberry Pi)



**A1에서 바뀌는 점**
- 웹 서버는 `ThreadingHTTPServer` 대신 단일 스레드 `HTTPServer`를 권장합니다(§5, R54).
- `SHELL_PATH` 키가 추가되어 A1 카탈로그가 17개가 됩니다(§1).
- `SYSTEM_PROMPT` 기본 문구를 플랫폼 중립으로 바꿉니다(§1).

---

> 대상 플랫폼은 **macOS, Linux PC, Raspberry Pi(Linux)**입니다. Windows는 범위에서 뺍니다.
> 조건은 **Python 3.8 이상, 표준 라이브러리만 사용**입니다.
> 병합 위치: dev-plan §1(범위 문구), §2(D3 수정, D12 추가), §3(Phase 2·4·5·6), §4(R44~R55), §5(5.10), tasks.md

### A2-0. 사전 결정

| # | 결정 항목 | 결정 | 관련 |
|---|---|---|---|
| D3 (수정) | 최소 Python 버전 | **3.8 이상.** 사용자 Mac이 3.8.2이고, Pi OS Bullseye는 3.9, Bookworm은 3.11입니다. `pyproject.toml`의 `requires-python`을 `>=3.8`로 고칩니다. **Ubuntu 20.04(3.8.10)도 지원 범위에 들어옵니다.** | R47, R55 |
| D12 (신규) | 플랫폼과 서비스 계층 | **POSIX만 지원합니다.** macOS는 launchd, Linux PC와 Pi는 systemd user unit을 씁니다. 범용 재시작 wrapper는 두지 않습니다. | R44~R53 |

**D12 근거.** launchd와 systemd는 둘 다 부팅 시 기동, 종료 시 재시작, 로그 수집을 기본으로 제공합니다. wrapper를 따로 두더라도 부팅 시 기동에는 결국 서비스 관리자가 필요하므로 중복입니다. Pi는 순수 Python이라 ARM이어도 차이가 없고, Linux와 같은 경로를 씁니다.

#### 3.8 호환 코딩 규칙 (dev-plan §3 머리말과 README 기여 규칙에 추가)

- 모든 모듈에 `from __future__ import annotations`를 씁니다. 이미 정책입니다.
- 이 import는 **어노테이션에만** 효과가 있습니다. 아래처럼 런타임에 평가되는 식에서는 3.9 문법을 쓰면 안 됩니다.
  - `X = dict[str, int]` 같은 타입 별칭
  - `isinstance(x, list[int])`
  - `cast(list[str], …)`
  - 이런 곳에는 `typing.Dict`/`List`를 쓰거나 문자열로 씁니다.
- 쓰지 말아야 할 3.9 이상 전용 기능

  | 도입 버전 | 기능 |
  |---|---|
  | 3.9 | dict `\|` 병합, `str.removeprefix`/`removesuffix`, `zoneinfo`, `functools.cache`, `math.lcm`, `random.randbytes`, `ast.unparse`, `pathlib.Path.is_relative_to`/`with_stem`, `os.waitstatus_to_exitcode`, `typing.Annotated`, `graphlib` |
  | 3.10 | `match`, `zip(strict=)`, `dataclass(slots=/kw_only=)`, 괄호로 묶은 여러 줄 `with`, `int.bit_count` |
  | 3.11 | `subprocess` `process_group=`, `tomllib` |

- 대신 쓸 것
  - `start_new_session=True`
  - `functools.lru_cache(maxsize=None)`
  - 슬라이싱으로 접두사 제거
- A1 설계에 쓰인 기능은 모두 3.8에 있으므로 **설계는 바뀌지 않습니다.**
  - `http.server.ThreadingHTTPServer`/`HTTPServer`, `secrets`, `hmac.compare_digest`
  - `os.replace`, `os.open` 모드 지정, `parse_qs(max_num_fields=)`
  - `difflib`, `fcntl`, `pwd`, `os.killpg`
  - `logging.basicConfig(force=)`, `threading.excepthook`

---

### A2-1. 셸 엔진 (Phase 2.2, 기존 [senior] 작업에 포함)

#### 1.1 기동 가드
`os.name != "posix"`이면 `__main__`이 "지원하지 않는 플랫폼입니다"를 출력하고 exit(1)합니다. 이 검사는 Phase 4.1에서 구현합니다.

#### 1.2 셸 선택은 설정 키로 둡니다: `SHELL_PATH` (A1 카탈로그 17번)

| 키 | 타입 / 검증 | 기본값 | 분류 | Telegram | 적용 |
|---|---|---|---|---|---|
| `SHELL_PATH` | `auto` 또는 절대경로. 파일이 있어야 하고 실행 가능(`os.access X_OK`)해야 합니다. `/etc/shells`가 있으면 그 목록에 있어야 합니다. | `auto` | 튜너블 | 허용 | 즉시(다음 명령) |

- **텔레그램 수정을 허용하는 이유**
  - 셸 권한 자체는 이미 사용자에게 있으므로 권한이 새로 늘지 않습니다.
  - `/etc/shells` 검사로 엉뚱한 실행 파일은 막습니다.
  - 값을 잘못 넣어도 셸 명령만 실패합니다. `/set SHELL_PATH auto`로 복구할 수 있으므로 A1의 P2 불변식(텔레그램 설정으로 텔레그램 제어 경로가 끊기지 않음)을 지킵니다.
- **`auto`일 때 결정 순서.** 명령을 실행할 때마다 판단하며, 결과를 캐시하지 않습니다.
  1. macOS(`sys.platform == "darwin"`)이면 `/bin/zsh`
  2. `$SHELL`
  3. `pwd.getpwuid(os.getuid()).pw_shell`. launchd나 systemd 환경에서는 `$SHELL`이 비어 있을 수 있어서 넣었습니다.
  4. `/bin/sh`
- 각 후보는 절대경로, 파일 존재, `X_OK`를 모두 만족해야 합니다. `nologin`이나 `false`로 끝나는 셸은 건너뜁니다.
- 명시한 경로가 실행 시점에 사라졌으면 "설정된 셸을 찾을 수 없음: <경로>"를 반환하고, 다른 셸로 자동 전환하지 않습니다.

#### 1.3 실행 방식 (초안의 `subprocess.run(shell=True, executable="/bin/zsh")`를 대체)

- `subprocess.Popen([shell, "-c", command], ...)`으로 argv를 명시합니다.
  - `start_new_session=True`: 새 세션과 프로세스 그룹을 만듭니다.
  - `stdin=DEVNULL`, `stdout=PIPE`, `stderr=PIPE`
  - `cwd=BASE_DIR`
  - `encoding="utf-8"`, `errors="replace"`
- 출력은 `communicate(timeout=SHELL_TIMEOUT_SEC)`로 받습니다. `subprocess.run`은 타임아웃이 나면 직계 자식만 죽이므로 쓰지 않습니다.
- **타임아웃 처리 순서**
  1. `os.killpg(pgid, SIGTERM)`
  2. 2초 동안 `communicate`로 대기
  3. 그래도 남아 있으면 `SIGKILL`을 보내고 남은 출력을 회수
  4. "실행 시간 초과 (N초 제한)"과 부분 출력을 반환

  `ProcessLookupError`와 `PermissionError`는 무시합니다. macOS와 Linux에서 동작이 같습니다.
- **실행 중인 자식 추적.** 모듈 변수에 현재 pgid를 기록합니다. 메시지 처리가 단일 스레드라 메인 스레드만 씁니다. `kill_active()`를 제공해 SIGTERM 핸들러에서 호출합니다(R52).
- **자식 프로세스 env**
  - `os.environ`의 복사본에서 `TELEGRAM_BOT_TOKEN`과 `GEMINI_API_KEY`를 뺍니다(A1의 R40).
  - PATH는 아래 디렉터리 중 **존재하고 아직 PATH에 없는 것만** 뒤에 붙입니다(R48).
    - macOS: `/opt/homebrew/bin`, `/usr/local/bin`
    - Linux/Pi: `~/.local/bin`, `/usr/local/bin`, `/snap/bin`
- 기존 2.2 요구사항은 그대로입니다: stdout과 stderr 모두 보존, 종료 코드 표시, `SHELL_OUTPUT_LIMIT` 잘라내기.
- `-l`(login shell)은 **쓰지 않습니다.** 매번 프로필을 읽어 느리고 부작용이 있어서, 대신 PATH를 보강합니다.

#### 1.4 LLM에게 실행 환경 알리기 (Phase 2.3에 추가, [senior])
- `execute_shell` tool 설명을 호출할 때마다 만듭니다. 예: "Linux(aarch64)의 /bin/bash에서 셸 명령어를 실행합니다."
- system instruction 끝에 `[실행 환경] OS / 아키텍처 / 셸 경로 / 작업 디렉터리` 블록을 자동으로 붙입니다. 값은 `platform.system()`, `platform.machine()`, 결정된 셸에서 가져옵니다.
- A1의 `SYSTEM_PROMPT` 기본값을 **플랫폼 중립 문구**로 바꿉니다. 예: "당신은 사용자의 컴퓨터에서 구동되는 최소형 에이전트입니다…". 명세 §4.6의 "macOS 환경" 표현을 없애는 것입니다.
- 목적은 R44를 줄이는 것입니다. Debian과 Pi의 `/bin/sh`는 dash라서, LLM이 zsh나 bash 문법을 쓰면 명령이 실패합니다.

---

### A2-2. 24/7 서비스 계층 (Phase 4)

| OS | 파일 | 핵심 설정 |
|---|---|---|
| macOS | `launchd/com.user.shellie.plist` (기존 4.2) | `KeepAlive=true`(종료 코드와 관계없이 재시작), `RunAtLoad`, **`Umask=63`(077) 추가**, `PYTHONUNBUFFERED=1`, `PATH`, 비밀값 없음(D2). 인터프리터 경로는 3.8.2가 있는 실제 경로로 맞춥니다. |
| Linux PC / Pi | `systemd/shellie.service` (user unit, 신규) | 아래 명세 |

#### 2.1 systemd user unit 명세

**`[Unit]`**
- `Description`
- `StartLimitIntervalSec=60`, `StartLimitBurst=10`: 크래시 루프가 나도 상한에서 멈춥니다.
- `network-online.target`은 **넣지 않습니다.** user 매니저에는 이 타깃이 없습니다. 네트워크가 끊기는 상황은 메인 루프의 backoff(R22)가 처리합니다.

**`[Service]`**
- `Type=simple`
- `WorkingDirectory=%h/shellie` (경로는 placeholder)
- `ExecStart=/usr/bin/python3 -m src.agent`
- `Environment=PYTHONPATH=.`, `Environment=PYTHONUNBUFFERED=1`
- **`Restart=always`.** `on-failure`로 두면 `/restart`가 exit 0으로 끝날 때 재시작하지 않습니다.
- `RestartSec=2`
- `UMask=0077`
- 기본값 `KillMode=control-group`: 서비스를 멈추면 셸 자식까지 cgroup 단위로 정리됩니다.
- 비밀값은 넣지 않습니다(D2).

**`[Install]`**
- `WantedBy=default.target`

**로그.** stdout/stderr가 journald로 갑니다. 로테이션은 journald가 처리하고, Pi의 SD 카드 쓰기도 줄어듭니다. 확인은 `journalctl --user -u shellie -f`로 합니다.

**부팅 시 기동.** 로그인하지 않은 상태에서도 부팅 때 뜨게 하려면 `loginctl enable-linger $USER`를 한 번 실행해야 합니다(R46). 헤드리스 Pi에서는 필수입니다. 설치 스크립트와 README에 명시합니다.

#### 2.2 `/restart` 의미 (플랫폼 공통, Phase 3.4 보강)
- 동작 순서는 같습니다. offset을 확정(R3)한 뒤 `sys.exit(0)`하면, launchd `KeepAlive` 또는 systemd `Restart=always`가 다시 띄웁니다.
- **재시작 지연**
  - launchd: 기본 throttle 때문에 약 **10초**
  - systemd: `RestartSec`에 따라 약 **2초**

  두 값 모두 실측해서 기록합니다(R23, R45). 명세의 "1초 내 복구" 문구는 README에서 고칩니다.
- **서비스 관리자 감지.** 다음 환경변수가 모두 없으면 응답에 "⚠️ 서비스 관리자 없이 실행 중입니다. 자동으로 재시작되지 않습니다"를 붙입니다. 그래도 종료는 합니다.
  - systemd: `INVOCATION_ID`
  - launchd: `XPC_SERVICE_NAME`(값이 `0`이 아닌 경우)
- A1에서 설정을 핫 적용하게 됐으므로 `/restart`는 이제 코드 갱신 반영과 비상 복구용이라는 점을 README에 적습니다.

#### 2.3 SIGTERM 처리 (Phase 4.1)
- `systemctl --user stop`이나 `launchctl bootout`이 보내는 SIGTERM을 핸들러로 받습니다.
- 처리 순서: `shell.kill_active()` → 웹 리스너 종료 → 로그 → `exit(0)`
- A1에서 파일 쓰기를 원자적으로 설계했으므로 도중에 끊겨도 파일이 손상되지 않습니다.
- systemd는 stop 명령으로 종료된 서비스를 재시작하지 않습니다.

---

### A2-3. 플랫폼에 따라 달라지는 기타 사항

- **그대로 두는 것.** `os.path` 기반 경로, `.env` 파싱, 파일 I/O, `os.replace`, `fcntl`, `pwd`, `os.killpg`는 macOS와 Linux에서 같습니다.
- **단일 인스턴스 락 (신규, Phase 4.1)**
  - `BASE_DIR/.shellie.lock`에 `fcntl.flock(LOCK_EX | LOCK_NB)`를 겁니다. 이미 잡혀 있으면 "다른 인스턴스가 실행 중입니다"를 출력하고 exit(1)합니다.
  - 포그라운드 인스턴스와 서비스 인스턴스가 동시에 뜨면 409, `settings.json`/offset 덮어쓰기, 웹 포트 충돌이 생깁니다. 이 락으로 막습니다(R28 확장, R50).
  - A1의 설정 스토어는 **같은 프로세스 안에서** `threading.Lock`만 쓰면 되고, 여러 프로세스 간 경합은 이 락이 담당합니다.
- **PATH.** launchd는 `/usr/bin:/bin:/usr/sbin:/sbin` 수준이고, systemd user 환경도 최소한으로 설정됩니다. R18 문구를 "launchd와 systemd 모두 PATH가 최소한"으로 바꾸고, 대응은 §1.3의 PATH 보강으로 통일합니다.
- **SSL.** Linux와 Pi는 시스템 CA(`ca-certificates` 패키지)를 씁니다. R8(`Install Certificates.command`)은 **python.org판 macOS Python에만** 해당한다고 명시합니다. Apple CLT의 3.8.2는 시스템 인증서 저장소를 씁니다. 실제 TLS 동작은 5.10에서 확인합니다.
- **웹 URL.** Linux 브라우저는 `localhost`를 `::1`로 먼저 해석할 수 있는데, 서버는 IPv4에만 바인딩합니다. 안내 문구, 로그, README에는 항상 `http://127.0.0.1:PORT/`를 씁니다. A1의 Host 검사가 `localhost`를 허용하는 것은 그대로 둡니다(R53).
- **macOS 전용 표현 교체 목록**
  - dev-plan §1: "macOS용" → "macOS·Linux(PC, Raspberry Pi)용"
  - Phase 4 제목: "진입점과 launchd 연결" → "진입점과 서비스 등록"
  - 4.2와 4.3이 plist만 다루는 부분 → OS별로 나눔
  - R23: launchd에만 해당한다고 명시
  - design-req §3.4와 §4.5의 `executable="/bin/zsh"`: A2-1로 대체된다고 표시(원 명세는 기준선이므로 수정하지 않고 dev-plan에 차이만 기록)
  - §4.6의 "macOS 환경" 문구와 tool 설명 "macOS zsh"
  - §5 실행 가이드와 6.1 README의 `launchctl` 전용 절차 → OS별 섹션
  - `pyproject.toml`의 `>=3.9` → `>=3.8`

---

### A2-4. 계획 변경

**Phase 0**
- 0.1b (신규) `systemd/` 디렉터리 생성 `[junior]`
- 0.5b (신규) `.gitignore`에 `.shellie.lock` 추가 `[junior]`
- 0.7 (변경) `pyproject.toml`의 `requires-python`을 `>=3.8`로 `[junior]`

**Phase 1**
- 1.2 (확인) `http.py`에 3.9 이상 전용 기능이 없는지 확인합니다. `tuple[dict, int]`는 어노테이션이라 문제없습니다. `[junior]`
- 1.4 (변경) A1 카탈로그에 `SHELL_PATH`를 추가하고, `/etc/shells` 검증기를 만듭니다. `SYSTEM_PROMPT` 기본값을 플랫폼 중립으로 바꿉니다. `[senior]`

**Phase 2**
- 2.2 (변경) `shell.py`를 POSIX 분기 엔진으로 만듭니다(A2-1 §1.1~§1.3 전체). 셸 결정 순서, Popen과 프로세스 그룹, killpg TERM→KILL, `kill_active()`, 비밀값을 뺀 env, OS별 PATH 보강을 포함합니다. `[senior]`
- 2.3 (변경) `gemini.py`: tool 설명과 `[실행 환경]` 블록을 호출할 때마다 만듭니다. `[senior]`

**Phase 3**
- 3.4 (변경) `/restart`: 서비스 관리자를 감지해 안내를 붙입니다. `[senior]`

**Phase 4: 진입점과 서비스 등록**
- 4.1 (변경) `__main__.py`: POSIX 가드, flock 단일 인스턴스, SIGTERM 핸들러(`kill_active` → 웹 종료 → exit 0) `[senior]`
- 4.2 (변경) plist에 `Umask=63`을 추가하고, 인터프리터 경로를 3.8.2가 있는 실제 경로로 맞춥니다. `[junior]`
- 4.4 (신규) `systemd/shellie.service`를 A2-2 §2.1 명세대로 작성합니다. `[junior]`
- 4.3 (변경) `install.sh`를 OS별로 나눕니다. `[junior]`
  - Darwin: plist 경로 치환 → `launchctl bootstrap gui/$(id -u)`
  - Linux: `~/.config/systemd/user/`에 복사 → `systemctl --user daemon-reload` → `enable --now` → `loginctl enable-linger` 안내
  - 공통: `.env`와 `settings.json`에 `chmod 600`

**Phase 5** (에이전트는 테스트를 작성만 하고, 실행과 판정은 Chester가 합니다)
- 5.1 (변경) 테스트에서 셸 경로를 하드코딩하지 않고 POSIX 공통 명령(`echo`, `printf`, `sleep`)만 씁니다. 테스트 코드도 3.8 호환 규칙을 따릅니다. `[senior]`
- 5.2 (변경, **senior로 승격**) shell 테스트 `[senior]`
  - 셸 결정 순서: `sys.platform`, env, `pwd`를 mock으로 바꿔 검증
  - 명시한 경로가 없어졌을 때의 오류
  - 타임아웃 시 손자 프로세스까지 정리되는지(`sleep 100 & sleep 100`)
  - 비밀 env 제거, 비UTF-8 출력
- 5.6 (변경) 수동 검증 **플랫폼 매트릭스** `[senior]`

  | 대상 | Python | 서비스 |
  |---|---|---|
  | macOS | 3.8.2 | launchd |
  | Linux PC x86_64 | 3.8 이상 | systemd |
  | Raspberry Pi OS Bullseye | 3.9 | systemd |
  | Raspberry Pi OS Bookworm | 3.11 | systemd |

  각 대상에서 다음을 확인합니다.
  - 서비스 설치, kill 후 복구, `/restart` 복구 시간
  - 재부팅 후 자동 기동(Linux는 linger 필요)
  - `!echo $0`에 결정된 셸이 나오는지
  - 이중 인스턴스 거부, SIGTERM 정상 종료
  - 웹 켬/끔 상태의 RSS(`ps -o rss= -p <pid>`, Pi는 필수)

**Phase 6**
- 6.1 (변경) README를 OS별로 구성합니다. `[junior]`
  - 지원 OS와 Python 버전 표(3.8 이상, Pi Bullseye와 Bookworm)
  - macOS: launchd `bootstrap` / `bootout` / `kickstart`
  - Linux/Pi: systemd `--user` 명령, `enable-linger`, `journalctl`
  - `SHELL_PATH` 설명
  - 재시작 지연 실측값
  - 3.8 호환 코딩 규칙
- 6.3 (변경) 최종 검토에 grep 두 가지를 추가합니다. `[senior]`
  - 문서와 코드에 `/bin/zsh`나 "macOS 전용" 표현이 남아 있는지
  - 3.9 이상 전용 API가 쓰였는지

### dev-plan §5에 추가: 5.10 플랫폼
- [ ] macOS(3.8.2), Linux PC, Pi(Bullseye 또는 Bookworm)에서 import 오류 없이 기동한다.
- [ ] `!echo $0`에 결정된 셸이 나오고, `SHELL_PATH`를 바꾸면 다음 명령부터 반영된다.
- [ ] 타임아웃이 나도 손자 프로세스가 남지 않는다.
- [ ] 서비스 kill 후 복구된다. `/restart` 복구 시간을 OS별로 실측한다. 재부팅 후 자동 기동한다(Linux는 linger 필요).
- [ ] 두 번째 인스턴스는 기동을 거부한다. SIGTERM을 받으면 정상 종료 로그가 남는다.
- [ ] Pi에서 웹을 켠 상태의 RSS를 기록하고, 30MB 이하다.

---

### A2-5. 지표와 Raspberry Pi 관련 사항

30MB RSS 목표는 Pi에서 가장 빠듯합니다. 64비트 Python은 기본 RSS가 더 큽니다.
- **지연 import 유지.** A1대로 `http.server`는 `WEB_ENABLED`가 true일 때만 import합니다.
- **웹 서버 클래스 변경 권장.** A1의 `ThreadingHTTPServer`는 요청마다 스레드를 새로 만듭니다. **`HTTPServer`(단일 스레드) 1개를 데몬 스레드 1개에서 돌리는** 방식으로 바꾸기를 권합니다.
  - 사용자가 한 명이라 요청을 차례로 처리해도 충분하고, 추가 스레드가 정확히 1개입니다.
  - Handler에 `timeout = 10`을 두어, 느린 연결 하나가 UI를 붙잡지 못하게 합니다.
  - 포트를 바꿀 때 `shutdown()`은 반드시 **별도 스레드에서** 불러야 합니다. 단일 스레드 서버에서는 핸들러 안에서 부르면 데드락이 납니다. A1 §3.4의 절차가 이미 별도 스레드를 쓰므로 그대로 두면 됩니다.
  - 되돌리려면 클래스만 바꾸면 됩니다.
- **캐시 금지.** 렌더링한 HTML, 응답, 설정 변경 이력을 메모리에 두지 않습니다. 요청 본문 64KB 상한도 유지합니다.
- **SD 카드 쓰기.** 설정 파일 쓰기는 드뭅니다. offset은 update마다 작은 파일을 쓰지만, 개인용 봇 빈도라 무시할 수준입니다. 로그는 journald로 보냅니다.
- **목표 초과 시.** Pi에서 웹을 켠 상태로 30MB를 넘으면, Pi의 권장 운영값을 `WEB_ENABLED=false`로 하고 필요할 때만 `/set WEB_ENABLED on`으로 켭니다.

---

### A2-6. 신규 리스크 (dev-plan §4, R44부터)

#### 멀티플랫폼
- [x] **R44: 셸 문법이 플랫폼마다 다릅니다.** zsh, bash, dash가 다르고, Debian과 Pi의 `/bin/sh`인 dash는 `[[`나 배열을 쓸 수 없습니다. LLM이 zsh를 가정하면 명령이 실패합니다. 대응은 실행 환경 블록, 동적 tool 설명, `SHELL_PATH`입니다. — **해결됨:** `[실행 환경]` 블록+`execute_shell` 동적 description+`SHELL_PATH` 설정키로 실제 셸을 매 호출 노출
- [x] **R45: 서비스 관리자마다 재시작 방식이 다릅니다.**
  - systemd는 `Restart=on-failure`면 exit 0 뒤에 재시작하지 않으므로 `always`가 필수입니다.
  - StartLimit에 걸리면 서비스가 멈춥니다. R3 같은 무한 재시작 루프에 상한이 생기는 효과도 있습니다.
  - launchd throttle은 약 10초, systemd는 2초입니다. 복구 시간을 OS별로 실측합니다. — **해결됨:** systemd 유닛 `Restart=always`+`RestartSec=2`+`StartLimit` 구현, README에 OS별 실측 기재란 마련
- [x] **R46: linger가 없으면 부팅 시 뜨지 않습니다.** systemd user unit은 `loginctl enable-linger`가 없으면 부팅 때 기동하지 않고, 로그아웃하면 멈춥니다. 헤드리스 Pi에서는 치명적입니다. 설치 스크립트와 README에 명시합니다. — **해결됨:** `install.sh`가 Linux 경로에서 `loginctl enable-linger $USER` 안내를 출력, README에도 문서화
- [x] **R47: 배포판마다 Python 버전이 다릅니다.** 최소 버전을 3.8로 낮춰 Ubuntu 20.04, Bullseye, Bookworm, macOS 3.8.2가 모두 지원 범위에 들어옵니다. 서비스 파일의 인터프리터 경로가 실제 설치 경로와 일치해야 합니다. — **해결됨:** D3(≥3.8) 통일, 3.9+ API 미사용을 grep으로 확인(Task C). 인터프리터 경로(`/usr/bin/python3`)는 plist/유닛에 고정값으로 남아 있어, 실제 설치 경로가 다르면 사용자가 직접 수정해야 함(README에 미기재 — 6.3 grep 발견 사항으로 별도 보고)
- [x] **R48: 서비스 환경의 PATH가 최소한입니다(launchd, systemd 모두).** R18을 넓혀 OS별 PATH 보강으로 대응합니다. — **해결됨:** `shell._augmented_path()`가 macOS(`/opt/homebrew/bin`,`/usr/local/bin`)/Linux·Pi(`~/.local/bin`,`/usr/local/bin`,`/snap/bin`)를 자동 보강
- [x] **R49: Pi SD 카드 쓰기 마모.** 영향이 작아 수용하고 로그는 journald로 보냅니다. macOS 로그 로테이션은 기존 미결 항목으로 남깁니다. — **수용함:** 개인용 봇 빈도라 영향 작음, Linux/Pi 로그는 journald가 처리, macOS 로그 로테이션 미구현은 README에 명시(사용자가 직접 로테이션)
- [x] **R50: 인스턴스가 두 개 뜰 수 있습니다.** 포그라운드와 서비스 인스턴스가 동시에 뜨면 409, `settings.json`/offset 덮어쓰기, 웹 포트 충돌이 생깁니다. `fcntl.flock` 단일 인스턴스 락으로 막습니다(R28 확장). — **해결됨:** `__main__._acquire_single_instance_lock()`이 `.shellie.lock`에 `flock(LOCK_EX|LOCK_NB)`, 실패 시 즉시 exit(1)
- [x] **R51: SIGTERM 핸들러가 없으면 정상 종료 절차 없이 끝납니다.** 원자적 쓰기로 손상은 없지만, 웹 종료와 종료 로그가 누락됩니다. 핸들러로 대응합니다. — **해결됨:** `signal.signal(SIGTERM/SIGINT, _handle_termination)`이 `kill_active()`→`manager.stop()`→로그→`exit(0)` 순으로 처리
- [x] **R52: 서비스를 멈춘 뒤 셸 자식이 남을 수 있습니다.** `start_new_session`으로 띄운 자식이 대상입니다. systemd는 cgroup 단위로 정리하지만, launchd는 다른 프로세스 그룹에 있는 자식이 남을 수 있습니다. SIGTERM 핸들러에서 `kill_active()`를 호출합니다. — **해결됨:** `shell.kill_active()`가 `_active_pgid`에 SIGTERM을 보내도록 구현되어 SIGTERM 핸들러에서 호출됨
- [x] **R53: `localhost`가 `::1`로 해석될 수 있습니다.** 서버는 IPv4에만 바인딩하므로 안내에는 항상 `127.0.0.1`을 씁니다. — **해결됨:** 서버 바인드는 `127.0.0.1` 상수 고정, README/로그/웹 UI 안내 문구 모두 `127.0.0.1`만 사용(Host 검사는 `localhost`도 허용은 유지)
- [x] **R54: Pi에서 RSS 목표가 빠듯합니다.** 단일 스레드 웹 서버, 지연 import, 캐시 금지로 줄이고 실측합니다. 넘으면 웹을 기본으로 끕니다. — **해결됨:** `HTTPServer`(단일 스레드, A2-5 권장대로 `ThreadingHTTPServer` 대신 채택)+지연 import+캐시 없음, docs/VERIFICATION.md §9에 Pi 필수 측정 항목
- [x] **R55: Python 3.8은 지원 종료(EOL, 2024-10) 버전입니다.** 보안 패치가 더는 나오지 않습니다. Apple CLT 3.8.2의 TLS 스택이 오래됐을 수 있으므로 Telegram/Gemini HTTPS 연결을 실측합니다. 3.9 이상 API가 섞여 들어오는 것은 6.3의 grep과 3.8 실기기 테스트로 잡습니다. 가능하면 이후 버전 상향을 권장합니다. — **수용함:** 대상 Mac이 3.8.2라 낮출 수밖에 없는 제약, 3.9+ API 미사용은 6.3 grep으로 확인 완료, 이후 버전 상향은 README/dev-plan에 권장 사항으로 기록

---

### A2-7. 범위 밖
- Windows 지원(기동 시 거부)과 범용 재시작 wrapper
- 시스템 전역(root) systemd unit. user unit만 씁니다.
- 로그인 셸 모드(`-l`)
- Linux Secret Service나 Keychain 연동(D2 유지)
- 컨테이너나 Docker 배포