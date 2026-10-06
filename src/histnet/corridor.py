"""Corridor 1890s road network by tile search (``docs/corridor_1890s/plan.md``).

The search policy proposed for a national run, run for real over the corridor core: read
outward from given roads and towns until every core parish is connected. Each tile goes
through the measured pipeline (Codex gpt-6.1-sol reading with the worked example, Opus 5.5
review, the existing assembly rule); reviews run through the Claude Code CLI in print mode so
the search runs unattended.

Subcommands: ``mosaic`` (virtual raster of the covering sheets), ``run`` (the search, resumable),
``assemble`` (network layers, figures, results). Machine-read lines reviewed by a second model;
not a historical road network checked by a person.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point, box

from histnet import core as vr
from histnet.paths import DATA, INPUTS, PROMPTS, ROOT, WORK, WORKING_CRS
from histnet.util import sha256_file

STAGE = "exp-corridor-1890s"
METHOD_VERSION = "corridor-1890s-0.1"
PLAN_PATH = WORK / "notes" / "corridor_1890s" / "plan.md"
OUT_DIR = WORK / "corridor_1890s"
TILES_DIR = OUT_DIR / "tiles"
MANIFEST_PATH = OUT_DIR / "manifest.jsonl"
VRT_PATH = OUT_DIR / "hk_core_mosaic.vrt"
OUTPUTS = OUT_DIR / "model_outputs"
RUNS = OUT_DIR / "runs"
OVERLAYS = OUT_DIR / "overlays" / "review"
TASKS = RUNS / "review_tasks"
CLI_ROOT = RUNS / "cli_root"  # empty working directory for the headless reviews
STATE_PATH = OUT_DIR / "search_state.json"
RESULTS_PATH = WORK / "notes" / "corridor_1890s" / "results.md"
SHEETS_PATH = DATA / "haradskartan_sheets.parquet"  # sheet footprints (Naturvårdsverket packages)
DOMAIN_PATH = DATA / "domain.gpkg"
OSM_PATH = WORK / "osm_highways.gpkg"  # built from the OSM extract by `make osm`
RUNNER = ROOT / "scripts" / "run_reading.sh"
PROMPTS = PROMPTS
READ_PROMPT = PROMPTS / "pass_a_K_inline.md"
REVIEW_PROMPT = PROMPTS / "pass_c_K.md"
REVIEW_TEMPLATE = PROMPTS / "review_task_K.md"
EXAMPLE_DIR = DATA / "worked_example"  # legend crops and the worked example (see SOURCES.md)

PACKAGES = ("Haradskarta_33.zip", "Haradskarta_42.zip")
CELL_M = 1000
MOSAIC_RES = 2.0
MARGIN_M = 3000.0
EDGE_M = 20.0
ROUND_SIZE = 12
TILE_CAP = 600
CODEX_PARALLEL = 12  # 8 until round 3; a runtime setting, not part of the search policy
REVIEW_PARALLEL = 12
PRODUCER, REVIEWER, ARM = "sol61", "opus", "K"
REQUIRED_MODEL = "claude-opus-5-5"
BASE_CLASSES = ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link")
START_TOWNS = ("Alingsås",)


# --------------------------------------------------------------------------------------
# Mosaic and tiles
# --------------------------------------------------------------------------------------


def core_geometry() -> Any:
    dom = gpd.read_file(DOMAIN_PATH, layer="domain")
    return dom[dom["name"] == "core"].geometry.iloc[0]


def search_box() -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = core_geometry().bounds
    return (x0 - MARGIN_M, y0 - MARGIN_M, x1 + MARGIN_M, y1 + MARGIN_M)


def covering_sheets() -> pd.DataFrame:
    s = pd.read_parquet(SHEETS_PATH)
    b = box(*search_box())
    keep = [box(r.xmin, r.ymin, r.xmax, r.ymax).intersects(b) for r in s.itertuples()]
    s = s[pd.Series(keep, index=s.index) & s["package"].isin(PACKAGES)].copy()
    return s.sort_values("sheet_id").reset_index(drop=True)


def verify_packages() -> dict[str, str]:
    """sha256 of the two Naturvårdsverket packages, recorded with the mosaic (no registry here)."""
    return {f"inputs/haradskartan/{p}": sha256_file(INPUTS / "haradskartan" / p) for p in PACKAGES}


# Sheets drawn per county: where a county border crosses a sheet, two sheets share one frame,
# each holding its own county's part and blank paper elsewhere. Each 50 m block goes to the
# sheet with more ink there (grey-level standard deviation), smoothed by a 5 x 5 majority.
PAIRED_SHEETS = (("112_42-24", "112_42-24a"),)
PAIR_BLOCK_PX = 25
MASKED_DIR = OUT_DIR / "sheets_masked"


def mask_pair(package: str, a: str, b: str) -> dict[str, Any]:
    import rasterio
    from scipy import ndimage as ndi

    paths = {k: f"/vsizip/{INPUTS}/haradskartan/{package}/{k}_0_prj.tif" for k in (a, b)}
    arrs, prof = {}, None
    for k, pth in paths.items():
        with rasterio.open(pth) as ds:
            arrs[k] = ds.read()
            if prof is None:
                prof = ds.profile
            elif ds.transform != prof["transform"] or ds.shape != (prof["height"], prof["width"]):
                raise RuntimeError(f"{a} and {b} are not on one pixel grid")
    n = PAIR_BLOCK_PX
    h, w = prof["height"] // n * n, prof["width"] // n * n

    def ink(x: np.ndarray) -> np.ndarray:
        g = x[:, :h, :w].astype(float).mean(0).reshape(h // n, n, w // n, n)
        return g.std(axis=(1, 3))

    take_a = ndi.median_filter((ink(arrs[a]) >= ink(arrs[b])).astype(np.uint8), size=5) > 0
    full = np.zeros((prof["height"], prof["width"]), bool)
    full[:h, :w] = np.repeat(np.repeat(take_a, n, 0), n, 1)
    full[h:, :] = full[h - 1 : h, :] if h else False
    full[:, w:] = full[:, w - 1 : w] if w else False
    MASKED_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for k, keep in ((a, full), (b, ~full)):
        arr = arrs[k].copy()
        arr[:, ~keep] = 0
        dst = MASKED_DIR / f"{k}_masked.tif"
        prof2 = {**prof, "driver": "GTiff", "compress": "deflate", "nodata": 0, "tiled": True}
        with rasterio.open(dst, "w", **prof2) as ds:
            ds.write(arr)
        out[k] = {"path": str(dst), "share_kept": round(float(keep.mean()), 3)}
    return out


def build_mosaic() -> dict[str, Any]:
    hashes = verify_packages()
    sheets = covering_sheets()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    masked = {}
    for a, b in PAIRED_SHEETS:
        pkg = sheets.loc[sheets.sheet_id == a, "package"].iloc[0]
        masked.update(mask_pair(pkg, a, b))
    srcs = [
        masked[r.sheet_id]["path"]
        if r.sheet_id in masked
        else f"/vsizip/{INPUTS}/haradskartan/{r.package}/{r.member}"
        for r in sheets.itertuples()
    ]
    cmd = [
        "gdalbuildvrt", "-overwrite", "-srcnodata", "0", "-vrtnodata", "0",
        "-tr", str(MOSAIC_RES), str(MOSAIC_RES), "-tap", "-r", "cubic",
        # the masked copies spell SWEREF 99 TM differently from the sheets; same projection
        "-allow_projection_difference", str(VRT_PATH), *srcs,
    ]  # fmt: skip
    res = subprocess.run(cmd, check=True, capture_output=True, text=True)
    if "Skipping" in res.stderr:
        raise RuntimeError(f"gdalbuildvrt skipped a source: {res.stderr}")
    meta = {
        "sheets": sheets["sheet_id"].tolist(),
        "inputs": hashes,
        "paired_sheets_masked": masked,
        "cmd": cmd[: -len(srcs)],
    }
    (OUT_DIR / "mosaic.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return {"sheets": len(sheets), "vrt": str(VRT_PATH)}


def tile_id(ix: int, iy: int) -> str:
    return f"cor_e{ix:04d}_n{iy:04d}"


def cell_of(tid: str) -> tuple[int, int]:
    m = re.match(r"cor_e(\d+)_n(\d+)$", tid)
    if not m:
        raise ValueError(tid)
    return int(m.group(1)), int(m.group(2))


def cell_box(ix: int, iy: int) -> Any:
    return box(ix * CELL_M, iy * CELL_M, (ix + 1) * CELL_M, (iy + 1) * CELL_M)


def make_tile(ix: int, iy: int) -> vr.TileSpec:
    import rasterio

    with rasterio.open(VRT_PATH) as ds:
        t = ds.transform
    col = round((ix * CELL_M - t.c) / t.a)
    row = round((t.f - (iy + 1) * CELL_M) / -t.e)
    n = round(CELL_M / t.a)
    cand = vr.Candidate(
        block=f"e{ix}n{iy}", split="corridor_1890s", col_off=col, row_off=row, width=n,
        height=n, draw=0.0, valid_fraction=float("nan"), road_length_m=0.0, junction_count=0,
        absent_uncertain_m=0.0,
    )  # fmt: skip
    spec = vr._make_tile(
        cand, 0, VRT_PATH, sha256_file(VRT_PATH), vr.SERIES_1890S, tile_id(ix, iy)
    )
    spec = replace(
        spec,
        rights_note="Häradsekonomiska kartan (Lantmäteriet), Naturvårdsverket georeferenced copy",
        selection={
            "rule": "corridor search (docs/corridor_1890s/plan.md)",
            "cell": [ix, iy],
        },
    )
    return vr._render_all(spec, VRT_PATH, tiles_dir=TILES_DIR)


def read_manifest() -> dict[str, vr.TileSpec]:
    if not MANIFEST_PATH.is_file():
        return {}
    return {t.tile_id: t for t in vr.read_manifest(MANIFEST_PATH)}


def append_manifest(tiles: list[vr.TileSpec]) -> None:
    with MANIFEST_PATH.open("a", encoding="utf-8") as fh:
        for t in tiles:
            fh.write(json.dumps(t.as_json(), sort_keys=True) + "\n")


def reading_path(tid: str) -> Path:
    return OUTPUTS / f"{tid}_{PRODUCER}_{ARM}_passA.json"


def review_path(tid: str) -> Path:
    return OUTPUTS / f"{tid}_{REVIEWER}_reviews_{PRODUCER}_{ARM}_passC.json"


def answer_ok(path: Path, key: str = "features") -> bool:
    if not path.is_file():
        return False
    try:
        doc, _ = vr.extract_json_object(path.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(doc, dict) and isinstance(doc.get(key), list)


def run_reading(tid: str) -> bool:
    """One gpt-6.1-sol reading; an absent or unparseable answer is kept aside and rerun once.

    A valid answer with an empty feature list is a reading (a tile can hold no road).
    """
    env = {
        **os.environ, "FAMILY": PRODUCER, "MODEL": "gpt-6.1-sol", "PROMPT_VARIANT": "_inline",
        "TILES_DIR": str(TILES_DIR), "OUT_ROOT": str(OUT_DIR),
    }  # fmt: skip
    for attempt in (1, 2):
        out = reading_path(tid)
        if answer_ok(out):
            return True
        if out.exists():
            out.rename(out.with_suffix(f".failed{attempt}.json"))
        subprocess.run(
            ["bash", str(RUNNER), ARM, tid, "r1"], env=env, capture_output=True, timeout=2400
        )
    return answer_ok(reading_path(tid))


def overlay_paths(tid: str) -> tuple[Path, Path]:
    return OVERLAYS / f"{tid}_proposal_ids.png", OVERLAYS / f"{tid}_proposal_overlay.png"


def review_task_text(tile: vr.TileSpec) -> str:
    """The review instructions (prompts/review_task_K.md) filled in for one tile."""
    ids, ov = overlay_paths(tile.tile_id)
    fill = {
        "{BARE}": ROOT / tile.renders["bare"]["path"],
        "{GRID}": ROOT / tile.renders["gridded"]["path"],
        "{IDS}": ids,
        "{OVERLAY}": ov,
        "{OUTPUT}": review_path(tile.tile_id),
        "{LEGEND_PAGE}": EXAMPLE_DIR / "legend_haradskartan_p4.png",
        "{LEGEND_ROADS}": EXAMPLE_DIR / "legend_roads_boundaries.png",
        "{EXAMPLE_BARE}": EXAMPLE_DIR / "example_bare.png",
        "{EXAMPLE_ANNOTATED}": EXAMPLE_DIR / "example_annotated.png",
        "{TILE_ID}": tile.tile_id,
    }
    text = REVIEW_TEMPLATE.read_text(encoding="utf-8")
    for key, value in fill.items():
        text = text.replace(key, str(value))
    return text


def accepted_records(
    tile: vr.TileSpec, family: str, path: Path, prompt_path: Path
) -> list[dict[str, Any]]:
    """The reading's validated features as records, as the review overlays draw them."""
    raw_doc, wrapped = vr.extract_json_object(path.read_text(encoding="utf-8"))
    source = {"path": vr.repo_relative(path), "sha256": sha256_file(path)}
    doc, _ = vr.normalise_pass_a(
        raw_doc, tile_id=tile.tile_id, family=family, source=source, was_wrapped=wrapped
    )
    recs = vr.pass_a_candidate_records(
        doc,
        tile_id=tile.tile_id,
        family=family,
        provenance={
            "prompt_sha256": sha256_file(prompt_path),
            "image_sha256": {k: v["sha256"] for k, v in tile.renders.items()},
            "model_id": family,
            "run_id": path.name[: -len(".json")],
        },
    )
    accepted, _ = vr.validate_candidates(recs, [tile])
    return accepted


