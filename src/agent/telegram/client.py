from __future__ import annotations

import logging
from typing import Dict, List, Optional

from src.agent.config import settings
from src.agent.utils.http import http_post

_LOGGER = logging.getLogger("shellie.telegram.client")


def _api_url(method: str, token: Optional[str] = None) -> str:
    current_token = token or settings.get("TELEGRAM_BOT_TOKEN")
    return f"https://api.telegram.org/bot{current_token}/{method}"


def mask_token(text: str) -> str:
    token = settings.get("TELEGRAM_BOT_TOKEN")
    if token and ":" in token and token in text:
        bot_id, _, rest = token.partition(":")
        tail = rest[-4:] if len(rest) >= 4 else rest
        masked = f"{bot_id}:••••{tail}"
        return text.replace(token, masked)
    return text


def get_updates(offset: Optional[int] = None) -> Dict:
    token = settings.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"_status": 0, "_error": "token not set"}

    poll_timeout = settings.get("POLL_TIMEOUT_SEC") or 30
    http_timeout = poll_timeout + 5

    payload = {
        "timeout": poll_timeout,
        "allowed_updates": ["message"]
    }
    if offset is not None:
        payload["offset"] = offset

    url = _api_url("getUpdates", token)
    response, status = http_post(url, payload, timeout=http_timeout)

    if status == 200:
        return response

    error_str = str(response.get("error", ""))
    masked_error = mask_token(error_str)
    _LOGGER.warning("getUpdates failed: status=%d error=%s", status, masked_error)

    return {"_status": status, "_error": masked_error}


def split_message(text: str, limit: int = 4096) -> List[str]:
    if not text:
        return [""]

    chunks: List[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        pre_count_open = remaining[:limit].count("<pre>")
        pre_count_close = remaining[:limit].count("</pre>")

        if pre_count_open > pre_count_close:
            effective_limit = limit - 11
        else:
            effective_limit = limit

        last_newline = remaining[:effective_limit].rfind("\n")

        if last_newline > 0:
            chunk = remaining[:last_newline + 1]
            remaining = remaining[last_newline + 1:]
        else:
            chunk = remaining[:effective_limit]
            remaining = remaining[effective_limit:]

        pre_count_open = chunk.count("<pre>")
        pre_count_close = chunk.count("</pre>")

        if pre_count_open > pre_count_close:
            if len(chunk) + 6 <= limit:
                chunk += "</pre>"
            if remaining:
                remaining = "<pre>" + remaining

        chunks.append(chunk)

    return chunks


def send_message(chat_id: int, text: str, parse_mode: Optional[str] = "HTML") -> Dict:
    token = settings.get("TELEGRAM_BOT_TOKEN")
    if not token:
        _LOGGER.warning("send_message: token not set")
        return {"ok": False}

    chunks = split_message(text)
    last_response: Dict = {"ok": False}

    for chunk in chunks:
        payload: Dict = {
            "chat_id": chat_id,
            "text": chunk
        }
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode

        url = _api_url("sendMessage", token)
        response, status = http_post(url, payload)

        if status != 200 or not response.get("ok"):
            if parse_mode and status == 400:
                payload_plain = {
                    "chat_id": chat_id,
                    "text": chunk
                }
                response, status = http_post(url, payload_plain)

            if status != 200 or not response.get("ok"):
                error_str = str(response.get("error", ""))
                masked_error = mask_token(error_str)
                _LOGGER.warning(
                    "send_message chunk failed: status=%d error=%s",
                    status, masked_error
                )

        last_response = response

    return last_response


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    token = settings.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return

    payload = {
        "chat_id": chat_id,
        "action": action
    }

    url = _api_url("sendChatAction", token)
    try:
        http_post(url, payload)
    except Exception:
        pass


def delete_message(chat_id: int, message_id: int) -> bool:
    token = settings.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False

    payload = {
        "chat_id": chat_id,
        "message_id": message_id
    }

    url = _api_url("deleteMessage", token)
    try:
        response, status = http_post(url, payload)
        return status == 200 and response.get("ok", False)
    except Exception:
        return False


def get_me(token: Optional[str] = None, timeout: int = 10) -> Dict:
    current_token = token or settings.get("TELEGRAM_BOT_TOKEN")
    if not current_token:
        return {"_status": 0, "_error": "token not set"}

    url = _api_url("getMe", current_token)
    response, status = http_post(url, {}, timeout=timeout)

    if status != 200 or not response.get("ok"):
        return {"_status": status, "_error": mask_token(str(response.get("error", "")))}

    return response


def register_prechecks() -> None:
    def check_bot_token(new_token: str) -> None:
        response = get_me(token=new_token, timeout=10)
        if response.get("_status") or not response.get("ok"):
            raise ValueError("Token validation failed: getMe returned no result or error")

    def check_allowed_user_id(new_id: int) -> None:
        response = get_me()
        if response.get("_status") or not response.get("ok"):
            return

        bot_id = response.get("result", {}).get("id")
        if bot_id == new_id:
            raise ValueError("Cannot set the bot's own ID as the allowed user ID")

    settings.register_precheck("TELEGRAM_BOT_TOKEN", check_bot_token)
    settings.register_precheck("ALLOWED_USER_ID", check_allowed_user_id)
