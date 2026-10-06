"""1960s corridor network: modern roads kept where the 1960s map shows them.

Declared in ``docs/network_1960s/plan.md`` (method B). Each Ekonomiska kartan sheet of
the core is reprojected to EPSG:3006 and every OpenStreetMap road edge inside it is scored with
the Stage 3 evidence method and the 7C 9b parameters, unchanged. The 1960s network keeps the
modern road edges that are present (or uncertain) on the sheets, the modern roads outside the
sheets unchanged (flagged), the modern main-line rail, and the VGJ as open in 1948–1966 with
the 1963 timetable running times (Stage 5 replay state A_section_wise:S2).

Run only after the owner's audit of the Stage 3 1962 extraction.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyproj import Transformer
from shapely.geometry import Polygon

from histnet import inkscore as s3
from histnet.paths import DATA, INPUTS, WORK, WORKING_CRS

STAGE = "exp-network-1960s"
METHOD_VERSION = "network-1960s-0.1"
PLAN_PATH = WORK / "notes" / "network_1960s" / "plan.md"
RAW_DIR = INPUTS / "ekonomiska_kartan"  # Lantmäteriet open data, see SOURCES.md
OUT_DIR = WORK / "network_1960s"
MAPS_DIR = OUT_DIR / "maps"
SCORED_PATH = OUT_DIR / "scored_edges.gpkg"
OSM_PATH = WORK / "osm_highways.gpkg"  # built from the OSM extract by `make osm`
MODERN_DIR = DATA / "modern_network"  # OpenStreetMap-derived, ODbL
REPLAY_DIR = DATA / "vgj_1963"  # VGJ rail edges 1948-66 and station links
SHEET_CRS = "EPSG:3021"  # RT90 2.5 gon V; carries the datum shift the files' own definition lacks
RAIL_STATE = "A_section_wise:S2"  # VGJ open on both sections, 1948-07-01 to 1966
SCORED_CLASSES = (
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link",
    "unclassified", "residential", "living_street", "service", "track",
)  # fmt: skip
SHEET_RE = re.compile(r"133_(\d+[a-z])(\d[a-j])(\d\d)_0_b2\.tif$", re.I)
FLAG_LATE_EDITION = {"8C 1e": "edition '74, later than the rest"}


def sheets() -> pd.DataFrame:
    rows = []
    for p in sorted(RAW_DIR.glob("*.tif")):
        m = SHEET_RE.match(p.name)
        if m:
            sid = f"{m.group(1).upper()} {m.group(2)}"
            rows.append(
                {
                    "sheet": sid,
                    "edition": m.group(3),
                    "path": p,
                    "flag": FLAG_LATE_EDITION.get(sid, ""),
                }
            )
    return pd.DataFrame(rows)


def warp(path: Path, sheet: str) -> Path:
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    out = MAPS_DIR / f"ekon_{sheet.replace(' ', '')}_3006.tif"
    if not out.exists():
        subprocess.run(
            [
                # the files' own RT90 definition lacks the datum shift; EPSG:3021 carries it
                # (as Stage 3, checked against control points); without it sheets land ~160 m off
                "gdalwarp", "-q", "-s_srs", SHEET_CRS, "-t_srs", WORKING_CRS,
                "-r", "cubic", "-tr", "1", "1", "-tap",
                "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=2",
                "-co", "BIGTIFF=IF_SAFER", str(path), str(out),
            ],
            check=True, capture_output=True,
        )  # fmt: skip
    return out


def footprint(path: Path) -> Polygon:
    import rasterio

    with rasterio.open(path) as ds:
        b = ds.bounds
    t = Transformer.from_crs(SHEET_CRS, WORKING_CRS, always_xy=True)
    return Polygon(
        [
            t.transform(x, y)
            for x, y in (
                (b.left, b.bottom),
                (b.right, b.bottom),
                (b.right, b.top),
                (b.left, b.top),
            )
        ]
    )


def footprint_from_id(sheet: str) -> Polygon:
    """A sheet's footprint from its id on the RT90 sheet grid (e.g. "7C 9b"), no file needed.

    A 50 km square such as 7C has its SW corner at northing 6,050 + 50 * row km and easting
    1,200 + 50 * column km (A = 0); its 10 x 10 sheets of 5 km are numbered 0-9 from the south
    and a-j from the west.
    """
    m = re.match(r"(\d+)([A-Z]) (\d)([a-j])$", sheet)
    if not m:
        raise ValueError(sheet)
    n0 = 6_050_000 + 50_000 * int(m.group(1)) + 5_000 * int(m.group(3))
    e0 = 1_200_000 + 50_000 * (ord(m.group(2)) - ord("A")) + 5_000 * "abcdefghij".index(m.group(4))
    t = Transformer.from_crs(SHEET_CRS, WORKING_CRS, always_xy=True)
    corners = ((e0, n0), (e0 + 5_000, n0), (e0 + 5_000, n0 + 5_000), (e0, n0 + 5_000))
    return Polygon([t.transform(x, y) for x, y in corners])


def osm_lines(bounds: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    df = pyogrio.read_dataframe(OSM_PATH, layer="highways", bbox=bounds)
    df = df[df["highway"].isin(SCORED_CLASSES)].rename(columns={"osm_id": "id"})
    df["id"] = pd.to_numeric(df["id"], errors="coerce").fillna(0).astype(np.int64)
    return df.set_crs(WORKING_CRS, allow_override=True)


def score_sheets() -> dict[str, Any]:
    sh = sheets()
    out = []
    params = s3.EvidenceParams()
    for r in sh.itertuples():
        raster = warp(r.path, r.sheet)
        foot = footprint(r.path)
        spec = replace(
            s3.ECON_1962,
            sheet_id=f"ekon_{r.sheet.replace(' ', '')}_{r.edition}",
            label=f"{r.sheet} edition '{r.edition}",
            raster=raster,
            source_raw=f"lm_economic_map_1960s/{r.path.name}",
        )
        lines = osm_lines(foot.bounds)
        edges = s3.build_edges(lines, foot, "highway")
        if not len(edges):
            continue
        scored = s3.extract_sheet(spec, edges, params)
        scored["sheet"] = r.sheet
        scored["edition"] = r.edition
        scored["flag"] = r.flag
        out.append(scored)
    allx = gpd.GeoDataFrame(pd.concat(out, ignore_index=True), crs=WORKING_CRS)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if SCORED_PATH.exists():
        SCORED_PATH.unlink()
    allx.to_file(SCORED_PATH, layer="scored_edges", driver="GPKG")
    km = allx.assign(km=allx.geometry.length / 1000).groupby("present")["km"].sum().round(1)
    return {"sheets": len(sh), "edges": len(allx), "km_by_status": km.to_dict()}


def score_sheet(sheet: str) -> gpd.GeoDataFrame:
    sh = sheets()
    r = sh[sh["sheet"] == sheet].iloc[0]
    raster = warp(r.path, r.sheet)
    foot = footprint(r.path)
    spec = replace(
        s3.ECON_1962,
        sheet_id=f"ekon_{r.sheet.replace(' ', '')}_{r.edition}",
        label=f"{r.sheet} edition '{r.edition}",
        raster=raster,
        source_raw=f"lm_economic_map_1960s/{r.path.name}",
    )
    edges = s3.build_edges(osm_lines(foot.bounds), foot, "highway")
    scored = s3.extract_sheet(spec, edges, s3.EvidenceParams())
    scored["sheet"], scored["edition"], scored["raster"] = r.sheet, r.edition, str(raster)
    return scored


GAP_BRIDGE_M = 300.0  # stranded fragments rejoined through absent chains up to 300 m
GAP_STATUS = "gap, assumed"


def bridge_gaps(e: pd.DataFrame, max_gap_m: float = GAP_BRIDGE_M) -> np.ndarray:
    """Absent road edges to keep so that stranded fragments reach the largest component.

    Kept road costs nothing and absent road costs its length; for each fragment outside the
    largest component, the cheapest chain to it is kept if its absent length is ``max_gap_m`` or
    less. Returns a boolean mask over ``e``.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components, dijkstra

    road = (e["mode"] == "road").to_numpy()
    absent = (e["status_1960s"] == "absent").to_numpy() & road
    kept = road & ~absent
    ids = pd.unique(np.concatenate([e["u"].to_numpy()[road], e["v"].to_numpy()[road]]))
    idx = pd.Series(np.arange(len(ids)), index=ids)
    ri = np.flatnonzero(road)
    u = idx[e["u"].to_numpy()[ri]].to_numpy()
    v = idx[e["v"].to_numpy()[ri]].to_numpy()
    k = kept[ri]
    n = len(ids)
    _nc, lab = connected_components(
        coo_matrix((np.ones(k.sum()), (u[k], v[k])), shape=(n, n)), directed=False
    )
    touched = np.zeros(n, dtype=bool)
    touched[u[k]] = touched[v[k]] = True
    km = pd.Series(e["length_m"].to_numpy()[ri][k]).groupby(lab[u[k]]).sum()
    big = int(km.idxmax())
    w = np.where(k, 1e-9, e["length_m"].to_numpy()[ri])
    # one entry per node pair: the cheapest edge, remembered for the trace back
    pair = pd.DataFrame({"a": np.minimum(u, v), "b": np.maximum(u, v), "w": w, "row": ri})
    pair = pair.sort_values("w", kind="stable").drop_duplicates(["a", "b"])
    m = coo_matrix(
        (pair["w"].to_numpy(), (pair["a"].to_numpy(), pair["b"].to_numpy())), shape=(n, n)
    ).tocsr()
    row_of = {(a, b): r for a, b, r in zip(pair["a"], pair["b"], pair["row"], strict=True)}
    dist, pred, _src = dijkstra(
        m, directed=False, indices=np.flatnonzero(lab == big), min_only=True,
        return_predecessors=True,
    )  # fmt: skip
    mask = np.zeros(len(e), dtype=bool)
    stranded = touched & (lab != big) & np.isfinite(dist)
    best = (
        pd.Series(dist[stranded], index=np.flatnonzero(stranded)).groupby(lab[stranded]).idxmin()
    )
    for node in sorted(best.to_numpy()):
        if dist[node] > max_gap_m:
            continue
        j = int(node)
        while pred[j] >= 0:
            p = int(pred[j])
            r = row_of[(min(p, j), max(p, j))]
            if absent[r]:
                mask[r] = True
            j = p
    return mask


