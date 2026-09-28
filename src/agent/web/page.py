from __future__ import annotations

import html
import time
from typing import Any, Dict, List, Optional, Tuple

# Small hardcoded map of numeric ranges for input[type=number] min/max hints.
# Mirrors the catalog in settings.py (dev-plan §6.2); kept here deliberately
# so page.py does not need to depend on SettingSpec internals.
_NUMBER_RANGES: Dict[str, Tuple[int, int]] = {
    "ALLOWED_USER_ID": (1, 9007199254740991),
    "GEMINI_TIMEOUT_SEC": (5, 300),
    "GEMINI_FALLBACK_DELAY_SEC": (0, 10),
    "FC_MAX_LOOPS": (1, 50),
    "CONTEXT_TURNS": (0, 50),
    "IDLE_RESET_MINUTES": (0, 1440),
    "SHELL_TIMEOUT_SEC": (1, 600),
    "SHELL_OUTPUT_LIMIT": (200, 20000),
    "POLL_TIMEOUT_SEC": (1, 50),
    "WEB_PORT": (1024, 65535),
}

_SELECT_OPTIONS: Dict[str, Tuple[str, ...]] = {
    "LOG_LEVEL": ("DEBUG", "INFO", "WARNING", "ERROR"),
}

_TEXTAREA_KEYS = ("SYSTEM_PROMPT",)
_BOOL_KEYS = ("WEB_ENABLED",)

_SOURCE_LABELS = {
    "default": "default",
    "env": "env",
    "override": "override",
    "none": "none",
}

