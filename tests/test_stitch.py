"""Pipeline contract of the railway-band road map (experiments/area_band/plan.md)."""

from __future__ import annotations

from dataclasses import replace

import geopandas as gpd
from affine import Affine
from shapely.geometry import LineString

from histnet import maptiles as vr
from histnet import stitch as ab
from histnet.paths import WORKING_CRS


def _tile(tid: str, col: int, row: int) -> vr.TileSpec:
    """A 500 m x 500 m tile at (col, row) on a 1 m grid, enough for face_polygon."""
    t = Affine(1.0, 0, 350000.0, 0, -1.0, 6450000.0)
    cand = vr.Candidate(
        block=tid,
        split="test",
        col_off=col * 500,
        row_off=row * 500,
        width=500,
        height=500,
        draw=0.0,
        valid_fraction=1.0,
        road_length_m=0.0,
        junction_count=0,
        absent_uncertain_m=0.0,
    )
    spec = vr.TileSpec(
        tile_id=tid,
        split="test",
        block=tid,
        series="x",
        source_raster="x",
        source_sha256="x",
        col_off=cand.col_off,
        row_off=cand.row_off,
        width=500,
        height=500,
        display_w=1000,
        display_h=1000,
        canvas_offset=(0, 0),
        canvas_size=(1000, 1000),
        parent_transform=tuple(t)[:6],
        image_transform=tuple(
            vr.derive_image_transform(t, cand.col_off, cand.row_off, 500, 500, 1000, 1000)
        )[:6],
        crs=WORKING_CRS,
        grid_spacing_px=100,
        grid_spacing_m=(50.0, 50.0),
        ground_size_m=(500.0, 500.0),
        rights_note="",
        selection={},
        renders={},
        paired_tile_id=None,
    )
    return spec


def _roads(rows: list[tuple[str, str, list[tuple[float, float]]]]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"tile_id": [r[0] for r in rows], "product_feature_id": [r[1] for r in rows]},
        geometry=[LineString(r[2]) for r in rows],
        crs=WORKING_CRS,
    )


def test_seam_join_needs_mutual_nearest_ends_close_to_the_shared_edge() -> None:
    a, b = _tile("a", 0, 0), _tile("b", 1, 0)  # shared edge at x = 350500
    x = 350500.0
    y = 6449750.0
    roads = _roads(
        [
            ("a", "a:f1", [(350200, y), (x - 5, y)]),  # ends 5 m from the edge
            ("b", "b:f1", [(x + 8, y + 10), (350800, y + 10)]),  # 8 m from edge, ~13 m apart
            ("a", "a:f2", [(350200, y - 200), (x - 3, y - 200)]),  # no partner -> gap
            (
                "b",
                "b:f2",
                [(x + 60, y - 100), (350800, y - 100)],
            ),  # 60 m from edge: not a seam end
        ]
    )
    joins, gaps = ab.stitch(roads, [a, b])
    assert len(joins) == 1
    assert set(joins.iloc[0][["feature_a", "feature_b"]]) == {"a:f1", "b:f1"}
    assert list(gaps["feature"]) == ["a:f2"]


def test_seam_ends_farther_than_the_pair_distance_are_gaps_not_joins() -> None:
    a, b = _tile("a", 0, 0), _tile("b", 1, 0)
    x, y = 350500.0, 6449750.0
    roads = _roads(
        [
            ("a", "a:f1", [(350200, y), (x - 2, y)]),
            ("b", "b:f1", [(x + 2, y + 45), (350800, y + 45)]),  # 45 m apart > 40 m
        ]
    )
    joins, gaps = ab.stitch(roads, [a, b])
    assert len(joins) == 0
    assert sorted(gaps["feature"]) == ["a:f1", "b:f1"]


def test_tiles_meeting_only_at_a_corner_share_no_edge() -> None:
    edges = ab.shared_edges([_tile("a", 0, 0), _tile("b", 1, 1)])
    assert edges == []


def test_faces_overlapping_by_a_rounding_sliver_still_share_an_edge() -> None:
    a = _tile("a", 0, 0)
    b = replace(_tile("b", 0, 1), parent_transform=(1.0, 0, 350000.0, 0, -1.0, 6450000.0 + 1e-9))
    edges = ab.shared_edges([a, b])
    assert len(edges) == 1
    assert abs(edges[0][2].length - 500.0) < 0.1
