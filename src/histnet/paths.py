"""Repository paths.

Inputs are downloaded into ``inputs/`` (see SOURCES.md); everything the pipeline writes goes
to ``work/``; ``data/`` holds the derived files shipped with the repository.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("HISTNET_ROOT", Path(__file__).resolve().parents[2]))
INPUTS = ROOT / "inputs"
WORK = ROOT / "work"
DATA = ROOT / "data"
PROMPTS = ROOT / "prompts"
WORKING_CRS = "EPSG:3006"  # SWEREF 99 TM, metres; recorded on every layer


def rel(path: Path | str) -> str:
    """A path relative to the repository root when possible (for provenance fields)."""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)
