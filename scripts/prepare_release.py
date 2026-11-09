"""Create a clean, allowlisted source snapshot. Never copy live runtime data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ("README.md", "LICENSE", "SECURITY.md", "THIRD_PARTY_NOTICES.md", ".env.example", ".gitignore", ".gitattributes",
              "pyproject.toml", "requirements.txt", "uv.lock", "main.py", "app.py")
PATTERNS = ("pocket_agent/**/*.py", "agent/*.py", "agent/prompts/*.py", "agent/tools/*.py", "tests/*.py",
            "examples/*.json", "examples/*.md", ".github/workflows/*.yml")
EXTRA_FILES = ("docs/UPGRADE_REPORT_0.3.md", "docs/UPGRADE_REPORT_0.4.md", "docs/ENGINEERING.md",
               "docs/VALIDATION_0.3.json", "docs/VALIDATION_0.4.json", "scripts/prepare_release.py", "scripts/smoke_live_model.py")
SECRET_PATTERNS = (r"sk-[A-Za-z0-9_-]{24,}", r"gh[pousr]_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{30,}",
                   r"AKIA[A-Z0-9]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def main():
    paths = {ROOT / path for path in (*ROOT_FILES, *EXTRA_FILES)}
    for pattern in PATTERNS:
        paths.update(ROOT.glob(pattern))
    files = []
    for path in sorted(paths):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"missing or unsafe release file: {path.name}")
        content = path.read_text(encoding="utf-8-sig")
        if any(re.search(pattern, content) for pattern in SECRET_PATTERNS):
            raise ValueError(f"possible credential found in {path.relative_to(ROOT)}; refusing publication")
        if re.search(r"[A-Z]:[/\\](?:Users|新项目)[/\\]", content):
            raise ValueError(f"machine-specific path found in {path.relative_to(ROOT)}")
        files.append(path)
    destination = ROOT / ".runtime" / "releases" / ("pocket-agent-evolve-" + uuid.uuid4().hex[:8])
    if not destination.resolve().is_relative_to(ROOT / ".runtime"):
        raise ValueError("release destination escaped workspace runtime")
    destination.mkdir(parents=True, exist_ok=False)
    manifest = []
    for path in files:
        relative = path.relative_to(ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Stable public bytes on Windows/Linux, matching .gitattributes and the manifest.
        with target.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        raw = target.read_bytes()
        manifest.append({"path": relative.as_posix(), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    public_manifest = {"version": "0.4.0", "files": manifest,
                       "scope": "Allowlisted source; runtime data and third-party assets excluded"}
    (destination / "RELEASE_MANIFEST.json").write_text(json.dumps(public_manifest, indent=2) + "\n", encoding="utf-8")
    private_manifest = {"root": str(destination), **public_manifest}
    (ROOT / ".runtime/release_manifest.json").write_text(json.dumps(private_manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"destination": str(destination), "source_files": len(manifest),
                      "bytes": sum(f["bytes"] for f in manifest), "credential_scan": "passed"}, indent=2))


if __name__ == "__main__":
    main()
