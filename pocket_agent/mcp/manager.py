from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pocket_agent.runtime.context import get_run_id
from pocket_agent.runtime.store import RunStore
from pocket_agent.tools.artifacts import ArtifactStore
from pocket_agent.tools.policy import PolicyEngine
from pocket_agent.tools.specs import ToolResult, ToolSpec

from .client import MCPClientError, MCPHttpClient


@dataclass
class MCPServer:
    name: str
    url: str
    timeout: float = 30.0
    headers: dict[str, str] = field(default_factory=dict)
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    healthy: bool = True
    failures: int = 0
    circuit_open_until: float = 0.0


class MCPManager:
    def __init__(
        self,
        policy: PolicyEngine,
        run_store: RunStore,
        artifacts: ArtifactStore,
    ):
        self.policy = policy
        self.run_store = run_store
        self.artifacts = artifacts
        self.servers: dict[str, MCPServer] = {}
        self.clients: dict[str, MCPHttpClient] = {}

    def register_server(
        self,
        name: str,
        url: str,
        *,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
        transport: str = "legacy",
    ) -> MCPServer:
        server = MCPServer(name, url, timeout, headers or {})
        self.servers[name] = server
        if transport == "sdk":
            from .sdk import SDKMCPClient
            self.clients[name] = SDKMCPClient(url, timeout=timeout, headers=headers)
        elif transport == "legacy":
            self.clients[name] = MCPHttpClient(url, timeout=timeout, headers=headers, retries=1)
        else:
            raise ValueError("transport must be sdk or legacy")
        return server

    def _server_for_url(self, url: str) -> MCPServer:
        for server in self.servers.values():
            if server.url == url:
                return server
        suffix = len(self.servers) + 1
        return self.register_server(f"server{suffix}", url)

    async def discover_tools(self, server_name: str) -> list[ToolSpec]:
        server = self.servers[server_name]
        tools = await self.clients[server_name].list_tools()
        specs: list[ToolSpec] = []
        for tool in tools:
            remote_name = str(tool.get("name") or "")
            public_name = f"mcp__{server_name}__{remote_name}"
            spec = self.policy.infer_spec(
                public_name,
                str(tool.get("description") or ""),
                tool.get("inputSchema") or tool.get("input_schema") or {},
            )
            server.tools[remote_name] = spec
            specs.append(spec)
        server.healthy = True
        server.failures = 0
        return specs

    async def list_tools_by_url(self, url: str) -> ToolResult:
        server = self._server_for_url(url)
        try:
            specs = await self.discover_tools(server.name)
            return ToolResult(
                True,
                f"发现 {len(specs)} 个 MCP 工具",
                data=[
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "input_schema": spec.input_schema,
                        "risk_level": spec.risk_level.value,
                    }
                    for spec in specs
                ],
            )
        except MCPClientError as exc:
            return ToolResult(False, "MCP 工具发现失败", error=str(exc), retryable=exc.retryable)

    async def call_by_url(
        self,
        url: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> ToolResult:
        server = self._server_for_url(url)
        if tool_name == "tools/list":
            return await self.list_tools_by_url(url)
        return await self.call(server.name, tool_name, arguments or {})

    async def call(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        authorized_decision=None,
    ) -> ToolResult:
        server = self.servers[server_name]
        now = time.monotonic()
        if server.circuit_open_until > now:
            return ToolResult(
                False,
                "MCP 服务暂时熔断",
                error="连续失败过多，请稍后重试",
                retryable=True,
            )

        spec = server.tools.get(tool_name)
        if spec is None:
            spec = self.policy.infer_spec(
                f"mcp__{server_name}__{tool_name}"
            )
        decision = authorized_decision or self.policy.evaluate(spec, arguments)
        if not decision.allowed:
            summary = (
                f"需要批准，approval_id={decision.approval_id}"
                if decision.action == "approval_required"
                else decision.reason
            )
            self.run_store.record_tool_call(
                tool_name=spec.name,
                arguments=arguments,
                status="denied",
                run_id=get_run_id(),
                risk_level=spec.risk_level.value,
                decision=decision.action,
                summary=summary,
            )
            return ToolResult(
                False,
                summary,
                error=decision.reason,
                retryable=decision.action == "approval_required",
                state_changes={"approval_id": decision.approval_id},
            )

        started = time.monotonic()
        try:
            raw = await self.clients[server_name].call_tool(tool_name, arguments, idempotent=spec.idempotent)
            result = self._normalize_result(raw, get_run_id())
            latency = int((time.monotonic() - started) * 1000)
            self.run_store.record_tool_call(
                tool_name=spec.name,
                arguments=arguments,
                status="completed" if result.ok else "failed",
                run_id=get_run_id(),
                risk_level=spec.risk_level.value,
                decision=decision.action,
                summary=result.summary,
                error=result.error,
                latency_ms=latency,
            )
            server.healthy = True
            server.failures = 0
            return result
        except MCPClientError as exc:
            latency = int((time.monotonic() - started) * 1000)
            server.failures += 1
            server.healthy = False
            if server.failures >= 3:
                server.circuit_open_until = time.monotonic() + 15
            self.run_store.record_tool_call(
                tool_name=spec.name,
                arguments=arguments,
                status="failed",
                run_id=get_run_id(),
                risk_level=spec.risk_level.value,
                decision=decision.action,
                error=str(exc),
                latency_ms=latency,
            )
            return ToolResult(
                False,
                "MCP 调用失败",
                error=str(exc),
                retryable=exc.retryable and spec.idempotent,
                state_changes={"outcome_unknown": not spec.idempotent},
            )

    def _normalize_result(
        self, raw: dict[str, Any], run_id: str
    ) -> ToolResult:
        if raw.get("isError"):
            return ToolResult(False, "MCP 工具返回错误", error=str(raw.get("content")))
        texts: list[str] = []
        artifacts: list[dict[str, Any]] = []
        for item in raw.get("content", []) or []:
            item_type = item.get("type")
            if item_type == "text":
                text, created = self.artifacts.externalize_if_large(
                    str(item.get("text") or ""),
                    kind="mcp-result",
                    run_id=run_id,
                )
                texts.append(str(text))
                artifacts.extend(created)
            elif item_type == "image" and item.get("data"):
                try:
                    artifact = self.artifacts.save_base64_image(
                        str(item["data"]),
                        run_id=run_id,
                        mime_type=str(item.get("mimeType") or "image/png"),
                    )
                    artifacts.append(artifact)
                    texts.append(f"[截图 Artifact: {artifact['path']}]")
                except Exception as exc:
                    texts.append(f"[截图保存失败: {exc}]")
        summary = "\n".join(texts).strip() or "调用成功，无返回内容"
        return ToolResult(True, summary, data=raw, artifacts=artifacts)
