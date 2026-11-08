from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import subprocess
import sys

from pocket_agent.agents.contracts import OutcomeContract
from pocket_agent.runtime.backends import FixtureBackend
from pocket_agent.runtime.executor import DurableExecutor, TaskSpec
from pocket_agent.runtime.ledger import ActionLedger, OutcomeUnknown
from pocket_agent.mcp.client import MCPClientError, MCPHttpClient
from tests.test_runtime_v2 import RuntimeCase


INITIAL = {"package": "test.settings", "elements": [
    {"id": "decoy", "text": "Bluetooth", "value": False, "protected": True},
    {"id": "target", "text": "Wi-Fi", "value": False},
]}
TASK = TaskSpec(objective="Enable Wi-Fi", allowed_tools=["android_get_ui_tree", "android_tap"],
    contract=OutcomeContract(postconditions=[{"path": "/elements", "op": "contains", "value": {"id": "target", "value": True}}],
                             invariants=[{"path": "/elements/0/value", "op": "unchanged"}]))


class TestActor:
    """Scripted test double, never performance/learning evidence."""
    def __init__(self):
        self.prompts = []

    async def ainvoke(self, prompt):
        self.prompts.append(json.loads(prompt))
        return SimpleNamespace(content='{"tool":"android_tap","arguments":{"element_id":"target"}}',
                               usage_metadata={"total_tokens": 20})


