import asyncio
import socket
import threading
import time
from unittest import TestCase

from mcp.client import Client
from mcp.server import MCPServer

from pocket_agent.mcp.sdk import SDKMCPClient


class SDKIntegrationTests(TestCase):
    def test_official_sdk_modern_and_legacy_in_memory(self):
        server = MCPServer("contract-test")

        @server.tool(structured_output=True)
        def read_value() -> dict[str, bool]:
            return {"value": True}

        async def exercise(mode):
            adapter = SDKMCPClient("http://unused.invalid")
            adapter._client = lambda: Client(server, mode=mode, cache=None)
            tools = await adapter.list_tools()
            self.assertEqual(tools[0]["name"], "read_value")
            result = await adapter.call_tool("read_value", {}, idempotent=True)
            self.assertFalse(result.get("isError", False))
            self.assertTrue(result["structuredContent"]["value"])

        asyncio.run(exercise("2026-07-28"))
        asyncio.run(exercise("legacy"))

    def test_official_sdk_streamable_http_transport(self):
        import uvicorn
        server = MCPServer("http-contract-test")

        @server.tool(structured_output=True)
        def read_value() -> dict[str, bool]:
            return {"value": True}

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        http = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), log_level="error"))
        thread = threading.Thread(target=lambda: http.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if http.started:
                    break
                time.sleep(0.02)
            self.assertTrue(http.started)

            async def exercise():
                adapter = SDKMCPClient(f"http://127.0.0.1:{port}/mcp", timeout=5)
                self.assertEqual((await adapter.list_tools())[0]["name"], "read_value")
                self.assertTrue((await adapter.call_tool("read_value", {}))["structuredContent"]["value"])
            asyncio.run(exercise())
        finally:
            http.should_exit = True
            thread.join(timeout=5)
            sock.close()
