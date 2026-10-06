"""Accessibility demonstration: routable networks for the 1890s and today.

Declared in ``docs/accessibility_demo/plan.md``. Builds a routable graph per period,
connects the corridor's core places, and computes travel time to Alingsås and a potential
accessibility index weighted by each period's own population (owner decision of 2026-10-01,
logged in docs/adaptations.md). A feasibility demonstration, not a historical accessibility
series and not an analysis of population response.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point
from shapely.ops import substring

from histnet.paths import DATA, WORK, WORKING_CRS

STAGE = "exp-accessibility-demo"
METHOD_VERSION = "accessibility-demo-0.1"
PLAN_PATH = WORK / "notes" / "accessibility_demo" / "plan.md"
OUT_DIR = WORK / "accessibility_demo"
RESULTS_PATH = WORK / "notes" / "accessibility_demo" / "results.md"
CROSSWALK = DATA / "crosswalk_folknet_histmaps.csv"  # unit keys only, no population
MODERN_DIR = DATA / "modern_network"  # OpenStreetMap-derived, ODbL
ROAD_KMH_1890S = 6.0
CONNECTOR_KMH = 5.0
MAX_CONNECTOR_M = 2000.0  # farther from the network: not connected in that period
SNAP_M = 40.0  # corridor 1890s network (plan addenda); was 15 m on the band
TAU_MIN = 30.0
ALINGSAS = "folknet:15:Stad:ALINGSÅS STAD"
PLACE_LEVELS = ("parish", "stad")
REF_YEAR = 1890
PERIODS = {
    "1890s": 1890,
    "1960s": 1960,
    "1960s_walk": 1960,
    "1960s_walk_novgj": 1960,
    "today": 1990,
}


# --------------------------------------------------------------------------------------
# Places and population
# --------------------------------------------------------------------------------------


def places() -> gpd.GeoDataFrame:
    """Core parishes and Alingsås town: representative points of their 1890 polygons."""
    from histnet.histmaps import load_histmaps_units

    cw = pd.read_csv(CROSSWALK, dtype=str)
    cw = cw[(cw["domain_role"] == "core") & cw["level"].isin(PLACE_LEVELS)]
    units = load_histmaps_units()
    v = units[(units["start"] <= REF_YEAR) & (units["end"] >= REF_YEAR)]
    v = v[v["ref_code"].isin(cw["histmaps_ref_code"])].drop_duplicates("ref_code")
    g = cw.merge(v[["ref_code", "geometry"]], left_on="histmaps_ref_code", right_on="ref_code")
    g = gpd.GeoDataFrame(g, geometry="geometry", crs=WORKING_CRS)
    g["polygon"] = g.geometry
    g["geometry"] = g.geometry.representative_point()
    g = g.rename(columns={"folknet_unit_key": "place"})
    cols = ["place", "folknet_namn", "level", "histmaps_ref_code", "geometry", "polygon"]
    return g[cols].sort_values("place").reset_index(drop=True)


def population(year: int, keys: list[str]) -> pd.Series:
    """FOLKNET population of the given units in ``year`` (from your own FOLKNET download)."""
    from histnet.population import population as folknet_population

    return folknet_population(year, keys)


# --------------------------------------------------------------------------------------
# Graphs
# --------------------------------------------------------------------------------------


def _key(x: float, y: float) -> tuple[float, float]:
    return (round(x, 1), round(y, 1))


def graph_from_lines(
    lines: list[LineString], speed_kmh: float
) -> tuple[np.ndarray, list[tuple[int, int, float, float]]]:
    """Nodes at line vertices' ends; one edge per line piece, cost in minutes."""
    index: dict[tuple[float, float], int] = {}
    edges = []
    for ln in lines:
        if ln.length <= 0:
            continue
        a, b = _key(*ln.coords[0]), _key(*ln.coords[-1])
        ia = index.setdefault(a, len(index))
        ib = index.setdefault(b, len(index))
        edges.append((ia, ib, ln.length, ln.length / 1000 / speed_kmh * 60))
    xy = np.array(sorted(index, key=index.get)) if index else np.zeros((0, 2))
    return xy, edges


def snap_ends(lines: list[LineString], snap_m: float = SNAP_M) -> tuple[list[LineString], int]:
    """Join each line end within snap_m of another line to it, splitting that line there."""
    import shapely

    tree = shapely.STRtree(lines)
    cuts: dict[int, list[float]] = {}
    connectors: list[LineString] = []
    for i, ln in enumerate(lines):
        for end in (Point(ln.coords[0]), Point(ln.coords[-1])):
            best = None
            for j in tree.query(end.buffer(snap_m)):
                if j == i:
                    continue
                d = lines[j].distance(end)
                if d <= snap_m and (best is None or d < best[0]):
                    best = (d, j)
            if best is None:
                continue
            d, j = best
            t = lines[j].project(end)
            other_end = min(
                (Point(lines[j].coords[0]), Point(lines[j].coords[-1])), key=end.distance
            )
            if other_end.distance(end) <= d + 1e-6:  # nearest is an end: join ends
                if other_end.distance(end) > 0.05:
                    connectors.append(LineString([end, other_end]))
                continue
            cuts.setdefault(int(j), []).append(t)
            p = lines[j].interpolate(t)
            if p.distance(end) > 0.05:
                connectors.append(LineString([end, p]))
    # crossings: two lines that cross are joined at the crossing (both split there)
    for i, ln in enumerate(lines):
        for j in tree.query(ln):
            if j <= i:
                continue
            x = ln.intersection(lines[j])
            pts = (
                [x]
                if x.geom_type == "Point"
                else [g for g in getattr(x, "geoms", []) if g.geom_type == "Point"]
            )
            for p in pts:
                cuts.setdefault(i, []).append(ln.project(p))
                cuts.setdefault(int(j), []).append(lines[j].project(p))
    out: list[LineString] = []
    for i, ln in enumerate(lines):
        ts = sorted({0.0, *cuts.get(i, []), ln.length})
        out += [substring(ln, a, b) for a, b in zip(ts[:-1], ts[1:], strict=True) if b - a > 0.01]
    return out + connectors, len(connectors)


