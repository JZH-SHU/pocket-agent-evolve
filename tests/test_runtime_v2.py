from __future__ import annotations

import asyncio
import base64
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pocket_agent.agents.verifier import ExecutionVerifier
from pocket_agent.bootstrap import get_runtime_services, reset_runtime_services
from pocket_agent.memory.service import MemoryService, rrf_merge
from pocket_agent.mcp.client import MCPHttpClient
from pocket_agent.runtime.context import (
    get_conversation_id,
    get_run_id,
    runtime_context,
)
from pocket_agent.runtime.interaction import InteractionQueue
from pocket_agent.runtime.state import RunStatus
from pocket_agent.skills.curator import SkillCurator, VerificationEvidence
from pocket_agent.skills.frontmatter import format_frontmatter
from pocket_agent.storage.database import Database
from pocket_agent.tools.policy import PolicyEngine
from pocket_agent.tools.specs import RiskLevel, ToolSpec


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        reset_runtime_services()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.services = get_runtime_services(self.root / "pocket.db", self.root)

    def tearDown(self):
        reset_runtime_services()
        self.tempdir.cleanup()


class FakeSkillDraftLLM:
    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    async def ainvoke(self, prompt):
        self.prompts.append(prompt)
        response = type("FakeResponse", (), {})()
        response.content = json.dumps(self.payload, ensure_ascii=False)
        return response


class DatabaseAndRunTests(RuntimeCase):
    def test_schema_is_versioned_and_wal_enabled(self):
        migrations = self.services.database.fetch_all(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
        self.assertEqual([row["version"] for row in migrations], [1, 2, 3])
        with self.services.database.connection() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_run_state_machine_and_restart_recovery(self):
        run_id = self.services.runs.create_run(
            "conversation-a",
            "打开设置并验证页面",
            [{"id": 1, "desc": "打开设置"}],
        )
        self.services.runs.transition(run_id, RunStatus.EXECUTING.value)
        self.services.runs.transition(run_id, RunStatus.VERIFYING.value)
        self.services.runs.transition(run_id, RunStatus.COMPLETED.value)
        with self.assertRaises(ValueError):
            self.services.runs.transition(run_id, RunStatus.EXECUTING.value)

        reset_runtime_services()
        recovered = get_runtime_services(self.root / "pocket.db", self.root)
        self.assertEqual(
            recovered.runs.get_run(run_id)["status"], RunStatus.COMPLETED.value
        )


class ContextAndQueueTests(RuntimeCase):
    def test_contextvars_isolate_two_conversations(self):
        async def worker(conversation_id, run_id):
            with runtime_context(
                conversation_id=conversation_id,
                run_id=run_id,
            ):
                await asyncio.sleep(0.01)
                return get_conversation_id(), get_run_id()

        async def scenario():
            return await asyncio.gather(
                worker("conversation-a", "run-a"),
                worker("conversation-b", "run-b"),
            )

        result = asyncio.run(scenario())
        self.assertEqual(
            result,
            [("conversation-a", "run-a"), ("conversation-b", "run-b")],
        )

    def test_cli_and_browser_requests_are_queued_in_order(self):
        observed = []

        async def scenario():
            async def handler(request):
                observed.append(("start", request.source, request.text))
                await asyncio.sleep(0.01)
                observed.append(("end", request.source, request.text))
                return request.text

            queue = InteractionQueue(handler)
            first = asyncio.create_task(queue.submit("cli", "one", "c1"))
            await asyncio.sleep(0)
            second = asyncio.create_task(queue.submit("browser", "two", "c1"))
            values = await asyncio.gather(first, second)
            await queue.stop()
            return values

        self.assertEqual(asyncio.run(scenario()), ["one", "two"])
        self.assertEqual(
            observed,
            [
                ("start", "cli", "one"),
                ("end", "cli", "one"),
                ("start", "browser", "two"),
                ("end", "browser", "two"),
            ],
        )


class PolicyTests(RuntimeCase):
    def test_read_tool_allowed_and_external_action_requires_approval(self):
        policy = self.services.policy
        read_spec = policy.infer_spec(
            "mcp__neuralbridge__android_get_ui_tree",
            "读取手机界面结构",
        )
        self.assertTrue(policy.evaluate(read_spec, {}).allowed)
        self.assertTrue(read_spec.read_only)

        send_spec = policy.infer_spec(
            "mcp__neuralbridge__android_send_message",
            "向联系人发送消息",
        )
        decision = policy.evaluate(send_spec, {"text": "hello"})
        self.assertEqual(decision.action, "approval_required")
        self.assertTrue(decision.approval_id)
        policy.decide(decision.approval_id, True)
        approved = policy.evaluate(send_spec, {"text": "hello"})
        self.assertTrue(approved.allowed)

    def test_plan_mode_denies_side_effects(self):
        spec = ToolSpec(
            name="file_write",
            side_effect=True,
            risk_level=RiskLevel.MEDIUM,
        )
        decision = self.services.policy.evaluate(spec, {"filepath": "x"}, mode="plan")
        self.assertEqual(decision.action, "deny")

    def test_file_write_rejects_path_outside_project(self):
        from agent.tools.basic_tools import _resolve_writable_path

        with self.assertRaises(PermissionError):
            _resolve_writable_path("../outside-pocket-agent.txt")


class ApiSecurityTests(unittest.TestCase):
    def test_config_masks_secrets(self):
        import app as api_app

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / ".env").write_text(
                "LLM_API_KEY=secret-value\nLLM_MODEL=demo-model\n",
                encoding="utf-8",
            )
            original_root = api_app.PROJECT_ROOT
            try:
                api_app.PROJECT_ROOT = str(root)
                config = api_app._masked_env_config()
            finally:
                api_app.PROJECT_ROOT = original_root
        self.assertEqual(config["LLM_API_KEY"], "***configured***")
        self.assertEqual(config["LLM_MODEL"], "demo-model")


