from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from pocket_agent.storage.database import Database, now_ms


class ArtifactStore:
    def __init__(
        self,
        database: Database,
        root: str | Path,
        inline_threshold: int = 16 * 1024,
    ):
        self.database = database
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.inline_threshold = inline_threshold

    def save_bytes(
        self,
        data: bytes,
        *,
        kind: str,
        run_id: str = "",
        mime_type: str = "application/octet-stream",
        suffix: str = "",
    ) -> dict[str, Any]:
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        suffix = suffix or mimetypes.guess_extension(mime_type) or ".bin"
        run_dir = self.root / (run_id or "unscoped")
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{artifact_id}{suffix}"
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.database.execute(
            """
            INSERT INTO artifacts(
                id, run_id, kind, path, mime_type, sha256, size_bytes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                run_id or None,
                kind,
                str(path),
                mime_type,
                digest,
                len(data),
                now_ms(),
            ),
        )
        return {
            "id": artifact_id,
            "kind": kind,
            "path": str(path),
            "mime_type": mime_type,
            "sha256": digest,
            "size_bytes": len(data),
        }

    def save_text(
        self, text: str, *, kind: str, run_id: str = ""
    ) -> dict[str, Any]:
        return self.save_bytes(
            text.encode("utf-8"),
            kind=kind,
            run_id=run_id,
            mime_type="text/plain",
            suffix=".txt",
        )

    def save_json(
        self, value: Any, *, kind: str, run_id: str = ""
    ) -> dict[str, Any]:
        data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        return self.save_bytes(
            data,
            kind=kind,
            run_id=run_id,
            mime_type="application/json",
            suffix=".json",
        )

    def save_base64_image(
        self,
        encoded: str,
        *,
        run_id: str = "",
        mime_type: str = "image/png",
    ) -> dict[str, Any]:
        return self.save_bytes(
            base64.b64decode(encoded),
            kind="screenshot",
            run_id=run_id,
            mime_type=mime_type,
        )

    def externalize_if_large(
        self, value: Any, *, kind: str, run_id: str = ""
    ) -> tuple[Any, list[dict[str, Any]]]:
        if isinstance(value, str):
            size = len(value.encode("utf-8"))
            if size > self.inline_threshold:
                artifact = self.save_text(value, kind=kind, run_id=run_id)
                return (
                    f"[大型结果已保存为 Artifact: {artifact['path']}]",
                    [artifact],
                )
        return value, []
