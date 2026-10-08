from __future__ import annotations

import json
import re
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .diagnostics import diagnostic


class RemoteError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False):
        self.code = code if re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", str(code)) else "REMOTE_ERROR"
        self.uncertain = uncertain
        super().__init__(self.code)


def request_json(url: str, payload: dict, timeout: int = 40) -> dict:
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            decoded = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeError):
            decoded = {}
        # Do not log remote descriptions or URL-bearing exceptions: they may contain credentials.
        if isinstance(decoded, dict) and isinstance(decoded.get("error"), str):
            raise RemoteError(decoded["error"]) from None
        raise RemoteError(f"HTTP_{exc.code}", uncertain=True) from None
    except (URLError, socket.timeout, TimeoutError, OSError):
        raise RemoteError("CONNECTION_ERROR", uncertain=True) from None
    except (ValueError, UnicodeError):
        raise RemoteError("INVALID_JSON", uncertain=True) from None
    if not isinstance(result, dict):
        raise RemoteError("INVALID_RESPONSE", uncertain=True)
    return result


def timed_request(service, method, url, payload, timeout):
    started = time.monotonic()
    status = 'ok'
    try:
        return request_json(url, payload, timeout)
    except RemoteError as exc:
        status = exc.code
        raise
    finally:
        elapsed = time.monotonic() - started
        # A successful long poll deliberately waits for incoming messages.
        if status != 'ok' or (elapsed >= 2 and method != 'getUpdates'):
            diagnostic(service + '.' + method, elapsed, status)


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, payload: dict | None = None, *, timeout: int = 40) -> Any:
        result = timed_request('telegram', method, self.base + method, payload or {}, timeout)
        if not result.get("ok"):
            raise RemoteError(f"TELEGRAM_{result.get('error_code', 'ERROR')}")
        return result.get("result")

    def send(self, chat_id: int, text: str, keyboard: list[list[str]] | None = None) -> None:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if keyboard is not None:
            payload["reply_markup"] = {"keyboard": keyboard, "resize_keyboard": True, "is_persistent": True}
        self.call("sendMessage", payload)

    def updates(self, offset: int) -> list[dict]:
        result = self.call("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]}, timeout=40)
        if not isinstance(result, list):
            raise RemoteError("INVALID_UPDATES")
        return result


class Bitrix:
    def __init__(self, webhook: str, *, read_timeout: int = 12):
        self.base = webhook
        self.read_timeout = read_timeout

    def call(self, method: str, payload: dict | None = None) -> dict:
        timeout = self.read_timeout if method.endswith(('.get', '.list', '.fields')) else 40
        result = timed_request('bitrix', method, self.base + method + ".json", payload or {}, timeout)
        if result.get("error"):
            raise RemoteError(str(result["error"]))
        if "result" not in result:
            raise RemoteError("MISSING_RESULT", uncertain=True)
        return result

    def list_all(self, method: str, payload: dict, *, key: str | None = None) -> list[dict]:
        rows, visited, start = [], set(), 0
        while True:
            if start in visited:
                raise RemoteError("INVALID_PAGINATION")
            visited.add(start)
            response = self.call(method, {**payload, "start": start})
            result = response["result"]
            page = result.get(key) if key and isinstance(result, dict) else result
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise RemoteError("INVALID_LIST")
            rows.extend(page)
            if response.get("next") is None:
                return rows
            try:
                start = int(response["next"])
            except (ValueError, TypeError):
                raise RemoteError("INVALID_PAGINATION") from None
