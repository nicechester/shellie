오픈클로(OpenClaw)를 완전히 대체하여 오래된 Mac 환경에서 최적의 속도, 백그라운드 안정성, 토큰 절감, 무의존성(Zero-Dependency)을 제공하는 텔레그램 기반 경량 에이전트 최종 시스템 설계 명세서 (System Architecture & Software Design Specification)입니다.

---

# 📋 최종 시스템 설계 명세서 (Lightweight Telegram Agent)

## 1. 프로젝트 개요 (Project Overview)

* **목적:** 오픈클로의 비대한 스택과 의존성을 배제하고, macOS Native 환경에서 **Standard Python 3 내장 라이브러리만**을 사용하여 텔레그램 메신저, 셸(Shell) 직통 실행, Gemini API Fallback Chain, 파일 기반 메모리/스킬 관리 시스템을 결합한 최적화 에이전트 구축.
* **핵심 지표:**
* **의존성 (Dependency):** 0% (`pip install` 불필요, Standard Python Library만 사용)
* **메모리 점유율:** 30MB 이하 (RAM)
* **응답 시간 (Latency):** 셸 직통 바이패스 시 0.1초 이내, LLM 추론 시 1~2초 내
* **가동성 (Availability):** `launchd` 기반 24/7 백그라운드 무중단 복구 구동



---

## 2. 표준 파이썬 프로젝트 구조 (Project Layout)

표준 PEP 518 / Modern Python Directory Structure 규칙을 준수하여 모듈화 및 관리가 용이하도록 구성합니다.

```text
mac-telegram-agent/
├── .env.example                # 환경 변수 템플릿
├── README.md                   # 프로젝트 문서 및 사용법
├── launchd/
│   └── com.user.telegramagent.plist # macOS launchd 자동 구동 서비스 파일
├── memory/
│   └── MEMORY.md               # 장기 기억 (시스템 규칙, 사용자 정보 등)
├── skills/                     # 동적 스킬/스크립트 저장소
│   ├── README.md
│   └── sample_skill.py
└── src/
    └── agent/                  # 메인 패키지
        ├── __init__.py
        ├── __main__.py          # python -m src.agent 실행 진입점
        ├── config.py            # 환경변수 및 패스 설정 모듈
        ├── core/
        │   ├── __init__.py
        │   ├── gemini.py        # urllib 기반 Gemini API Client & 429 Fallback
        │   ├── memory.py        # MEMORY.md I/O 관리
        │   └── shell.py         # Subprocess zsh Engine
        ├── telegram/
        │   ├── __init__.py
        │   ├── client.py        # urllib 기반 Telegram Long-Polling Client
        │   └── handlers.py      # Bypass, Command, LLM Router
        └── utils/
            ├── __init__.py
            └── http.py          # urllib REST HTTP Wrapper (requests 대체)

```

---

## 3. 핵심 모듈별 상세 설계 명세

### 3.1 `src/agent/config.py` (설정 및 가드)

