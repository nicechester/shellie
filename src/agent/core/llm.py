from __future__ import annotations

# Chat-turn dispatch over one mixed fallback chain (issue #33). LLM_MODEL_CHAIN
# is read at call time; each entry is "gemini/<model>" or "openai/<model>".
# Entries are tried in order. A fallback-eligible failure moves on to the next
# entry, and when a whole pass fails the chain cools down and repeats. The
# per-provider rules (RPM/RPD, 5xx backoff, key checks) live in each adapter's
# attempt_model(). Both adapters return a Gemini-shaped response, so
# parse_response() and the tool loop are shared, and a conversation can switch
# providers mid-way.

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.config import settings
from src.agent.core import gemini, openai
from src.agent.core.gemini import _MAX_COOLDOWN_SEC, _set_retry_status

_LOGGER = logging.getLogger("shellie.llm")


def call_llm(
    contents: List[Dict[str, Any]],
    on_cooldown: Optional[Callable[[int, int, float], None]] = None,
    allow_tools: bool = True,
) -> Tuple[Dict[str, Any], str]:
    """Call the LLM_MODEL_CHAIN, returning (Gemini-shaped response, entry).

    Settings are snapshotted once at the top of the call and cover all retry
    passes. Cooldown and the pause between entries use the GEMINI_* timing
    settings for every provider. A whole pass that fails cools down up to
    GEMINI_MAX_RETRIES times: the largest server-suggested delay in the pass
    if any, otherwise exponential backoff from GEMINI_RETRY_BASE_DELAY_SEC,
    capped at _MAX_COOLDOWN_SEC. `on_cooldown(attempt, max_retries, delay)` is
    called (best-effort) before each cooldown sleep. When allow_tools is
    False, function calling is disabled for this call.

    Live retry/cooldown state is published via gemini.get_retry_status() and
    reset to idle when the call returns or raises.
    """
    chain = settings.get("LLM_MODEL_CHAIN")
    gemini_key = settings.get("GEMINI_API_KEY")
    gemini_timeout = settings.get("GEMINI_TIMEOUT_SEC")
    openai_base = settings.get("OPENAI_API_BASE")
    openai_key = settings.get("OPENAI_API_KEY")
    openai_timeout = settings.get("OPENAI_TIMEOUT_SEC")
    delay = settings.get("GEMINI_FALLBACK_DELAY_SEC")
    base_delay = settings.get("GEMINI_RETRY_BASE_DELAY_SEC")
    max_retries = settings.get("GEMINI_MAX_RETRIES")

    # Built on first use per provider, so an all-Gemini chain never touches
    # the OpenAI message converter and vice versa.
    payloads: Dict[str, Dict[str, Any]] = {}

    def payload_for(provider: str) -> Dict[str, Any]:
        if provider not in payloads:
            build = gemini.build_payload if provider == "gemini" else openai.build_payload
            payloads[provider] = build(contents, allow_tools)
        return payloads[provider]

    try:
        for attempt in range(max_retries + 1):
            hinted: Optional[float] = None

            for index, entry in enumerate(chain):
                provider, _, model = entry.partition("/")
                if provider == "gemini":
                    result = gemini.attempt_model(model, payload_for(provider), gemini_key, gemini_timeout, delay)
                elif provider == "openai":
                    result = openai.attempt_model(
                        model, payload_for(provider), openai_base, openai_key, openai_timeout
                    )
                else:
                    raise Exception("Unknown LLM provider in chain: {}".format(entry))

                if result.ok:
                    return result.response, entry

                if result.hint is not None and (hinted is None or result.hint > hinted):
                    hinted = result.hint
                if index < len(chain) - 1:
                    time.sleep(delay)

            if attempt == max_retries:
                raise Exception("All LLM models failed (rate limit or transient error).")

            cooldown = min(max(hinted or 0, base_delay * (2 ** attempt)), _MAX_COOLDOWN_SEC)
            if hinted is not None and cooldown == hinted:
                cooldown_source = "server hint"
            elif attempt == 0:
                cooldown_source = "base delay"
            else:
                cooldown_source = "exponential backoff (attempt {})".format(attempt + 1)
            _LOGGER.warning(
                "All LLM models failed on attempt %d/%d, cooling down %.1fs before retry (source=%s)",
                attempt + 1, max_retries + 1, cooldown, cooldown_source,
            )
            _set_retry_status(
                "cooldown", reason="chain_cooldown", model=None, delay=cooldown,
                attempt=attempt + 1, max_attempts=max_retries,
            )
            if on_cooldown is not None:
                try:
                    on_cooldown(attempt + 1, max_retries, cooldown)
                except Exception:
                    _LOGGER.debug("on_cooldown callback raised, ignoring", exc_info=True)
            time.sleep(cooldown)

        raise Exception("All LLM models failed (rate limit or transient error).")
    finally:
        _set_retry_status("idle")
