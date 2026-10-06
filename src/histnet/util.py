"""Small shared helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from histnet.paths import WORKING_CRS


def sha256_file(path: Path | str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while b := fh.read(chunk):
            h.update(b)
    return h.hexdigest()


def require_crs(gdf: Any, crs: str = WORKING_CRS) -> Any:
    """Fail unless the layer carries an explicit CRS equal to ``crs`` (never assume one)."""
    if gdf.crs is None:
        raise ValueError("layer has no CRS; set it explicitly")
    if gdf.crs.to_string() != crs and gdf.crs.to_epsg() != int(crs.split(":")[1]):
        raise ValueError(f"layer CRS {gdf.crs} is not {crs}")
    return gdf