def prepare_review(tile: vr.TileSpec) -> Path:
    recs = accepted_records(tile, PRODUCER, reading_path(tile.tile_id), READ_PROMPT)
    bare = ROOT / tile.renders["bare"]["path"]
    ids, ov = overlay_paths(tile.tile_id)
    vr.render_overlay(tile, recs, bare, ids, ids_only=True)
    vr.render_overlay(tile, recs, bare, ov)
    TASKS.mkdir(parents=True, exist_ok=True)
    task = TASKS / f"{tile.tile_id}.txt"
    task.write_text(review_task_text(tile), encoding="utf-8")
    return task


def run_review(tile: vr.TileSpec) -> dict[str, Any]:
    """One headless Opus 5.5 review; the stream is kept and the model and tools are checked."""
    tid = tile.tile_id
    done = [r for r in _jsonl(RUNS / "review_log.jsonl") if r["tile_id"] == tid and r.get("ok")]
    if done and answer_ok(review_path(tid), "reviews"):
        return done[-1]  # verified on an earlier run (the search is resumable)
    task = prepare_review(tile)
    CLI_ROOT.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {"tile_id": tid}
    for attempt in (1, 2):
        out = review_path(tid)
        if out.exists():
            out.rename(out.with_suffix(f".failed{attempt}.json"))
        log = RUNS / f"{tid}_review_attempt{attempt}.stream.jsonl"
        t0 = time.time()
        cmd = [
            "claude", "-p", "--model", REQUIRED_MODEL, "--output-format", "stream-json",
            "--verbose", "--allowedTools", "Read", "Write", "--disallowedTools", "Bash", "Edit",
            "WebFetch", "WebSearch", "Agent", "Task", "NotebookEdit",
            "--add-dir", str(OUT_DIR), str(WORK / "vision_roads"),
            str(WORK / "informed_reading"), "--",
            task.read_text(encoding="utf-8"),
        ]  # fmt: skip
        with log.open("w") as fh:
            subprocess.run(
                cmd, cwd=CLI_ROOT, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                timeout=1800,
            )  # fmt: skip
        rec.update(check_stream(log, task, out))
        rec.update({"attempt": attempt, "wall_s": round(time.time() - t0, 1)})
        if rec["ok"]:
            break
    with (RUNS / "review_log.jsonl").open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def check_stream(log: Path, task: Path, out: Path) -> dict[str, Any]:
    allowed = set(re.findall(r"/\S+?\.png", task.read_text()))
    models: set[str] = set()
    reads, writes, other = [], [], []
    usage, cost = None, None
    for line in log.read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = e.get("message") or {}
        if msg.get("model"):
            models.add(msg["model"])
        for c in msg.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                fp = (c.get("input") or {}).get("file_path")
                {"Read": reads, "Write": writes}.get(c["name"], other).append(fp or c["name"])
        if e.get("type") == "result":
            models |= set((e.get("modelUsage") or {}).keys())
            usage, cost = e.get("usage"), e.get("total_cost_usd")
    tools_ok = not other and all(p in allowed for p in reads) and writes == [str(out)]
    ok = models == {REQUIRED_MODEL} and tools_ok and answer_ok(out, "reviews")
    tokens = None
    if usage:
        tokens = sum(
            usage.get(k, 0)
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                      "output_tokens")
        )  # fmt: skip
    return {
        "models": sorted(models), "reads": len(reads),
        "reads_outside": [p for p in reads if p not in allowed],
        "writes": writes, "other_tools": other, "tools_ok": tools_ok, "ok": ok,
        "tokens": tokens, "cost_usd": cost, "stream": vr.repo_relative(log),
    }  # fmt: skip


