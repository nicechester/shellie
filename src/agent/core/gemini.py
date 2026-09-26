from __future__ import annotations

import ast
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.config import SKILLS_DIR, settings
from src.agent.core import mcp, shell
from src.agent.core.memory import append_memory, read_memory
from src.agent.utils.http import http_post

_LOGGER = logging.getLogger("shellie.gemini")

_GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

_FALLBACK_5XX = (500, 502, 503, 504)


class ToolEntry:
    """Registry entry: a Gemini tool declaration builder plus its runner.

    Structured so an external adapter can append entries without changing
    run_tool/build_tools_schema. The HTTP MCP client (src.agent.core.mcp) is
    one such adapter: it is not a TOOL_REGISTRY entry, but is wired into
    build_tools_schema()/run_tool() directly below.
    """

    def __init__(
        self,
        name: str,
        declaration: Callable[[], Dict[str, Any]],
        run: Callable[[Dict[str, Any]], str],
    ) -> None:
        self.name = name
        self.declaration = declaration
        self.run = run


def _execute_shell_declaration() -> Dict[str, Any]:
    env = shell.execution_environment()
    description = "{}({})의 {}에서 셸 명령어를 실행합니다. 파일 관리, 스크립트 실행 등이 가능합니다.".format(
        env["os"], env["arch"], env["shell"]
    )
    return {
        "name": "execute_shell",
        "description": description,
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "실행할 셸 명령어"},
            },
            "required": ["command"],
        },
    }


def _execute_shell_run(args: Dict[str, Any]) -> str:
    return shell.execute_shell(str(args.get("command", "")))


def _append_memory_declaration() -> Dict[str, Any]:
    return {
        "name": "append_memory",
        "description": "중요한 규칙이나 사용자 개인화 정보를 MEMORY.md에 저장합니다.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "content": {"type": "STRING", "description": "기억할 핵심 요약 내용"},
            },
            "required": ["content"],
        },
    }


def _append_memory_run(args: Dict[str, Any]) -> str:
    return append_memory(str(args.get("content", "")))


TOOL_REGISTRY: List[ToolEntry] = [
    ToolEntry("execute_shell", _execute_shell_declaration, _execute_shell_run),
    ToolEntry("append_memory", _append_memory_declaration, _append_memory_run),
]


def run_tool(name: str, args: Dict[str, Any]) -> str:
    for entry in TOOL_REGISTRY:
        if entry.name == name:
            return entry.run(args)
    mcp_result = mcp.try_run(name, args)
    if mcp_result is not None:
        return mcp_result
    return "Unknown function: {}".format(name)


def build_tools_schema() -> List[Dict[str, Any]]:
    declarations = [entry.declaration() for entry in TOOL_REGISTRY]
    try:
        declarations.extend(mcp.tool_declarations())
    except Exception:
        _LOGGER.exception("MCP 툴 선언 생성 중 예외 발생, 기본 툴만 사용합니다")
    return [{"functionDeclarations": declarations}]


def _describe_py_skill(name: str) -> str:
    line = "- skills/{}".format(name)
    try:
        path = os.path.join(SKILLS_DIR, name)
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        doc = ast.get_docstring(ast.parse(source))
        if doc:
            first_line = doc.strip().splitlines()[0].strip()
            if first_line:
                line = "- skills/{}: {}".format(name, first_line)
    except Exception:
        pass
    return line


def _describe_md_skill(name: str) -> str:
    line = "- skills/{}".format(name)
    try:
        path = os.path.join(SKILLS_DIR, name)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        for raw_line in content.splitlines():
            stripped = raw_line.strip()
            if stripped:
                summary = stripped.lstrip("#").strip()
                if summary:
                    line = "- skills/{}: {}".format(name, summary[:80])
                break
    except Exception:
        pass
    return line


def list_skills() -> str:
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return ""

    lines: List[str] = []
    for name in names:
        if name.endswith(".py") and not name.startswith("_"):
            lines.append(_describe_py_skill(name))
        elif name.endswith(".md") and not name.startswith("_") and name.lower() != "readme.md":
            lines.append(_describe_md_skill(name))
    return "\n".join(lines)