class ExecutorTests(RuntimeCase):
    def executor(self, actor=None, backend=None):
        self.backend = backend or FixtureBackend(self.services.database, "device", INITIAL)
        return DurableExecutor(self.services, self.backend, actor or TestActor(), actor_id="scripted-test",
                               checkpoint_path=self.root / "checkpoints.db")

    def test_same_graph_uses_outcome_and_hides_oracle_from_actor(self):
        actor = TestActor()
        result = asyncio.run(self.executor(actor).run(TASK))
        self.assertTrue(result["verified"])
        self.assertEqual(result["tokens"], 20)
        self.assertNotIn("contract", actor.prompts[0])
        self.assertNotIn("postconditions", actor.prompts[0])
        self.assertEqual(self.services.runs.get_run(result["run_id"])["status"], "completed")
        self.assertEqual(len(result["trace"]), 3)

    def test_already_satisfied_goal_does_not_toggle(self):
        state = json.loads(json.dumps(INITIAL))
        state["elements"][1]["value"] = True
        actor = TestActor()
        backend = FixtureBackend(self.services.database, "already-on", state)
        result = asyncio.run(self.executor(actor, backend).run(TASK))
        self.assertTrue(result["verified"])
        self.assertEqual(actor.prompts, [])

    def test_confirmed_action_survives_restart_before_graph_checkpoint(self):
        actor = TestActor()
        executor = self.executor(actor)
        save = self.services.artifacts.save_json
        def crash(value, *, kind, **kwargs):
            if kind == "executor-tool":
                raise SystemExit("simulated process exit after ledger commit")
            return save(value, kind=kind, **kwargs)
        with patch.object(self.services.artifacts, "save_json", side_effect=crash):
            with self.assertRaises(SystemExit):
                asyncio.run(executor.run(TASK, run_id="crash-run"))
        # Rebuild executor/backend objects; persistent checkpoint and ledger are the only memory.
        restarted = self.executor(actor)
        result = asyncio.run(restarted.run(TASK, run_id="crash-run", resume=True))
        self.assertTrue(result["verified"])
        self.assertEqual(len(actor.prompts), 1)
        self.assertTrue(asyncio.run(self.backend.observe())["elements"][1]["value"])

    def test_lost_response_stops_and_resume_only_reobserves(self):
        class LostResponse(FixtureBackend):
            calls = 0
            async def call(self, name, args, **kwargs):
                self.calls += 1
                await super().call(name, args, **kwargs)
                raise TimeoutError("response lost")
        backend = LostResponse(self.services.database, "lost", INITIAL)
        executor = self.executor(backend=backend)
        result = asyncio.run(executor.run(TASK, run_id="lost-run"))
        self.assertEqual(result["status"], "outcome_unknown")
        result = asyncio.run(executor.run(TASK, run_id="lost-run", resume=True))
        self.assertTrue(result["verified"])
        self.assertEqual(backend.calls, 1)

    def test_unknown_without_effect_never_blindly_retries(self):
        class Offline(FixtureBackend):
            calls = 0
            async def call(self, name, args, **kwargs):
                self.calls += 1
                raise TimeoutError("unknown delivery")
        backend = Offline(self.services.database, "offline", INITIAL)
        executor = self.executor(backend=backend)
        asyncio.run(executor.run(TASK, run_id="unknown-run"))
        result = asyncio.run(executor.run(TASK, run_id="unknown-run", resume=True))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(backend.calls, 1)

    def test_resume_rejects_changed_skill_and_task(self):
        executor = self.executor()
        asyncio.run(executor.run(TASK, run_id="identity-run"))
        with self.assertRaises(ValueError):
            asyncio.run(executor.run(TASK, run_id="identity-run", skill="changed", resume=True))

    def test_claim_is_atomic_and_refuses_uncertain_or_changed_intent(self):
        run = self.services.runs.create_run("test", "test")
        ledger = ActionLedger(self.services.database)
        self.assertIsNone(ledger.claim(run, "one", {"tool": "tap"}))
        with self.assertRaises(OutcomeUnknown):
            ledger.claim(run, "one", {"tool": "tap"})
        with self.assertRaises(ValueError):
            ledger.claim(run, "one", {"tool": "send"})

    def test_legacy_transport_never_retries_writes(self):
        client = MCPHttpClient("http://unused.invalid", retries=3)
        def fail(payload):
            raise MCPClientError("lost response", retryable=True)
        with patch.object(client, "_post_sync", side_effect=fail) as transport:
            with self.assertRaises(MCPClientError):
                asyncio.run(client.request("tools/call", {"name": "send_message"}))
            self.assertEqual(transport.call_count, 1)

    def test_contract_is_strict_and_not_executable(self):
        contract = OutcomeContract(postconditions=[{"path": "/value", "value": True}])
        self.assertFalse(contract.evaluate({"value": 1})[0])
        self.assertFalse(contract.evaluate({})[0])
        self.assertTrue(contract.evaluate({"value": True})[0])

    def test_hard_process_exit_reuses_confirmed_action_after_lease_expiry(self):
        (self.root / "hard-crash-input.json").write_text(json.dumps({"task": TASK.model_dump(), "initial": INITIAL}), encoding="utf-8")
        child = """
import asyncio,json,os,sys
from pathlib import Path
from pocket_agent.bootstrap import get_runtime_services
from pocket_agent.runtime.backends import FixtureBackend
from pocket_agent.runtime.executor import DurableExecutor,TaskSpec
from tests.test_executor import TestActor
root=Path(sys.argv[1]); data=json.loads((root/'hard-crash-input.json').read_text(encoding='utf-8'))
services=get_runtime_services(root/'pocket.db',root)
backend=FixtureBackend(services.database,'device',data['initial'])
save=services.artifacts.save_json
def crash(value,*,kind,**kwargs):
 if kind=='executor-tool': os._exit(81)
 return save(value,kind=kind,**kwargs)
services.artifacts.save_json=crash
executor=DurableExecutor(services,backend,TestActor(),actor_id='scripted-test',checkpoint_path=root/'checkpoints.db')
asyncio.run(executor.run(TaskSpec.model_validate(data['task']),run_id='hard-crash'))
"""
        process = subprocess.run([sys.executable, "-c", child, str(self.root)], capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 81, process.stderr.decode(errors="replace"))
        self.assertEqual(self.services.database.fetch_one("SELECT status FROM action_ledger WHERE run_id='hard-crash'")["status"], "confirmed")
        # Advance the lease clock in the test, rather than waiting 90 wall-clock seconds.
        self.services.database.execute("UPDATE executor_jobs SET lease_until=0 WHERE run_id='hard-crash'")
        actor = TestActor()
        result = asyncio.run(self.executor(actor).run(TASK, run_id="hard-crash", resume=True))
        self.assertTrue(result["verified"])
        self.assertEqual(actor.prompts, [])