# --------------------------------------------------------------------------------------
# Products and search state
# --------------------------------------------------------------------------------------


def tile_product(tile: vr.TileSpec) -> gpd.GeoDataFrame | None:
    from histnet import core as rs

    r_path, c_path = reading_path(tile.tile_id), review_path(tile.tile_id)
    if not (answer_ok(r_path) and answer_ok(c_path, "reviews")):
        return None
    lines, _d, _c, _j = rs._read_pass_a(tile, PRODUCER, r_path, prompt_path=READ_PROMPT)
    product, _ = rs._reviewed_product(
        tile, PRODUCER, REVIEWER, lines, c_path, prompt_path=REVIEW_PROMPT
    )
    product, _ = rs._with_coarse(product, "proposed_class", rs.VISION_COARSE)
    return product


NEIGHBOURS = {"E": (1, 0), "W": (-1, 0), "N": (0, 1), "S": (0, -1)}


def shared_edge(ix: int, iy: int, side: str) -> LineString:
    x0, y0, x1, y1 = ix * CELL_M, iy * CELL_M, (ix + 1) * CELL_M, (iy + 1) * CELL_M
    return {
        "E": LineString([(x1, y0), (x1, y1)]),
        "W": LineString([(x0, y0), (x0, y1)]),
        "N": LineString([(x0, y1), (x1, y1)]),
        "S": LineString([(x0, y0), (x1, y0)]),
    }[side]