* **역할:** 환경변수 로드, 필수 폴더 경로 확정, 보안(Allowed User ID) 검증.
* **명세:**
* `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `ALLOWED_USER_ID` 검증.
* `BASE_DIR`, `MEMORY_FILE`, `SKILLS_DIR` 경로 정의 및 존재하지 않을 시 자동 생성(`os.makedirs`).



### 3.2 `src/agent/utils/http.py` (REST Engine)

* **역할:** 외부 패키지(`requests`, `httpx`) 없이 `urllib.request`만을 이용해 JSON 통신 수행.
* **명세:**
* `http_post(url, payload, headers)`: Timeout(60초) 설정 및 HTTP Status Code/JSON 반환.
* `http_get(url, timeout)`: Long-Polling 지원을 위한 GET Wrapper.



### 3.3 `src/agent/core/gemini.py` (LLM & 429 Fallback Engine)

* **역할:** Gemini REST API 직접 호출 및 Rate Limit 우회 로직.
* **명세:**
* **Model Chain Sequence:** `gemini-2.5-pro` ➔ `gemini-2.5-flash` ➔ `gemini-2.5-flash-lite`
* **Fallback Trigger:** HTTP 429 반환 또는 `RESOURCE_EXHAUSTED` 에러 코드 수신 시 즉시 다음 모델로 Swapping.
* **System Instruction Injection:** 매 호출 시 `MEMORY.md` 파일의 최신 내용을 시스템 프롬프트에 자동 동기화.



### 3.4 `src/agent/core/shell.py` (macOS Shell Engine)

* **역할:** macOS Native `zsh` 명령어 실행 및 결과 반환.
* **명세:**
* `subprocess.run(command, shell=True, executable="/bin/zsh", timeout=45, cwd=BASE_DIR)`
* 출력 길이 3,000자 초과 시 하단 잘라내기(Truncate)하여 토큰 소모 및 텔레그램 출력 폭발 방지.



### 3.5 `src/agent/core/memory.py` (Memory Manager)

* **역할:** 간단한 Markdown 파일 기반 장기 기억 관리.
* **명세:**
* `read_memory()`: `MEMORY.md` 읽기 (실패 시 기본 문구 반환).
* `append_memory(content)`: 새 정보를 한 줄 단위(`- content`)로 파일 하단에 이어쓰기(Append).



### 3.6 `src/agent/telegram/handlers.py` (Router & Bypass Track)

* **역할:** 메시지 수신 시 처리 트랙 분기.
* **명세:**
1. **User Check:** `user_id != ALLOWED_USER_ID` 시 거부.
2. **Track A (Bypass Track):**
* 메시지가 `!` 또는 `/sh `로 시작 ➔ **LLM 호출 0%, 토큰 0, 즉시 셸 실행** ➔ 결과 반환.
* 메시지가 `/mem` ➔ **LLM 호출 0%** ➔ `MEMORY.md` 바로 출력.
* 메시지가 `/restart` 또는 `/reset` ➔ `sys.exit(0)` 호출 (launchd가 1초 내 즉시 프로세스 재시동).


3. **Track B (LLM + Tool Track):**
* 일반 자연어 대화 수신 시 Gemini API Manager 호출.
* Function Call (`execute_shell`, `append_memory`) 발생 시 최대 5회 루프 처리 후 최종 답변 반환.





---

## 4. 전체 구현 소스 코드 (Pure Python / Standard Library Only)

아래 코드들을 지정된 파이썬 프로젝트 구조에 맞게 생성하여 배치합니다.

### 4.1 `.env.example`

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here
ALLOWED_USER_ID=123456789

```

---

### 4.2 `src/agent/config.py`

```python
import os
import sys

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    print("❌ 에러: TELEGRAM_BOT_TOKEN 및 GEMINI_API_KEY 환경변수가 필요합니다.")
    sys.exit(1)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MEMORY_DIR = os.path.join(BASE_DIR, "memory")
MEMORY_FILE = os.path.join(MEMORY_DIR, "MEMORY.md")
SKILLS_DIR = os.path.join(BASE_DIR, "skills")

os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(SKILLS_DIR, exist_ok=True)

```

---

### 4.3 `src/agent/utils/http.py`

```python
import json
import urllib.request
import urllib.error

def http_post(url: str, payload: dict, headers: dict = None) -> tuple[dict, int]:
    if headers is None:
        headers = {}
    headers["Content-Type"] = "application/json"
    
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            body = res.read().decode("utf-8")
            return json.loads(body) if body else {}, res.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            err_json = json.loads(body)
        except Exception:
            err_json = {"error": body}
        return err_json, e.code
    except Exception as e:
        return {"error": str(e)}, 500

def http_get(url: str, timeout: int = 35) -> tuple[dict, int]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8")
            return json.loads(body) if body else {}, res.status
    except urllib.error.HTTPError as e:
        return {}, e.code
    except Exception as e:
        return {"error": str(e)}, 500

```

---

### 4.4 `src/agent/core/memory.py`

```python
import os
from src.agent.config import MEMORY_FILE

def read_memory() -> str:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return content if content else "등록된 메모리가 없습니다."
    return "등록된 메모리가 없습니다."

def append_memory(content: str) -> str:
    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n- {content.strip()}")
    return "메모리가 저장되었습니다."

```

---

### 4.5 `src/agent/core/shell.py`