def graph_1890s(
    roads: gpd.GeoDataFrame, joins: gpd.GeoDataFrame, repairs: gpd.GeoDataFrame | None = None
) -> dict[str, Any]:
    """Routable 1890s graph: the largest connected component (plan addendum)."""
    from scipy.sparse.csgraph import connected_components

    lines = [g for g in roads.geometry if g is not None and not g.is_empty]
    lines += [g for g in joins.geometry] if len(joins) else []
    if repairs is not None and len(repairs):
        lines += [g for g in repairs.geometry]
    pieces, n_snap = snap_ends(lines)
    xy, edges = graph_from_lines(pieces, ROAD_KMH_1890S)
    u, v = np.array([e[0] for e in edges]), np.array([e[1] for e in edges])
    m = coo_matrix((np.ones(len(u)), (u, v)), shape=(len(xy), len(xy)))
    _n, lab = connected_components(m, directed=False)
    length = np.bincount(lab[u], weights=np.array([e[2] for e in edges]), minlength=_n)
    main = int(np.argmax(length))
    keep = [i for i, e in enumerate(edges) if lab[e[0]] == main]
    nodes = np.unique(np.concatenate([u[keep], v[keep]]))
    remap = -np.ones(len(xy), dtype=np.int64)
    remap[nodes] = np.arange(len(nodes))
    edges_main = [
        (int(remap[edges[i][0]]), int(remap[edges[i][1]]), edges[i][2], edges[i][3]) for i in keep
    ]
    return {
        "xy": xy[nodes], "edges": edges_main, "pieces": [pieces[i] for i in keep],
        "all_pieces": pieces, "snap_connectors": n_snap, "components": int(_n),
        "main_km": float(length[main] / 1000), "total_km": float(length.sum() / 1000),
    }  # fmt: skip


def graph_modern() -> dict[str, Any]:
    n = pd.read_parquet(MODERN_DIR / "nodes.parquet", columns=["node_id", "x", "y"])
    e = pd.read_parquet(MODERN_DIR / "edges.parquet", columns=["u", "v", "cost_min", "length_m"])
    idx = pd.Series(np.arange(len(n)), index=n["node_id"].to_numpy())
    e = e[e["u"].isin(idx.index) & e["v"].isin(idx.index)]
    edges = list(
        zip(
            idx[e["u"]].to_numpy(), idx[e["v"]].to_numpy(),
            e["length_m"].to_numpy(), e["cost_min"].to_numpy(), strict=True,
        )
    )  # fmt: skip
    return {"xy": n[["x", "y"]].to_numpy(), "edges": edges, "node_ids": n["node_id"].to_numpy()}