def exits(product: gpd.GeoDataFrame, ix: int, iy: int) -> set[tuple[int, int]]:
    """Neighbour cells across edges that a reviewed line ends within EDGE_M of."""
    ends = []
    for g in product.geometry:
        cs = list(g.coords)
        ends += [Point(cs[0]), Point(cs[-1])]
    out = set()
    for side, (dx, dy) in NEIGHBOURS.items():
        e = shared_edge(ix, iy, side)
        if any(p.distance(e) <= EDGE_M for p in ends):
            out.add((ix + dx, iy + dy))
    return out


def start_cells() -> set[tuple[int, int]]:
    import pyogrio

    core = core_geometry()
    osm = pyogrio.read_dataframe(OSM_PATH, layer="highways", bbox=core.bounds)
    base = osm[osm["highway"].isin(BASE_CLASSES)].clip(core)
    cells = set()
    for g in base.geometry:
        g2 = g.segmentize(50.0)
        for x, y in (
            g2.coords if g2.geom_type == "LineString" else [c for p in g2.geoms for c in p.coords]
        ):
            cells.add((math.floor(x / CELL_M), math.floor(y / CELL_M)))
    from histnet.nationwide import load_parishes

    _par, towns, _ = load_parishes()
    for name in START_TOWNS:
        p = towns[towns["name"].str.startswith(name)].geometry.representative_point().iloc[0]
        cells.add((math.floor(p.x / CELL_M), math.floor(p.y / CELL_M)))
    return cells


def parish_cells() -> dict[str, set[tuple[int, int]]]:
    from histnet.nationwide import load_parishes

    par, _t, _c = load_parishes()
    core = core_geometry()
    par = par[par.intersects(core)]
    out: dict[str, set[tuple[int, int]]] = {}
    for ref, g in zip(par["ref_code"], par.geometry, strict=True):
        x0, y0, x1, y1 = g.bounds
        cells = {
            (ix, iy)
            for ix in range(math.floor(x0 / CELL_M), math.ceil(x1 / CELL_M))
            for iy in range(math.floor(y0 / CELL_M), math.ceil(y1 / CELL_M))
            if g.contains(Point((ix + 0.5) * CELL_M, (iy + 0.5) * CELL_M))
            and core.contains(Point((ix + 0.5) * CELL_M, (iy + 0.5) * CELL_M))
        }
        if cells:
            out[ref] = cells
    return out


