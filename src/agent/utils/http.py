from __future__ import annotations

import json
import urllib.error
import urllib.request


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
