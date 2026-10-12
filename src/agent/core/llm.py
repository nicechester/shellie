from __future__ import annotations

# Backend dispatch for chat turns (issue #13). LLM_BACKEND is read at call
# time, so switching backends applies to the next message without a restart.
# Both backends return a Gemini-shaped response, so parse_response() is shared.

from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.config import settings
from src.agent.core import gemini, openai


def call_llm(
    contents: List[Dict[str, Any]],
    on_cooldown: Optional[Callable[[int, int, float], None]] = None,
    allow_tools: bool = True,
) -> Tuple[Dict[str, Any], str]:
    if settings.get("LLM_BACKEND") == "openai":
        return openai.call_openai(contents, on_cooldown=on_cooldown, allow_tools=allow_tools)
    return gemini.call_gemini(contents, on_cooldown=on_cooldown, allow_tools=allow_tools)