def load_state() -> dict[str, Any]:
    if STATE_PATH.is_file():
        return json.loads(STATE_PATH.read_text())
    return {"rounds": [], "tiles": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def connectivity(
    state: dict[str, Any], start: set[tuple[int, int]], parishes: dict[str, set[tuple[int, int]]]
) -> tuple[set[tuple[int, int]], set[str], set[tuple[int, int]]]:
    """(connected read cells, connected parishes, frontier cells) from the read results."""
    read = {tuple(v["cell"]): v for v in state["tiles"].values() if v.get("status") == "ok"}
    ex = {c: {tuple(e) for e in v["exits"]} for c, v in read.items()}
    linked = {c: set() for c in read}
    for c, es in ex.items():
        for n in es:
            if n in read:
                linked[c].add(n)
                linked[n].add(c)
    seen = {c for c in start if c in read}
    stack = list(seen)
    while stack:
        c = stack.pop()
        for n in linked[c]:
            if n not in seen:
                seen.add(n)
                stack.append(n)
    with_road = {c for c, v in read.items() if v.get("lines", 0) > 0}
    done = {p for p, cells in parishes.items() if cells & seen & with_road}
    attempted = {tuple(v["cell"]) for v in state["tiles"].values()}
    frontier = {n for c in seen for n in ex[c] if n not in attempted}
    return seen, done, frontier


def choose(
    frontier: set[tuple[int, int]], todo_cells: set[tuple[int, int]], k: int
) -> list[tuple[int, int]]:
    x0, y0, x1, y1 = search_box()
    inbox = [
        c for c in frontier
        if x0 <= (c[0] + 0.5) * CELL_M <= x1 and y0 <= (c[1] + 0.5) * CELL_M <= y1
    ]  # fmt: skip

    def dist(c: tuple[int, int]) -> int:
        return min(max(abs(c[0] - t[0]), abs(c[1] - t[1])) for t in todo_cells)

    return sorted(inbox, key=lambda c: (dist(c), c))[:k]


def process(cells: list[tuple[int, int]], state: dict[str, Any], rnd: int) -> None:
    have = read_manifest()
    new = []
    for ix, iy in cells:
        tid = tile_id(ix, iy)
        if tid not in have:
            new.append(make_tile(ix, iy))
    append_manifest(new)
    tiles = read_manifest()
    tids = [tile_id(ix, iy) for ix, iy in cells]
    with ThreadPoolExecutor(CODEX_PARALLEL) as ex:
        read_ok = dict(zip(tids, ex.map(run_reading, tids), strict=True))
    todo = [tiles[t] for t in tids if read_ok[t]]
    with ThreadPoolExecutor(REVIEW_PARALLEL) as ex:
        reviews = {r["tile_id"]: r for r in ex.map(run_review, todo)}
    for (ix, iy), tid in zip(cells, tids, strict=True):
        rec: dict[str, Any] = {"cell": [ix, iy], "round": rnd}
        if not read_ok[tid]:
            rec["status"] = "reading failed"
        elif not reviews.get(tid, {}).get("ok"):
            rec["status"] = "review failed or not verified"
        else:
            prod = tile_product(tiles[tid])
            rec["status"] = "ok" if prod is not None else "assembly failed"
            if prod is not None:
                rec["lines"] = len(prod)
                rec["length_m"] = float(prod.geometry.length.sum())
                rec["exits"] = sorted(exits(prod, ix, iy))
        state["tiles"][tid] = rec
    save_state(state)


def run_search() -> dict[str, Any]:
    for d in (OUTPUTS, RUNS, OVERLAYS, TASKS, CLI_ROOT, TILES_DIR):
        d.mkdir(parents=True, exist_ok=True)
    state = load_state()
    start = start_cells()
    parishes = parish_cells()
    state.setdefault("start", sorted(start))
    state.setdefault("parishes", sorted(parishes))
    if not state["rounds"]:
        cells = sorted(start)
        state["rounds"].append({"round": 0, "cells": cells, "kind": "start"})
        save_state(state)
        process(cells, state, 0)
    while True:
        seen, done, frontier = connectivity(state, start, parishes)
        todo = {p: c for p, c in parishes.items() if p not in done}
        n_read = len(state["tiles"])
        log = {"round": len(state["rounds"]), "read": n_read, "connected_parishes": len(done),
               "parishes": len(parishes), "frontier": len(frontier)}  # fmt: skip
        print(json.dumps(log), flush=True)
        if not todo or n_read >= TILE_CAP:
            state["stop"] = "all parishes connected" if not todo else "tile cap"
            break
        cells = choose(frontier, set().union(*todo.values()), min(ROUND_SIZE, TILE_CAP - n_read))
        if not cells:
            state["stop"] = "no frontier left inside the box"
            state["not_connected"] = sorted(todo)
            break
        rnd = len(state["rounds"])
        state["rounds"].append({"round": rnd, "cells": cells, "kind": "search", **log})
        save_state(state)
        process(cells, state, rnd)
    save_state(state)
    return {"stop": state["stop"], "tiles": len(state["tiles"])}


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------

ROADS_PATH = OUT_DIR / "roads_1890s.gpkg"
FIG_DIR = OUT_DIR / "figures"


def assemble() -> dict[str, Any]:
    from histnet import stitch as ab
    from histnet.provenance import Provenance

    state = load_state()
    tiles = read_manifest()
    ok = [t for t, v in state["tiles"].items() if v.get("status") == "ok"]
    frames = []
    for tid in sorted(ok):
        prod = tile_product(tiles[tid])
        if prod is not None and len(prod):
            prod = prod.copy()
            prod["round"] = state["tiles"][tid]["round"]
            frames.append(prod)
    roads = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=WORKING_CRS)
    joins, gaps = ab.stitch(roads, [tiles[t] for t in ok])
    keep = [
        "product_feature_id", "tile_id", "feature_id", "provenance_kind", "verdict",
        "proposed_class", "coarse_class", "round", "method_version", "geometry",
    ]  # fmt: skip
    out = roads[[c for c in keep if c in roads.columns]].copy()
    if ROADS_PATH.exists():
        ROADS_PATH.unlink()
    out.to_file(ROADS_PATH, layer="roads", driver="GPKG")
    if len(joins):
        joins.to_file(ROADS_PATH, layer="seam_joins", driver="GPKG")
    if len(gaps):
        gaps.to_file(ROADS_PATH, layer="seam_gaps", driver="GPKG")
    tl = gpd.GeoDataFrame(
        [
            {"tile_id": t, "status": v.get("status"), "round": v.get("round"),
             "lines": v.get("lines", 0), "geometry": cell_box(*v["cell"])}
            for t, v in sorted(state["tiles"].items())
        ],
        crs=WORKING_CRS,
    )  # fmt: skip
    tl.to_file(ROADS_PATH, layer="tiles", driver="GPKG")
    Provenance(
        stage=STAGE,
        method_version=METHOD_VERSION,
        inputs={  # the mosaic only when the maps were downloaded (not needed for `make demo`)
            vr.repo_relative(f): sha256_file(f)
            for f in (VRT_PATH, STATE_PATH, PLAN_PATH, MANIFEST_PATH)
            if f.is_file()
        },
        parameters={"edge_m": EDGE_M, "round_size": ROUND_SIZE, "crs": WORKING_CRS},
    ).write(ROADS_PATH)
    fig = draw_progress(out, joins, tl)
    return {
        "tiles_ok": len(ok), "lines": len(out), "km": round(out.geometry.length.sum() / 1000, 1),
        "seam_joins": len(joins), "seam_gaps": len(gaps), "figure": str(fig),
    }  # fmt: skip


