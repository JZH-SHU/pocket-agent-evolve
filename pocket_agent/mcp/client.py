from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any


class MCPClientError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class MCPHttpClient:
    """Minimal MCP JSON-RPC client for Streamable HTTP style endpoints."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
        retries: int = 1,
    ):
        self.url = url
        self.timeout = timeout
        self.headers = headers or {}
        self.retries = max(0, retries)
        self._request_id = 0
        self._session_id = ""
        self._initialized = False

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self._session_id = session_id
                raw = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1000]
            raise MCPClientError(
                f"MCP HTTP {exc.code}: {body}", retryable=exc.code >= 500
            ) from exc
        except urllib.error.URLError as exc:
            raise MCPClientError(
                f"无法连接 MCP 服务 {self.url}: {exc.reason}", retryable=True
            ) from exc
        except TimeoutError as exc:
            raise MCPClientError("MCP 请求超时", retryable=True) from exc

        if "text/event-stream" in content_type:
            data_lines = [
                line[5:].strip()
                for line in raw.splitlines()
                if line.startswith("data:")
            ]
            raw = data_lines[-1] if data_lines else ""
        if not raw.strip():
            return {}
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPClientError(
                f"MCP 返回的不是合法 JSON: {raw[:300]}", retryable=False
            ) from exc
        if "error" in result:
            error = result["error"]
            raise MCPClientError(
                f"MCP JSON-RPC 错误: {error}", retryable=False
            )
        return result

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, idempotent: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        last_error: MCPClientError | None = None
        retries = self.retries if idempotent or method in {"initialize", "tools/list", "server/discover"} else 0
        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._post_sync, payload),
                    timeout=self.timeout + 1,
                )
            except MCPClientError as exc:
                last_error = exc
                if not exc.retryable or attempt >= retries:
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
            except asyncio.TimeoutError as exc:
                last_error = MCPClientError("MCP 请求超时", retryable=True)
                if attempt >= retries:
                    raise last_error from exc
        raise last_error or MCPClientError("MCP 请求失败")

    async def notify(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await asyncio.wait_for(
            asyncio.to_thread(self._post_sync, payload),
            timeout=self.timeout + 1,
        )

    async def initialize(self) -> dict[str, Any]:
        response = await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pocket-agent", "version": "0.2.0"},
            },
        )
        try:
            await self.notify("notifications/initialized")
        except MCPClientError:
            # Compatibility with older lightweight MCP HTTP servers.
            pass
        self._initialized = True
        return response.get("result", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        if not self._initialized:
            try:
                await self.initialize()
            except MCPClientError:
                # Some lightweight servers accept tools/list without initialize.
                self._initialized = True
        response = await self.request("tools/list")
        return list(response.get("result", {}).get("tools", []))

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *, idempotent: bool = False
    ) -> dict[str, Any]:
        if not self._initialized:
            try:
                await self.initialize()
            except MCPClientError:
                self._initialized = True
        response = await self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}, idempotent=idempotent
        )
        return response.get("result", {})
