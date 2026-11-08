"""Official MCP 2 transport, normalized to the existing manager boundary."""
from __future__ import annotations

import asyncio
from typing import Any

from .client import MCPClientError


class SDKMCPClient:
    def __init__(self, url: str, *, timeout: float = 30, headers: dict | None = None):
        self.url, self.timeout, self.headers = url, timeout, headers or {}

    def _client(self):
        from contextlib import asynccontextmanager
        import httpx2
        from mcp.client import Client
        from mcp.client.streamable_http import streamable_http_client

        @asynccontextmanager
        async def transport():
            async with httpx2.AsyncClient(headers=self.headers, timeout=self.timeout) as http:
                async with streamable_http_client(self.url, http_client=http) as streams:
                    yield streams

        return Client(transport(),
                      read_timeout_seconds=self.timeout, mode="auto", cache=None)

    async def list_tools(self) -> list[dict[str, Any]]:
        try:
            async with self._client() as client:
                tools, cursor, seen = [], None, set()
                for _ in range(100):
                    page = await asyncio.wait_for(client.list_tools(cursor=cursor), self.timeout)
                    tools.extend(tool.model_dump(by_alias=True, exclude_none=True) for tool in page.tools)
                    cursor = page.next_cursor
                    if not cursor:
                        return tools
                    if cursor in seen:
                        raise ValueError("MCP pagination cursor repeated")
                    seen.add(cursor)
                raise ValueError("MCP tool pagination limit exceeded")
        except Exception as exc:
            raise MCPClientError(f"SDK discovery failed: {type(exc).__name__}", retryable=True) from exc

    async def call_tool(self, name: str, arguments: dict | None = None, *, idempotent: bool = False) -> dict:
        # No application-level retries. The SDK may re-list a rejected stale schema;
        # a lost response must still be treated as uncertain by the caller's ledger.
        try:
            async with self._client() as client:
                result = await asyncio.wait_for(client.call_tool(name, arguments or {}), self.timeout)
                return result.model_dump(by_alias=True, exclude_none=True)
        except Exception as exc:
            raise MCPClientError(f"SDK tool call failed: {type(exc).__name__}", retryable=idempotent) from exc
