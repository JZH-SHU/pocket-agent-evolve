"""GEPA searches on train/dev; a one-use frozen holdout gates version promotion."""
from __future__ import annotations

import asyncio
import logging
from importlib.metadata import version
from pathlib import Path

from pocket_agent.agents.contracts import canonical_hash
from pocket_agent.evaluation.harness import BenchmarkRunner, BenchmarkSuite, evaluator_fingerprint, summarize
from pocket_agent.skills.curator import SkillCurator, redact
from pocket_agent.skills.evolution import CallBudget, compare
from pocket_agent.skills.frontmatter import format_frontmatter, parse_frontmatter
from pocket_agent.storage.database import now_ms


def text_hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


class GEPAAdapter:
    propose_new_texts = None

    def __init__(self, runner: BenchmarkRunner, loop: asyncio.Runner):
        self.runner, self.loop = runner, loop

    def evaluate(self, batch, candidate, capture_traces=False):
        from gepa.core.adapter import EvaluationBatch
        if set(candidate) != {"skill"} or not 1 <= len(candidate["skill"]) <= 12000:
            raise ValueError("candidate must contain one bounded skill body")
        trials = self.loop.run(self.runner.evaluate(batch, candidate["skill"]))
        outputs, scores, traces = [], [], []
        for case in batch:
            group = [t for t in trials if t["case_id"] == case.id]
            outputs.append(summarize(group))
            scores.append(sum(t["verified"] for t in group) / len(group))
            traces.append(group)
        return EvaluationBatch(outputs, scores, traces if capture_traces else None, num_metric_calls=len(trials))

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        # Only acquisition/dev traces ever reach this method. Never pass suite/test data.
        feedback = [{"Inputs": {"objective": trial["objective"]},
                     "Generated Outputs": trial.get("trace", []),
                     "Feedback": {"success": trial["verified"], "error": trial.get("error", "")}}
                    for group in eval_batch.trajectories or [] for trial in group]
        return {"skill": feedback}