def travel_times(
    g: dict[str, Any], pts: gpd.GeoDataFrame
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Place-to-place minutes (undirected), connector lengths and snapped node per place."""
    xy = g["xy"]
    tree = cKDTree(xy)
    d, node = tree.query(np.column_stack([pts.geometry.x, pts.geometry.y]))
    conn_min = d / 1000 / CONNECTOR_KMH * 60
    u, v, _l, c = (np.array(x) for x in zip(*g["edges"], strict=True))
    n = len(xy)
    m = coo_matrix(
        (np.concatenate([c, c]), (np.concatenate([u, v]), np.concatenate([v, u]))), shape=(n, n)
    ).tocsr()
    dist = dijkstra(m, directed=False, indices=node)
    tt = dist[:, node] + conn_min[:, None] + conn_min[None, :]
    far = d > MAX_CONNECTOR_M
    tt[far, :] = np.inf
    tt[:, far] = np.inf
    np.fill_diagonal(tt, 0.0)
    return tt, d, node


def accessibility(tt: np.ndarray, pop: np.ndarray) -> np.ndarray:
    w = np.exp(-tt / TAU_MIN)
    np.fill_diagonal(w, 0.0)
    w[~np.isfinite(tt)] = 0.0
    return w @ np.nan_to_num(pop)


# --------------------------------------------------------------------------------------
# 250 m accessibility surface (plan addendum of 2026-10-01)
# --------------------------------------------------------------------------------------

CELL_SURF_M = 250.0
FLOOR_M = 50.0
BASE_RADIUS_MIN = 10.0
EXACT_SAMPLE = 200
EXACT_SEED = 20261001
ZONE_OFFSET = 10**12  # zone node ids live above every network node id


def surface_cells(pts: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """250 m cells whose centre lies in a linked parish's 1890 polygon."""
    area = gpd.GeoSeries(list(pts.loc[pts["level"] == "parish", "polygon"]), crs=WORKING_CRS)
    union = area.union_all()
    x0, y0, x1, y1 = union.bounds
    xs = np.arange(np.floor(x0 / CELL_SURF_M), np.ceil(x1 / CELL_SURF_M)) * CELL_SURF_M
    ys = np.arange(np.floor(y0 / CELL_SURF_M), np.ceil(y1 / CELL_SURF_M)) * CELL_SURF_M
    gx, gy = np.meshgrid(xs + CELL_SURF_M / 2, ys + CELL_SURF_M / 2)
    cells = gpd.GeoDataFrame(geometry=gpd.points_from_xy(gx.ravel(), gy.ravel()), crs=WORKING_CRS)
    cells = cells[cells.within(union)].reset_index(drop=True)
    cells["cell"] = [
        f"c{int(x)}_{int(y)}" for x, y in zip(cells.geometry.x, cells.geometry.y, strict=True)
    ]
    cells["kind"] = "cell"
    return cells


def cell_road_length(cells: gpd.GeoDataFrame, segs: np.ndarray) -> np.ndarray:
    """Road length per cell, each segment's length assigned to the cell of its midpoint.

    ``segs`` is (n, 4): x0, y0, x1, y1.
    """
    mx = (segs[:, 0] + segs[:, 2]) / 2
    my = (segs[:, 1] + segs[:, 3]) / 2
    ln = np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])
    key = (np.floor(mx / CELL_SURF_M).astype(np.int64) * 10**7) + np.floor(
        my / CELL_SURF_M
    ).astype(np.int64)
    ck = (np.floor(cells.geometry.x / CELL_SURF_M).astype(np.int64) * 10**7) + np.floor(
        cells.geometry.y / CELL_SURF_M
    ).astype(np.int64)
    s = pd.Series(ln).groupby(key).sum()
    return s.reindex(ck.to_numpy()).fillna(0.0).to_numpy()


def disaggregate(
    cells: gpd.GeoDataFrame, road_m: np.ndarray, pts: gpd.GeoDataFrame, pop: pd.Series
) -> np.ndarray:
    """Parish population split over the parish's cells by road length + floor; totals kept.

    Towns are separate zones (see :func:`write_surface_inputs`).
    """
    poly = gpd.GeoDataFrame(
        {"place": pts["place"], "level": pts["level"]}, geometry=pts["polygon"], crs=WORKING_CRS
    )
    parishes = poly[poly["level"] == "parish"]
    j = gpd.sjoin(cells[["cell", "geometry"]], parishes, predicate="within", how="left")
    j = j[~j.index.duplicated()]
    w = road_m + FLOOR_M
    out = np.zeros(len(cells))
    for place, idx in j.groupby("place").groups.items():
        ii = np.asarray(list(idx))
        total = float(pop.get(place, np.nan))
        if np.isfinite(total):
            out[ii] = total * w[ii] / w[ii].sum()
    return out


