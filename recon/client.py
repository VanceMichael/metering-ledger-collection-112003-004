"""联调来源服务的 HTTP 客户端。

只依赖标准库；把 4xx/5xx 统一抛成 :class:`SourceError`，由上层按状态码决定
重试、归档降级或会计回条查询。
"""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SourceError(RuntimeError):
    """来源服务返回了非 2xx 响应。"""

    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        super().__init__(f"source status {status}: {payload!r}")


class SourceClient:
    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _open(self, path: str, body=None, retry: int = 1):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, dict(response.headers), raw
        except HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                payload = raw
            raise SourceError(error.code, payload) from None
        except URLError:
            # 只对瞬时连接问题做一次保守重试，请求本身尚未被对端处理。
            if retry <= 0:
                raise
            return self._open(path, body, retry=retry - 1)

    def get_json(self, path: str) -> dict:
        _, _, raw = self._open(path)
        return json.loads(raw.decode("utf-8"))

    def get_bytes(self, path: str) -> tuple[dict, bytes]:
        _, headers, raw = self._open(path)
        return headers, raw

    def post_json(self, path: str, body: dict) -> dict:
        _, _, raw = self._open(path, body)
        return json.loads(raw.decode("utf-8"))