def draw_progress(
    roads: gpd.GeoDataFrame, joins: gpd.GeoDataFrame, tiles: gpd.GeoDataFrame
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from histnet.nationwide import load_parishes

    par, _t, _c = load_parishes()
    core = core_geometry()
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 10))
    par[par.intersects(core)].boundary.plot(ax=ax, color="#bbbbbb", linewidth=0.5)
    gpd.GeoSeries([core], crs=WORKING_CRS).boundary.plot(ax=ax, color="#555555", linewidth=1)
    tiles[tiles.status == "ok"].plot(ax=ax, color="#f2e6c9", edgecolor="#d9c9a3", linewidth=0.3)
    bad = tiles[tiles.status != "ok"]
    if len(bad):
        bad.plot(ax=ax, color="#f4b6b6", edgecolor="#d98c8c", linewidth=0.3)
    roads.plot(ax=ax, color="#8c2d04", linewidth=0.8)
    if len(joins):
        joins.plot(ax=ax, color="#8c2d04", linewidth=0.8)
    ax.set_title(
        f"Corridor 1890s search: {int((tiles.status == 'ok').sum())} tiles read", fontsize=10
    )
    ax.text(
        0.01, 0.01, "Road lines read from Häradsekonomiska kartan · map origin: Lantmäteriet",
        transform=ax.transAxes, fontsize=7,
        bbox={"facecolor": "white", "edgecolor": "#888888", "boxstyle": "round,pad=0.3"},
    )  # fmt: skip
    ax.set_axis_off()
    path = FIG_DIR / "corridor_progress.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


SIMULATED_TILES = 392  # docs/corridor_1890s/plan.md: simulated estimate, core + Alingsås start


