"""Stitching tiles: shared edges between neighbouring tiles and seam joins across them.

A line end within SEAM_EDGE_M of an edge shared with a neighbouring tile is joined to the
nearest line end of the neighbour within SEAM_PAIR_M when the two are each other's nearest;
unpaired ends near a shared edge are seam gaps. Nothing else is snapped or merged.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd

from histnet.maptiles import TileSpec
from histnet.paths import WORKING_CRS

SEAM_EDGE_M = 20.0  # a line end this close to a shared tile edge is a seam end


SEAM_PAIR_M = 40.0  # two seam ends on the same edge this close, mutually nearest, are joined


EDGE_TOL_M = 0.01  # neighbouring faces can overlap by a rounding sliver instead of meeting


def shared_edges(tiles: list[TileSpec]) -> list[tuple[str, str, Any]]:
    """(tile a, tile b, shared edge line) for every pair of band tiles that share an edge.

    Neighbouring faces computed from the same transform can overlap by a floating-point
    sliver instead of meeting on a line, so the edge is taken as the part of a's boundary
    within EDGE_TOL_M of b; tiles meeting only at a corner give a few centimetres and are
    not neighbours.
    """
    faces = {t.tile_id: t.face_polygon for t in tiles}
    out = []
    ids = sorted(faces)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            inter = faces[a].boundary.intersection(faces[b].buffer(EDGE_TOL_M))
            if not inter.is_empty and inter.length > 1.0:
                out.append((a, b, inter))
    return out


def line_ends(lines: gpd.GeoDataFrame) -> list[tuple[int, str, Any]]:
    from shapely.geometry import Point

    ends = []
    for idx, g in zip(lines.index, lines.geometry, strict=True):
        cs_ = list(g.coords)
        ends.append((idx, "start", Point(cs_[0])))
        ends.append((idx, "end", Point(cs_[-1])))
    return ends


def stitch(
    roads: gpd.GeoDataFrame, tiles: list[TileSpec]
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Seam joins and seam gaps by the plan's rule; nothing else is changed."""
    from shapely.geometry import LineString

    joins, gaps = [], []
    for a, b, edge in shared_edges(tiles):
        ends = {}
        for t in (a, b):
            sub = roads[roads["tile_id"] == t]
            ends[t] = [
                (i, which, p) for i, which, p in line_ends(sub) if p.distance(edge) <= SEAM_EDGE_M
            ]
        near_ab = {}
        for key, other in ((a, b), (b, a)):
            for i, which, p in ends[key]:
                cands = [(p.distance(q), j, w2, q) for j, w2, q in ends[other]]
                cands = [c for c in cands if c[0] <= SEAM_PAIR_M]
                near_ab[(key, i, which)] = min(cands, key=lambda c: c[0]) if cands else None
        paired = set()
        for i, which, p in ends[a]:
            m = near_ab[(a, i, which)]
            if m is None:
                continue
            d, j, w2, q = m
            back = near_ab.get((b, j, w2))
            if back is not None and back[1] == i and back[2] == which:
                joins.append(
                    {
                        "tile_a": a,
                        "tile_b": b,
                        "feature_a": roads.loc[i, "product_feature_id"],
                        "feature_b": roads.loc[j, "product_feature_id"],
                        "gap_m": round(d, 2),
                        "kind": "seam_join",
                        "geometry": LineString([p, q]),
                    }
                )
                paired.add((a, i, which))
                paired.add((b, j, w2))
        for t, other in ((a, b), (b, a)):
            for i, which, p in ends[t]:
                if (t, i, which) not in paired:
                    gaps.append(
                        {
                            "tile_id": t,
                            "neighbour": other,
                            "feature": roads.loc[i, "product_feature_id"],
                            "end": which,
                            "distance_to_edge_m": round(p.distance(edge), 2),
                            "kind": "seam_gap",
                            "geometry": p,
                        }
                    )
    j = (
        gpd.GeoDataFrame(joins, geometry="geometry", crs=WORKING_CRS)
        if joins
        else gpd.GeoDataFrame({"kind": []}, geometry=gpd.GeoSeries([], crs=WORKING_CRS))
    )
    g = (
        gpd.GeoDataFrame(gaps, geometry="geometry", crs=WORKING_CRS)
        if gaps
        else gpd.GeoDataFrame({"kind": []}, geometry=gpd.GeoSeries([], crs=WORKING_CRS))
    )
    return j, g
