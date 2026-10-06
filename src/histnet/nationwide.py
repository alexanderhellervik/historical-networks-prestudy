"""How many tiles would a national road reading need? Metadata-only estimate.

Declared in ``docs/national_estimate/plan.md``. No map tile is read and no model is
called. Subcommands:

``sheets``   footprints of every sheet in Naturvårdsverket's Häradsekonomiska packages, read
             from each GeoTIFF header by HTTP range requests (zip central directory, then the
             TIFF header through GDAL's /vsizip//vsicurl/).
``roads``    OpenStreetMap highways inside the sheet coverage, from the registered Sweden
             extract, with the plan's class groups.
``estimate`` tile grid, parish points, policies A/B/C and the lower bound, per county; results.

Modern roads are a proxy for the 1890s network, not a reconstruction of it.
"""

from __future__ import annotations

import argparse
import heapq
import io
import json
import re
import time
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

from histnet.paths import WORK, WORKING_CRS

STAGE = "exp-national-estimate"
METHOD_VERSION = "national-estimate-0.1"
PLAN_PATH = WORK / "notes" / "national_estimate" / "plan.md"
OUT_DIR = WORK / "national_estimate"
NV_BASE = "https://geodata.naturvardsverket.se/nedladdning/haradskartan/"
SHEETS_PATH = OUT_DIR / "sheets.parquet"
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".zip,.tif",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
}


class HttpRangeFile(io.RawIOBase):
    """A seekable read-only file over HTTP range requests (enough for zipfile's directory)."""

    def __init__(self, url: str, session: requests.Session) -> None:
        self.url, self.s, self.pos = url, session, 0
        self.size = int(session.head(url, timeout=60).headers["Content-Length"])

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def tell(self) -> int:
        return self.pos

    def read(self, n: int = -1) -> bytes:
        end = self.size - 1 if n is None or n < 0 else min(self.size, self.pos + n) - 1
        if end < self.pos:
            return b""
        r = self.s.get(self.url, headers={"Range": f"bytes={self.pos}-{end}"}, timeout=120)
        r.raise_for_status()
        self.pos += len(r.content)
        return r.content


def package_names(session: requests.Session) -> list[str]:
    html = session.get(NV_BASE, timeout=60).text
    return sorted(set(re.findall(r'href="(Haradskarta_\d+\.zip)"', html)))


def package_members(name: str, session: requests.Session) -> list[tuple[str, int]]:
    with zipfile.ZipFile(HttpRangeFile(NV_BASE + name, session)) as z:
        return [
            (i.filename, i.file_size) for i in z.infolist() if i.filename.lower().endswith(".tif")
        ]


def sheet_header(package: str, member: str) -> dict[str, Any]:
    import rasterio

    path = f"/vsizip/vsicurl/{NV_BASE}{package}/{member}"
    rec: dict[str, Any] = {"package": package, "member": member}
    try:
        with rasterio.Env(**GDAL_ENV), rasterio.open(path) as r:
            b = r.bounds
            rec.update(
                width=r.width,
                height=r.height,
                xmin=b.left,
                ymin=b.bottom,
                xmax=b.right,
                ymax=b.top,
                res_x=abs(r.transform.a),
                res_y=abs(r.transform.e),
                crs_wkt_head=(r.crs.to_wkt()[:60] if r.crs else ""),
                status="ok",
            )
    except Exception as e:  # noqa: BLE001 - every failure is recorded, not raised
        rec.update(status=f"error: {type(e).__name__}: {e}"[:300])
    return rec


