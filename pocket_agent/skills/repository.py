from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from pocket_agent.storage.database import Database, now_ms

from .frontmatter import format_frontmatter, parse_frontmatter


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _safe_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    if slug:
        return slug[:60]
    return f"skill-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:10]}"


def _next_patch(version: str) -> str:
    try:
        major, minor, patch = [int(part) for part in version.split(".")[:3]]
        return f"{major}.{minor}.{patch + 1}"
    except Exception:
        return "0.1.1"


class SkillRepository:
    """SKILL.md is current content; SQLite stores lifecycle and snapshots."""

    def __init__(
        self,
        database: Database,
        candidates_root: str | Path,
        active_root: str | Path,
    ):
        self.database = database
        self.candidates_root = Path(candidates_root).resolve()
        self.active_root = Path(active_root).resolve()
        self.candidates_root.mkdir(parents=True, exist_ok=True)
        self.active_root.mkdir(parents=True, exist_ok=True)
        # ponytail: single-process release lock; use DB CAS before adding multiple workers.
        self._release_lock = threading.RLock()

    def create_candidate(
        self,
        *,
        name: str,
        display_name: str,
        description: str,
        body: str,
        source_run_id: str,
        suggested_action: str = "add",
        merge_target: str = "",
        allowed_tools: list[str] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        metadata_extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        skill_id = _safe_id(name)
        existing = self.database.fetch_one(
            "SELECT id FROM skills WHERE id=?", (skill_id,)
        )
        if existing or (self.candidates_root / skill_id).exists():
            skill_id = f"{skill_id}-{uuid.uuid4().hex[:6]}"
        version = "0.1.0"
        metadata = {
            "name": skill_id,
            "title": display_name,
            "description": description,
            "version": version,
            "status": "candidate",
            "source-run": source_run_id,
            "allowed-tools": ",".join(allowed_tools or []),
        }
        for key, value in (metadata_extra or {}).items():
            safe_key = re.sub(r"[^a-z0-9_-]+", "-", str(key).lower()).strip("-")
            if safe_key and value:
                metadata[safe_key] = str(value).replace("\n", " ")[:500]
        content = format_frontmatter(metadata, body)
        skill_dir = self.candidates_root / skill_id
        skill_dir.mkdir(parents=True, exist_ok=False)
        path = skill_dir / "SKILL.md"
        path.write_text(content, encoding="utf-8")
        digest = _hash(content)
        created = now_ms()
        with self.database.connection() as conn:
            conn.execute(
                """
                INSERT INTO skills(
                    id, display_name, description, status, current_version,
                    scope, source_run_id, content_path, content_hash,
                    suggested_action, merge_target, created_at, updated_at
                ) VALUES (?, ?, ?, 'candidate', ?, 'project', ?, ?, ?, ?, ?, ?, ?)
                """,
                (skill_id, display_name, description, version, source_run_id or None,
                 str(path), digest, suggested_action, merge_target, created, created),
            )
            conn.execute("INSERT INTO skill_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (f"skill_version_{uuid.uuid4().hex}", skill_id, version, content, digest,
                          Database.json_dumps(evidence or []), created))
        return self.get(skill_id) or {}

    def _snapshot(
        self,
        skill_id: str,
        version: str,
        content: str,
        evidence: list[dict[str, Any]],
    ) -> None:
        self.database.execute(
            """
            INSERT OR REPLACE INTO skill_versions(
                id, skill_id, version, content, content_hash,
                evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"skill_version_{uuid.uuid4().hex}",
                skill_id,
                version,
                content,
                _hash(content),
                Database.json_dumps(evidence),
                now_ms(),
            ),
        )

    def get(self, skill_id: str) -> dict[str, Any] | None:
        return self.database.fetch_one("SELECT * FROM skills WHERE id=?", (skill_id,))

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.database.fetch_all(
                "SELECT * FROM skills WHERE status=? ORDER BY updated_at DESC",
                (status,),
            )
        return self.database.fetch_all(
            "SELECT * FROM skills ORDER BY updated_at DESC"
        )

    def approve(self, skill_id: str) -> dict[str, Any]:
        with self._release_lock:
            return self._approve(skill_id)

    def evidence(self, skill_id: str) -> list[dict[str, Any]]:
        skill = self.get(skill_id)
        if not skill:
            raise KeyError(f"skill not found: {skill_id}")
        row = self.database.fetch_one(
            "SELECT evidence_json FROM skill_versions WHERE skill_id=? AND version=?",
            (skill_id, skill["current_version"]),
        )
        return json.loads(row["evidence_json"]) if row else []

    def record_evidence(self, skill_id: str, item: dict[str, Any]) -> None:
        skill = self.get(skill_id)
        if not skill or skill["status"] != "candidate":
            raise ValueError("evidence can only be appended to a candidate")
        evidence = self.evidence(skill_id)
        evidence.append(item)
        self.database.execute(
            "UPDATE skill_versions SET evidence_json=? WHERE skill_id=? AND version=?",
            (Database.json_dumps(evidence), skill_id, skill["current_version"]),
        )

    def _approve(self, skill_id: str) -> dict[str, Any]:
        skill = self.get(skill_id)
        if not skill or skill["status"] != "candidate":
            raise KeyError(f"candidate skill not found: {skill_id}")
        source = Path(skill["content_path"]).resolve()
        if source.parent.parent != self.candidates_root:
            raise ValueError("candidate path is outside the repository")
        if not source.is_file():
            raise FileNotFoundError(source)
        evidence = self.evidence(skill_id)
        proposals = [item for item in evidence if item.get("type") == "rsi_proposal"]
        meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        if any(item.get("type") == "evolution_proposal" for item in evidence) or meta.get("evolution-managed") == "true":
            return self._approve_verified(candidate=skill, evidence=evidence)
        if proposals or meta.get("rsi-managed") == "true":
            return self._approve_evolution(skill, evidence)
        if any(item.get("type") == "acquisition_proposal" for item in evidence):
            raise ValueError("acquired drafts must pass optimize/evaluation before first activation")
        target_dir = self.active_root / skill_id
        if target_dir.exists():
            raise FileExistsError(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source.parent), str(target_dir))
        target_file = target_dir / "SKILL.md"
        content = target_file.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(content)
        meta["status"] = "active"
        content = format_frontmatter(meta, body)
        target_file.write_text(content, encoding="utf-8")
        self.database.execute(
            """
            UPDATE skills SET status='active', content_path=?, content_hash=?,
                              activated_at=?, updated_at=?
            WHERE id=?
            """,
            (str(target_file), _hash(content), now_ms(), now_ms(), skill_id),
        )
        return self.get(skill_id) or {}

    def promote_evaluated(self, skill_id: str) -> dict:
        """Pre-authorized automatic promotion is limited to the local fixture scope."""
        with self._release_lock:
            evidence = self.evidence(skill_id)
            reports = [e for e in evidence if e.get("type") == "skill_evaluation_v2"]
            if not reports:
                raise ValueError("no deployment-executor evaluation")
            report = reports[-1]
            from pocket_agent.runtime.backends import UI_TOOLS
            holdout = report.get("candidate_holdout", [])
            counts = {t["case_id"]: sum(x["case_id"] == t["case_id"] for x in holdout) for t in holdout}
            if (report.get("environment") != "local_fixture" or not set(report.get("permissions", [])) <= UI_TOOLS
                    or len(counts) < 2 or min(counts.values(), default=0) < 2):
                raise ValueError("automatic promotion requires a repeated local-fixture test with at least two held-out tasks")
            return self._approve(skill_id)

    def _approve_verified(self, candidate: dict, evidence: list[dict]) -> dict:
        from .optimization import text_hash
        from pocket_agent.evaluation.harness import evaluator_fingerprint
        from .evolution import compare
        reports = [e for e in evidence if e.get("type") == "skill_evaluation_v2"]
        if not reports:
            raise ValueError("missing deployment-executor evaluation")
        report = reports[-1]
        if (not report.get("eligible") or report.get("budget_exhausted", True) or report.get("errors")
                or report.get("evaluator_hash") != evaluator_fingerprint()
                or not compare(report.get("champion_dev", []), report.get("candidate_dev", []),
                               require_gain=report.get("release_mode", "update") == "update")
                or not compare(report.get("champion_holdout", []), report.get("candidate_holdout", []), require_gain=False)):
            raise ValueError("evaluation gate failed or evaluator changed")
        content = Path(candidate["content_path"]).read_text(encoding="utf-8")
        meta, body = parse_frontmatter(content)
        if text_hash(content) != candidate["content_hash"] or text_hash(content) != report.get("candidate_hash"):
            raise ValueError("candidate changed since evaluation")
        if text_hash(body) != report.get("tested_body_hash"):
            raise ValueError("candidate body differs from tested body")
        consumed = self.database.fetch_one("SELECT * FROM evaluation_holdouts WHERE suite_hash=?", (report.get("suite_hash"),))
        if not consumed or consumed["candidate_hash"] != report["tested_body_hash"]:
            raise ValueError("frozen test provenance does not match")
        seed = self.get(report.get("seed_id", report["champion_id"]))
        if not seed or seed["status"] not in {"active", "candidate"}:
            raise ValueError("seed lineage changed")
        seed_content = Path(seed["content_path"]).read_text(encoding="utf-8")
        if text_hash(seed_content) != report.get("seed_hash", report["champion_hash"]):
            raise ValueError("seed changed since evaluation")
        seed_meta, _ = parse_frontmatter(seed_content)
        tools = {t for t in meta.get("allowed-tools", "").split(",") if t}
        seed_tools = {t for t in seed_meta.get("allowed-tools", "").split(",") if t}
        if not tools or not tools <= seed_tools or sorted(tools) != report.get("permissions"):
            raise ValueError("skill permissions changed")
        if report.get("release_mode") == "initial":
            if seed["status"] != "candidate" or candidate["merge_target"] != seed["id"] or seed["merge_target"]:
                raise ValueError("initial release lineage changed")
            updated = self._activate_verified(seed["id"], content, evidence, report["seed_hash"], candidate["id"])
            return {**updated, "released_candidate": candidate["id"], "validation_environment": report["environment"]}
        parent = self.get(report["champion_id"])
        if not parent or parent["status"] != "active" or parent["id"] != candidate["merge_target"]:
            raise ValueError("baseline lineage changed")
        parent_meta, _ = parse_frontmatter(Path(parent["content_path"]).read_text(encoding="utf-8"))
        old_tools = {t for t in parent_meta.get("allowed-tools", "").split(",") if t}
        if not tools or not tools <= old_tools or sorted(tools) != report.get("permissions"):
            raise ValueError("skill permissions changed")
        updated = self._update_active(parent["id"], content, evidence, expected_hash=report["champion_hash"],
                                      archive_candidate=candidate["id"])
        return {**updated, "released_candidate": candidate["id"], "validation_environment": report["environment"]}

    def _approve_evolution(self, candidate: dict, evidence: list[dict]) -> dict:
        reports = [item for item in evidence if item.get("type") == "rsi_evaluation"]
        if not reports or reports[-1].get("eligible") is not True:
            raise ValueError("RSI candidate has no passing evaluation")
        report = reports[-1]
        from .evolution import compare
        if (report.get("environment") != "simulated_ui" or report.get("budget_exhausted", True)
                or not compare(report.get("champion_dev", []), report.get("candidate_dev", []), require_gain=True)
                or not compare(report.get("champion_holdout", []), report.get("candidate_holdout", []), require_gain=False)):
            raise ValueError("RSI evaluation gate failed")
        evaluator_hash = hashlib.sha256(Path(__file__).with_name("evaluation.py").read_bytes()).hexdigest()
        if report.get("evaluator_hash") != evaluator_hash:
            raise ValueError("evaluator changed since evaluation")
        content = Path(candidate["content_path"]).read_text(encoding="utf-8")
        if _hash(content) != candidate["content_hash"] or _hash(content) != report["candidate_hash"]:
            raise ValueError("candidate changed since evaluation")
        parent = self.get(report["champion_id"])
        if not parent or parent["status"] != report["champion_status"]:
            raise ValueError("champion lifecycle changed since evaluation")
        current = Path(parent["content_path"]).read_text(encoding="utf-8")
        if _hash(current) != report["champion_hash"]:
            raise ValueError("champion changed since evaluation; reevaluate")
        if candidate["merge_target"] != parent["id"]:
            raise ValueError("candidate lineage mismatch")
        meta, body = parse_frontmatter(content)
        old_meta, _ = parse_frontmatter(current)
        tools = {t for t in meta.get("allowed-tools", "").split(",") if t}
        old_tools = {t for t in old_meta.get("allowed-tools", "").split(",") if t}
        if not tools or not tools <= old_tools:
            raise ValueError("candidate expands tool permissions")
        if parent["status"] == "active":
            updated = self.update_active(parent["id"], content, evidence)
            self.reject(candidate["id"])
            return {**(self.get(updated["id"]) or {}), "released_candidate": candidate["id"]}
        if parent["status"] != "candidate":
            raise ValueError("champion must be active or candidate")
        # For a first skill, reuse ordinary promotion only after the same gate.
        target_dir = self.active_root / candidate["id"]
        if target_dir.exists():
            raise FileExistsError(target_dir)
        shutil.move(str(Path(candidate["content_path"]).parent), str(target_dir))
        target_file = target_dir / "SKILL.md"
        meta["status"] = "active"
        rendered = format_frontmatter(meta, body)
        target_file.write_text(rendered, encoding="utf-8")
        self.database.execute(
            "UPDATE skills SET status='active', content_path=?, content_hash=?, activated_at=?, updated_at=? WHERE id=?",
            (str(target_file), _hash(rendered), now_ms(), now_ms(), candidate["id"]),
        )
        return self.get(candidate["id"]) or {}

    def reject(self, skill_id: str) -> dict[str, Any]:
        skill = self.get(skill_id)
        if not skill or skill["status"] != "candidate":
            raise KeyError(f"candidate skill not found: {skill_id}")
        self.database.execute(
            "UPDATE skills SET status='archived', updated_at=? WHERE id=?",
            (now_ms(), skill_id),
        )
        return self.get(skill_id) or {}

    def update_active(
        self, skill_id: str, content: str, evidence: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        with self._release_lock:
            return self._update_active(skill_id, content, evidence)

    def _immutable_content(self, skill_id: str, content: str) -> Path:
        path = self.active_root / "_versions" / _safe_id(skill_id) / f"{_hash(content)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # A partially written new file cannot become active: DB points to it only after this returns.
            import os
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        if path.read_text(encoding="utf-8") != content:
            raise ValueError("immutable skill content is corrupt")
        return path

    def _activate_verified(self, skill_id: str, content: str, evidence: list[dict],
                           expected_hash: str, archive_candidate: str) -> dict:
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
            if not row or row["status"] != "candidate" or row["content_hash"] != expected_hash or row["merge_target"]:
                raise ValueError("seed changed before activation")
            if _hash(Path(row["content_path"]).read_text(encoding="utf-8")) != expected_hash:
                raise ValueError("seed file changed before activation")
            meta, body = parse_frontmatter(content)
            meta.update(name=skill_id, version="0.1.0", status="active")
            rendered = format_frontmatter(meta, body)
            path = self._immutable_content(skill_id, rendered)
            conn.execute("UPDATE skills SET status='active',content_path=?,content_hash=?,activated_at=?,updated_at=? "
                         "WHERE id=? AND status='candidate' AND content_hash=?",
                         (str(path), _hash(rendered), now_ms(), now_ms(), skill_id, expected_hash))
            conn.execute("UPDATE skill_versions SET content=?,content_hash=?,evidence_json=? WHERE skill_id=? AND version='0.1.0'",
                         (rendered, _hash(rendered), Database.json_dumps(evidence), skill_id))
            conn.execute("UPDATE skills SET status='archived',updated_at=? WHERE id=? AND status='candidate'",
                         (now_ms(), archive_candidate))
        return self.get(skill_id) or {}

    def _update_active(self, skill_id: str, content: str, evidence: list[dict[str, Any]] | None,
                       *, expected_hash: str | None = None, archive_candidate: str = "") -> dict[str, Any]:
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
            if not row or row["status"] != "active":
                raise KeyError(f"active skill not found: {skill_id}")
            skill = dict(row)
            current = Path(skill["content_path"]).read_text(encoding="utf-8")
            if _hash(current) != skill["content_hash"] or expected_hash and expected_hash != skill["content_hash"]:
                raise ValueError("baseline changed; reevaluate before release")
            version = _next_patch(str(skill["current_version"]))
            while conn.execute("SELECT 1 FROM skill_versions WHERE skill_id=? AND version=?", (skill_id, version)).fetchone():
                version = _next_patch(version)
            meta, body = parse_frontmatter(content)
            meta.update(name=skill_id, version=version, status="active")
            rendered = format_frontmatter(meta, body)
            path = self._immutable_content(skill_id, rendered)
            conn.execute("UPDATE skill_versions SET content=?, content_hash=? WHERE skill_id=? AND version=?",
                         (current, _hash(current), skill_id, skill["current_version"]))
            conn.execute("UPDATE skills SET current_version=?, content_path=?, content_hash=?, updated_at=?, display_name=?, description=? "
                         "WHERE id=? AND content_hash=?", (version, str(path), _hash(rendered), now_ms(),
                         meta.get("title", skill["display_name"]), meta.get("description", skill["description"]), skill_id, skill["content_hash"]))
            conn.execute("INSERT INTO skill_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (f"skill_version_{uuid.uuid4().hex}", skill_id, version, rendered, _hash(rendered),
                          Database.json_dumps(evidence or []), now_ms()))
            if archive_candidate:
                conn.execute("UPDATE skills SET status='archived', updated_at=? WHERE id=? AND status='candidate'",
                             (now_ms(), archive_candidate))
        return self.get(skill_id) or {}

    def rollback(self, skill_id: str, version: str) -> dict[str, Any]:
        with self._release_lock:
            return self._rollback(skill_id, version)

    def _rollback(self, skill_id: str, version: str) -> dict[str, Any]:
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM skills WHERE id=? AND status='active'", (skill_id,)).fetchone()
            snapshot = conn.execute("SELECT content FROM skill_versions WHERE skill_id=? AND version=?", (skill_id, version)).fetchone()
            if not row or not snapshot:
                raise KeyError(f"skill version not found: {skill_id}@{version}")
            skill = dict(row)
            current = Path(skill["content_path"]).read_text(encoding="utf-8")
            if _hash(current) != skill["content_hash"]:
                raise ValueError("active skill content is corrupt")
            conn.execute("UPDATE skill_versions SET content=?, content_hash=? WHERE skill_id=? AND version=?",
                         (current, _hash(current), skill_id, skill["current_version"]))
            meta, body = parse_frontmatter(snapshot["content"])
            meta.update(name=skill_id, version=version, status="active")
            restored = format_frontmatter(meta, body)
            restored_path = self._immutable_content(skill_id, restored)
            conn.execute("UPDATE skills SET current_version=?, content_hash=?, updated_at=?, display_name=?, description=?, content_path=? "
                         "WHERE id=? AND content_hash=?", (version, _hash(restored), now_ms(),
                         meta.get("title", skill["display_name"]), meta.get("description", skill["description"]),
                         str(restored_path), skill_id, skill["content_hash"]))
        return self.get(skill_id) or {}

    def find_similar(self, text: str, threshold: float = 0.70) -> dict[str, Any] | None:
        best: tuple[float, dict[str, Any]] | None = None
        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        for skill in self.list("active"):
            candidate = f"{skill['display_name']} {skill['description']}".lower()
            score = SequenceMatcher(None, normalized, candidate).ratio()
            if score >= threshold and (best is None or score > best[0]):
                best = (score, skill)
        if not best:
            return None
        return {**best[1], "similarity": best[0]}
