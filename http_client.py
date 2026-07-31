"""HTTP 客户端模块 — 处理所有 RVC Studio B 端的 HTTP 通信

特性：
- 标准 aiohttp 异步 HTTP（符合 NEKO asyncio 事件循环）
- 鉴权头自动注入（P2 api_key）
- SSL 证书验证控制（自签证书支持）
- 重试机制
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Any

import aiohttp


class AsyncHttpClient:
    """异步 HTTP 客户端（基于 aiohttp）"""

    _RETRY_COUNT: int = 3  # 重试次数

    def __init__(self, base_url: str, api_key: str = "",
                 ssl_verify: bool = True, timeout: float = 5):
        self.base_url = base_url
        self.api_key = api_key
        self.ssl_verify = ssl_verify
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP session（连接池复用，带并发保护）"""
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    connector = aiohttp.TCPConnector(
                        limit=20,
                        limit_per_host=10,
                        ttl_dns_cache=600,
                        force_close=False,
                        enable_cleanup_closed=True,
                    )
                    if self.base_url.startswith("https://") and not self.ssl_verify:
                        ssl_context = ssl.create_default_context()
                        ssl_context.check_hostname = False
                        ssl_context.verify_mode = ssl.CERT_NONE
                        connector = aiohttp.TCPConnector(
                            limit=20,
                            limit_per_host=10,
                            ttl_dns_cache=600,
                            force_close=False,
                            enable_cleanup_closed=True,
                            ssl=ssl_context,
                        )
                    self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        """关闭 session（持有锁，防止与 _get_session 竞态）"""
        async with self._session_lock:
            if self._session:
                await self._session.close()
                self._session = None

    def _get_headers(self) -> dict:
        """构建请求头"""
        headers = {"User-Agent": "NEKO-RVC-Singer/2.1"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _request(self, method: str, endpoint: str,
                       json_data: dict | None = None,
                       timeout: float | None = None) -> tuple[int, Any]:
        """通用 HTTP 请求（GET/POST）

        返回: (status_code, 数据) — 成功(200, dict|str), 失败(status, {"error": ...}), 异常(-1, {"error": ...})
        """
        url = f"{self.base_url}{endpoint}"
        headers = self._get_headers()

        # SSL 配置：仅在关闭验证时构造自定义 context
        ssl_param = None
        if url.startswith("https://") and not self.ssl_verify:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            ssl_param = ssl_ctx

        _timeout = timeout if timeout is not None else self.timeout
        timeout_cfg = aiohttp.ClientTimeout(total=_timeout)
        session = await self._get_session()

        for attempt in range(self._RETRY_COUNT):
            try:
                async with session.request(
                    method, url,
                    headers=headers,
                    json=json_data if method == "POST" else None,
                    timeout=timeout_cfg,
                    ssl=ssl_param,
                ) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = await resp.text()
                    return (resp.status, data)
            except asyncio.TimeoutError:
                if attempt < self._RETRY_COUNT - 1:
                    await asyncio.sleep(1.0)
                    continue
                return (-1, {"error": "timeout"})
            except Exception as e:
                if attempt < self._RETRY_COUNT - 1:
                    await asyncio.sleep(1.0)
                    continue
                return (-1, {"error": str(e)})

        return (-1, {"error": "max retries exceeded"})

    async def get(self, endpoint: str, timeout: float | None = None) -> tuple[int, Any]:
        return await self._request("GET", endpoint, timeout=timeout)

    async def post(self, endpoint: str, json_data: dict | None = None,
                   timeout: float | None = None) -> tuple[int, Any]:
        return await self._request("POST", endpoint, json_data, timeout=timeout)
