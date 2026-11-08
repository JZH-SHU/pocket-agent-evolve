from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pocket_agent.skills.curator import _redact_value
from pocket_agent.skills.evaluation import SimulatedUIRunner, TaskSuite, UI_TOOLS
from pocket_agent.skills.evolution import SkillEvolution
from pocket_agent.skills.rsi import DemoModel, demo_suite
from tests.test_runtime_v2 import RuntimeCase


class CapturingModel(DemoModel):
    def __init__(self):
        self.proposals = []

    async def ainvoke(self, prompt):
        data = json.loads(prompt)
        if "schema" in data:
            self.proposals.append(data)
        return await super().ainvoke(prompt)


class RSITests(RuntimeCase):
    def seed(self):
        candidate = self.services.skills.create_candidate(
            name="toggle", display_name="模拟基线", description="读取后点击第一个控件的基线",
            body="# 基线\n读取界面，点击第一个元素。", source_run_id="", allowed_tools=sorted(UI_TOOLS),
        )
        return self.services.skills.approve(candidate["id"])

    def evolve(self, active, model=None, **kwargs):
        return asyncio.run(SkillEvolution(self.services.skills, self.services.artifacts,
            model or DemoModel(), model_id="scripted-test", **kwargs).evolve(active["id"], demo_suite()))

    def test_evolution_does_not_activate_and_approval_updates_same_skill_then_rolls_back(self):
        active = self.seed()
        original = Path(active["content_path"]).read_text(encoding="utf-8")
        report = self.evolve(active)
        self.assertTrue(report["eligible"])
        self.assertEqual(report["environment"], "simulated_ui")
        self.assertEqual(self.services.skills.get(active["id"])["current_version"], "0.1.0")
        self.assertTrue(Path(report["artifact"]["path"]).is_file())
        published = self.services.skills.approve(report["candidate_id"])
        self.assertEqual(published["id"], active["id"])
        self.assertEqual(published["current_version"], "0.1.1")
        self.assertEqual(self.services.skills.get(report["candidate_id"])["status"], "archived")
        self.assertIn("按目标文字唯一定位", Path(published["content_path"]).read_text(encoding="utf-8"))
        rollback = self.services.skills.rollback(active["id"], "0.1.0")
        self.assertEqual(Path(rollback["content_path"]).read_text(encoding="utf-8"), original)
        self.assertEqual(rollback["display_name"], active["display_name"])
        with self.assertRaises(KeyError):
            self.services.skills.approve(report["candidate_id"])
        # A second cycle after rollback must retain the first release for inspection.
        second = self.evolve(rollback)
        next_release = self.services.skills.approve(second["candidate_id"])
        self.assertEqual(next_release["current_version"], "0.1.2")
        self.assertIsNotNone(self.services.database.fetch_one(
            "SELECT id FROM skill_versions WHERE skill_id=? AND version='0.1.1'", (active["id"],)))

    def test_first_candidate_can_evolve_before_first_activation(self):
        seed = self.services.skills.create_candidate(name="new-skill", display_name="新的模拟基线",
            description="首个未启用的技能", body="# 基线\n点击第一个元素", source_run_id="", allowed_tools=sorted(UI_TOOLS))
        report = self.evolve(seed)
        published = self.services.skills.approve(report["candidate_id"])
        self.assertEqual(published["status"], "active")
        self.assertEqual(self.services.skills.get(seed["id"])["status"], "candidate")

    def test_holdout_never_reaches_proposer_and_unselected_candidate_cannot_be_approved(self):
        active, model = self.seed(), CapturingModel()
        report = self.evolve(active, model)
        self.assertEqual(len(model.proposals), 2)
        for proposal in model.proposals:
            self.assertNotIn("target-holdout", json.dumps(proposal))
            self.assertTrue({item["split"] for item in proposal["feedback"]} <= {"train", "dev"})
        others = set(report["candidates"]) - {report["candidate_id"]}
        self.assertTrue(others)
        for candidate_id in others:
            with self.assertRaises(ValueError):
                self.services.skills.approve(candidate_id)

    def test_candidate_tampering_including_removing_marker_invalidates_evaluation(self):
        report = self.evolve(self.seed())
        skill = self.services.skills.get(report["candidate_id"])
        path = Path(skill["content_path"])
        path.write_text(path.read_text(encoding="utf-8").replace("rsi-managed: true", "") + "changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed since evaluation"):
            self.services.skills.approve(skill["id"])

    def test_champion_change_invalidates_release(self):
        active = self.seed()
        report = self.evolve(active)
        path = Path(active["content_path"])
        self.services.skills.update_active(active["id"], path.read_text(encoding="utf-8") + "\nmanual update")
        with self.assertRaisesRegex(ValueError, "champion changed"):
            self.services.skills.approve(report["candidate_id"])

    def test_holdout_failure_blocks_release(self):
        class RegressingModel(DemoModel):
            async def ainvoke(self, prompt):
                data = json.loads(prompt)
                if "schema" not in data and "target-holdout" in data["objective"]:
                    return SimpleNamespace(content='{"tool":"finish"}')
                return await super().ainvoke(prompt)
        report = self.evolve(self.seed(), RegressingModel())
        self.assertFalse(report["eligible"])
        with self.assertRaises(ValueError):
            self.services.skills.approve(report["candidate_id"])

    def test_proposal_cannot_add_payment_tool(self):
        class UnsafeModel(DemoModel):
            async def ainvoke(self, prompt):
                response = await super().ainvoke(prompt)
                if "schema" in json.loads(prompt):
                    payload = json.loads(response.content)
                    payload["allowed_tools"].append("android_pay")
                    response.content = json.dumps(payload)
                return response
        report = self.evolve(self.seed(), UnsafeModel())
        self.assertFalse(report["eligible"])
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["errors"][0]["error"], "ValueError")

    def test_budget_exhaustion_never_releases(self):
        report = self.evolve(self.seed(), max_calls=1)
        self.assertFalse(report["eligible"])
        self.assertTrue(report["budget_exhausted"])
        self.assertEqual(report["model_calls"], 1)

    def test_suite_rejects_group_and_identical_fixture_leakage(self):
        payload = demo_suite().model_dump()
        payload["cases"][2]["group"] = payload["cases"][0]["group"]
        with self.assertRaises(ValueError):
            TaskSuite.model_validate(payload)
        payload = demo_suite().model_dump()
        duplicate = {**payload["cases"][0], "id": "duplicate", "split": "holdout", "group": "new-group"}
        payload["cases"].append(duplicate)
        with self.assertRaises(ValueError):
            TaskSuite.model_validate(payload)

    def test_noop_claim_cannot_pass_and_protected_control_is_denied(self):
        class NoopModel:
            async def ainvoke(self, prompt):
                return SimpleNamespace(content='{"tool":"finish","arguments":{"claim":"success"}}')
        case = demo_suite().cases[0]
        result = asyncio.run(SimulatedUIRunner(NoopModel(), model_id="noop").run("", case, UI_TOOLS))
        self.assertFalse(result["verified"])
        protected = case.model_copy(deep=True)
        protected.elements[1].protected = True
        result = asyncio.run(SimulatedUIRunner(DemoModel(), model_id="scripted").run(
            "按目标文字唯一定位", protected, UI_TOOLS))
        self.assertFalse(result["verified"])
        self.assertEqual(result["violations"], 1)

    def test_nested_secret_values_are_redacted(self):
        clean = _redact_value({"arguments": {"api_key": "sensitive", "password": "hidden"}})
        self.assertNotIn("sensitive", json.dumps(clean))
        self.assertNotIn("hidden", json.dumps(clean))

    def test_seed_cli_uses_isolated_model_and_preserves_manual_gate(self):
        from pocket_agent.skills.rsi import main
        import os
        import sys
        import contextlib
        import io
        seed = self.root / "seed.md"
        seed.write_text("---\nname: cli-seed\ntitle: CLI seed\nallowed-tools: " +
            ",".join(sorted(UI_TOOLS)) + "\n---\n\n点击第一个元素。", encoding="utf-8")
        suite_path = self.root / "suite.json"
        suite_path.write_text(demo_suite().model_dump_json(), encoding="utf-8")
        arguments = ["rsi", "evolve", "--seed", str(seed), "--suite", str(suite_path)]
        with patch.object(sys, "argv", arguments), patch("dotenv.load_dotenv"), patch.dict(os.environ, {
            "LLM_MODEL": "scripted", "LLM_API_KEY": "synthetic", "EXECUTOR_MODEL": "", "EXECUTOR_API_KEY": ""}), \
                patch("pocket_agent.skills.rsi.get_runtime_services", return_value=self.services), \
                patch("langchain_openai.ChatOpenAI", return_value=DemoModel()), contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        report = json.loads(output.getvalue())
        self.assertTrue(report["eligible"])
        self.assertEqual(self.services.skills.get(report["candidate_id"])["status"], "candidate")
        self.assertEqual(self.services.skills.list("active"), [])

    def test_existing_approval_api_enforces_evaluation_and_auth(self):
        import app as api
        from fastapi import HTTPException
        from starlette.requests import Request
        report = self.evolve(self.seed())
        pending = next(candidate for candidate in report["candidates"] if candidate != report["candidate_id"])
        local = Request({"type": "http", "headers": [], "client": ("testclient", 1)})
        remote = Request({"type": "http", "headers": [], "client": ("203.0.113.1", 1)})
        with patch.object(api, "_services", return_value=self.services), patch.dict("os.environ", {"POCKET_ADMIN_TOKEN": ""}):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(api.approve_skill(pending, local))
            self.assertEqual(caught.exception.status_code, 409)
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(api.approve_skill(report["candidate_id"], remote))
            self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
