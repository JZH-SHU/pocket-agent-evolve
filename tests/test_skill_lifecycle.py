"""Scripted end-to-end lifecycle checks; no model-learning or device claims."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pocket_agent.cli import OptimizationRequest, RunRequest, execute_request
from pocket_agent.evaluation.harness import BenchmarkRunner, BenchmarkSuite
from pocket_agent.skills.optimization import GEPASkillOptimizer
from tests.test_executor import INITIAL, TASK
from tests.test_optimization import suite
from tests.test_runtime_v2 import RuntimeCase


class LifecycleActor:
    def __init__(self):
        self.action_calls = self.reflections = 0

    async def ainvoke(self, prompt):
        if not prompt.startswith("{"):
            return SimpleNamespace(content="{}")  # Exercise validated deterministic extraction fallback.
        self.action_calls += 1
        data = json.loads(prompt)
        target = "target" if data["objective"] == TASK.objective or "use target" in data["skill"] else "decoy"
        return SimpleNamespace(content=json.dumps({"tool": "android_tap", "arguments": {"element_id": target}}),
                               usage_metadata={"total_tokens": 10})

    def invoke(self, prompt):
        self.reflections += 1
        return "use target"  # Scripted reflector passed through the real GEPA library.


class LifecycleTests(RuntimeCase):
    def run_task(self, run_id="learn-one", *, request=None, actor=None, resume=False):
        return asyncio.run(execute_request(self.services, request or RunRequest(task=TASK, initial_state=INITIAL),
                                          actor or LifecycleActor(), "scripted-lifecycle", run_id, resume=resume))

    def optimizer(self):
        return GEPASkillOptimizer(self.services, LifecycleActor(), lambda prompt: "use target",
                                 actor_id="scripted-lifecycle", max_actor_calls=150)

    def test_acquisition_is_idempotent_deduplicated_and_not_activated(self):
        first = self.run_task()
        self.assertTrue(first["verified"])
        self.assertEqual(first["learning"]["status"], "candidate")
        candidate = first["learning"]["candidate_id"]
        self.assertEqual(self.services.skills.list("active"), [])
        self.assertTrue(any(e["type"] == "acquisition_proposal" for e in self.services.skills.evidence(candidate)))
        resumed = self.run_task(resume=True)
        self.assertEqual(resumed["learning"]["candidate_id"], candidate)
        duplicate = self.run_task("learn-two")
        self.assertEqual(duplicate["learning"]["status"], "duplicate")
        self.assertEqual(len(self.services.skills.list()), 1)
        # Removing the marker from the file cannot remove DB provenance.
        with self.assertRaisesRegex(ValueError, "must pass"):
            self.services.skills.approve(candidate)

    def test_noop_failed_and_evaluation_runs_do_not_create_drafts(self):
        already = json.loads(json.dumps(INITIAL))
        already["elements"][1]["value"] = True
        result = self.run_task(request=RunRequest(task=TASK, initial_state=already))
        self.assertEqual(result["learning"]["reason"], "no_executed_action")
        failed = self.run_task("failed", request=RunRequest(task=TASK.model_copy(update={"objective": "Unsolvable fixture"}),
                                                           initial_state=INITIAL))
        self.assertFalse(failed["verified"])
        self.assertEqual(failed["learning"]["reason"], "outcome_not_verified")
        runner = BenchmarkRunner(self.services, LifecycleActor(), actor_id="scripted-lifecycle",
                                 checkpoint_path=self.root / "eval.db")
        trials = asyncio.run(runner.evaluate(suite().cases, "use target"))
        self.assertTrue(all(t["verified"] for t in trials))
        self.assertTrue(all(t["learning"]["reason"] == "learning_disabled" for t in trials))
        failures = asyncio.run(runner.evaluate(suite().cases[:1], "bad instruction"))
        self.assertFalse(failures[0]["verified"])
        self.assertEqual(self.services.skills.list(), [])
        self.assertEqual(self.services.database.fetch_all("SELECT * FROM skill_acquisitions"), [])

    def test_full_task_to_gepa_first_release_and_reuse_with_resume(self):
        actor = LifecycleActor()
        request = RunRequest(task=TASK, initial_state=INITIAL, optimization=OptimizationRequest(
            suite=suite(), max_metric_calls=16, max_actor_calls=150, auto_promote=True))
        result = self.run_task(request=request, actor=actor)
        self.assertEqual(result["optimization"]["status"], "published", result.get("optimization"))
        self.assertEqual(result["optimization"]["release_mode"], "initial")
        active = result["optimization"]["release"]
        self.assertEqual(active["id"], result["learning"]["candidate_id"])
        self.assertEqual(active["current_version"], "0.1.0")
        self.assertIn("_versions", active["content_path"])
        self.assertGreater(actor.reflections, 0)
        self.assertEqual(len(self.services.database.fetch_all("SELECT * FROM skill_acquisitions")), 1)
        count = len(self.services.skills.list())
        reflections = actor.reflections
        resumed = self.run_task(request=request, actor=actor, resume=True)
        self.assertEqual(resumed["optimization"]["status"], "published")
        self.assertEqual(actor.reflections, reflections)
        self.assertEqual(len(self.services.skills.list()), count)
        reused = self.run_task("reuse", request=RunRequest(task=TASK.model_copy(update={"learn": False}),
                                                          initial_state=INITIAL, skill_id=active["id"]))
        self.assertTrue(reused["verified"])

    def test_seed_tampering_blocks_first_release(self):
        seed_id = self.run_task()["learning"]["candidate_id"]
        report = self.optimizer().optimize(seed_id, suite(), max_metric_calls=16)
        self.assertTrue(report["eligible"], report)
        seed = self.services.skills.get(seed_id)
        Path(seed["content_path"]).write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "seed changed"):
            self.services.skills.approve(report["candidate_id"])
        self.assertEqual(self.services.skills.list("active"), [])

    def test_acquired_update_compares_active_champion_and_rejects_stale_release(self):
        baseline = self.services.skills.create_candidate(name="existing", display_name="Existing policy",
            description="Scripted update baseline", body="use first control", source_run_id="",
            allowed_tools=["android_get_ui_tree", "android_tap"])
        active = self.services.skills.approve(baseline["id"])
        original = Path(active["content_path"]).read_text(encoding="utf-8")
        learned = self.run_task(request=RunRequest(task=TASK, initial_state=INITIAL, skill_id=active["id"]))
        seed = self.services.skills.get(learned["learning"]["candidate_id"])
        self.assertEqual(seed["merge_target"], active["id"])
        report = self.optimizer().optimize(seed["id"], suite(), max_metric_calls=16)
        self.assertTrue(report["eligible"], report)
        self.assertEqual(report["champion_id"], active["id"])
        self.services.skills.update_active(active["id"], original.replace("use first control", "concurrent change"))
        with self.assertRaisesRegex(ValueError, "baseline changed"):
            self.services.skills.approve(report["candidate_id"])
        self.services.skills.rollback(active["id"], "0.1.0")
        self.assertEqual(Path(self.services.skills.get(active["id"])["content_path"]).read_text(encoding="utf-8"), original)

    def test_unchanged_good_seed_can_qualify_without_premature_activation(self):
        seed = self.services.skills.create_candidate(name="already-good", display_name="Good seed",
            description="Scripted initial publication", body="use target", source_run_id="",
            allowed_tools=["android_get_ui_tree", "android_tap"])
        report = self.optimizer().optimize(seed["id"], suite(), max_metric_calls=8)
        self.assertTrue(report["eligible"], report)
        self.assertEqual(self.services.skills.get(seed["id"])["status"], "candidate")
        published = self.services.skills.approve(report["candidate_id"])
        self.assertEqual(published["id"], seed["id"])

    def test_crash_after_candidate_commit_recovers_without_duplicate(self):
        original = self.services.database.execute
        def crash(sql, parameters=()):
            if sql.startswith("UPDATE skill_acquisitions SET state="):
                raise SystemExit("crash after candidate committed")
            return original(sql, parameters)
        with patch.object(self.services.database, "execute", side_effect=crash):
            with self.assertRaises(SystemExit):
                self.run_task()
        self.assertEqual(len(self.services.skills.list()), 1)
        result = self.run_task(resume=True)
        self.assertEqual(result["learning"]["status"], "candidate")
        self.assertEqual(len(self.services.skills.list()), 1)

    def test_acquisition_task_cannot_be_used_as_unseen_dev_case(self):
        seed_id = self.run_task()["learning"]["candidate_id"]
        contaminated = suite()
        contaminated.cases[2].task = TASK
        contaminated.cases[2].initial_state = INITIAL
        with self.assertRaisesRegex(ValueError, "acquisition task"):
            self.optimizer().optimize(seed_id, contaminated, max_metric_calls=16)

    def test_learning_toggle_cannot_hide_duplicate_examples_across_splits(self):
        payload = suite().model_dump()
        payload["cases"][2]["task"] = {**payload["cases"][0]["task"], "learn": False}
        payload["cases"][2]["initial_state"] = payload["cases"][0]["initial_state"]
        with self.assertRaisesRegex(ValueError, "identical examples"):
            BenchmarkSuite.model_validate(payload)

    def test_optimization_failure_does_not_reclassify_successful_task(self):
        request = RunRequest(task=TASK, initial_state=INITIAL, optimization=OptimizationRequest(suite=suite()))
        with patch.object(GEPASkillOptimizer, "optimize", side_effect=RuntimeError("reflection unavailable")):
            result = self.run_task(request=request)
        self.assertTrue(result["verified"])
        self.assertEqual(result["optimization"]["status"], "failed")
        self.assertEqual(self.services.runs.get_run(result["run_id"])["status"], "completed")

    def test_curation_failure_does_not_reclassify_successful_task(self):
        with patch("pocket_agent.skills.acquisition.SkillCurator.create_candidate_async", side_effect=RuntimeError("unavailable")):
            result = self.run_task()
        self.assertTrue(result["verified"])
        self.assertEqual(result["learning"]["status"], "failed")
        self.assertEqual(self.services.runs.get_run(result["run_id"])["status"], "completed")
