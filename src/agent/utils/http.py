from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Dict, Optional, Tuple


def http_post_h(
    url: str, payload: dict, headers: dict | None = None, timeout: int = 60
) -> tuple[dict, int, dict]:
    """POST JSON payload to URL and return (response_dict, status_code, response_headers).

    Same contract as http_post, but also returns the response headers as a
    plain dict with lowercased keys. On HTTPError, the headers come from the
    error response; on a transport-level failure (no response at all), an
    empty dict is returned instead.
    """
    try:
        if headers is None:
            headers = {}

        headers["Content-Type"] = "application/json"

        json_payload = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url, data=json_payload, headers=headers, method="POST"
        )

        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            resp_headers = {k.lower(): v for k, v in res.headers.items()}
            return data, res.status, resp_headers

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"error": body}
        resp_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return data, e.code, resp_headers

    except Exception as e:
        return {"error": str(e)}, 0, {}


def http_post(
    url: str, payload: dict, headers: dict | None = None, timeout: int = 60
) -> tuple[dict, int]:
    """POST JSON payload to URL and return (response_dict, status_code).

    On HTTPError: reads body, attempts JSON parse, returns error dict with status code.
    On other exceptions: returns {"error": str(exception)} with status 0 (synthetic failure).

    Thin wrapper over http_post_h that drops the response headers.
    """
    data, status, _resp_headers = http_post_h(url, payload, headers=headers, timeout=timeout)
    return data, status


def http_get(url: str, timeout: int = 35) -> tuple[dict, int]:
    """GET JSON from URL and return (response_dict, status_code).

    On HTTPError: reads body, attempts JSON parse, returns error dict with status code.
    On other exceptions: returns {"error": str(exception)} with status 0 (synthetic failure).
    """
    try:
        req = urllib.request.Request(url, method="GET")

        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            return data, res.status

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"error": body}
        return data, e.code

    except Exception as e:
        return {"error": str(e)}, 0


def http_download(
    url: str, fileobj, max_bytes: int, timeout: int = 120
) -> Tuple[int, int, Optional[str]]:
    """GET url and stream the body into fileobj in 64KB chunks.

    Returns (status, bytes_written, error). error is None on success. The
    error text never includes the URL (the caller is responsible for any
    additional masking of secrets it may itself contain, e.g. a bot token).
    """
    n = 0
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as res:
            status = res.status
            while True:
                chunk = res.read(65536)
                if not chunk:
                    break
                n += len(chunk)
                if n > max_bytes:
                    return status, n, "too large"
                fileobj.write(chunk)
        return status, n, None
    except urllib.error.HTTPError as e:
        return e.code, 0, "HTTP {}".format(e.code)
    except Exception as e:
        return 0, n, str(e)


def _sanitize_disposition_filename(name: str) -> str:
    for ch in ('"', "\\", "\r", "\n"):
        name = name.replace(ch, "_")
    return name


def http_post_multipart(
    url: str,
    fields: Dict[str, str],
    file_field: str,
    file_path: str,
    filename: str,
    content_type: str = "application/octet-stream",
    timeout: int = 120,
) -> Tuple[dict, int]:
    """POST a multipart/form-data request streaming the file body, and
    return (response_dict, status_code). Same response contract as
    http_post: HTTPError body is parsed as JSON (or wrapped as {"error": ..}),
    other exceptions (including a file that shrank mid-upload) return
    {"error": str(exception)} with status 0.
    """
    try:
        boundary = uuid.uuid4().hex

        header_parts = []
        for key, value in fields.items():
            header_parts.append(
                "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(
                    boundary, key, value
                )
            )
        safe_filename = _sanitize_disposition_filename(filename)
        header_parts.append(
            "--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\n"
            "Content-Type: {}\r\n\r\n".format(boundary, file_field, safe_filename, content_type)
        )
        header_bytes = "".join(header_parts).encode("utf-8")
        closing_bytes = "\r\n--{}--\r\n".format(boundary).encode("ascii")

        file_size = os.path.getsize(file_path)
        content_length = len(header_bytes) + file_size + len(closing_bytes)

        def _body():
            yield header_bytes
            remaining = file_size
            with open(file_path, "rb") as f:
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        raise IOError("file shrank while uploading")
                    remaining -= len(chunk)
                    yield chunk
            yield closing_bytes

        headers = {
            "Content-Type": "multipart/form-data; boundary={}".format(boundary),
            "Content-Length": str(content_length),
        }

        req = urllib.request.Request(url, data=_body(), headers=headers, method="POST")

        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            return data, res.status

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"error": body}
        return data, e.code

    except Exception as e:
        return {"error": str(e)}, 0