def write_results(summary: dict[str, Any]) -> Path:
    state = load_state()
    tiles = pd.DataFrame(
        [
            {"tile_id": t, **{k: v for k, v in r.items() if k != "exits"}}
            for t, r in state["tiles"].items()
        ]
    )
    reads = pd.DataFrame(_jsonl(RUNS / "pass_a_log.jsonl"))
    revs = pd.DataFrame(_jsonl(RUNS / "review_log.jsonl"))
    sc = pd.DataFrame()  # the check tile is scored in the report's accuracy section
    rounds = pd.DataFrame(state["rounds"])
    L: list[str] = []
    add = L.append
    add("# Corridor 1890s road network by tile search — results")
    add("")
    add(
        f"Generated by `make corridor-assemble` (`histnet.corridor`, {METHOD_VERSION}) under "
        "`docs/corridor_1890s/plan.md`. Machine-read 1890s road lines (Häradsekonomiska "
        "kartan, Naturvårdsverket copies; map origin Lantmäteriet), reviewed by Opus 5.5, not "
        "checked by a person except the check tile. Not a historical road network."
    )
    add("")
    add("## Search")
    add("")
    ok = tiles[tiles.status == "ok"]
    add(f"* Stop: **{state.get('stop', 'running')}** after {len(rounds)} rounds.")
    add(
        f"* Tiles read: {len(tiles)} ({len(ok)} with a verified reviewed product); "
        f"start tiles {len(state.get('start', []))}; the simulated estimate for this policy on "
        f"modern roads was {SIMULATED_TILES}."
    )
    if state.get("not_connected"):
        add(f"* Parishes not connected: {', '.join(state['not_connected'])}.")
    for st, n in tiles["status"].value_counts().items():
        if st != "ok":
            add(
                f"* {st}: {n} tiles "
                f"({', '.join(sorted(tiles.loc[tiles.status == st, 'tile_id']))})."
            )
    add(
        f"* Network: {summary.get('lines', 0)} lines, {summary.get('km', 0)} km; seams "
        f"{summary.get('seam_joins', 0)} joined, {summary.get('seam_gaps', 0)} gaps."
    )
    add("")
    add("| round | kind | tiles | connected parishes before the round | frontier |")
    add("|---|---|---|---|---|")
    for r in rounds.itertuples():
        kind = r.kind
        later = [x.cells for x in rounds.itertuples() if x.round > r.round]
        if r.cells in later:
            kind = "abandoned (same cells rerun in a later round after the restart)"
        add(
            f"| {r.round} | {kind} | {len(r.cells)} | "
            f"{_n(getattr(r, 'connected_parishes', None))} / {_n(getattr(r, 'parishes', None))} | "
            f"{_n(getattr(r, 'frontier', None))} |"
        )
    add("")
    add("## Check tile (t07 footprint, same route), against the frozen t07 reference")
    add("")
    if len(sc):
        add(
            "| tolerance (m) | completeness | FP rate | class acc. "
            "| all three Stage 3 thresholds |"
        )
        add("|---|---|---|---|---|")
        for r in sc.itertuples():
            add(
                f"| {r.tolerance_m:g} | {r.completeness:.2f} | {r.false_positive_rate:.2f} | "
                f"{r.class_accuracy:.2f} | {'pass' if r.all_three_pass_at_this_d else 'fail'} |"
            )
    add("")
    add("## Effort")
    add("")
    if len(reads):
        u = reads["usage"].apply(
            lambda x: (x or {}).get("input_tokens", 0) + (x or {}).get("output_tokens", 0)
        )
        add(
            f"* Codex gpt-6.1-sol readings: {len(reads)} runs, {reads.wall_s.sum() / 3600:.1f} h "
            f"summed ({reads.wall_s.median() / 60:.1f} min median), {u.sum() / 1e6:.1f} M tokens "
            "(input + output as reported), exit codes "
            f"{sorted(reads.exit_code.unique().tolist())}."
        )
    if len(revs):
        add(
            f"* Opus 5.5 reviews (headless CLI): {len(revs)} runs; model `{REQUIRED_MODEL}` on "
            f"every run: {all(m == [REQUIRED_MODEL] for m in revs.models)}; "
            f"{int(revs.ok.sum())} passed every check (one tile's reviewer wrote its answer twice "
            f"on both attempts and was excluded by the one-write rule); "
            f"{int((revs.attempt > 1).sum())} needed a "
            f"retry; {revs.wall_s.median():.0f} s median; {revs.tokens.sum() / 1e6:.1f} M tokens "
            f"(CLI count, includes its cached system prompt; not comparable with the subagent "
            f"counts of earlier runs); about ${revs.cost_usd.sum():.0f} at API prices."
        )
    RESULTS_PATH.write_text("\n".join(L) + "\n", encoding="utf-8")
    return RESULTS_PATH


def _n(v: Any) -> str:
    return "–" if v is None or (isinstance(v, float) and math.isnan(v)) else str(int(v))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# --------------------------------------------------------------------------------------
# Resumed search with a geometric link rule (plan addendum of 2026-10-02)
# --------------------------------------------------------------------------------------

STRICT_CAP = 800
SAMPLE_M = 50.0


def read_layer(name: str) -> gpd.GeoDataFrame:
    try:
        return gpd.read_file(ROADS_PATH, layer=name)
    except Exception:  # noqa: BLE001 - an absent layer is a valid state
        return gpd.GeoDataFrame({"kind": []}, geometry=gpd.GeoSeries([], crs=WORKING_CRS))


