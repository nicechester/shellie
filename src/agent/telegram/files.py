from __future__ import annotations

import base64
import mimetypes
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.agent.config import BASE_DIR, SETTINGS_FILE, SHELLIE_WORKSPACE
from src.agent.telegram.client import download_file, get_file, send_chat_action, send_document

# Telegram Bot API hard limit for bot downloads (getFile does not return a
# file_path for larger files at all).
TG_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
# Telegram Bot API hard limit for sendDocument uploads.
TG_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
CAPTION_MAX = 1024
_MAX_NAME_BYTES = 200
LS_MAX_ENTRIES = 100

# Multimodal inline attachments (issue #6): uploaded media/documents become
# part of the next Gemini turn instead of just a saved-path note.
# INLINE_MEDIA_MAX_BYTES is both the per-file media cap AND the total
# media+text budget for one turn's attachments.
INLINE_MEDIA_MAX_BYTES = 5 * 1024 * 1024
MAX_INLINE_ATTACHMENTS = 5
INLINE_TEXT_MAX_BYTES = 100 * 1024

SUPPORTED_MEDIA_MIMES = {
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
    "application/pdf",
    "audio/ogg", "audio/mpeg", "audio/mp3", "audio/wav", "audio/aac", "audio/flac", "audio/aiff",
    "video/mp4", "video/mpeg", "video/quicktime", "video/webm", "video/3gpp", "video/x-flv",
    "video/mpg", "video/wmv",
}

# Normalize a few common mime spellings Telegram/clients send that do not
# exactly match the canonical names above.
_MIME_ALIASES = {
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/opus": "audio/ogg",
    "image/jpg": "image/jpeg",
}

# Extensions the stdlib mimetypes module often misses or gets wrong.
_EXT_MIME = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".webp": "image/webp",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "video/webm",
}

# Telegram message field -> default mime when the client sends none.
_KIND_MIME = {
    "photo": "image/jpeg",
    "voice": "audio/ogg",
    "video": "video/mp4",
    "audio": "audio/mpeg",
}

# Mime types (besides "text/*") treated as inlineable text.
_TEXT_MIME_EXACT = {
    "application/json", "application/xml", "application/x-yaml", "application/yaml",
    "application/javascript", "application/x-sh", "application/toml", "application/sql",
}

# Extensions treated as inlineable text regardless of the reported mime type.
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".zsh", ".bash",
    ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php",
    ".sql", ".html", ".css", ".xml", ".swift", ".gradle", ".properties",
}