class GEPASkillOptimizer:
    def __init__(self, services, actor, reflector, *, actor_id: str, max_actor_calls: int = 500,
                 max_reflections: int = 20, repetitions: int = 2, backend_factory=None):
        self.services, self.actor, self.reflector = services, actor, reflector
        self.actor_id, self.max_actor_calls = actor_id, max_actor_calls
        self.max_reflections, self.repetitions = max_reflections, repetitions
        self.backend_factory = backend_factory
        if not 1 <= max_actor_calls <= 5000 or not 1 <= max_reflections <= 100:
            raise ValueError("invalid optimization budget")

    def optimize(self, skill_id: str, suite: BenchmarkSuite, *, max_metric_calls: int = 40,
                 auto_promote: bool = False, proposal_fn=None) -> dict:
        import gepa
        if not 4 <= max_metric_calls <= 1000:
            raise ValueError("metric budget must be in [4, 1000]")
        if suite.environment == "mcp_device" and self.backend_factory is None:
            raise ValueError("device evaluation is not enabled by the operator")
        if auto_promote and suite.environment != "local_fixture":
            raise ValueError("automatic promotion is currently limited to the local-fixture scope")
        repository = self.services.skills
        seed = repository.get(skill_id)
        if not seed or seed["status"] not in {"active", "candidate"}:
            raise ValueError("optimizer requires an active skill or unpublished seed")
        # An evaluated release candidate must not be reinterpreted as a fresh seed.
        if any(e.get("type") == "evolution_proposal" for e in repository.evidence(skill_id)):
            raise ValueError("optimize the original seed, not an evaluated release candidate")
        acquired = [e for e in repository.evidence(skill_id) if e.get("type") == "acquisition_proposal"]
        for case in suite.cases:
            digest = canonical_hash({"objective": case.task.objective.strip().casefold(), "state": case.initial_state})
            if case.split != "train" and any(e.get("task_digest") == digest for e in acquired):
                raise ValueError("acquisition task cannot appear in dev/holdout")
        champion = seed if seed["status"] == "active" else repository.get(seed["merge_target"]) if seed["merge_target"] else None
        if seed["merge_target"] and not champion or champion and champion["status"] != "active":
            raise ValueError("merge target must be active")
        original = Path(seed["content_path"]).read_text(encoding="utf-8")
        if text_hash(original) != seed["content_hash"]:
            raise ValueError("seed file differs from repository hash")
        meta, body = parse_frontmatter(original)
        allowed = {name for name in meta.get("allowed-tools", "").split(",") if name}
        baseline_body = ""  # New skills must qualify against the same actor without a Skill.
        if champion:
            baseline = Path(champion["content_path"]).read_text(encoding="utf-8")
            if text_hash(baseline) != champion["content_hash"]:
                raise ValueError("baseline file differs from repository hash")
            baseline_meta, baseline_body = parse_frontmatter(baseline)
            old_tools = {name for name in baseline_meta.get("allowed-tools", "").split(",") if name}
            if not allowed <= old_tools:
                raise ValueError("seed expands active skill permissions")
        if not allowed or any(not set(case.task.allowed_tools) <= allowed for case in suite.cases):
            raise ValueError("suite tools exceed baseline skill permissions")
        budget = CallBudget(self.actor, self.max_actor_calls, 120)
        reflection_calls = 0

        def reflect(prompt):
            nonlocal reflection_calls
            if reflection_calls >= self.max_reflections:
                raise RuntimeError("reflection budget exhausted")
            reflection_calls += 1
            result = self.reflector(prompt)
            return result if isinstance(result, str) else SkillCurator._response_text(result)

        train = [c for c in suite.cases if c.split == "train"]
        dev = [c for c in suite.cases if c.split == "dev"]
        holdout = [c for c in suite.cases if c.split == "holdout"]
        root = self.services.artifacts.root.parent
        report = {"type": "skill_evaluation_v2", "environment": suite.environment,
            "suite_hash": suite.digest, "evaluator_hash": evaluator_fingerprint(),
            "actor_id": self.actor_id, "seed_id": skill_id, "seed_hash": text_hash(original),
            "release_mode": "update" if champion else "initial",
            "champion_id": champion["id"] if champion else "", "champion_hash": champion["content_hash"] if champion else "",
            "gepa_version": version("gepa"), "eligible": False, "candidate_id": "",
            "reason": "no dev improvement", "permissions": sorted(allowed), "errors": []}
        with asyncio.Runner() as loop:
            runner = BenchmarkRunner(self.services, budget, actor_id=self.actor_id,
                                    checkpoint_path=root / "evaluation-checkpoints.db", repetitions=self.repetitions,
                                    environment=suite.environment, backend_factory=self.backend_factory)
            adapter = GEPAAdapter(runner, loop)
            try:
                baseline_dev = loop.run(runner.evaluate(dev, baseline_body))
                result = gepa.optimize(seed_candidate={"skill": body}, trainset=train, valset=dev, adapter=adapter,
                    reflection_lm=reflect, max_metric_calls=max_metric_calls, seed=0,
                    reflection_minibatch_size=min(2, len(train)), skip_perfect_score=False,
                    custom_candidate_proposer=proposal_fn, display_progress_bar=False, raise_on_exception=True,
                    logger=_SearchLogger())
                best = redact(result.best_candidate["skill"]).strip()
                if not 1 <= len(best) <= 12000:
                    raise ValueError("candidate body exceeds limit")
                candidate_dev = loop.run(runner.evaluate(dev, best))
                report.update(champion_dev=baseline_dev, candidate_dev=candidate_dev,
                              metric_calls=result.total_metric_calls, search_candidates=result.num_candidates)
                if not budget.exhausted and compare(baseline_dev, candidate_dev, require_gain=bool(champion)):
                    # Reserve this frozen test once, before reading any holdout outcome.
                    self.services.database.execute("INSERT INTO evaluation_holdouts VALUES(?, ?, ?)",
                                                   (suite.digest, text_hash(best), now_ms()))
                    baseline_holdout = loop.run(runner.evaluate(holdout, baseline_body))
                    candidate_holdout = loop.run(runner.evaluate(holdout, best))
                    report.update(champion_holdout=baseline_holdout, candidate_holdout=candidate_holdout)
                    report["eligible"] = not budget.exhausted and compare(baseline_holdout, candidate_holdout, require_gain=False)
                    if any(t["status"] in {"infra_error", "outcome_unknown", "awaiting_approval"}
                           for key in ("champion_dev", "candidate_dev", "champion_holdout", "candidate_holdout") for t in report[key]):
                        report["eligible"] = False
                    candidate = repository.create_candidate(name=skill_id + "-gepa", display_name=seed["display_name"],
                        description=seed["description"], body=best, source_run_id=seed["source_run_id"] or "", allowed_tools=sorted(allowed),
                        suggested_action="merge" if champion else "add", merge_target=champion["id"] if champion else skill_id,
                        metadata_extra={"evolution-managed": "true"}, evidence=[{"type": "evolution_proposal", "seed_id": skill_id}])
                    report.update(candidate_id=candidate["id"], candidate_hash=candidate["content_hash"], tested_body_hash=text_hash(best),
                                  reason="eligible for scoped promotion" if report["eligible"] else "frozen test gate failed")
            except Exception as exc:
                report["eligible"] = False
                report["errors"].append(type(exc).__name__)
                report["reason"] = "optimization/evaluation did not complete"
        report.update(actor_calls=budget.calls, reflection_calls=reflection_calls, budget_exhausted=budget.exhausted)
        report["metrics"] = {key: summarize(report[key]) for key in (
            "champion_dev", "candidate_dev", "champion_holdout", "candidate_holdout") if key in report}
        if report["candidate_id"]:
            repository.record_evidence(report["candidate_id"], report)
            if auto_promote and report["eligible"]:
                report["release"] = repository.promote_evaluated(report["candidate_id"])
        report["artifact"] = self.services.artifacts.save_json(report, kind="gepa-evaluation")
        return report


class _SearchLogger:
    def log(self, message):
        logging.getLogger("pocket_agent.gepa").debug("%s", message)