_STYLE = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; background: #fafafa; }
  h1 { font-size: 1.4rem; }
  .banner { padding: 0.6rem 0.9rem; border-radius: 6px; margin-bottom: 1rem; font-size: 0.92rem; }
  .banner-setup { background: #fff3cd; border: 1px solid #ffe08a; }
  .banner-saved { background: #d9f2d9; border: 1px solid #a8dba8; }
  .banner-notice { background: #e2edff; border: 1px solid #b7cdfa; }
  .banner-error { background: #fbdada; border: 1px solid #f2a6a6; }
  fieldset { border: 1px solid #ddd; border-radius: 6px; padding: 0.8rem 1rem; margin-bottom: 0.9rem; background: #fff; }
  legend { font-weight: 600; padding: 0 0.4rem; }
  label { display: block; font-size: 0.85rem; margin: 0.3rem 0 0.15rem; }
  input[type=text], input[type=number], input[type=password], textarea, select {
      width: 100%; box-sizing: border-box; padding: 0.35rem 0.5rem; font-size: 0.92rem;
      border: 1px solid #ccc; border-radius: 4px; font-family: inherit;
  }
  textarea { min-height: 5rem; resize: vertical; }
  .meta { font-size: 0.78rem; color: #666; margin-top: 0.2rem; }
  .badge { display: inline-block; padding: 0.05rem 0.4rem; border-radius: 3px; font-size: 0.72rem;
           background: #eee; margin-right: 0.3rem; }
  .field-error { color: #b00020; font-size: 0.82rem; margin-top: 0.2rem; }
  .row-actions { margin-top: 0.35rem; }
  button { font-size: 0.85rem; padding: 0.3rem 0.7rem; border-radius: 4px; border: 1px solid #999;
           background: #f2f2f2; cursor: pointer; }
  .unset-form { display: inline; }
  .save-bar { position: sticky; bottom: 0; background: #fafafa; padding: 0.7rem 0; }
  footer { margin-top: 1.5rem; font-size: 0.78rem; color: #777; border-top: 1px solid #ddd; padding-top: 0.6rem; }
  .checkbox-row label { display: inline; margin-left: 0.3rem; }
</style>
"""


def render(
    rows: List[Dict[str, Any]],
    csrf: str,
    saved: Optional[int] = None,
    errors: Optional[Dict[str, str]] = None,
    submitted: Optional[Dict[str, str]] = None,
    notice: Optional[str] = None,
    status: Optional[Dict[str, Any]] = None,
) -> str:
    errors = errors or {}
    submitted = submitted or {}
    status = status or {}

    parts: List[str] = []
    parts.append('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    parts.append("<title>Shellie Settings</title>")
    parts.append(_STYLE)
    parts.append("</head><body>")
    parts.append("<h1>Shellie Settings</h1>")

    if status.get("setup_required"):
        parts.append(
            '<div class="banner banner-setup">⚙️ Setup required: enter the required values to start automatically</div>'
        )
    if saved is not None:
        parts.append(
            '<div class="banner banner-saved">Saved: {} change(s)</div>'.format(
                html.escape(str(saved), quote=True)
            )
        )
    if notice:
        parts.append(
            '<div class="banner banner-notice">{}</div>'.format(html.escape(notice, quote=True))
        )

    top_level_errors = {k: v for k, v in errors.items() if k.startswith("_")}
    for message in top_level_errors.values():
        parts.append(
            '<div class="banner banner-error">{}</div>'.format(html.escape(str(message), quote=True))
        )
    field_errors = {k: v for k, v in errors.items() if not k.startswith("_")}
    if field_errors:
        parts.append(
            '<div class="banner banner-error">Please check your input: {} field(s) have errors. '
            "Nothing was saved.</div>".format(len(field_errors))
        )

    parts.append('<form method="post" action="/settings">')
    parts.append('<input type="hidden" name="csrf" value="{}">'.format(html.escape(csrf, quote=True)))

    for row in rows:
        parts.append(_render_row(row, field_errors.get(row["key"]), submitted.get(row["key"])))

    parts.append('<div class="save-bar"><button type="submit">Save</button></div>')
    parts.append("</form>")

    parts.append("<footer>")
    parts.append("revision: {} · ".format(html.escape(str(status.get("revision", "-")), quote=True)))
    started_at = status.get("started_at")
    if started_at:
        started_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at))
    else:
        started_text = "-"
    parts.append("Started: {} · ".format(html.escape(started_text, quote=True)))
    polling = status.get("polling")
    if polling is None:
        polling_text = "-"
    else:
        polling_text = "polling" if polling else "stopped"
    parts.append("Polling: {}".format(html.escape(polling_text, quote=True)))
    parts.append("</footer>")

    parts.append("</body></html>")
    return "".join(parts)


def _render_row(
    row: Dict[str, Any],
    error: Optional[str],
    submitted_value: Optional[str],
) -> str:
    key = row["key"]
    source = row["source"]
    field_name = "v." + key
    label = html.escape(key, quote=True)
    field_id = html.escape(field_name, quote=True)
    source_label = _SOURCE_LABELS.get(source, source)

    current_value = row["value"] or ""
    display_value = submitted_value if submitted_value is not None else current_value

    out: List[str] = []
    out.append("<fieldset>")
    out.append("<legend>{}</legend>".format(label))
    out.append('<label for="{0}">{1}</label>'.format(field_id, label))

    if row["secret"]:
        out.append(
            '<input type="password" id="{0}" name="{0}" value="" autocomplete="off">'.format(field_id)
        )
        hint = current_value if current_value else "(not set)"
        out.append('<div class="meta">current: {}</div>'.format(html.escape(hint, quote=True)))
    elif key in _TEXTAREA_KEYS:
        out.append(
            '<textarea id="{0}" name="{0}">{1}</textarea>'.format(
                field_id, html.escape(display_value, quote=True)
            )
        )
    elif key in _BOOL_KEYS:
        current_on = current_value.strip().lower() in ("true", "on", "1", "yes")
        selected_value = submitted_value if submitted_value is not None else ("on" if current_on else "off")
        out.append('<select id="{0}" name="{0}">'.format(field_id))
        for opt_value, opt_label in (("on", "On"), ("off", "Off")):
            selected_attr = " selected" if opt_value == selected_value else ""
            out.append('<option value="{0}"{1}>{2}</option>'.format(opt_value, selected_attr, opt_label))
        out.append("</select>")
    elif key in _SELECT_OPTIONS:
        options = _SELECT_OPTIONS[key]
        selected_value = (submitted_value if submitted_value is not None else current_value).strip().upper()
        out.append('<select id="{0}" name="{0}">'.format(field_id))
        for opt in options:
            selected_attr = " selected" if opt == selected_value else ""
            out.append('<option value="{0}"{1}>{0}</option>'.format(opt, selected_attr))
        out.append("</select>")
    elif key in _NUMBER_RANGES:
        lo, hi = _NUMBER_RANGES[key]
        out.append(
            '<input type="number" id="{0}" name="{0}" value="{1}" min="{2}" max="{3}">'.format(
                field_id, html.escape(display_value, quote=True), lo, hi
            )
        )
    else:
        out.append(
            '<input type="text" id="{0}" name="{0}" value="{1}">'.format(
                field_id, html.escape(display_value, quote=True)
            )
        )

    if key == "ALLOWED_USER_ID":
        confirm_id = html.escape("confirm.ALLOWED_USER_ID", quote=True)
        out.append('<div class="checkbox-row">')
        out.append('<input type="checkbox" id="{0}" name="{0}" value="on">'.format(confirm_id))
        out.append(
            '<label for="{0}">I confirm this change (both old and new accounts will be notified)</label>'.format(confirm_id)
        )
        out.append("</div>")

    if key == "WEB_ENABLED":
        out.append(
            '<div class="meta">If disabled, you can only re-enable it via Telegram /set WEB_ENABLED on</div>'
        )

    badges = '<span class="badge">{}</span>'.format(html.escape(source_label, quote=True))
    if not row["telegram_editable"]:
        badges += '<span class="badge">🔒web-only</span>'
    out.append(
        '<div class="meta">{} apply: {}</div>'.format(badges, html.escape(row["apply_timing"], quote=True))
    )
    if row["constraint"]:
        out.append('<div class="meta">{}</div>'.format(html.escape(row["constraint"], quote=True)))
    if row.get("description"):
        out.append('<div class="meta">{}</div>'.format(html.escape(row["description"], quote=True)))
    if error:
        out.append('<div class="field-error">{}</div>'.format(html.escape(str(error), quote=True)))

    if source == "override":
        # A nested <form> would be invalid HTML (this row lives inside the
        # single outer /settings form); formaction/formmethod repoint just
        # this submit button to POST /unset without any JS.
        out.append('<div class="row-actions">')
        out.append(
            '<button type="submit" formaction="/unset" formmethod="post" '
            'name="key" value="{}">Reset to default</button>'.format(html.escape(key, quote=True))
        )
        out.append("</div>")

    out.append("</fieldset>")
    return "".join(out)
