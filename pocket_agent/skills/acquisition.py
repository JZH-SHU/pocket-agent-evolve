"""Turn verified deployment traces into reviewable seeds, never active skills."""
from __future__ import annotations

import json

from pocket_agent.agents.contracts import canonical_hash
from pocket_agent.skills.curator import SkillCurator, VerificationEvidence, _redact_value, redact
from pocket_agent.storage.database import now_ms


async def acquire_skill(services, task, result: dict, *, llm=None, parent_skill_id: str = "") -> dict:
    if not task.learn:
        return {"status": "skipped", "reason": "learning_disabled"}
    if not result.get("verified"):
        return {"status": "skipped", "reason": "outcome_not_verified"}
    trace = result.get("trace", [])
    actions = [e for e in trace if not e.get("read_only", False)]
    observations = [e for e in trace if e.get("read_only") and e.get("ok")]
    if not actions:
        return {"status": "skipped", "reason": "no_executed_action"}
    if len(trace) < 3 or len(observations) < 2 or any(not e.get("ok") for e in trace):
        return {"status": "skipped", "reason": "insufficient_clean_evidence"}
    # Fingerprint the redacted procedure, not run IDs, timestamps or hidden oracle values.
    fingerprint = canonical_hash({"objective": redact(task.objective).strip().casefold(),
        "environment": result["environment"], "package": result.get("initial", {}).get("package", ""),
        "actions": [{"tool": e["tool_name"], "arguments": _redact_value(e.get("arguments", {}))} for e in actions]})
    db, repository, run_id = services.database, services.skills, result["run_id"]
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM skill_acquisitions WHERE run_id=?", (run_id,)).fetchone()
        if row and row["state"] != "pending":
            return json.loads(row["decision_json"])
        if not row:
            other = conn.execute("SELECT * FROM skill_acquisitions WHERE fingerprint=? AND state IN ('pending','completed')",
                                 (fingerprint,)).fetchone()
            if other:
                decision = {"status": "duplicate", "reason": "procedure_already_recorded",
                            "source_run_id": other["run_id"], "candidate_id": other["candidate_id"]}
                conn.execute("INSERT INTO skill_acquisitions VALUES(?, ?, 'duplicate', ?, ?, ?)",
                             (run_id, fingerprint, other["candidate_id"], db.json_dumps(decision), now_ms()))
                return decision
            conn.execute("INSERT INTO skill_acquisitions(run_id,fingerprint,state,updated_at) VALUES(?,?,'pending',?)",
                         (run_id, fingerprint, now_ms()))
    try:
        # Recover a crash after atomic candidate creation but before recording this decision.
        existing = db.fetch_one("SELECT * FROM skills WHERE source_run_id=? ORDER BY created_at LIMIT 1", (run_id,))
        if existing and any(e.get("type") == "acquisition_proposal" for e in repository.evidence(existing["id"])):
            candidate = existing
        else:
            selected = trace if len(trace) <= 30 else trace[:29] + trace[-1:]
            steps = [{"desc": f"{e['tool_name']}: {'observe state' if e.get('read_only') else 'execute action'}",
                      "status": "completed", "verification": "Recorded successful tool response"} for e in selected]
            candidate = await SkillCurator(repository, llm=llm).create_candidate_async(
                run_id=run_id, objective=task.objective, steps=steps, trace=trace,
                verification=VerificationEvidence(verified=True, reason="Declared outcome and invariants passed",
                    observations=["Successful structured observation before and after action"], artifacts=[]),
                provenance={"type": "acquisition_proposal", "run_id": run_id, "fingerprint": fingerprint,
                            "task_digest": canonical_hash({"objective": task.objective.strip().casefold(),
                                                           "state": result.get("initial", {})}),
                            "environment": result["environment"]})
        if not candidate:
            raise ValueError("curator_rejected_material")
        if parent_skill_id:
            parent = repository.get(parent_skill_id)
            if parent and parent["status"] == "active":
                db.execute("UPDATE skills SET suggested_action='merge',merge_target=? WHERE id=? AND status='candidate'",
                           (parent_skill_id, candidate["id"]))
        decision = {"status": "candidate", "candidate_id": candidate["id"], "reason": "verified_trace_extracted",
                    "generation_method": candidate.get("generation_method", "recovered"),
                    "model_calls": int(llm is not None and not existing), "tokens": None if llm is not None else 0}
        state = "completed"
    except Exception as exc:
        # Learning failures must not turn a completed external task into a failed one.
        decision = {"status": "failed", "reason": type(exc).__name__}
        state = "failed"
    db.execute("UPDATE skill_acquisitions SET state=?,candidate_id=?,decision_json=?,updated_at=? WHERE run_id=?",
               (state, decision.get("candidate_id", ""), db.json_dumps(decision), now_ms(), run_id))
    return decision