def _default_name(kind: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if kind == "photo":
        return "photo_{}.jpg".format(ts)
    if kind == "voice":
        return "voice_{}.ogg".format(ts)
    if kind == "video":
        return "video_{}.mp4".format(ts)
    if kind == "audio":
        return "audio_{}.mp3".format(ts)
    return "document_{}".format(ts)


def _build_attachment(kind: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    file_size = obj.get("file_size")
    if not isinstance(file_size, int):
        file_size = None
    file_name = obj.get("file_name")
    if not isinstance(file_name, str) or not file_name:
        file_name = _default_name(kind)
    return {
        "kind": kind,
        "file_id": obj["file_id"],
        "file_unique_id": obj.get("file_unique_id"),
        "file_size": file_size,
        "file_name": file_name,
        "mime_type": obj.get("mime_type"),
    }


def extract_attachment(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return None

    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        return _build_attachment("document", document)

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        valid = [p for p in photos if isinstance(p, dict) and p.get("file_id")]
        if valid:
            sized = [p for p in valid if isinstance(p.get("file_size"), int)]
            chosen = max(sized, key=lambda p: p["file_size"]) if sized else valid[-1]
            return _build_attachment("photo", chosen)

    video = message.get("video")
    if isinstance(video, dict) and video.get("file_id"):
        return _build_attachment("video", video)

    audio = message.get("audio")
    if isinstance(audio, dict) and audio.get("file_id"):
        return _build_attachment("audio", audio)

    voice = message.get("voice")
    if isinstance(voice, dict) and voice.get("file_id"):
        return _build_attachment("voice", voice)

    return None


def _strip_control_chars(name: str) -> str:
    return "".join(ch for ch in name if ord(ch) >= 0x20 and ord(ch) != 0x7F)


def _truncate_utf8_keep_ext(name: str, max_bytes: int) -> str:
    root, ext = os.path.splitext(name)
    ext_bytes = ext.encode("utf-8")
    if len(ext_bytes) >= max_bytes:
        # Pathological extension: fall back to truncating the whole name.
        root, ext, ext_bytes = name, "", b""
    budget = max_bytes - len(ext_bytes)
    while len(root.encode("utf-8")) > budget and root:
        root = root[:-1]
    return root + ext


def sanitize_filename(name: str, fallback: str) -> str:
    if not isinstance(name, str):
        name = ""
    name = name.replace("\\", "/")
    name = os.path.basename(name)
    name = _strip_control_chars(name)
    name = name.strip()
    name = name.lstrip(".")
    name = name.strip()
    if not name:
        return fallback
    name = _truncate_utf8_keep_ext(name, _MAX_NAME_BYTES)
    if not name:
        return fallback
    return name


def claim_unique_path(directory: str, filename: str) -> str:
    root, ext = os.path.splitext(filename)
    for i in range(1000):
        candidate = filename if i == 0 else "{} ({}){}".format(root, i, ext)
        path = os.path.join(directory, candidate)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return path
    raise OSError("Could not claim a unique filename for {}".format(filename))


def save_incoming(att: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    try:
        file_size = att.get("file_size")
        if isinstance(file_size, int) and file_size > TG_DOWNLOAD_MAX_BYTES:
            return None, "too large (Telegram bots can download at most 20 MB)"

        result = get_file(att["file_id"])
        if result.get("_error"):
            return None, result["_error"]

        file_info = result.get("result")
        if not isinstance(file_info, dict) or not file_info.get("file_path"):
            return None, "getFile returned no file path"

        remote_size = file_info.get("file_size")
        if isinstance(remote_size, int) and remote_size > TG_DOWNLOAD_MAX_BYTES:
            return None, "too large (Telegram bots can download at most 20 MB)"

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=SHELLIE_WORKSPACE, prefix=".shellie-upload-", suffix=".part"
        )
    except Exception as exc:
        return None, str(exc)

    try:
        with os.fdopen(tmp_fd, "wb") as f:
            error = download_file(file_info["file_path"], f, TG_DOWNLOAD_MAX_BYTES)
        if error:
            return None, error

        fallback = _default_name(att.get("kind") or "document")
        safe_name = sanitize_filename(att.get("file_name") or "", fallback)
        final_path = claim_unique_path(SHELLIE_WORKSPACE, safe_name)
        os.replace(tmp_path, final_path)
        os.chmod(final_path, 0o600)
        return final_path, None
    except Exception as exc:
        return None, str(exc)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def format_size(n: int) -> str:
    if n < 1024:
        return "{} B".format(n)
    kb = n / 1024.0
    if kb < 1024:
        return "{:.1f} KB".format(kb)
    mb = kb / 1024.0
    return "{:.1f} MB".format(mb)


def _normalize_path(raw: str) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(raw, str):
        return None, "Empty path"
    s = raw.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        s = s[1:-1].strip()
    if not s:
        return None, "Empty path"

    expanded = os.path.expanduser(s)
    joined = os.path.join(SHELLIE_WORKSPACE, expanded)
    path = os.path.realpath(joined)
    return path, None


def _blocked_paths() -> set:
    return {
        os.path.realpath(os.path.join(BASE_DIR, ".env")),
        os.path.realpath(SETTINGS_FILE),
    }


def resolve_send_path(raw: str) -> Tuple[Optional[str], Optional[str]]:
    path, err = _normalize_path(raw)
    if err:
        return None, err

    # Reconstruct s for error messages (must match _normalize_path logic)
    s = raw.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        s = s[1:-1].strip()

    if not os.path.exists(path):
        return None, "File not found: {}".format(s)
    if os.path.isdir(path):
        return None, "{} is a directory — zip it first".format(s)
    if not os.path.isfile(path):
        return None, "Not a regular file: {}".format(s)

    if path in _blocked_paths():
        return None, "That file cannot be sent"

    size = os.path.getsize(path)
    if size == 0:
        return None, "File is empty"
    if size > TG_UPLOAD_MAX_BYTES:
        return None, "File is too large (max {})".format(format_size(TG_UPLOAD_MAX_BYTES))

    return path, None


def resolve_dir(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if raw is None or not raw.strip():
        return os.path.realpath(SHELLIE_WORKSPACE), None

    path, error = _normalize_path(raw)
    if error:
        return None, error

    stripped_raw = raw.strip()
    if not os.path.exists(path):
        return None, "Not found: {}".format(stripped_raw)
    if not os.path.isdir(path):
        return None, "Not a directory: {} — use /file".format(stripped_raw)

    return path, None


def list_dir(path: str, limit: int = LS_MAX_ENTRIES) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
    try:
        with os.scandir(path) as entries_iter:
            entries = []
            for entry in entries_iter:
                if entry.name.startswith('.'):
                    continue

                is_dir = False
                try:
                    is_dir = entry.is_dir(follow_symlinks=True)
                except OSError:
                    pass

                size = None
                if not is_dir:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        pass

                entries.append({
                    "name": entry.name,
                    "path": os.path.join(path, entry.name),
                    "is_dir": is_dir,
                    "size": size,
                })
    except OSError as exc:
        return [], 0, str(exc)

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))

    total_visible = len(entries)
    limited_entries = entries[:limit]

    return limited_entries, total_visible, None


def send_file(chat_id: int, raw_path: str, caption: Optional[str] = None) -> Tuple[bool, str]:
    path, err = resolve_send_path(raw_path)
    if err:
        return False, err

    send_chat_action(chat_id, "upload_document")

    filename = sanitize_filename(os.path.basename(path), "file")
    cap = caption[:CAPTION_MAX] if caption else None
    response = send_document(chat_id, path, filename, cap)

    if response.get("_error"):
        return False, "Error: {}".format(response["_error"])

    size = os.path.getsize(path)
    return True, "Sent {} ({})".format(filename, format_size(size))


def upload_note(path: str, att: Dict[str, Any], status: str = "") -> str:
    try:
        size = os.path.getsize(path)
    except OSError:
        size = att.get("file_size")
    size_str = format_size(size) if isinstance(size, int) else "unknown size"
    mime_or_kind = att.get("mime_type") or att.get("kind")
    note = (
        "[system] The user uploaded a file via Telegram. It was saved at: {} "
        "(original name: {}, type: {}, size: {})."
    ).format(path, att.get("file_name"), mime_or_kind, size_str)
    if status:
        note += " " + status
    return note


# ---------------------------------------------------------------------------
# Multimodal inline attachments (issue #6).
# ---------------------------------------------------------------------------


def _looks_like_text_mime(mime: str) -> bool:
    return mime.startswith("text/") or mime in _TEXT_MIME_EXACT


def _has_text_extension(file_name: str) -> bool:
    return os.path.splitext(file_name)[1].lower() in _TEXT_EXTENSIONS


def _no_nul_bytes(path: str, max_bytes: int = 8192) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(max_bytes)
    except OSError:
        return False
    return b"\x00" not in chunk


def resolve_mime(att: Dict[str, Any]) -> Optional[str]:
    """Best-effort mime type for an attachment: Telegram's reported
    mime_type first, then kind/extension-based fallbacks when missing or
    generic, then alias normalization. Never raises."""
    raw = att.get("mime_type")
    mime: Optional[str] = None
    if isinstance(raw, str) and raw.strip():
        mime = raw.split(";", 1)[0].strip().lower()

    if not mime or mime == "application/octet-stream":
        candidate: Optional[str] = None
        kind = att.get("kind")
        if isinstance(kind, str):
            candidate = _KIND_MIME.get(kind)
        file_name = att.get("file_name")
        if not candidate and isinstance(file_name, str) and file_name:
            ext = os.path.splitext(file_name)[1].lower()
            candidate = _EXT_MIME.get(ext)
        if not candidate and isinstance(file_name, str) and file_name:
            candidate = mimetypes.guess_type(file_name)[0]
        if candidate:
            mime = candidate

    if not mime:
        return None
    return _MIME_ALIASES.get(mime, mime)


def classify_upload(att: Dict[str, Any], path: str, size: int) -> Tuple[str, Optional[str], str]:
    """Classify an already-saved upload for inline attachment to the next
    Gemini turn.

    Returns (mode, mime, reason): mode is "media", "text" or "none"; reason
    is "" when attachable, "type" for an unsupported/undetected type, or
    "size" for exceeding the per-mode cap. Never raises.
    """
    try:
        mime = resolve_mime(att)

        if mime in SUPPORTED_MEDIA_MIMES:
            if 0 < size <= INLINE_MEDIA_MAX_BYTES:
                return "media", mime, ""
            return "none", mime, "size"

        is_text = bool(mime) and _looks_like_text_mime(mime)
        if not is_text:
            file_name = att.get("file_name")
            is_text = isinstance(file_name, str) and _has_text_extension(file_name)

        if is_text:
            if size <= 0 or size > INLINE_TEXT_MAX_BYTES:
                return "none", mime, "size"
            if not _no_nul_bytes(path):
                return "none", mime, "type"
            return "text", mime, ""

        return "none", mime, "type"
    except Exception:
        return "none", None, "type"


def resolve_view_target(raw: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve a path for the view_file LLM tool: same classification
    (media/text/none) as an upload, and a ref shaped for
    build_attachment_part. Never raises.
    """
    try:
        path, err = _normalize_path(raw)
        if err:
            return None, err

        s = raw.strip()
        if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
            s = s[1:-1].strip()

        if not os.path.exists(path):
            return None, "File not found: {}".format(s)
        if os.path.isdir(path):
            return None, "{} is a directory; list it with execute_shell instead".format(s)
        if not os.path.isfile(path):
            return None, "Not a regular file: {}".format(s)

        if path in _blocked_paths():
            return None, "That file cannot be viewed"

        size = os.path.getsize(path)
        if size == 0:
            return None, "File is empty"

        name = os.path.basename(path)
        att = {"file_name": name, "mime_type": None, "kind": "document"}
        mode, mime, reason = classify_upload(att, path, size)

        if mode == "none" and reason == "size":
            return None, (
                "{} is too large to view ({}; max 5 MB for media, 100 KB for text). "
                "Use execute_shell to inspect parts of it."
            ).format(name, format_size(size))
        if mode == "none" and reason == "type":
            return None, "{} ({}) cannot be viewed directly; use execute_shell to inspect it.".format(
                name, mime or "unknown type"
            )

        return {"path": path, "name": name, "mime": mime, "mode": mode, "size": size}, None
    except Exception as exc:
        return None, str(exc)


def build_attachment_part(ref: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Build one Gemini `contents` part for an already-classified attachment.

    Runs on the queue worker thread. ref = {"path", "name", "mime", "mode",
    "size"}. Never raises; never logs file contents. Returns (part,
    history_placeholder) — the placeholder replaces this part once the turn
    is saved to history, so raw media/text never lingers in memory/history.
    """
    path = ref.get("path")
    name = ref.get("name") or os.path.basename(str(path))
    mime = ref.get("mime")
    mode = ref.get("mode")
    placeholder = (
        "[attachment removed from history: {} ({}) — saved at {} "
        "(use view_file to look at it again)]"
    ).format(name, mime, path)
    unavailable = {"text": "[attachment unavailable: {} could not be read or is now too large]".format(name)}

    cap = INLINE_MEDIA_MAX_BYTES if mode == "media" else INLINE_TEXT_MAX_BYTES
    try:
        current_size = os.path.getsize(path)
    except OSError:
        return unavailable, placeholder

    if current_size <= 0 or current_size > cap:
        return unavailable, placeholder

    try:
        if mode == "media":
            with open(path, "rb") as f:
                raw = f.read()
            b64 = base64.b64encode(raw).decode("ascii")
            del raw
            return {"inlineData": {"mimeType": mime, "data": b64}}, placeholder

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        text = "[content of {}]\n<<<\n{}\n>>>".format(name, content)
        return {"text": text}, placeholder
    except OSError:
        return unavailable, placeholder