```python
import subprocess
from src.agent.config import BASE_DIR

def execute_shell(command: str) -> str:
    try:
        res = subprocess.run(
            command,
            shell=True,
            executable="/bin/zsh",
            capture_output=True,
            text=True,
            timeout=45,
            cwd=BASE_DIR
        )
        out = (res.stdout if res.stdout else res.stderr).strip()
        if not out:
            return "성공적으로 실행되었습니다 (출력 없음)."
        if len(out) > 3000:
            return out[:3000] + "\n... (출력이 길어 3000자에서 잘림)"
        return out
    except subprocess.TimeoutExpired:
        return "에러: 명령어 실행 시간 초과 (45초 제한)."
    except Exception as e:
        return f"에러 발생: {str(e)}"

```

---

### 4.6 `src/agent/core/gemini.py`

```python
import time
import logging
from src.agent.config import GEMINI_API_KEY
from src.agent.utils.http import http_post
from src.agent.core.memory import read_memory

MODEL_CHAIN = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite"
]

TOOLS_SCHEMA = [
    {
        "functionDeclarations": [
            {
                "name": "execute_shell",
                "description": "macOS zsh 셸 명령어를 실행합니다. 파일 관리, 스크립트 실행 등이 가능합니다.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "command": {"type": "STRING", "description": "실행할 zsh 명령어"}
                    },
                    "required": ["command"]
                }
            },
            {
                "name": "append_memory",
                "description": "중요한 규칙이나 사용자 개인화 정보를 MEMORY.md에 저장합니다.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "content": {"type": "STRING", "description": "기억할 핵심 요약 내용"}
                    },
                    "required": ["content"]
                }
            }
        ]
    }
]

def call_gemini(contents: list) -> tuple[dict, str]:
    memory_context = read_memory()
    system_instruction = {
        "parts": [{
            "text": f"당신은 macOS 환경에서 구동되는 최소형 에이전트입니다. 필요시 셸 명령어와 메모리 툴을 활용하세요.\n\n[장기 기억]\n{memory_context}"
        }]
    }

    payload = {
        "contents": contents,
        "systemInstruction": system_instruction,
        "tools": TOOLS_SCHEMA
    }

    for model in MODEL_CHAIN:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        res, status = http_post(url, payload)

        if status == 200:
            return res, model
        elif status == 429:
            logging.warning(f"[{model}] 429 Rate Limit. 다음 Fallback 모델 시도...")
            time.sleep(1)
            continue
        else:
            logging.error(f"[{model}] Error {status}: {res}")
            raise Exception(f"Gemini API 에러: {status}")

    raise Exception("모든 Gemini 모델의 Rate Limit(429)에 도달했습니다.")

```

---

### 4.7 `src/agent/telegram/client.py`

```python
from src.agent.config import TELEGRAM_BOT_TOKEN
from src.agent.utils.http import http_get, http_post

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def get_updates(offset: int = None) -> dict:
    url = f"{BASE_URL}/getUpdates?timeout=30"
    if offset:
        url += f"&offset={offset}"
    res, status = http_get(url, timeout=35)
    return res if status == 200 else {}

def send_message(chat_id: int, text: str, parse_mode: str = None) -> dict:
    url = f"{BASE_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    res, status = http_post(url, payload)
    return res

def send_chat_action(chat_id: int, action: str = "typing"):
    url = f"{BASE_URL}/sendChatAction"
    http_post(url, {"chat_id": chat_id, "action": action})

```

---

### 4.8 `src/agent/telegram/handlers.py`

