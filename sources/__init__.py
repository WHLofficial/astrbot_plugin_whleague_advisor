"""公开数据源客户端层。

红线（HANDOFF §4，实测口径）：
* 只允许公开只读 GET；club 侧 /api/clubs/<id>、/api/clubs/<id>/standing 匿名 401，
  明确列入拒绝清单，任何客户端构造这类请求直接抛错（在发请求之前拦截）；
* 不携带任何鉴权头 / Cookie / OIDC 凭据；
* 响应做严格形状校验：字段缺失/类型不对 → SourcePayloadError，
  上游契约变化绝不让 KeyError 打断同步（设计 §8.4），由 jobs 捕获后保留旧值并标降级。

传输层可注入：生产用 aiohttp_transport（10s 超时、最多 2 次重试、4xx 不重试、
指数退避+抖动，设计 §8.1）；测试用录制 fixture 的假传输，绝不触网。
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode, urlsplit

import aiohttp

# 设计 §8.1：User-Agent 固定标识 + 10s 超时 + 最多 2 次重试 + 4xx 不重试。
USER_AGENT = "WHL-Advisor/1.0"
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 2
RETRY_BASE_DELAY = 0.5


class SourceError(Exception):
    """数据源请求失败（网络 / 非 200 / 红线路径被拦）。"""

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None):
        super().__init__(message)
        self.url = url
        self.status = status


class SourcePayloadError(SourceError):
    """响应能拿到但形状不符合契约（字段缺失 / 类型不对 / 非法 JSON）。"""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


# 传输签名：(method, url, params) -> HttpResponse
Transport = Callable[[str, str, dict[str, Any] | None], Awaitable[HttpResponse]]


def build_url(base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    """拼接 base_url + path（+ query）。base_url 必须是 http(s) 绝对地址。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise SourceError("base_url 未配置")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SourceError(f"base_url 非法: {base_url!r}")
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


async def aiohttp_transport(
    method: str, url: str, params: dict[str, Any] | None = None
) -> HttpResponse:
    """默认传输：aiohttp GET，带超时与重试（4xx 不重试）。"""
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.request(method, url, params=params) as resp:
                    body = await resp.read()
                    if 400 <= resp.status < 500:
                        return HttpResponse(resp.status, body)  # 4xx 不重试
                    if resp.status >= 500 and attempt < MAX_RETRIES:
                        last_error = SourceError(
                            f"上游 {resp.status}", url=url, status=resp.status
                        )
                        raise _Retryable()
                    return HttpResponse(resp.status, body)
        except _Retryable:
            pass
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = exc
            if attempt >= MAX_RETRIES:
                break
        # 指数退避 + 抖动（设计 §8.1）
        await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 0.25))
    raise SourceError(f"上游请求失败（已重试 {MAX_RETRIES} 次）: {last_error}", url=url)


class _Retryable(Exception):
    """内部标记：本次响应可重试。"""


def parse_json_body(response: HttpResponse, url: str) -> Any:
    if response.status != 200:
        raise SourceError(
            f"上游返回 {response.status}", url=url, status=response.status
        )
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourcePayloadError(f"响应不是合法 UTF-8 JSON: {exc}", url=url) from exc


# ---- 严格校验辅助（设计 §8.4：sources 做严格 schema 校验）----


def require_mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise SourcePayloadError(f"{where} 应为对象，实际 {type(value).__name__}")
    return value


def require_list(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise SourcePayloadError(f"{where} 应为数组，实际 {type(value).__name__}")
    return value


def require_key(mapping: dict, key: str, where: str) -> Any:
    if key not in mapping:
        raise SourcePayloadError(f"{where} 缺少字段 {key}")
    return mapping[key]


def as_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourcePayloadError(f"{where} 应为整数，实际 {value!r}")
    return value


def as_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourcePayloadError(f"{where} 应为非空字符串，实际 {value!r}")
    return value


def optional_int(mapping: dict, key: str, where: str) -> int | None:
    if mapping.get(key) is None:
        return None
    return as_int(mapping[key], f"{where}.{key}")


def optional_str(mapping: dict, key: str, where: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourcePayloadError(f"{where}.{key} 应为字符串或 null，实际 {value!r}")
    return value


def optional_float(mapping: dict, key: str, where: str) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourcePayloadError(f"{where}.{key} 应为数值或 null，实际 {value!r}")
    return float(value)