def surface_graph(period: str, cells: gpd.GeoDataFrame) -> dict[str, Any]:
    """Network edges (u, v, cost min), segments, and one zone node per connected cell."""
    if period == "1890s":
        roads, joins, repairs = corridor_roads()
        g = graph_1890s(roads, joins, repairs)
        xy, edges = g["xy"], g["edges"]
        node_ids = np.arange(len(xy), dtype=np.int64)
        u, v, _l, c = (np.array(x) for x in zip(*edges, strict=True))
        segs = np.column_stack([xy[u], xy[v]])
    elif period.startswith("1960s"):
        n = pd.read_parquet(
            MODERN_DIR / "nodes.parquet", columns=["node_id", "x", "y", "in_graph_ring"]
        )
        n = n[n["in_graph_ring"]]
        e = pd.read_parquet(WORK / "network_1960s" / "edges_1960s.parquet")
        # no modern railways (owner decision): roads only, plus the VGJ in the walking variant
        keep_modes = (
            ["road", "rail_vgj_1963", "station_access"] if period == "1960s_walk" else ["road"]
        )
        e = e[e["mode"].isin(keep_modes)]
        if "_walk" in period:  # every road at the 1890s speed; the VGJ at its timetable times
            road_e = (e["mode"] == "road").to_numpy()
            e = e.copy()
            e.loc[road_e, "cost_min"] = e.loc[road_e, "length_m"] / 1000 / ROAD_KMH_1890S * 60
        ids = pd.unique(np.concatenate([e["u"].to_numpy(), e["v"].to_numpy()])).astype(np.int64)
        pos = n.set_index("node_id")[["x", "y"]]
        node_ids = ids
        has_xy = np.isin(ids, pos.index.to_numpy())
        idx = pd.Series(np.arange(len(ids)), index=ids)
        u, v = idx[e["u"]].to_numpy(), idx[e["v"]].to_numpy()
        c = e["cost_min"].to_numpy()
        road = (e["mode"] == "road").to_numpy()
        segs = np.column_stack(
            [pos.loc[e["u"][road]].to_numpy(), pos.loc[e["v"][road]].to_numpy()]
        )
        xy = np.full((len(ids), 2), np.nan)
        xy[has_xy] = pos.loc[ids[has_xy]].to_numpy()
    else:
        n = pd.read_parquet(
            MODERN_DIR / "nodes.parquet", columns=["node_id", "x", "y", "in_graph_ring"]
        )
        n = n[n["in_graph_ring"]]
        e = pd.read_parquet(MODERN_DIR / "edges.parquet", columns=["u", "v", "cost_min", "mode"])
        e = e[e["u"].isin(n["node_id"]) & e["v"].isin(n["node_id"]) & (e["mode"] == "road")]
        pos = n.set_index("node_id")[["x", "y"]]
        node_ids = n["node_id"].to_numpy(np.int64)
        xy = n[["x", "y"]].to_numpy()
        u_id, v_id = e["u"].to_numpy(np.int64), e["v"].to_numpy(np.int64)
        c = e["cost_min"].to_numpy()
        road = (e["mode"] == "road").to_numpy()
        segs = np.column_stack([pos.loc[u_id[road]].to_numpy(), pos.loc[v_id[road]].to_numpy()])
        idx = pd.Series(np.arange(len(n)), index=node_ids)
        u, v = idx[u_id].to_numpy(), idx[v_id].to_numpy()
    # zones connect only to nodes of the largest component (plan addendum); virtual station
    # nodes have no position and are never a connection point
    from scipy.sparse.csgraph import connected_components

    nn = len(xy)
    adj = coo_matrix((np.ones(len(u)), (np.asarray(u), np.asarray(v))), shape=(nn, nn))
    _nc, lab = connected_components(adj, directed=False)
    main = np.argmax(np.bincount(lab))
    ok_xy = np.flatnonzero(np.isfinite(xy[:, 0]) & (lab == main))
    tree = cKDTree(xy[ok_xy])
    d, near = tree.query(np.column_stack([cells.geometry.x, cells.geometry.y]))
    near = ok_xy[near]
    ok = d <= MAX_CONNECTOR_M
    return {
        "u": node_ids[u], "v": node_ids[v], "cost": c, "segs": segs,
        "zone_ok": ok, "zone_node": node_ids[near], "connector_min": d / 1000 / CONNECTOR_KMH * 60,
        "connector_m": d,
    }  # fmt: skip


def write_surface_inputs(period: str) -> dict[str, Any]:
    pts = places()
    pop = population(PERIODS[period], pts["place"].tolist())
    cells = surface_cells(pts)
    towns = pts[pts["level"] == "stad"]
    tz = gpd.GeoDataFrame(
        {"cell": towns["place"].to_numpy(), "kind": "town"},
        geometry=list(towns.geometry),
        crs=WORKING_CRS,
    )
    zones = gpd.GeoDataFrame(pd.concat([cells, tz], ignore_index=True), crs=WORKING_CRS)
    g = surface_graph(period, zones)
    is_cell = (zones["kind"] == "cell").to_numpy()
    road_m = np.zeros(len(zones))
    road_m[is_cell] = cell_road_length(zones[is_cell], g["segs"])
    popv = np.zeros(len(zones))
    popv[is_cell] = disaggregate(zones[is_cell].reset_index(drop=True), road_m[is_cell], pts, pop)
    popv[~is_cell] = [float(np.nan_to_num(pop.get(k, 0.0))) for k in zones.loc[~is_cell, "cell"]]
    zones["population"] = popv
    zones["road_m"] = road_m
    zones["connected"] = g["zone_ok"]
    zones["connector_m"] = g["connector_m"]
    zones["zone_id"] = ZONE_OFFSET + np.arange(len(zones), dtype=np.int64)
    z = zones[zones["connected"]]
    zi = np.flatnonzero(g["zone_ok"])
    edges = pd.DataFrame(
        {
            "u": np.concatenate([g["u"], z["zone_id"].to_numpy()]),
            "v": np.concatenate([g["v"], g["zone_node"][zi]]),
            "cost": np.concatenate([g["cost"], g["connector_min"][zi]]),
        }
    )
    d = OUT_DIR / f"surface_{period}"
    d.mkdir(parents=True, exist_ok=True)
    edges.to_parquet(d / "edges.parquet", index=False)
    z[["zone_id", "population"]].to_parquet(d / "zones.parquet", index=False)
    zones.drop(columns="geometry").assign(x=zones.geometry.x, y=zones.geometry.y).to_parquet(
        d / "cells.parquet", index=False
    )
    return {
        "period": period, "cells": int(is_cell.sum()), "towns": int((~is_cell).sum()),
        "connected": int(zones.connected.sum()),
        "population_in_zones": round(float(zones.population.sum())),
        "population_linked_units": round(float(np.nansum(pop.to_numpy()))),
        "edges": len(edges),
    }  # fmt: skip


