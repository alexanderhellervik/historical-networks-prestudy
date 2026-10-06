"""One namespace for the tile and answer functions the pipeline modules share."""

from __future__ import annotations

from histnet.answers import (
    VISION_COARSE,
    _read_pass_a,
    _reviewed_product,
    _with_coarse,
    extract_json_object,
    normalise_pass_a,
    pass_a_candidate_records,
    validate_candidates,
)
from histnet.maptiles import (
    MANIFEST_PATH,
    SERIES_1890S,
    TILES_DIR,
    Candidate,
    TileSpec,
    _make_tile,
    _render_all,
    read_manifest,
    render_overlay,
    repo_relative,
    tile_by_id,
)

__all__ = [
    "MANIFEST_PATH", "SERIES_1890S", "TILES_DIR", "VISION_COARSE", "Candidate", "TileSpec",
    "_make_tile", "_read_pass_a", "_render_all", "_reviewed_product", "_with_coarse",
    "extract_json_object", "normalise_pass_a", "pass_a_candidate_records", "read_manifest",
    "render_overlay", "repo_relative", "tile_by_id", "validate_candidates",
]  # fmt: skip