```python
import sys
import logging
from src.agent.config import ALLOWED_USER_ID
from src.agent.core.shell import execute_shell
from src.agent.core.memory import read_memory, append_memory
from src.agent.core.gemini import call_gemini
from src.agent.telegram.client import send_message, send_chat_action

def process_update(update: dict):
    message = update.get("message", {})
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")

    if not text or not chat_id:
        return

    # 1. 보안 체크
    if ALLOWED_USER_ID != 0 and user_id != ALLOWED_USER_ID:
        send_message(chat_id, "⛔ 권한이 없습니다.")
        return

    # 2. [Bypass Track] 셸 직접 실행
    if text.startswith("!") or text.startswith("/sh "):
        cmd = text[1:].strip() if text.startswith("!") else text[4:].strip()
        send_chat_action(chat_id)
        res = execute_shell(cmd)
        send_message(chat_id, f"```zsh\n{res}\n```")
        return

    # 3. [Bypass Track] 메모리 단순 조회
    if text == "/mem":
        mem = read_memory()
        send_message(chat_id, f"🧠 **장기 기억 (MEMORY.md):**\n\n{mem}")
        return

    # 4. [Bypass Track] 시스템 리셋/재시동
    if text in ["/restart", "/reset"]:
        send_message(chat_id, "🔄 프로세스를 종료합니다. launchd가 즉시 재시동합니다...")
        sys.exit(0)

    # 5. [LLM Track] 대화 및 Function Calling
    send_chat_action(chat_id)
    contents = [{"role": "user", "parts": [{"text": text}]}]

    try:
        response, used_model = call_gemini(contents)
        candidate = response.get("candidates", [{}])[0]
        content_out = candidate.get("content", {})
        parts = content_out.get("parts", [])

        # Function Call 처리 (최대 5회)
        loop_count = 0
        while parts and "functionCall" in parts[0] and loop_count < 5:
            loop_count += 1
            call_info = parts[0]["functionCall"]
            fn_name = call_info.get("name")
            args = call_info.get("args", {})

            if fn_name == "execute_shell":
                tool_res = execute_shell(args.get("command", ""))
            elif fn_name == "append_memory":
                tool_res = append_memory(args.get("content", ""))
            else:
                tool_res = "Unknown function"

            # 내역 누적 후 Gemini 재호출
            contents.append(content_out)
            contents.append({
                "role": "function",
                "parts": [{
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"output": tool_res}
                    }
                }]
            })
            response, used_model = call_gemini(contents)
            candidate = response.get("candidates", [{}])[0]
            content_out = candidate.get("content", {})
            parts = content_out.get("parts", [])

        # 최종 응답 발송
        final_text = parts[0].get("text", "처리가 완료되었습니다.") if parts else "응답이 없습니다."
        send_message(chat_id, final_text)

    except Exception as e:
        logging.error(f"오류: {e}")
        send_message(chat_id, f"⚠️ 오류가 발생했습니다: {str(e)}")

```

---

### 4.9 `src/agent/__main__.py` (진입점)

```python
import time
import logging
from src.agent.telegram.client import get_updates
from src.agent.telegram.handlers import process_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def main():
    logging.info("🚀 Pure Python macOS Telegram Agent 가동 시작.")
    offset = None

    while True:
        try:
            updates = get_updates(offset)
            if updates and updates.get("ok"):
                for result in updates.get("result", []):
                    offset = result["update_id"] + 1
                    process_update(result)
        except Exception as e:
            logging.error(f"Main Loop Error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()

```

---

### 4.10 `launchd/com.user.telegramagent.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.telegramagent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>-m</string>
        <string>src.agent</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/사용자계정명/mac-telegram-agent</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>.</string>
        <key>TELEGRAM_BOT_TOKEN</key>
        <string>여기에_토큰</string>
        <key>GEMINI_API_KEY</key>
        <string>여기에_KEY</string>
        <key>ALLOWED_USER_ID</key>
        <string>여기에_USER_ID</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/사용자계정명/mac-telegram-agent/agent.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/사용자계정명/mac-telegram-agent/agent_err.log</string>
</dict>
</plist>

```

---

## 5. 실행 및 서비스 등록 가이드

1. **프로젝트 폴더 이동 및 구동 테스트:**
```bash
cd mac-telegram-agent
export TELEGRAM_BOT_TOKEN="your_token"
export GEMINI_API_KEY="your_key"
export ALLOWED_USER_ID="123456789"

# 파이썬 표준 모듈 실행
python3 -m src.agent

```


2. **macOS `launchd` 데몬 서비스 등록:**
```bash
cp launchd/com.user.telegramagent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.telegramagent.plist

```


3. **원격 동작 검증 (텔레그램):**
* **`!ls -la`** ➔ LLM 바이패스로 즉시 파일 디렉토리 출력
* **`/mem`** ➔ 장기 기억 출력
* **`/restart`** ➔ 프로세스 재시동 (launchd가 1초 내 복구)