def exact_sample(period: str) -> dict[str, Any]:
    """Plain shortest paths from a seeded sample of origin zones: exact A_i and timing."""
    d = OUT_DIR / f"surface_{period}"
    e = pd.read_parquet(d / "edges.parquet")
    z = pd.read_parquet(d / "zones.parquet")
    ids = pd.unique(np.concatenate([e["u"].to_numpy(), e["v"].to_numpy()]))
    idx = pd.Series(np.arange(len(ids)), index=ids)
    n = len(ids)
    a, b, c = idx[e["u"]].to_numpy(), idx[e["v"]].to_numpy(), e["cost"].to_numpy()
    m = coo_matrix(
        (np.concatenate([c, c]), (np.concatenate([a, b]), np.concatenate([b, a]))), shape=(n, n)
    ).tocsr()
    rng = np.random.default_rng(EXACT_SEED)
    sample = rng.choice(len(z), size=min(EXACT_SAMPLE, len(z)), replace=False)
    zi = idx[z["zone_id"]].to_numpy()
    t0 = time.perf_counter()
    dist = dijkstra(m, directed=False, indices=zi[sample])
    secs = time.perf_counter() - t0
    tt = dist[:, zi]
    w = np.exp(-tt / TAU_MIN)
    w[np.arange(len(sample)), sample] = 0.0
    exact = w @ z["population"].to_numpy()
    out = pd.DataFrame({"zone_id": z["zone_id"].to_numpy()[sample], "A_exact": exact})
    out.to_parquet(d / "exact_sample.parquet", index=False)
    return {"period": period, "sample": len(sample), "seconds": secs,
            "seconds_per_origin": secs / len(sample)}  # fmt: skip


def exact_all(period: str, chunk: int = 100) -> dict[str, Any]:
    """Plain shortest paths from every origin zone, in chunks: exact A_i for all zones."""
    d = OUT_DIR / f"surface_{period}"
    e = pd.read_parquet(d / "edges.parquet")
    z = pd.read_parquet(d / "zones.parquet")
    ids = pd.unique(np.concatenate([e["u"].to_numpy(), e["v"].to_numpy()]))
    idx = pd.Series(np.arange(len(ids)), index=ids)
    n = len(ids)
    a, b, c = idx[e["u"]].to_numpy(), idx[e["v"]].to_numpy(), e["cost"].to_numpy()
    m = coo_matrix(
        (np.concatenate([c, c]), (np.concatenate([a, b]), np.concatenate([b, a]))), shape=(n, n)
    ).tocsr()
    zi = idx[z["zone_id"]].to_numpy()
    pop = z["population"].to_numpy()
    out = np.zeros(len(z))
    t0 = time.perf_counter()
    for s0 in range(0, len(z), chunk):
        rows = np.arange(s0, min(s0 + chunk, len(z)))
        tt = dijkstra(m, directed=False, indices=zi[rows])[:, zi]
        w = np.exp(-tt / TAU_MIN)
        w[np.arange(len(rows)), rows] = 0.0
        out[rows] = w @ pop
    secs = time.perf_counter() - t0
    pd.DataFrame({"zone_id": z["zone_id"].to_numpy(), "A_exact": out}).to_parquet(
        d / "exact_all.parquet", index=False
    )
    return {"period": period, "zones": len(z), "seconds": secs}


# --------------------------------------------------------------------------------------
# Relative scores and maps
# --------------------------------------------------------------------------------------

HIERX_SETTING = "r10_o2"  # base radius 10 min, overlap 2.0 (plan addendum, owner decision)
FIG_DIR = OUT_DIR / "figures"
BADGES = {
    "1890s": "Roads read from Häradsekonomiska kartan (1890s) · map origin: Lantmäteriet",
    "1960s": "Roads: © OpenStreetMap contributors (ODbL), checked against Ekonomiska kartan "
    "1963–74 · map origin: Lantmäteriet",
    "1960s_walk": "Roads: © OpenStreetMap contributors (ODbL), checked against Ekonomiska kartan "
    "1963–74 · map origin: Lantmäteriet",
    "1960s_walk_novgj": "Roads: © OpenStreetMap contributors (ODbL), checked against Ekonomiska "
    "kartan 1963–74 · map origin: Lantmäteriet",
    "today": "Network: © OpenStreetMap contributors (ODbL)",
}
TITLES = {
    "1890s": "1890s on foot (6 km/h): roads read from the map; population 1890",
    "1960s": "1960s by car (modern speeds by road class): roads on the 1960s map; population 1960",
    "1960s_walk": "1960s on foot (6 km/h) with the VGJ (1963 timetable); population 1960",
    "1960s_walk_novgj": "1960s on foot (6 km/h), no railway; population 1960",
    "today": "Today by car (speeds by road class): OpenStreetMap roads; population 1990",
}


GREY_NOTE = {
    "default": "grey: more than 2 km from the period's routed network",
    "1890s": "grey: more than 2 km from the 1890s network (nearly all outside the area read)",
}


def surface_dir(period: str, setting: str | None = None) -> Path:
    return OUT_DIR / (f"surface_{period}" + (f"_{setting}" if setting else ""))