def build_system_instruction() -> Dict[str, Any]:
    text = settings.get("SYSTEM_PROMPT")
    text += "\n\n[장기 기억]\n" + read_memory()
    text += '\n(오래된 기억 검색: 셸에서 grep -ri "<키워드>" memory/ 실행)'

    skills_text = list_skills()
    if skills_text:
        text += (
            "\n\n[스킬 목록] (.py: python3 skills/<이름>.py 로 실행 / "
            ".md: cat skills/<이름>.md 로 전문을 읽고 그 절차를 따르세요)\n" + skills_text
        )

    env = shell.execution_environment()
    text += "\n\n[실행 환경]\nOS: {} / 아키텍처: {} / 셸: {} / 작업 디렉터리: {}".format(
        env["os"], env["arch"], env["shell"], env["cwd"]
    )
    return {"parts": [{"text": text}]}


def _error_status(res: Dict[str, Any]) -> Optional[str]:
    error = res.get("error") if isinstance(res, dict) else None
    if isinstance(error, dict):
        status = error.get("status")
        if isinstance(status, str):
            return status
    return None


def _should_fallback(status: int, res: Dict[str, Any]) -> bool:
    if status == 429:
        return True
    if status in _FALLBACK_5XX:
        return True
    if status == 0:
        return True
    if _error_status(res) == "RESOURCE_EXHAUSTED":
        return True
    return False


def _fallback_reason(status: int, res: Dict[str, Any]) -> str:
    if status == 429:
        return "429"
    if status == 0:
        return "전송 오류"
    if status in _FALLBACK_5XX:
        return "HTTP {}".format(status)
    if _error_status(res) == "RESOURCE_EXHAUSTED":
        return "RESOURCE_EXHAUSTED"
    return "알 수 없는 오류"


def call_gemini(contents: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], str]:
    chain = settings.get("GEMINI_MODEL_CHAIN")
    key = settings.get("GEMINI_API_KEY")
    timeout = settings.get("GEMINI_TIMEOUT_SEC")
    delay = settings.get("GEMINI_FALLBACK_DELAY_SEC")

    payload = {
        "contents": contents,
        "systemInstruction": build_system_instruction(),
        "tools": build_tools_schema(),
    }

    for model in chain:
        url = _GEMINI_URL_TEMPLATE.format(model)
        headers = {"x-goog-api-key": key}
        res, status = http_post(url, payload, headers=headers, timeout=timeout)

        if status == 200:
            return res, model

        if _should_fallback(status, res):
            _LOGGER.warning(
                "Gemini 모델 %s 호출 실패(%s), 다음 모델로 전환", model, _fallback_reason(status, res)
            )
            time.sleep(delay)
            continue

        raise Exception("Gemini API 호출 실패 (모델={}, status={})".format(model, status))

    raise Exception("모든 Gemini 모델 호출에 실패했습니다 (rate limit 또는 일시 오류).")


class ParsedReply:
    def __init__(
        self,
        text: str,
        function_calls: List[Tuple[str, Dict[str, Any]]],
        blocked: bool,
        block_reason: Optional[str],
        finish_reason: Optional[str],
        raw_content: Dict[str, Any],
    ) -> None:
        self.text = text
        self.function_calls = function_calls
        self.blocked = blocked
        self.block_reason = block_reason
        self.finish_reason = finish_reason
        self.raw_content = raw_content


def parse_response(response: Dict[str, Any]) -> ParsedReply:
    if not isinstance(response, dict):
        return ParsedReply("", [], False, None, None, {})

    candidates = response.get("candidates")
    if not candidates:
        block_reason = None
        prompt_feedback = response.get("promptFeedback")
        if isinstance(prompt_feedback, dict):
            block_reason = prompt_feedback.get("blockReason")
        return ParsedReply("", [], bool(block_reason), block_reason, None, {})

    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return ParsedReply("", [], False, None, None, {})

    finish_reason = candidate.get("finishReason")
    content = candidate.get("content")
    if not isinstance(content, dict):
        return ParsedReply("", [], False, None, finish_reason, {})

    parts = content.get("parts")
    if not isinstance(parts, list):
        parts = []

    text_chunks: List[str] = []
    function_calls: List[Tuple[str, Dict[str, Any]]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought"):
            continue
        if "functionCall" in part:
            fc = part.get("functionCall") or {}
            function_calls.append((fc.get("name", ""), fc.get("args") or {}))
            continue
        if "text" in part:
            text_chunks.append(part.get("text") or "")

    return ParsedReply("".join(text_chunks), function_calls, False, None, finish_reason, content)
