"""平台只读绑定端点客户端（设计 §4.7 契约，逐字实现）。

POST /api/internal/bot/bindings
  X-Timestamp: <秒>
  X-Sign: hex(HMAC-SHA256(secret, "POST|/api/internal/bot/bindings|<ts>|<raw>"))
  {"groupId":"<群号>","qqs":["<qq1>","<qq2>"]}

200 {"ok":true,"bindings":[
  {"qq":"<qq1>","bound":true,"clubId":11,"clubName":"曼联","teamName":"曼联","isCpu":false},
  {"qq":"<qq2>","bound":false}]}

平台端点尚未上线（HANDOFF §2/§7 阶段 1）：客户端按上述契约实现并带 enabled 开关；
未启用（secret/地址未配）抛 BindingsUnavailable，工具层转 NOT_BOUND，绝不抛异常到模型。
响应按 Q12-B 严格校验：形状不对一律 BindingsError（工具层降级为群名片 + caveat）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

PATH = "/api/internal/bot/bindings"
MAX_QQS = 200  # §4.7：qqs 上限 200，去重
BINDING_KEYS = ("qq", "bound")


class BindingsUnavailable(Exception):
    """绑定服务未启用（secret 或地址未配置）。工具层 → NOT_BOUND。"""


class BindingsError(Exception):
    """绑定服务已启用但请求失败（网络/验签/限流/形状漂移）。工具层 → 降级群名片 + caveat。"""


def sign_request(secret: str, ts: str, raw: str) -> str:
    """X-Sign = hex(HMAC-SHA256(secret, "POST|<path>|<ts>|<raw>"))，§4.7 原文。"""
    message = f"POST|{PATH}|{ts}|{raw}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def normalize_qqs(qqs) -> list[str]:
    """数字 QQ 去重保序；非数字/超 200 个 → ValueError（§4.7 只接受数字 QQ）。"""
    out: list[str] = []
    seen: set[str] = set()
    for qq in qqs or []:
        text = str(qq).strip()
        if not text.isdigit():
            raise ValueError(f"非数字 QQ: {text!r}")
        if text not in seen:
            seen.add(text)
            out.append(text)
    if len(out) > MAX_QQS:
        raise ValueError(f"qqs 超上限 {MAX_QQS}")
    return out


def parse_response(payload: dict | str) -> dict[str, dict]:
    """严格校验 200 响应 → {qq: binding}；Q12-B 插件侧严格校验。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise BindingsError(f"绑定响应不是 JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise BindingsError("绑定响应 ok != true")
    bindings = payload.get("bindings")
    if not isinstance(bindings, list):
        raise BindingsError("绑定响应缺 bindings 列表")
    out: dict[str, dict] = {}
    for item in bindings:
        if not isinstance(item, dict) or any(k not in item for k in BINDING_KEYS):
            raise BindingsError("绑定条目缺 qq/bound 字段")
        qq = str(item["qq"])
        if not isinstance(item["bound"], bool):
            raise BindingsError(f"绑定条目 bound 不是布尔: {qq}")
        if item["bound"]:
            # bound=true 必须给 clubId/clubName 或至少 teamName（§4.7 clubId 可空）
            if item.get("clubId") is None and not (item.get("teamName") or item.get("clubName")):
                raise BindingsError(f"绑定条目 bound=true 但无俱乐部信息: {qq}")
        out[qq] = item
    return out


class BindingsClient:
    """绑定查询客户端；transport 可注入（测试离线回放）。"""

    def __init__(
        self,
        base_url: str = "",
        secret: str = "",
        *,
        enabled: bool | None = None,
        timeout: float = 5.0,
        transport: Any = None,
        now_fn: Any = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.secret = secret or ""
        self.enabled = (bool(self.base_url) and bool(self.secret)) if enabled is None else enabled
        self.timeout = timeout
        self._transport = transport
        self._now = now_fn or (lambda: int(time.time()))

    async def fetch(self, group_id: str, qqs) -> dict[str, dict]:
        """查询绑定；未启用抛 BindingsUnavailable，请求失败抛 BindingsError。"""
        if not self.enabled:
            raise BindingsUnavailable("绑定服务未启用（未配置 bot_identity_secret / 端点地址）")
        ids = normalize_qqs(qqs)
        if not ids:
            return {}
        body = json.dumps({"groupId": str(group_id), "qqs": ids}, ensure_ascii=False, separators=(",", ":"))
        ts = str(self._now())
        headers = {
            "X-Timestamp": ts,
            "X-Sign": sign_request(self.secret, ts, body),
            "Content-Type": "application/json",
        }
        status, text = await self._post(body, headers)
        if status == 401:
            raise BindingsError("绑定端点返回 401（匿名/签名被拒）")
        if status == 403:
            raise BindingsError("绑定端点返回 403（HMAC 错签或重放）")
        if status == 400:
            raise BindingsError("绑定端点返回 400（请求形状错误）")
        if status == 503:
            raise BindingsError("绑定端点返回 503（平台未配 BOT_IDENTITY_SECRET）")
        if status != 200:
            raise BindingsError(f"绑定端点返回 {status}")
        return parse_response(text)

    async def _post(self, body: str, headers: dict) -> tuple[int, str]:
        if self._transport is not None:
            try:
                return await self._transport("POST", self.base_url + PATH, headers=headers, body=body)
            except BindingsError:
                raise
            except Exception as exc:  # noqa: BLE001 - 注入传输的异常同样归一
                raise BindingsError(f"绑定请求失败: {exc}") from exc
        return await self._post_aiohttp(body, headers)

    async def _post_aiohttp(self, body: str, headers: dict) -> tuple[int, str]:
        import aiohttp  # 惰性导入：空转安装零额外依赖导入

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": "WHL-Advisor/1.0"},
            ) as session:
                async with session.post(
                    self.base_url + PATH, data=body.encode("utf-8"), headers=headers
                ) as resp:
                    return resp.status, await resp.text()
        except Exception as exc:  # noqa: BLE001 - 网络/超时/风控一律归一
            raise BindingsError(f"绑定请求失败: {exc}") from exc
