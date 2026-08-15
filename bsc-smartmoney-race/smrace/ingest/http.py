"""共用 HTTP 客户端：重试、指数退避、限流、429/5xx 处理。

Bitquery 和 Dune 都有明确的速率上限（Bitquery Scale 240 req/min；
Dune Free 15–40 rpm、Plus 70–200 rpm），拉几个月的历史必然会撞上。
把限流做在这一层，上面的适配器就不用各写一遍。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RateLimitError(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, msg: str, status: int = 0, body: str = "") -> None:
        super().__init__(msg)
        self.status, self.body = status, body


@dataclass
class RateLimiter:
    """令牌桶。rpm=0 表示不限流。"""
    rpm: int = 0
    _stamps: list[float] = field(default_factory=list)

    def acquire(self) -> None:
        if self.rpm <= 0:
            return
        now = time.monotonic()
        self._stamps = [t for t in self._stamps if now - t < 60.0]
        if len(self._stamps) >= self.rpm:
            sleep = 60.0 - (now - self._stamps[0]) + 0.05
            if sleep > 0:
                time.sleep(sleep)
            now = time.monotonic()
            self._stamps = [t for t in self._stamps if now - t < 60.0]
        self._stamps.append(now)


@dataclass
class HttpClient:
    base_headers: dict[str, str] = field(default_factory=dict)
    rpm: int = 0
    max_retries: int = 5
    timeout: float = 60.0
    backoff_base: float = 1.5
    backoff_cap: float = 60.0
    on_retry: Callable[[int, str], None] | None = None

    def __post_init__(self) -> None:
        self._limiter = RateLimiter(self.rpm)
        self._session = requests.Session()

    def post_json(self, url: str, payload: dict[str, Any],
                  headers: dict[str, str] | None = None) -> dict[str, Any]:
        return self._request("POST", url, json_body=payload, headers=headers)

    def get_json(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> dict[str, Any]:
        return self._request("GET", url, params=params, headers=headers)

    def _request(self, method: str, url: str, *, json_body: dict | None = None,
                 params: dict | None = None, headers: dict | None = None) -> dict[str, Any]:
        h = {**self.base_headers, **(headers or {})}
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._limiter.acquire()
            try:
                r = self._session.request(method, url, json=json_body, params=params,
                                          headers=h, timeout=self.timeout)
            except requests.RequestException as e:
                last = e
                self._sleep(attempt, f"网络异常 {e}")
                continue

            if r.status_code in RETRYABLE_STATUS:
                # 优先尊重服务端的 Retry-After
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        time.sleep(min(float(ra), self.backoff_cap))
                        continue
                    except ValueError:
                        pass
                last = ApiError(f"HTTP {r.status_code}", r.status_code, r.text[:500])
                self._sleep(attempt, f"HTTP {r.status_code}")
                continue

            if r.status_code >= 400:
                raise ApiError(f"HTTP {r.status_code}: {r.text[:500]}",
                               r.status_code, r.text[:500])
            try:
                return r.json()
            except json.JSONDecodeError as e:
                raise ApiError(f"响应不是合法 JSON: {r.text[:300]}") from e

        raise ApiError(f"重试 {self.max_retries} 次后仍失败: {last}")

    def _sleep(self, attempt: int, why: str) -> None:
        delay = min(self.backoff_cap, self.backoff_base ** attempt) * (0.5 + random.random())
        if self.on_retry:
            self.on_retry(attempt, f"{why}，{delay:.1f}s 后重试")
        time.sleep(delay)