class MemoryTests(RuntimeCase):
    def test_chinese_query_without_spaces_and_persistence(self):
        memory_id = self.services.memory.save(
            "用户希望在深圳寻找 Agent 开发实习岗位",
            kind="fact",
            importance=8,
        )
        self.services.memory.save(
            "用户喜欢喝无糖咖啡",
            kind="fact",
            importance=5,
        )
        results = self.services.memory.search("深圳Agent实习")
        self.assertEqual(results[0]["id"], memory_id)

        reset_runtime_services()
        recovered = get_runtime_services(self.root / "pocket.db", self.root)
        self.assertEqual(
            recovered.memory.search("深圳Agent实习")[0]["id"], memory_id
        )

    def test_rrf_merges_lexical_and_semantic_results(self):
        lexical = [
            {"id": "a", "importance": 5, "created_at": 1},
            {"id": "b", "importance": 5, "created_at": 1},
        ]
        semantic = [
            {"id": "b", "similarity": 0.95, "importance": 5, "created_at": 1}
        ]
        merged = rrf_merge(lexical, semantic, top_n=2)
        self.assertEqual(merged[0]["id"], "b")


class VerificationAndSkillTests(RuntimeCase):
    def test_verifier_requires_post_action_evidence(self):
        task = {
            "contract": {"postconditions": [{"path": "/package", "value": "settings"}]},
            "steps": [
                {"id": 1, "status": "completed"},
                {"id": 2, "status": "completed"},
            ]
        }
        verifier = ExecutionVerifier()
        unverified = verifier.verify(
            task, [{"tool_name": "mcp_call", "remote_tool": "android_tap"}]
        )
        self.assertFalse(unverified.verified)
        verified = verifier.verify(
            task,
            [
                {"tool_name": "mcp_call", "remote_tool": "android_tap"},
                {
                    "tool_name": "mcp_call",
                    "remote_tool": "android_get_ui_tree",
                    "ok": True,
                    "result": {"package": "settings"},
                },
            ],
        )
        self.assertTrue(verified.verified)

    def test_candidate_approval_update_and_rollback(self):
        run_id = self.services.runs.create_run("c1", "demo")
        steps = [
            {"id": 1, "status": "completed", "desc": "打开 Boss 直聘"},
            {"id": 2, "status": "completed", "desc": "搜索深圳岗位"},
            {"id": 3, "status": "completed", "desc": "读取结果并核验"},
        ]
        trace = [
            {
                "tool_name": "mcp_call",
                "remote_tool": "android_launch_app",
            },
            {
                "tool_name": "mcp_call",
                "remote_tool": "android_get_ui_tree",
            },
        ]
        candidate = SkillCurator(self.services.skills).create_candidate(
            run_id=run_id,
            objective="搜索深圳 Agent 实习 13800138000",
            steps=steps,
            trace=trace,
            verification=VerificationEvidence(
                True,
                "页面出现深圳岗位列表",
                observations=["android_get_ui_tree 已验证岗位列表"],
            ),
        )
        self.assertIsNotNone(candidate)
        self.assertRegex(candidate["id"], r"^[a-z0-9_-]+$")
        candidate_path = Path(candidate["content_path"])
        self.assertNotIn("13800138000", candidate_path.read_text(encoding="utf-8"))

        active = self.services.skills.approve(candidate["id"])
        self.assertEqual(active["status"], "active")
        updated_content = format_frontmatter(
            {
                "name": candidate["id"],
                "title": "深圳岗位检索",
                "description": "更新后的流程",
            },
            "# 更新后的正文",
        )
        updated = self.services.skills.update_active(
            candidate["id"], updated_content
        )
        self.assertEqual(updated["current_version"], "0.1.1")
        rolled_back = self.services.skills.rollback(candidate["id"], "0.1.0")
        restored = Path(rolled_back["content_path"]).read_text(encoding="utf-8")
        self.assertIn("status: active", restored)
        self.assertIn("version: 0.1.0", restored)

    def test_rejected_candidate_is_archived(self):
        candidate = self.services.skills.create_candidate(
            name="demo-candidate",
            display_name="演示候选技能",
            description="只用于测试",
            body="# 演示",
            source_run_id="",
        )
        rejected = self.services.skills.reject(candidate["id"])
        self.assertEqual(rejected["status"], "archived")

    def test_llm_draft_is_validated_parameterized_and_persisted(self):
        llm = FakeSkillDraftLLM(
            {
                "display_name": "在 Boss 直聘搜索岗位",
                "description": "在指定城市按关键词搜索岗位，并通过页面元素验证结果。",
                "use_cases": ["用户需要查询指定城市和方向的招聘岗位"],
                "prerequisites": ["NeuralBridge 已连接", "Boss 直聘已安装"],
                "variables": [
                    {
                        "name": "city",
                        "description": "目标城市",
                        "example": "深圳",
                        "required": True,
                    },
                    {
                        "name": "keyword",
                        "description": "岗位搜索关键词",
                        "example": "Agent 开发",
                        "required": True,
                    },
                ],
                "allowed_tools": [
                    "android_launch_app",
                    "android_get_ui_tree",
                ],
                "steps": [
                    {
                        "order": 1,
                        "title": "启动应用",
                        "action": "启动 Boss 直聘并等待首页稳定。",
                        "tool": "android_launch_app",
                        "arguments": {"package_name": "com.hpbr.bosszhipin"},
                        "verification": "使用 UI 树确认首页元素出现。",
                        "on_failure": "检查应用是否安装后停止。",
                    },
                    {
                        "order": 2,
                        "title": "设置搜索条件",
                        "action": "将城市设置为 {{city}}，关键词设置为 {{keyword}}。",
                        "tool": "android_get_ui_tree",
                        "arguments": {},
                        "verification": "重新读取 UI 树，确认城市和关键词已显示。",
                        "on_failure": "刷新页面后重新定位元素一次。",
                    },
                    {
                        "order": 3,
                        "title": "核验结果",
                        "action": "读取岗位列表并检查结果与输入条件一致。",
                        "tool": "android_get_ui_tree",
                        "arguments": {},
                        "verification": "岗位卡片、{{city}} 和 {{keyword}} 同时出现在页面证据中。",
                        "on_failure": "保留页面证据并向用户说明未找到结果。",
                    },
                ],
                "success_criteria": ["页面出现与城市和关键词匹配的岗位卡片"],
                "failure_recovery": ["UI 变化时重新读取 UI 树，不复用旧坐标"],
                "safety_notes": ["不得自动投递或发送消息"],
                "limitations": ["不同 App 版本的元素标识可能变化"],
            }
        )
        run_id = self.services.runs.create_run("c1", "search jobs")
        candidate = asyncio.run(
            SkillCurator(self.services.skills, llm=llm).create_candidate_async(
                run_id=run_id,
                objective="搜索深圳 Agent 开发岗位 13800138000",
                steps=[
                    {"status": "completed", "desc": "打开 Boss 直聘"},
                    {"status": "completed", "desc": "搜索深圳 Agent 开发岗位"},
                    {"status": "completed", "desc": "读取并核验岗位列表"},
                ],
                trace=[
                    {
                        "tool_name": "mcp_call",
                        "remote_tool": "android_launch_app",
                        "arguments": {"package_name": "com.hpbr.bosszhipin"},
                    },
                    {
                        "tool_name": "mcp_call",
                        "remote_tool": "android_get_ui_tree",
                        "result": "页面出现深圳 Agent 开发岗位",
                    },
                ],
                verification=VerificationEvidence(
                    True,
                    "岗位列表验证通过",
                    observations=["UI 树中出现目标城市、关键词和岗位卡片"],
                ),
            )
        )
        self.assertIsNotNone(candidate)
        content = Path(candidate["content_path"]).read_text(encoding="utf-8")
        self.assertIn("generation-method: llm_validated", content)
        self.assertIn("{{city}}", content)
        self.assertIn("## 失败恢复", content)
        self.assertNotIn("13800138000", content)
        self.assertNotIn("13800138000", llm.prompts[0])

    def test_invalid_llm_tool_is_rejected_and_falls_back(self):
        run_id = self.services.runs.create_run("c1", "invalid draft")
        invalid = FakeSkillDraftLLM(
            {
                "display_name": "危险草稿",
                "description": "包含执行轨迹中不存在的工具，应被规则拒绝。",
                "use_cases": ["测试工具白名单"],
                "prerequisites": [],
                "variables": [],
                "allowed_tools": ["android_pay"],
                "steps": [
                    {
                        "order": index,
                        "title": f"步骤 {index}",
                        "action": "调用不存在于轨迹中的支付工具。",
                        "tool": "android_pay",
                        "arguments": {},
                        "verification": "检查支付结果是否出现。",
                        "on_failure": "停止。",
                    }
                    for index in range(1, 4)
                ],
                "success_criteria": ["支付成功"],
                "failure_recovery": ["停止执行"],
                "safety_notes": [],
                "limitations": [],
            }
        )
        candidate = asyncio.run(
            SkillCurator(self.services.skills, llm=invalid).create_candidate_async(
                run_id=run_id,
                objective="读取页面并确认状态",
                steps=[
                    {"status": "completed", "desc": "打开页面"},
                    {"status": "completed", "desc": "读取页面"},
                    {"status": "completed", "desc": "确认页面"},
                ],
                trace=[
                    {
                        "tool_name": "mcp_call",
                        "remote_tool": "android_get_ui_tree",
                    }
                ],
                verification=VerificationEvidence(
                    True,
                    "UI 树验证通过",
                    observations=["页面元素存在"],
                ),
            )
        )
        content = Path(candidate["content_path"]).read_text(encoding="utf-8")
        self.assertIn("generation-method: python_fallback", content)
        self.assertIn("LLM 草稿未通过校验", content)
        self.assertNotIn("android_pay", content)