GAP_DETOUR_M = 2000.0  # a short chain is kept when its ends are this far apart over kept road


def bridge_cuts(
    e: pd.DataFrame, max_gap_m: float = GAP_BRIDGE_M, min_detour_m: float = GAP_DETOUR_M
) -> np.ndarray:
    """Absent road pieces to keep because leaving them out forces a long detour.

    For every two nodes on kept road joined by a path of absent road of ``max_gap_m`` or less, the
    shortest such path is kept if the two nodes are more than ``min_detour_m`` apart over kept road
    (or not connected over it). Returns a boolean mask over ``e``.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra

    road = (e["mode"] == "road").to_numpy()
    absent = (e["status_1960s"] == "absent").to_numpy() & road
    kept = road & ~absent
    ids = pd.unique(np.concatenate([e["u"].to_numpy()[road], e["v"].to_numpy()[road]]))
    idx = pd.Series(np.arange(len(ids)), index=ids)
    n = len(ids)
    u = np.full(len(e), -1)
    v = np.full(len(e), -1)
    u[road] = idx[e["u"].to_numpy()[road]].to_numpy()
    v[road] = idx[e["v"].to_numpy()[road]].to_numpy()
    length = np.maximum(e["length_m"].to_numpy(dtype=float), 1e-9)
    on_kept = np.zeros(n, dtype=bool)
    on_kept[u[kept]] = on_kept[v[kept]] = True
    kept_graph = coo_matrix((length[kept], (u[kept], v[kept])), shape=(n, n)).tocsr()
    # absent graph on its own nodes: one entry per node pair, the shortest piece, kept for tracing
    rows = np.flatnonzero(absent)
    mask = np.zeros(len(e), dtype=bool)
    if len(rows) == 0:
        return mask
    nodes = np.unique(np.concatenate([u[rows], v[rows]]))
    loc = pd.Series(np.arange(len(nodes)), index=nodes)
    la, lb = loc[u[rows]].to_numpy(), loc[v[rows]].to_numpy()
    pair = (
        pd.DataFrame(
            {"a": np.minimum(la, lb), "b": np.maximum(la, lb), "w": length[rows], "row": rows}
        )
        .sort_values("w", kind="stable")
        .drop_duplicates(["a", "b"])
    )
    m = len(nodes)
    absent_graph = coo_matrix(
        (pair["w"].to_numpy(), (pair["a"].to_numpy(), pair["b"].to_numpy())), shape=(m, m)
    ).tocsr()
    row_of = {(a, b): r for a, b, r in zip(pair["a"], pair["b"], pair["row"], strict=True)}
    starts = np.flatnonzero(on_kept[nodes])  # local indices of absent nodes on kept road
    for s in starts:
        gap, pred = dijkstra(
            absent_graph, directed=False, indices=int(s), limit=max_gap_m,
            return_predecessors=True,
        )  # fmt: skip
        ends = starts[(starts > s) & np.isfinite(gap[starts])]
        if len(ends) == 0:
            continue
        detour = dijkstra(kept_graph, directed=False, indices=int(nodes[s]), limit=min_detour_m)
        for b in ends[~np.isfinite(detour[nodes[ends]])]:
            j = int(b)
            while j != s:
                p = int(pred[j])
                mask[row_of[(min(p, j), max(p, j))]] = True
                j = p
    return mask


GRAFSNAS_XY = (352446.0, 6441000.0)  # owner decision RQ-S3-0002, candidate 3
STATION_ACCESS_MIN = 10.0  # Stage 5 connector cost
EDGES_PATH = OUT_DIR / "edges_1960s.parquet"


def build_network(match_m: float = 10.0, bridge_m: float | None = GAP_BRIDGE_M) -> dict[str, Any]:
    """Modern road edges kept/dropped by the scored sheets, modern rail, VGJ 1948-66."""
    import shapely
    from scipy.spatial import cKDTree

    n = pd.read_parquet(
        MODERN_DIR / "nodes.parquet", columns=["node_id", "x", "y", "in_graph_ring"]
    )
    n = n[n["in_graph_ring"]]
    e = pd.read_parquet(
        MODERN_DIR / "edges.parquet",
        columns=["u", "v", "cost_min", "length_m", "mode", "osm_way_id"],
    )
    e = e[e["u"].isin(n["node_id"]) & e["v"].isin(n["node_id"])].reset_index(drop=True)
    pos = n.set_index("node_id")[["x", "y"]]
    mx = (pos.loc[e["u"], "x"].to_numpy() + pos.loc[e["v"], "x"].to_numpy()) / 2
    my = (pos.loc[e["u"], "y"].to_numpy() + pos.loc[e["v"], "y"].to_numpy()) / 2
    scored = gpd.read_file(SCORED_PATH, layer="scored_edges")
    sheets_union = shapely.union_all(
        [footprint_from_id(s) for s in sorted(scored["sheet"].unique())]
    )
    inside = shapely.contains_xy(sheets_union, mx, my)
    status = np.where(
        e["mode"] == "road",
        np.where(inside, "unscored", "modern, not checked"),
        "modern rail, not checked",
    )
    status = np.asarray(status, dtype=object)
    # match each road edge inside the sheets to the nearest scored piece of the same OSM way
    sc = scored.assign(way=pd.to_numeric(scored["osm_way_id"], errors="coerce"))
    by_way = {w: g for w, g in sc.groupby("way")}
    for i in np.flatnonzero((e["mode"] == "road").to_numpy() & inside):
        g = by_way.get(float(e.at[i, "osm_way_id"])) if pd.notna(e.at[i, "osm_way_id"]) else None
        if g is None:
            continue
        pt = shapely.Point(mx[i], my[i])
        d = g.geometry.distance(pt)
        j = int(np.argmin(d.to_numpy()))
        if d.iloc[j] <= match_m:
            status[i] = str(g["present"].iloc[j])
    e["status_1960s"] = status
    bridged = bridge_gaps(e) if bridge_m else np.zeros(len(e), dtype=bool)
    e.loc[bridged, "status_1960s"] = GAP_STATUS
    cuts = bridge_cuts(e) if bridge_m else np.zeros(len(e), dtype=bool)
    e.loc[cuts, "status_1960s"] = GAP_STATUS
    keep = e["status_1960s"] != "absent"
    out = e[keep][["u", "v", "cost_min", "length_m", "mode", "status_1960s"]].copy()
    # VGJ 1948-66 (state S2, candidate SC-01) with the owner's Gräfsnäs station
    st = pd.read_parquet(REPLAY_DIR / "state_edges.parquet")
    st = st[(st["state_id"] == RAIL_STATE) & (st["grafsnas_candidate"] == "SC-01")]
    con = pd.read_parquet(REPLAY_DIR / "rail_connectors.parquet")
    con = (
        con[con["grafsnas_candidate"] == "SC-01"]
        .drop_duplicates("station_key")
        .set_index("station_key")
    )
    tree = cKDTree(n[["x", "y"]].to_numpy())
    _d, gi = tree.query(GRAFSNAS_XY)
    con.loc["grafsnas", "road_node_id"] = int(n["node_id"].iloc[gi])
    rail = [
        {"u": int(con.at[r.from_key, "station_node_id"]),
         "v": int(con.at[r.to_key, "station_node_id"]),
         "cost_min": float(r.cost_min), "length_m": np.nan, "mode": "rail_vgj_1963",
         "status_1960s": "vgj 1948-66"}
        for r in st.itertuples()
    ]  # fmt: skip
    links = [
        {"u": int(c.station_node_id), "v": int(c.road_node_id), "cost_min": STATION_ACCESS_MIN,
         "length_m": np.nan, "mode": "station_access", "status_1960s": "vgj 1948-66"}
        for c in con.itertuples() if c.Index in set(st["from_key"]) | set(st["to_key"])
    ]  # fmt: skip
    out = pd.concat([out, pd.DataFrame(rail + links)], ignore_index=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.to_parquet(EDGES_PATH, index=False)
    km = e.assign(km=e["length_m"] / 1000).groupby("status_1960s")["km"].sum().round(1)
    return {"edges": len(out), "dropped_absent": int((~keep).sum()), "km_by_status": km.to_dict(),
            "bridged_edges": int(bridged.sum()), "cut_edges": int(cuts.sum()),
            "vgj_edges": len(rail),
            "station_links": len(links)}  # fmt: skip


REALIGN_BAND_M = 60.0  # roads straightened after the 1960s: search a wider band


def rescore_absent() -> dict[str, Any]:
    """Absent edges scored again with a 60 m band; found 'present' -> kept as 'realigned'."""
    scored = gpd.read_file(SCORED_PATH, layer="scored_edges")
    params = replace(s3.EvidenceParams(), slack_band_m=REALIGN_BAND_M)
    out = []
    sh = sheets().set_index("sheet")
    for sheet, grp in scored[scored["present"] == "absent"].groupby("sheet"):
        r = sh.loc[sheet]
        spec = replace(
            s3.ECON_1962,
            sheet_id=f"ekon_{sheet.replace(' ', '')}_{r.edition}",
            label=f"{sheet} edition '{r.edition}",
            raster=warp(r.path, sheet),
            source_raw=f"ekonomiska_kartan/{r.path.name}",
        )
        edges = grp[["edge_id", "osm_way_id", "modern_class", "geometry"]]
        wide = s3.extract_sheet(spec, edges, params)
        out.append(wide[["edge_id", "present"]].rename(columns={"present": "present_60m"}))
    wide = (
        pd.concat(out, ignore_index=True)
        if out
        else pd.DataFrame(columns=["edge_id", "present_60m"])
    )
    scored = scored.drop(
        columns=[c for c in ("present_60m", "present_15m") if c in scored.columns]
    )
    scored = scored.merge(wide, on="edge_id", how="left")
    scored["present_15m"] = scored["present"]
    realigned = (scored["present_15m"] == "absent") & (scored["present_60m"] == "present")
    scored.loc[realigned, "present"] = "realigned"
    if SCORED_PATH.exists():
        SCORED_PATH.unlink()
    scored.to_file(SCORED_PATH, layer="scored_edges", driver="GPKG")
    t = scored[scored["present_15m"] == "absent"].assign(km=lambda d: d["length_m"] / 1000)
    return {
        "absent_km_by_class": t.groupby("modern_class")["km"].sum().round(1).to_dict(),
        "realigned_km_by_class": t[t["present"] == "realigned"]
        .groupby("modern_class")["km"]
        .sum()
        .round(1)
        .to_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["score", "rescore-absent", "network"])
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    if args.cmd == "score":
        print(json.dumps(score_sheets(), default=str))
    elif args.cmd == "rescore-absent":
        print(json.dumps(rescore_absent(), default=str))
    elif args.cmd == "network":
        print(json.dumps(build_network(), default=str))
    print(f"compute {time.perf_counter() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
