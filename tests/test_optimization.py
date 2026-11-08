from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pocket_agent.evaluation.harness import BenchmarkSuite
from pocket_agent.skills.optimization import GEPASkillOptimizer
from tests.test_runtime_v2 import RuntimeCase


def suite():
    return BenchmarkSuite.model_validate({"name": "scripted-library-integration", "cases": [
        {"id": f"{split}-{i}", "group": f"{split}-{i}", "split": split,
         "task": {"objective": f"Enable target-{split}-{i}", "allowed_tools": ["android_tap", "android_get_ui_tree"],
                  "max_steps": 2, "contract": {"postconditions": [{"path": "/elements", "op": "contains", "value": {"id": "target", "value": True}}],
                  "invariants": [{"path": "/elements/0/value", "op": "unchanged"}]}},
         "initial_state": {"package": "test.settings", "elements": [
             {"id": "decoy", "text": f"decoy-{split}-{i}", "value": False, "protected": True},
             {"id": "target", "text": f"target-{split}-{i}", "value": False}]}}
        for split in ("train", "dev", "holdout") for i in range(2)]})


class SkillSensitiveTestActor:
    """Scripted fixture only: validates actual GEPA/graph plumbing, not learning."""
    async def ainvoke(self, prompt):
        data = json.loads(prompt)
        target = "target" if "use target" in data["skill"] else "decoy"
        return SimpleNamespace(content=json.dumps({"tool": "android_tap", "arguments": {"element_id": target}}),
                               usage_metadata={"total_tokens": 10})


class OptimizationTests(RuntimeCase):
    def seed(self):
        skill = self.services.skills.create_candidate(name="test-policy", display_name="Test policy",
            description="Integration-only baseline", body="use first control", source_run_id="",
            allowed_tools=["android_tap", "android_get_ui_tree"])
        return self.services.skills.approve(skill["id"])

    def optimizer(self):
        return GEPASkillOptimizer(self.services, SkillSensitiveTestActor(), lambda prompt: "unused",
                                 actor_id="scripted-integration", max_actor_calls=150, repetitions=2)

    def test_real_gepa_engine_runs_shared_graph_then_scoped_promotion(self):
        baseline = self.seed()
        original = Path(baseline["content_path"]).read_text(encoding="utf-8")
        report = self.optimizer().optimize(baseline["id"], suite(), max_metric_calls=16,
            auto_promote=True, proposal_fn=lambda *args: {"skill": "use target"})
        self.assertTrue(report["eligible"], report.get("errors"))
        self.assertEqual(report["release"]["current_version"], "0.1.1")
        self.assertGreaterEqual(report["search_candidates"], 2)
        self.assertEqual(report["metrics"]["candidate_holdout"]["successes"], 4)
        self.assertEqual(report["environment"], "local_fixture")
        published = Path(report["release"]["content_path"])
        self.assertIn("_versions", str(published))
        published_content = published.read_text(encoding="utf-8")
        self.services.skills.rollback(baseline["id"], "0.1.0")
        restored = self.services.skills.get(baseline["id"])
        self.assertEqual(Path(restored["content_path"]).read_text(encoding="utf-8"), original)
        self.assertEqual(published.read_text(encoding="utf-8"), published_content)

    def test_frozen_test_is_not_reused_for_another_search(self):
        baseline = self.seed()
        first = self.optimizer().optimize(baseline["id"], suite(), max_metric_calls=16,
                                         proposal_fn=lambda *args: {"skill": "use target"})
        self.assertTrue(first["eligible"])
        second = self.optimizer().optimize(baseline["id"], suite(), max_metric_calls=16,
                                          proposal_fn=lambda *args: {"skill": "use target"})
        self.assertFalse(second["eligible"])
        self.assertIn("IntegrityError", second["errors"])

    def test_evidence_gate_cannot_be_removed_by_editing_frontmatter(self):
        baseline = self.seed()
        report = self.optimizer().optimize(baseline["id"], suite(), max_metric_calls=16,
                                          proposal_fn=lambda *args: {"skill": "use target"})
        candidate = self.services.skills.get(report["candidate_id"])
        path = Path(candidate["content_path"])
        path.write_text("# tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.services.skills.approve(candidate["id"])
