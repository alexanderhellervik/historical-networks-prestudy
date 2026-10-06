"""Provenance stamp attached to every derived table: input hashes, code commit, method version."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from histnet.paths import ROOT as REPO


def code_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        dirty = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "no-git"


@dataclass
class Provenance:
    stage: str
    method_version: str
    inputs: dict[str, str]  # relative raw path or upstream table -> sha256
    parameters: dict[str, object] = field(default_factory=dict)
    code_commit: str = field(default_factory=code_commit)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    def write(self, target: Path) -> Path:
        """Write `<target>.provenance.json` next to a derived file."""
        p = target.with_name(target.name + ".provenance.json")
        p.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return p