class MockMCPHandler(BaseHTTPRequestHandler):
    methods = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        method = payload.get("method")
        self.__class__.methods.append(method)
        request_id = payload.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock", "version": "1"},
            }
        elif method == "notifications/initialized":
            result = {}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "android_get_ui_tree",
                        "description": "读取 UI 树",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                    {
                        "name": "android_screenshot",
                        "description": "获取截图",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            }
        elif method == "tools/call":
            name = payload.get("params", {}).get("name")
            if name == "android_screenshot":
                result = {
                    "content": [
                        {
                            "type": "image",
                            "mimeType": "image/png",
                            "data": base64.b64encode(b"fake-png").decode("ascii"),
                        }
                    ]
                }
            else:
                result = {
                    "content": [{"type": "text", "text": "UI tree: Boss直聘首页"}]
                }
        else:
            self.send_error(404)
            return
        body = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "mock-session")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class MCPTests(RuntimeCase):
    def setUp(self):
        super().setUp()
        MockMCPHandler.methods = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockMCPHandler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}/mcp"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def test_initialize_list_call_and_artifact(self):
        async def scenario():
            self.services.mcp.register_server("mock", self.url)
            specs = await self.services.mcp.discover_tools("mock")
            tree = await self.services.mcp.call(
                "mock", "android_get_ui_tree", {}
            )
            screenshot = await self.services.mcp.call(
                "mock", "android_screenshot", {}
            )
            return specs, tree, screenshot

        specs, tree, screenshot = asyncio.run(scenario())
        self.assertEqual(len(specs), 2)
        self.assertTrue(tree.ok)
        self.assertIn("Boss直聘首页", tree.summary)
        self.assertTrue(screenshot.ok)
        self.assertEqual(len(screenshot.artifacts), 1)
        self.assertTrue(Path(screenshot.artifacts[0]["path"]).is_file())
        self.assertEqual(
            MockMCPHandler.methods[:3],
            ["initialize", "notifications/initialized", "tools/list"],
        )
        self.assertIn("tools/call", MockMCPHandler.methods)

    def test_client_can_call_tools_without_manual_initialize(self):
        async def scenario():
            client = MCPHttpClient(self.url, timeout=2, retries=0)
            return await client.call_tool("android_get_ui_tree", {})

        result = asyncio.run(scenario())
        self.assertEqual(result["content"][0]["type"], "text")

    def test_unavailable_mcp_returns_retryable_error(self):
        async def scenario():
            self.services.mcp.register_server(
                "offline",
                "http://127.0.0.1:1/mcp",
                timeout=0.1,
            )
            return await self.services.mcp.call(
                "offline", "android_get_ui_tree", {}
            )

        result = asyncio.run(scenario())
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)

    def test_large_mcp_text_is_externalized(self):
        text, artifacts = self.services.artifacts.externalize_if_large(
            "x" * (17 * 1024),
            kind="mcp-result",
        )
        self.assertIn("Artifact", text)
        self.assertEqual(len(artifacts), 1)
        self.assertTrue(Path(artifacts[0]["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