def relative_scores(period: str) -> pd.DataFrame:
    """Per zone: HierX A, exact A where computed, and A / 99th percentile (capped at 1)."""
    base = surface_dir(period)
    cells = pd.read_parquet(base / "cells.parquet")
    h = pd.read_parquet(surface_dir(period, HIERX_SETTING) / "hierx_result.parquet")
    df = cells.merge(h, on="zone_id", how="left")
    ex = base / "exact_all.parquet"
    if ex.is_file():
        df = df.merge(pd.read_parquet(ex), on="zone_id", how="left")
    for col in [c for c in ("A_hierx", "A_exact") if c in df.columns]:
        q = np.nanquantile(df.loc[df["kind"] == "cell", col], 0.99)
        df[col.replace("A_", "rel_")] = np.minimum(df[col] / q, 1.0)
    return df


def hierx_surface(period: str, base_radius_min: float = 10.0, overlap: float = 2.0) -> dict:
    """A_i with HierX 0.1.1 for every zone of a surface (in-process; hierx is a dependency)."""
    import math

    import hierx
    import networkx as nx

    d = OUT_DIR / f"surface_{period}"
    e = pd.read_parquet(d / "edges.parquet")
    z = pd.read_parquet(d / "zones.parquet")
    t0 = time.perf_counter()
    g = nx.Graph()
    g.add_weighted_edges_from(
        zip(e["u"].tolist(), e["v"].tolist(), e["cost"].tolist(), strict=True), weight="cost"
    )
    h = hierx.Hierarchy(
        g, base_radius=base_radius_min, overlap_factor=overlap, zones=z["zone_id"].tolist()
    )
    ih = hierx.InteractionHierarchy(h, lambda c: math.exp(-c / TAU_MIN), self_interaction=False)
    order = list(h.zones)
    pos = {zid: i for i, zid in enumerate(order)}
    w = np.zeros(len(order))
    for zid, p in zip(z["zone_id"].tolist(), z["population"].tolist(), strict=True):
        w[pos[zid]] = p
    a = ih.matvec(w)
    out_dir = surface_dir(period, HIERX_SETTING)
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"zone_id": order, "A_hierx": np.asarray(a, dtype=float)}).to_parquet(
        out_dir / "hierx_result.parquet", index=False
    )
    meta = {"hierx_version": hierx.__version__, "base_radius_min": base_radius_min,
            "overlap_factor": overlap, "zones": len(order),
            "seconds": time.perf_counter() - t0}  # fmt: skip
    (out_dir / "hierx_meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


def alingsas_times(period: str) -> pd.DataFrame:
    """Minutes from Alingsås town to the cell at each parish's representative point.

    Population-free; on the same graph as the period's surface (road only, plus the VGJ in the
    1960s walking variant). Undirected shortest paths from the town's zone.
    """
    d = OUT_DIR / f"surface_{period}"
    e = pd.read_parquet(d / "edges.parquet")
    cells = pd.read_parquet(d / "cells.parquet")
    ids = pd.unique(np.concatenate([e["u"].to_numpy(), e["v"].to_numpy()]))
    idx = pd.Series(np.arange(len(ids)), index=ids)
    a, b, c = idx[e["u"]].to_numpy(), idx[e["v"]].to_numpy(), e["cost"].to_numpy()
    n = len(ids)
    m = coo_matrix(
        (np.concatenate([c, c]), (np.concatenate([a, b]), np.concatenate([b, a]))), shape=(n, n)
    ).tocsr()
    town = cells[(cells["kind"] == "town") & (cells["cell"] == ALINGSAS)]
    if town.empty or not bool(town["connected"].iloc[0]):
        return pd.DataFrame()
    dist = dijkstra(m, directed=False, indices=int(idx[int(town["zone_id"].iloc[0])]))
    pts = places()
    grid = cells[(cells["kind"] == "cell") & cells["connected"].astype(bool)]
    tree = cKDTree(grid[["x", "y"]].to_numpy())
    rows = []
    for r in pts[pts["level"] == "parish"].itertuples():
        dd, i = tree.query((r.geometry.x, r.geometry.y))
        z = int(grid["zone_id"].iloc[i])
        rows.append({"period": period, "place": r.place, "name": r.folknet_namn.title(),
                     "minutes": float(dist[idx[z]]) if dd <= CELL_SURF_M else np.nan})  # fmt: skip
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------


TIME_TABLE_PERIODS = ("1890s", "1960s_walk_novgj", "1960s_walk", "1960s", "today")


def travel_time_table() -> pd.DataFrame:
    """Minutes to Alingsås per parish and period, for the periods whose surface inputs exist."""
    have = [p for p in TIME_TABLE_PERIODS if (OUT_DIR / f"surface_{p}" / "edges.parquet").exists()]
    t = pd.concat([alingsas_times(p) for p in have], ignore_index=True)
    t.to_parquet(OUT_DIR / "alingsas_times.parquet", index=False)
    w = t.pivot_table(index="name", columns="period", values="minutes")
    return w[[p for p in TIME_TABLE_PERIODS if p in w.columns]].sort_values(w.columns[-1])


def draw_surface(period: str, df: pd.DataFrame, col: str = "rel_hierx") -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pts = places()
    cells = df[df["kind"] == "cell"]
    x0, y0 = cells["x"].min(), cells["y"].min()
    nx_, ny_ = (
        int(round((cells["x"].max() - x0) / CELL_SURF_M)) + 1,
        int(round((cells["y"].max() - y0) / CELL_SURF_M)) + 1,
    )
    grid = np.full((ny_, nx_), np.nan)
    conn = np.zeros((ny_, nx_), bool)
    ix = np.round((cells["x"] - x0) / CELL_SURF_M).astype(int)
    iy = np.round((cells["y"] - y0) / CELL_SURF_M).astype(int)
    grid[iy, ix] = cells[col]
    conn[iy, ix] = cells["connected"].astype(bool)
    inside = np.zeros((ny_, nx_), bool)
    inside[iy, ix] = True
    extent = [x0 - CELL_SURF_M / 2, x0 + (nx_ - 0.5) * CELL_SURF_M,
              y0 - CELL_SURF_M / 2, y0 + (ny_ - 0.5) * CELL_SURF_M]  # fmt: skip
    fig, ax = plt.subplots(figsize=(8.5, 9.5))
    grey = np.where(inside & ~conn, 1.0, np.nan)
    ax.imshow(
        grey, origin="lower", extent=extent, cmap="Greys", vmin=0, vmax=4, interpolation="nearest"
    )
    im = ax.imshow(
        np.where(conn, grid, np.nan), origin="lower", extent=extent, cmap="magma",
        vmin=0, vmax=1, interpolation="nearest",
    )  # fmt: skip
    poly = gpd.GeoSeries(list(pts.loc[pts["level"] == "parish", "polygon"]), crs=WORKING_CRS)
    poly.boundary.plot(ax=ax, color="white", linewidth=0.4, alpha=0.6)
    import matplotlib.patheffects as pe

    halo = [pe.withStroke(linewidth=1.6, foreground="black")]
    for r in pts.itertuples():
        if r.level == "parish":
            p = r.polygon.representative_point()
            ax.text(p.x, p.y, r.folknet_namn.title(), fontsize=5.5, ha="center", color="white",
                    path_effects=halo)  # fmt: skip
    town = pts[pts["place"] == ALINGSAS].geometry.iloc[0]
    ax.scatter(
        [town.x], [town.y], s=40, marker="s", facecolor="white", edgecolor="black", zorder=5
    )
    ax.annotate("Alingsås", (town.x, town.y), xytext=(0, 7), textcoords="offset points",
                ha="center",
                fontsize=7, fontweight="bold", color="white", path_effects=halo)  # fmt: skip
    cb = fig.colorbar(im, ax=ax, shrink=0.6, pad=0.02)
    cb.set_label("relative accessibility (share of the period's 99th percentile)", fontsize=8)
    how = "HierX 0.1.1" if col == "rel_hierx" else "exact shortest paths"
    ax.set_title(f"{TITLES[period]}\n(accessibility computed with {how})", fontsize=10)
    ax.text(
        0.01, 0.01, BADGES[period], transform=ax.transAxes, fontsize=7,
        bbox={"facecolor": "white", "edgecolor": "#888888", "boxstyle": "round,pad=0.3"},
    )  # fmt: skip
    ax.text(
        0.01, 0.985, GREY_NOTE.get(period, GREY_NOTE["default"]),
        transform=ax.transAxes, fontsize=6.5, ha="left", va="top", color="#555555",
    )  # fmt: skip
    ax.set_axis_off()
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    path = FIG_DIR / f"accessibility_{period}.png"
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return path


def draw_vgj_difference() -> Path:
    """VGJ contribution on foot, 1960s: (A with VGJ - A without) / q99(without), shared scale."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    a = relative_scores("1960s_walk")
    b = relative_scores("1960s_walk_novgj")
    q = np.nanquantile(b.loc[b["kind"] == "cell", "A_exact"], 0.99)
    df = a[["zone_id", "kind", "x", "y", "connected", "A_exact"]].merge(
        b[["zone_id", "A_exact"]], on="zone_id", suffixes=("", "_novgj")
    )
    df["delta"] = (df["A_exact"] - df["A_exact_novgj"]) / q
    cells = df[df["kind"] == "cell"]
    x0, y0 = cells["x"].min(), cells["y"].min()
    nx_ = int(round((cells["x"].max() - x0) / CELL_SURF_M)) + 1
    ny_ = int(round((cells["y"].max() - y0) / CELL_SURF_M)) + 1
    grid = np.full((ny_, nx_), np.nan)
    ix = np.round((cells["x"] - x0) / CELL_SURF_M).astype(int)
    iy = np.round((cells["y"] - y0) / CELL_SURF_M).astype(int)
    grid[iy, ix] = cells["delta"]
    extent = [x0 - CELL_SURF_M / 2, x0 + (nx_ - 0.5) * CELL_SURF_M,
              y0 - CELL_SURF_M / 2, y0 + (ny_ - 0.5) * CELL_SURF_M]  # fmt: skip
    fig, ax = plt.subplots(figsize=(8.5, 9.5))
    vmax = max(np.nanquantile(cells["delta"], 0.99), 1e-6)
    im = ax.imshow(grid, origin="lower", extent=extent, cmap="viridis", vmin=0, vmax=vmax,
                   interpolation="nearest")  # fmt: skip
    pts = places()
    poly = gpd.GeoSeries(list(pts.loc[pts["level"] == "parish", "polygon"]), crs=WORKING_CRS)
    poly.boundary.plot(ax=ax, color="white", linewidth=0.4, alpha=0.6)
    import matplotlib.patheffects as pe

    halo = [pe.withStroke(linewidth=1.6, foreground="black")]
    for r in pts[pts["level"] == "parish"].itertuples():
        p = r.polygon.representative_point()
        ax.text(p.x, p.y, r.folknet_namn.title(), fontsize=5.5, ha="center", color="white",
                path_effects=halo)  # fmt: skip
    # VGJ stations: where each station connects to the road network
    e60 = pd.read_parquet(WORK / "network_1960s" / "edges_1960s.parquet")
    nodes = pd.read_parquet(MODERN_DIR / "nodes.parquet", columns=["node_id", "x", "y"])
    st = nodes.set_index("node_id").reindex(e60.loc[e60["mode"] == "station_access", "v"])
    st = st.dropna()
    st = st[(st["x"] >= extent[0]) & (st["x"] <= extent[1]) & (st["y"] >= extent[2])
            & (st["y"] <= extent[3])]  # fmt: skip
    ax.scatter(st["x"], st["y"], s=28, marker="o", facecolor="#c0162c", edgecolor="white",
               linewidth=0.8, zorder=5, label="VGJ station")  # fmt: skip
    town = pts[pts["place"] == ALINGSAS].geometry.iloc[0]
    ax.scatter(
        [town.x], [town.y], s=40, marker="s", facecolor="white", edgecolor="black", zorder=5
    )
    ax.annotate("Alingsås", (town.x, town.y), xytext=(0, 7), textcoords="offset points",
                ha="center", fontsize=7, fontweight="bold", color="white",
                path_effects=halo)  # fmt: skip
    ax.legend(loc="upper left", fontsize=7, frameon=True)
    cb = fig.colorbar(im, ax=ax, shrink=0.6, pad=0.02)
    cb.set_label("gain from the VGJ (share of the no-VGJ 99th percentile)", fontsize=8)
    ax.set_title("1960s on foot: what the VGJ railway added\n(exact shortest paths)", fontsize=10)
    box = {"facecolor": "white", "edgecolor": "#888888", "boxstyle": "round,pad=0.3"}
    ax.text(0.01, 0.01, BADGES["1960s_walk"], transform=ax.transAxes, fontsize=6.5, bbox=box)
    ax.set_axis_off()
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    path = FIG_DIR / "vgj_gain_1960s_walk.png"
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return path


# --------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------


def corridor_roads() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    path = WORK / "corridor_1890s" / "roads_1890s.gpkg"
    roads = gpd.read_file(path, layer="roads")
    empty = gpd.GeoDataFrame({"kind": []}, geometry=gpd.GeoSeries([], crs=WORKING_CRS))
    layers = {}
    for name in ("seam_joins", "repairs"):
        try:
            layers[name] = gpd.read_file(path, layer=name)
        except Exception:  # noqa: BLE001 - an absent layer is a valid state
            layers[name] = empty
    return roads, layers["seam_joins"], layers["repairs"]


def run() -> dict:
    pts = places()
    keys = pts["place"].tolist()
    alingsas = keys.index(ALINGSAS)
    roads, joins, repairs = corridor_roads()
    graphs = {"1890s": graph_1890s(roads, joins, repairs), "today": graph_modern()}
    rows, mats = [], {}
    for period, g in graphs.items():
        tt, conn_m, _node = travel_times(g, pts)
        pop = population(PERIODS[period], keys).to_numpy(dtype=float)
        acc = accessibility(tt, pop)
        mats[period] = tt
        for i, k in enumerate(keys):
            rows.append(
                {
                    "period": period,
                    "place": k,
                    "name": pts.loc[i, "folknet_namn"],
                    "population_year": PERIODS[period],
                    "connector_m": float(conn_m[i]),
                    "minutes_to_alingsas": float(tt[i, alingsas]),
                    "reachable_places": int(np.isfinite(tt[i]).sum() - 1),
                    "accessibility": float(acc[i]),
                }
            )
    df = pd.DataFrame(rows)
    df["accessibility_share_of_max"] = df.groupby("period")["accessibility"].transform(
        lambda s: s / s.max()
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_DIR / "accessibility.parquet", index=False)
    g90 = graphs["1890s"]
    gpd.GeoDataFrame(
        {"piece": range(len(g90["pieces"]))}, geometry=g90["pieces"], crs=WORKING_CRS
    ).to_file(OUT_DIR / "graph_1890s.gpkg", layer="edges", driver="GPKG")
    for period, tt in mats.items():
        pd.DataFrame(tt, index=keys, columns=keys).to_parquet(OUT_DIR / f"tt_{period}.parquet")
    return {"places": len(keys), "snap_connectors_1890s": g90["snap_connectors"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["run"])
    ap.parse_args(argv)
    t0 = time.perf_counter()
    print(json.dumps(run()))
    print(f"compute {time.perf_counter() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