def collect_sheets(workers: int = 12) -> pd.DataFrame:
    s = requests.Session()
    packages = package_names(s)
    members: list[tuple[str, str, int]] = []
    for p in packages:
        for m, size in package_members(p, s):
            members.append((p, m, size))
    with ThreadPoolExecutor(workers) as ex:
        recs = list(ex.map(lambda t: sheet_header(t[0], t[1]), members))
    df = pd.DataFrame(recs)
    df["size_bytes"] = [m[2] for m in members]
    df["sheet_id"] = df["member"].str.replace(r"_0_prj\.tif$", "", regex=True)
    df["listing_url"] = NV_BASE
    df["retrieved_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return df.sort_values(["package", "member"]).reset_index(drop=True)


# --------------------------------------------------------------------------------------
# Estimate
# --------------------------------------------------------------------------------------


OSM_PATH = OUT_DIR / "osm_highways.gpkg"
RESULTS_PATH = WORK / "notes" / "national_estimate" / "results.md"
CELL_M = 1000
SAMPLE_M = 50.0
REF_YEAR = 1890
GROUPS = {
    "base": ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"),
    "main": ("secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified"),
    "minor": ("residential", "track", "service", "living_street"),
}
VARIANTS = {"main": ("base", "main"), "all": ("base", "main", "minor")}
SEED_RADIUS = 3  # A* is seeded with read tiles within this many tiles of the nearest one
MAX_EXPANSIONS = 50_000
# Measured in docs/area_band (34 tiles): per tile
CODEX_TOKENS, CODEX_FRESH_IN, CODEX_CACHED_IN, CODEX_OUT = 110_444, 22_234, 79_081, 9_129
OPUS_TOKENS = 53_319
CODEX_MIN, OPUS_MIN = 6.4, 3.4
BAND_TILES_PER_HOUR = 34 / (42 / 60)  # six readings in parallel, reviews alongside


def cell_id(ix: np.ndarray, iy: np.ndarray) -> np.ndarray:
    return ix.astype(np.int64) * 100_000 + iy.astype(np.int64)


def cell_xy(cid: int) -> tuple[int, int]:
    return int(cid // 100_000), int(cid % 100_000)


def coverage_cells(sheets: pd.DataFrame) -> set[int]:
    out: set[int] = set()
    for r in sheets[sheets.status == "ok"].itertuples():
        ix = np.arange(np.ceil(r.xmin / CELL_M - 0.5), np.floor(r.xmax / CELL_M - 0.5) + 1)
        iy = np.arange(np.ceil(r.ymin / CELL_M - 0.5), np.floor(r.ymax / CELL_M - 0.5) + 1)
        gx, gy = np.meshgrid(ix, iy)
        out.update(cell_id(gx.ravel(), gy.ravel()).tolist())
    return out


def load_parishes() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, pd.DataFrame]:
    from histnet import histmaps as s1

    u = s1.load_histmaps_units()
    v = u[(u["start"] <= REF_YEAR) & (u["end"] >= REF_YEAR)]
    par = v[v["type"] == "Parish"].copy()
    par["county_code"] = par["ref_code"].str.slice(3, 5)
    towns = v[(v["type"] == "Municipal") & v["name"].str.contains(r"\bstad\b", case=False)].copy()
    counties = v[v["type"] == "County"][["ref_code", "name"]].copy()
    counties["county_code"] = counties["ref_code"].str.slice(3, 5)
    return par, towns, counties


def road_samples(bbox: tuple[float, float, float, float]) -> tuple[np.ndarray, ...]:
    """(cell id, group index, line index, segment length) per 50 m sample of every road."""
    import pyogrio
    import shapely

    df = pyogrio.read_dataframe(OSM_PATH, layer="highways", bbox=bbox, columns=["highway"])
    df = df[df["highway"].isin(sum(GROUPS.values(), ()))].reset_index(drop=True)
    gidx = np.zeros(len(df), dtype=np.int8)
    for k, classes in enumerate(GROUPS.values()):
        gidx[df["highway"].isin(classes).to_numpy()] = k
    geoms = shapely.segmentize(df.geometry.values, SAMPLE_M)
    coords, idx = shapely.get_coordinates(geoms, return_index=True)
    ix = np.floor(coords[:, 0] / CELL_M)
    iy = np.floor(coords[:, 1] / CELL_M)
    cid = cell_id(ix, iy)
    seg = np.zeros(len(coords))
    same = idx[1:] == idx[:-1]
    d = np.hypot(np.diff(coords[:, 0]), np.diff(coords[:, 1]))
    seg[1:][same] = d[same]
    return cid, gidx[idx], idx, seg, int(len(df))


def tile_graph(
    cid: np.ndarray, grp: np.ndarray, line: np.ndarray, seg: np.ndarray, groups: tuple[int, ...]
) -> tuple[dict[int, set[int]], dict[int, float]]:
    keep = np.isin(grp, groups)
    c, ln, sg = cid[keep], line[keep], seg[keep]
    u, inv = np.unique(c, return_inverse=True)
    sums = np.bincount(inv, weights=sg)
    length = dict(zip(u.tolist(), sums.tolist(), strict=True))
    adj: dict[int, set[int]] = {int(x): set() for x in u.tolist()}
    move = (ln[1:] == ln[:-1]) & (c[1:] != c[:-1])
    a, b = c[:-1][move], c[1:][move]
    pairs = np.unique(np.stack([np.minimum(a, b), np.maximum(a, b)], axis=1), axis=0)
    for x, y in pairs.tolist():
        adj[x].add(y)
        adj[y].add(x)
    return adj, length


def bfs(adj: dict[int, set[int]], sources: set[int]) -> dict[int, int]:
    """Parent pointers of a multi-source BFS (sources map to themselves)."""
    parent = {s: s for s in sources if s in adj}
    q = deque(parent)
    while q:
        x = q.popleft()
        for y in adj[x]:
            if y not in parent:
                parent[y] = x
                q.append(y)
    return parent


def lower_bound_tiles(parent: dict[int, int], targets: list[int]) -> set[int]:
    out: set[int] = set()
    for t in targets:
        x = t
        while x not in out:
            out.add(x)
            if parent[x] == x:
                break
            x = parent[x]
    return out


def cheb(a: int, b: int) -> int:
    ax, ay = cell_xy(a)
    bx, by = cell_xy(b)
    return max(abs(ax - bx), abs(ay - by))


def nearest_read(read: set[int], t: int, max_r: int = 400) -> list[int]:
    tx, ty = cell_xy(t)
    found: list[int] = []
    rmin = None
    for r in range(max_r + 1):
        if rmin is not None and r > rmin + SEED_RADIUS:
            break
        ring = (
            [(tx + dx, ty + dy) for dx in range(-r, r + 1) for dy in (-r, r)]
            + [(tx + dx, ty + dy) for dx in (-r, r) for dy in range(-r + 1, r)]
            if r
            else [(tx, ty)]
        )
        for x, y in ring:
            c = x * 100_000 + y
            if c in read:
                found.append(c)
                if rmin is None:
                    rmin = r
    return found


def simulate_search(
    adj: dict[int, set[int]], start: set[int], targets: list[int], order: dict[int, int]
) -> dict[str, Any]:
    """Policy C: goal-directed tile search; every popped tile is read and counted."""
    read = {s for s in start if s in adj}
    n_start = len(read)
    failures = 0
    for t in sorted(targets, key=lambda x: (order.get(x, 10**9), x)):
        if t in read:
            continue
        seeds = nearest_read(read, t)
        heap = [(cheb(s, t), 0, s) for s in seeds]
        heapq.heapify(heap)
        g = {s: 0 for s in seeds}
        done = False
        pops = 0
        while heap and pops < MAX_EXPANSIONS:
            f, gc, x = heapq.heappop(heap)
            if gc > g.get(x, 10**9):
                continue
            pops += 1
            read.add(x)
            if x == t:
                done = True
                break
            for y in adj[x]:
                ng = gc + 1
                if ng < g.get(y, 10**9):
                    g[y] = ng
                    heapq.heappush(heap, (ng + cheb(y, t), ng, y))
        failures += not done
    return {"read": read, "start": n_start, "failures": failures}


def estimate() -> dict[str, Any]:
    from histnet.paths import INPUTS as RAW
    from histnet.provenance import Provenance
    from histnet.util import sha256_file

    sheets = pd.read_parquet(SHEETS_PATH)
    cover = coverage_cells(sheets)
    par, towns, counties = load_parishes()
    county_name = dict(zip(counties.county_code, counties.name, strict=False))

    ids = np.fromiter(cover, dtype=np.int64)
    cx = (ids // 100_000 + 0.5) * CELL_M
    cy = (ids % 100_000 + 0.5) * CELL_M
    pts = gpd.GeoDataFrame({"cell": ids}, geometry=gpd.points_from_xy(cx, cy), crs=WORKING_CRS)
    j = gpd.sjoin(
        pts, par[["ref_code", "county_code", "geometry"]], predicate="within", how="left"
    )
    j = j.drop_duplicates("cell")
    cells = pd.DataFrame({"cell": j["cell"], "parish": j["ref_code"], "county": j["county_code"]})
    cells["land"] = cells["parish"].notna()

    bbox = (
        float(sheets.xmin.min()),
        float(sheets.ymin.min()),
        float(sheets.xmax.max()),
        float(sheets.ymax.max()),
    )
    cid, grp, line, seg, n_lines = road_samples(bbox)
    cover_set = set(cells["cell"].tolist())
    out: dict[str, Any] = {"n_osm_lines": n_lines, "variants": {}}
    for vname, gnames in VARIANTS.items():
        gi = tuple(list(GROUPS).index(g) for g in gnames)
        adj, length = tile_graph(cid, grp, line, seg, gi)
        adj = {c: {y for y in n if y in cover_set} for c, n in adj.items() if c in cover_set}
        cells[f"road_m_{vname}"] = cells["cell"].map(length).fillna(0.0)
        # start set: base-network tiles and town tiles
        base_adj, _ = tile_graph(cid, grp, line, seg, (list(GROUPS).index("base"),))
        start = {c for c in base_adj if c in cover_set}
        tw = towns.copy()
        tw["geometry"] = tw.geometry.representative_point()
        tcell = cell_id(np.floor(tw.geometry.x / CELL_M), np.floor(tw.geometry.y / CELL_M))
        start |= {int(c) for c in tcell.tolist() if int(c) in adj}
        # parish targets: the parish's covered cell with most proxy road
        pc = cells[cells["parish"].notna()].copy()
        pc["road"] = pc[f"road_m_{vname}"]
        pc = pc[pc["cell"].isin(list(adj))]
        best = pc.sort_values(["parish", "road", "cell"], ascending=[True, False, True])
        best = best.drop_duplicates("parish")
        parishes_covered = set(cells.loc[cells.land, "parish"])
        targets = dict(zip(best["parish"], best["cell"].astype(int), strict=True))
        parent = bfs(adj, start)
        reach = {p: t for p, t in targets.items() if t in parent}
        lb = lower_bound_tiles(parent, list(reach.values()))
        order = {}
        for t in reach.values():  # BFS depth from the start set
            d, x = 0, t
            while parent[x] != x:
                x, d = parent[x], d + 1
            order[t] = d
        sim = simulate_search(adj, start, list(reach.values()), order)
        cells[f"lb_{vname}"] = cells["cell"].isin(lb)
        cells[f"read_{vname}"] = cells["cell"].isin(sim["read"])
        cells[f"start_{vname}"] = cells["cell"].isin(start)
        out["variants"][vname] = {
            "parishes_covered": len(parishes_covered),
            "parishes_with_road_cell": len(targets),
            "parishes_reachable": len(reach),
            "parishes_unreachable": sorted(set(targets) - set(reach)),
            "parishes_no_road": sorted(parishes_covered - set(targets)),
            "start_tiles": sim["start"],
            "search_failures": sim["failures"],
        }
    cells["county_name"] = cells["county"].map(county_name)
    cells = cells.sort_values("cell").reset_index(drop=True)
    prov = Provenance(
        stage=STAGE,
        method_version=METHOD_VERSION,
        inputs={
            "raw/osm_geofabrik/sweden-latest.osm.pbf": sha256_file(
                RAW / "osm_geofabrik" / "sweden-latest.osm.pbf"
            ),
            "raw/histmaps/data/geom_sp.rda": sha256_file(
                RAW / "histmaps" / "data" / "geom_sp.rda"
            ),
            "data/national_estimate/sheets.parquet": sha256_file(SHEETS_PATH),
            "docs/national_estimate/plan.md": sha256_file(PLAN_PATH),
        },
        parameters={
            "cell_m": CELL_M,
            "sample_m": SAMPLE_M,
            "ref_year": REF_YEAR,
            "groups": GROUPS,
            "variants": VARIANTS,
            "seed_radius": SEED_RADIUS,
            "crs": WORKING_CRS,
        },
    )
    cells.to_parquet(OUT_DIR / "tiles.parquet", index=False)
    prov.write(OUT_DIR / "tiles.parquet")
    summary = county_table(cells)
    summary.to_parquet(OUT_DIR / "counties.parquet", index=False)
    prov.write(OUT_DIR / "counties.parquet")
    (OUT_DIR / "estimate_meta.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
    write_results(cells, summary, out)
    return {
        k: {kk: (vv if not isinstance(vv, list) else len(vv)) for kk, vv in v.items()}
        for k, v in out["variants"].items()
    }


def county_table(cells: pd.DataFrame) -> pd.DataFrame:
    land = cells[cells.land]
    g = land.groupby(["county", "county_name"], dropna=False)
    t = pd.DataFrame(
        {
            "A_land_tiles": g.size(),
            "B_any_road_main": g["road_m_main"].apply(lambda s: int((s > 0).sum())),
            "B_any_road_all": g["road_m_all"].apply(lambda s: int((s > 0).sum())),
            "C_search_main": g["read_main"].sum(),
            "C_search_all": g["read_all"].sum(),
            "LB_main": g["lb_main"].sum(),
            "LB_all": g["lb_all"].sum(),
        }
    ).reset_index()
    return t.sort_values("county").reset_index(drop=True)


def cost(n: int) -> dict[str, float]:
    return {
        "codex_bn": n * CODEX_TOKENS / 1e9,
        "codex_fresh_in_bn": n * CODEX_FRESH_IN / 1e9,
        "codex_cached_in_bn": n * CODEX_CACHED_IN / 1e9,
        "codex_out_bn": n * CODEX_OUT / 1e9,
        "opus_bn": n * OPUS_TOKENS / 1e9,
        "model_hours": n * (CODEX_MIN + OPUS_MIN) / 60,
        "days_at_band_pace": n / BAND_TILES_PER_HOUR / 24,
    }


def write_results(cells: pd.DataFrame, counties: pd.DataFrame, meta: dict[str, Any]) -> Path:
    land = cells[cells.land]
    rows = {
        "A: every covered land tile": len(land),
        "B: tiles with any main/base road": int((land.road_m_main > 0).sum()),
        "B: tiles with any road incl. tracks/residential": int((land.road_m_all > 0).sum()),
        "C: search, main/base roads": int(land.read_main.sum()),
        "C: search, all roads": int(land.read_all.sum()),
        "C, main/base, not counting the start tiles (base network given)": int(
            (land.read_main & ~land.start_main).sum()
        ),
        "Start tiles alone (modern trunk/primary roads and towns)": int(land.start_main.sum()),
        "Lower bound (paths known), main/base": int(land.lb_main.sum()),
        "Lower bound (paths known), all roads": int(land.lb_all.sum()),
    }
    L: list[str] = []
    add = L.append
    add("# National tile-count estimate — results")
    add("")
    add(
        f"Generated by `make national-estimate` (`histnet.nationwide estimate`, "
        f"{METHOD_VERSION}) under `docs/national_estimate/plan.md`. No tile was read and "
        "no model was called. Modern OpenStreetMap roads stand in for the 1890s network; the "
        "counts model the search, they do not test it."
    )
    add("")
    add(
        f"**Coverage:** {len(cells):,} 1 km cells inside the 1,447 sheet footprints, of which "
        f"{len(land):,} are land (centre in an 1890 parish)."
    )
    add("")
    add("## Tiles to read and implied model cost (band-run unit costs, 34 tiles of one sheet)")
    add("")
    add(
        "| policy | tiles | Codex tokens (bn) | of which fresh input / cached / output | "
        "Opus tokens (bn) | model hours | days at the band run's pace |"
    )
    add("|---|---|---|---|---|---|---|")
    for k, n in rows.items():
        c = cost(n)
        add(
            f"| {k} | {n:,} | {c['codex_bn']:.2f} | {c['codex_fresh_in_bn']:.2f} / "
            f"{c['codex_cached_in_bn']:.2f} / {c['codex_out_bn']:.2f} | {c['opus_bn']:.2f} | "
            f"{c['model_hours']:,.0f} | {c['days_at_band_pace']:.0f} |"
        )
    add("")
    add(
        "Band-run unit costs: Codex gpt-6.1-sol about 110k tokens per tile (22k fresh input, 79k "
        "cached input, 9k output), 6.4 min median; Opus 5.5 review about 53k subagent tokens, "
        "3.4 min median; pace 34 tiles in 42 min with six readings in parallel. Sparse tiles "
        "cost about 25 % less in the band run, which this table ignores."
    )
    add("")
    add("## Parishes")
    add("")
    for v, m in meta["variants"].items():
        add(
            f"* Roads `{v}`: {m['parishes_covered']} parishes with a covered land cell; "
            f"{m['parishes_with_road_cell']} have a proxy road in a covered cell; "
            f"{m['parishes_reachable']} are reachable from the start set "
            f"({m['start_tiles']:,} start tiles: base network and towns); "
            f"{len(m['parishes_unreachable'])} unreachable, {len(m['parishes_no_road'])} without "
            f"any proxy road; search failures {m['search_failures']}."
        )
    add("")
    add("## Per county (land tiles)")
    add("")
    add(
        "| county | A all | B road main | B road all | C search main | C search all | "
        "lower bound main |"
    )
    add("|---|---|---|---|---|---|---|")
    for r in counties.itertuples():
        add(
            f"| {r.county} {r.county_name} | {r.A_land_tiles:,} | {r.B_any_road_main:,} | "
            f"{r.B_any_road_all:,} | {int(r.C_search_main):,} | {int(r.C_search_all):,} | "
            f"{int(r.LB_main):,} |"
        )
    add("")
    add("## Limits")
    add("")
    add(
        "* Modern roads are a proxy: denser than the 1890s network where forest tracks and "
        "suburbs are new, sparser where old roads were abandoned."
    )
    add(
        "* Tile-level graph: roads inside a cell are assumed connected; a road crossing a cell "
        "corner links diagonal cells."
    )
    add(
        "* The search model assumes a reader that sees which neighbouring tiles a road enters; "
        "the per-tile cost is from farmland tiles of one sheet."
    )
    add("* Sheet footprints are bounding boxes and overstate the drawn area slightly.")
    RESULTS_PATH.write_text("\n".join(L) + "\n", encoding="utf-8")
    return RESULTS_PATH


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sheets")
    sub.add_parser("estimate")
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    if args.cmd == "sheets":
        from histnet.provenance import Provenance

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        df = collect_sheets()
        df.to_parquet(SHEETS_PATH, index=False)
        Provenance(
            stage=STAGE,
            method_version=METHOD_VERSION,
            inputs={NV_BASE: "directory listing and GeoTIFF headers read on retrieval"},
            parameters={
                "crs": WORKING_CRS,
                "declared_in": "docs/national_estimate/plan.md",
            },
        ).write(SHEETS_PATH)
        print(json.dumps({"sheets": len(df), "ok": int((df.status == "ok").sum())}))
    if args.cmd == "estimate":
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        print(json.dumps(estimate(), indent=1, default=str))
    print(f"compute {time.perf_counter() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