def connected_network(
    products: dict[str, gpd.GeoDataFrame], tiles: dict[str, vr.TileSpec]
) -> dict[str, Any]:
    """Largest component of the stitched, snapped, repaired geometry of the read tiles."""
    import networkx as nx

    from histnet import stitch as ab
    from histnet.accessibility import snap_ends

    roads = gpd.GeoDataFrame(
        pd.concat(list(products.values()), ignore_index=True), crs=WORKING_CRS
    )
    joins, _gaps = ab.stitch(roads, [tiles[t] for t in products])
    repairs = read_layer("repairs")
    lines = [g for g in roads.geometry if g is not None and not g.is_empty]
    lines += list(joins.geometry) if len(joins) else []
    lines += list(repairs.geometry) if len(repairs) else []
    pieces, _ = snap_ends(lines)
    g = nx.Graph()
    for i, pc in enumerate(pieces):
        a = (round(pc.coords[0][0], 1), round(pc.coords[0][1], 1))
        b = (round(pc.coords[-1][0], 1), round(pc.coords[-1][1], 1))
        g.add_edge(a, b, piece=i, length=pc.length)
    best, best_len = set(), -1.0
    total = 0.0
    for comp in nx.connected_components(g):
        ln = sum(d["length"] for _, _, d in g.subgraph(comp).edges(data=True))
        total += ln
        if ln > best_len:
            best, best_len = comp, ln
    sub = g.subgraph(best)
    main_pieces = [pieces[d["piece"]] for _, _, d in sub.edges(data=True)]
    ends = [n for n in sub.nodes if sub.degree(n) == 1]
    cells: set[tuple[int, int]] = set()
    for pc in main_pieces:
        n = max(2, int(pc.length // SAMPLE_M) + 2)
        for t in np.linspace(0, pc.length, n):
            q = pc.interpolate(float(t))
            cells.add((math.floor(q.x / CELL_M), math.floor(q.y / CELL_M)))
    return {"cells": cells, "ends": ends, "main_km": best_len / 1000, "total_km": total / 1000,
            "components": nx.number_connected_components(g)}  # fmt: skip


def strict_frontier(net: dict[str, Any], attempted: set[tuple[int, int]]) -> set[tuple[int, int]]:
    out = set()
    for x, y in net["ends"]:
        ix, iy = math.floor(x / CELL_M), math.floor(y / CELL_M)
        if (ix, iy) not in attempted:
            continue
        for side, (dx, dy) in NEIGHBOURS.items():
            if Point(x, y).distance(shared_edge(ix, iy, side)) <= EDGE_M:
                n = (ix + dx, iy + dy)
                if n not in attempted:
                    out.add(n)
    return out


def run_search_strict() -> dict[str, Any]:
    from histnet import repair as corridor_repair

    state = load_state()
    parishes = parish_cells()
    tiles = read_manifest()
    products: dict[str, gpd.GeoDataFrame] = {}
    for tid, v in state["tiles"].items():
        if v.get("status") == "ok":
            prod = tile_product(tiles[tid])
            if prod is not None and len(prod):
                products[tid] = prod
    state.setdefault("strict_rounds", [])
    repaired_at_stall = False
    while True:
        tiles = read_manifest()
        net = connected_network(products, tiles)
        done = {p for p, cells in parishes.items() if cells & net["cells"]}
        todo = {p: c for p, c in parishes.items() if p not in done}
        attempted = {tuple(v["cell"]) for v in state["tiles"].values()}
        frontier = strict_frontier(net, attempted)
        log = {
            "round": len(state["rounds"]), "read": len(state["tiles"]),
            "connected_parishes": len(done), "parishes": len(parishes), "frontier": len(frontier),
            "main_km": round(net["main_km"], 1), "total_km": round(net["total_km"], 1),
            "components": net["components"], "rule": "geometric",
        }  # fmt: skip
        print(json.dumps(log), flush=True)
        if not todo:
            state["stop_strict"] = "all parishes connected (geometric rule)"
            break
        if len(state["tiles"]) >= STRICT_CAP:
            state["stop_strict"] = "tile cap"
            break
        cells = choose(
            frontier,
            set().union(*todo.values()),
            min(ROUND_SIZE, STRICT_CAP - len(state["tiles"])),
        )
        if not cells:
            if repaired_at_stall:
                state["stop_strict"] = "stalled after a repair pass"
                state["not_connected_strict"] = sorted(todo)
                break
            # stall: write the current network, run one repair pass, recompute
            save_state(state)
            assemble_keep_repairs()
            corridor_repair.repair()
            repaired_at_stall = True
            state["strict_rounds"].append({**log, "kind": "repair pass at stall"})
            continue
        repaired_at_stall = False
        rnd = len(state["rounds"])
        state["rounds"].append(
            {"round": rnd, "cells": cells, "kind": "search (geometric rule)", **log}
        )
        state["strict_rounds"].append({**log, "kind": "search", "cells": cells})
        save_state(state)
        process(cells, state, rnd)
        for ix, iy in cells:
            tid = tile_id(ix, iy)
            if state["tiles"].get(tid, {}).get("status") == "ok":
                prod = tile_product(read_manifest()[tid])
                if prod is not None and len(prod):
                    products[tid] = prod
    save_state(state)
    return {"stop": state["stop_strict"], "tiles": len(state["tiles"])}


def assemble_keep_repairs() -> None:
    """Rewrite roads_1890s.gpkg for the current tiles without losing the repairs layer."""
    repairs = read_layer("repairs")
    assemble()
    if len(repairs):
        repairs.to_file(ROADS_PATH, layer="repairs", driver="GPKG")


FILL_BATCH = 24


def core_cells_all() -> list[tuple[int, int]]:
    core = core_geometry()
    x0, y0, x1, y1 = core.bounds
    return sorted(
        (ix, iy)
        for ix in range(math.floor(x0 / CELL_M), math.ceil(x1 / CELL_M))
        for iy in range(math.floor(y0 / CELL_M), math.ceil(y1 / CELL_M))
        if core.contains(Point((ix + 0.5) * CELL_M, (iy + 0.5) * CELL_M))
    )


def run_fill_core() -> dict[str, Any]:
    """Option B: read every core cell not yet attempted, in batches."""
    state = load_state()
    attempted = {tuple(v["cell"]) for v in state["tiles"].values()}
    todo = [c for c in core_cells_all() if c not in attempted]
    print(json.dumps({"core_cells": len(core_cells_all()), "to_read": len(todo)}), flush=True)
    for i in range(0, len(todo), FILL_BATCH):
        cells = todo[i : i + FILL_BATCH]
        rnd = len(state["rounds"])
        state["rounds"].append({"round": rnd, "cells": cells, "kind": "fill core (option B)"})
        save_state(state)
        process(cells, state, rnd)
        print(json.dumps({"round": rnd, "read": len(state["tiles"])}), flush=True)
    state["stop_fill"] = "every core cell attempted"
    save_state(state)
    return {"tiles": len(state["tiles"])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("mosaic", "run", "run-strict", "fill-core", "assemble"):
        sub.add_parser(c)
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    if args.cmd == "mosaic":
        print(json.dumps(build_mosaic()))
    elif args.cmd == "run":
        print(json.dumps(run_search()))
    elif args.cmd == "fill-core":
        print(json.dumps(run_fill_core()))
    elif args.cmd == "run-strict":
        print(json.dumps(run_search_strict()))
    elif args.cmd == "assemble":
        summary = assemble()
        summary["results"] = str(write_results(summary))
        print(json.dumps(summary))
    print(f"compute {time.perf_counter() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
