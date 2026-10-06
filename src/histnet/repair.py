"""Best-guess repair pass for the corridor 1890s network (corridor plan, repair addendum).

One Opus 5.5 agent looks at each loose component near the main network, on a crop of the sheet
mosaic, and either connects it along what is drawn or leaves it. Repairs are a separate layer,
each labelled ``agent repair (best guess)``; no reviewed line is moved or deleted.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.windows import from_bounds
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import nearest_points

from histnet import corridor as cor
from histnet.accessibility import snap_ends
from histnet.paths import PROMPTS, WORKING_CRS

METHOD_VERSION = "corridor-repair-0.1"
OUT_DIR = cor.OUT_DIR / "repair"
CASES_DIR = OUT_DIR / "cases"
MIN_COMPONENT_M = 100.0
MAX_GAP_M = 400.0
CROP_M = 400.0
CROP_PX = 600
BATCH = 20
LABEL = "agent repair (best guess)"


def components(roads: gpd.GeoDataFrame, joins: gpd.GeoDataFrame) -> list[dict[str, Any]]:
    lines = [g for g in roads.geometry if g is not None and not g.is_empty]
    lines += list(joins.geometry) if len(joins) else []
    pieces, _ = snap_ends(lines)
    g = nx.Graph()
    for i, p in enumerate(pieces):
        a = tuple(np.round(p.coords[0], 1))
        b = tuple(np.round(p.coords[-1], 1))
        g.add_edge(a, b, piece=i, length=p.length)
    comps = []
    for nodes in nx.connected_components(g):
        sub = g.subgraph(nodes)
        geoms = [pieces[d["piece"]] for _, _, d in sub.edges(data=True)]
        ends = [Point(n) for n in nodes if sub.degree(n) == 1]
        comps.append(
            {"geom": MultiLineString(geoms), "length": sum(x.length for x in geoms), "ends": ends}
        )
    comps.sort(key=lambda c: -c["length"])
    return comps


def make_cases(comps: list[dict[str, Any]], main: Any, prefix: str) -> list[dict[str, Any]]:
    cases = []
    others = [c for c in comps[1:] if c["length"] >= MIN_COMPONENT_M]
    others.sort(key=lambda c: c["geom"].distance(main))
    for k, c in enumerate(others):
        d = c["geom"].distance(main)
        if d > MAX_GAP_M:
            continue
        a0, b0 = nearest_points(c["geom"], main)
        a_pts = [a0] + sorted(c["ends"], key=lambda p: p.distance(main))[:2]
        a_pts = [p for i, p in enumerate(a_pts) if all(p.distance(q) > 5 for q in a_pts[:i])]
        b_pts = [nearest_points(p, main)[1] for p in a_pts]
        b_pts = [p for i, p in enumerate(b_pts) if all(p.distance(q) > 5 for q in b_pts[:i])]
        cases.append(
            {
                "case_id": f"{prefix}{k + 1:03d}",
                "gap_m": round(d, 1),
                "component_m": round(c["length"], 1),
                "A": [(p.x, p.y) for p in a_pts],
                "B": [(p.x, p.y) for p in b_pts],
                "centre": ((a0.x + b0.x) / 2, (a0.y + b0.y) / 2),
                "component": c["geom"],
            }
        )
    return cases


def crop_affine(centre: tuple[float, float]) -> tuple[float, float, float]:
    """(x0, y_top, metres per pixel) of a case image."""
    return centre[0] - CROP_M / 2, centre[1] + CROP_M / 2, CROP_M / CROP_PX


def to_uv(xy: tuple[float, float], aff: tuple[float, float, float]) -> tuple[float, float]:
    x0, yt, m = aff
    return (xy[0] - x0) / m, (yt - xy[1]) / m


def to_xy(uv: tuple[float, float], aff: tuple[float, float, float]) -> tuple[float, float]:
    x0, yt, m = aff
    return x0 + uv[0] * m, yt - uv[1] * m


def render_case(case: dict[str, Any], main: Any) -> Path:
    aff = crop_affine(case["centre"])
    x0, yt, _m = aff
    with rasterio.open(cor.VRT_PATH) as ds:
        arr = ds.read(
            window=from_bounds(x0, yt - CROP_M, x0 + CROP_M, yt, ds.transform),
            out_shape=(3, CROP_PX, CROP_PX),
            boundless=True,
            fill_value=255,
            resampling=rasterio.enums.Resampling.cubic,
        )
    im = Image.fromarray(np.moveaxis(arr, 0, -1))
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=16)

    def draw(geom: Any, colour: tuple[int, int, int]) -> None:
        parts = geom.geoms if hasattr(geom, "geoms") else [geom]
        for p in parts:
            if p.geom_type == "LineString":
                d.line([to_uv(c, aff) for c in p.coords], fill=colour, width=3)

    draw(main, (220, 0, 0))
    draw(case["component"], (0, 70, 220))
    for side, colour in (("A", (0, 70, 220)), ("B", (220, 0, 0))):
        for i, xy in enumerate(case[side]):
            u, v = to_uv(xy, aff)
            d.ellipse([u - 6, v - 6, u + 6, v + 6], outline=colour, width=3)
            d.rectangle([u + 7, v - 9, u + 34, v + 9], fill=(255, 255, 255))
            d.text((u + 9, v - 9), f"{side}{i + 1}", fill=colour, font=font)
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    path = CASES_DIR / f"{case['case_id']}.png"
    im.save(path)
    return path


TASK_FILE = PROMPTS / "repair_task.md"


def run_batch(batch: list[dict[str, Any]], images: dict[str, Path], n: int) -> dict[str, Any]:
    out = OUT_DIR / f"batch_{n:02d}_answer.json"
    lines = [f"- {c['case_id']}: {images[c['case_id']]}" for c in batch]
    head = TASK_FILE.read_text(encoding="utf-8").replace("<output path>", str(out))
    text = head + "\n".join(lines) + "\n"
    task = OUT_DIR / f"batch_{n:02d}_task.txt"
    task.write_text(text, encoding="utf-8")
    log = OUT_DIR / f"batch_{n:02d}.stream.jsonl"
    t0 = time.time()
    cmd = [
        "claude", "-p", "--model", cor.REQUIRED_MODEL, "--output-format", "stream-json",
        "--verbose", "--allowedTools", "Read", "Write", "--disallowedTools", "Bash", "Edit",
        "WebFetch", "WebSearch", "Agent", "Task", "NotebookEdit", "--add-dir", str(cor.OUT_DIR),
        "--", text,
    ]  # fmt: skip
    cor.CLI_ROOT.mkdir(parents=True, exist_ok=True)
    with log.open("w") as fh:
        subprocess.run(
            cmd, cwd=cor.CLI_ROOT, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            timeout=3600,
        )  # fmt: skip
    check = cor.check_stream(log, task, out)
    # check_stream's answer test looks for "reviews"; a repair answer holds "cases"
    ok = (
        check["models"] == [cor.REQUIRED_MODEL]
        and check["tools_ok"]
        and cor.answer_ok(out, "cases")
    )
    return {
        **check,
        "ok": ok,
        "batch": n,
        "wall_s": round(time.time() - t0, 1),
        "answer": str(out),
    }


def apply_answers(cases: list[dict[str, Any]], answers: dict[str, dict]) -> list[dict[str, Any]]:
    rows = []
    for c in cases:
        a = answers.get(c["case_id"])
        if not a or a.get("decision") != "connect":
            continue
        aff = crop_affine(c["centre"])
        try:
            ai = int(str(a["from"]).lstrip("A")) - 1
            bi = int(str(a["to"]).lstrip("B")) - 1
            start, end = c["A"][ai], c["B"][bi]
        except (KeyError, ValueError, IndexError):
            continue
        mid = [to_xy(tuple(w), aff) for w in a.get("waypoints_uv") or []]
        pts = [start, *mid, end]
        pts = [
            p for i, p in enumerate(pts) if i == 0 or Point(p).distance(Point(pts[i - 1])) > 0.5
        ]
        if len(pts) < 2:
            continue
        rows.append(
            {
                "case_id": c["case_id"], "label": LABEL, "reason": str(a.get("reason", ""))[:300],
                "gap_m": c["gap_m"], "component_m": c["component_m"], "geometry": LineString(pts),
            }
        )  # fmt: skip
    return rows


def repair() -> dict[str, Any]:
    roads = gpd.read_file(cor.ROADS_PATH, layer="roads")
    try:
        joins = gpd.read_file(cor.ROADS_PATH, layer="seam_joins")
    except Exception:  # noqa: BLE001
        joins = gpd.GeoDataFrame({"kind": []}, geometry=gpd.GeoSeries([], crs=WORKING_CRS))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_rows, logs, summary = [], [], []
    extra = joins
    for pass_no, prefix in enumerate("RSTUVWXY", start=1):  # until a pass connects nothing, max 8
        comps = components(roads, extra)
        main = comps[0]["geom"]
        cases = make_cases(comps, main, prefix)
        summary.append(
            {
                "pass": pass_no, "components": len(comps),
                "components_ge_100m": sum(c["length"] >= MIN_COMPONENT_M for c in comps[1:]),
                "cases": len(cases), "main_km": round(comps[0]["length"] / 1000, 1),
                "total_km": round(sum(c["length"] for c in comps) / 1000, 1),
            }
        )  # fmt: skip
        if not cases:
            break
        images = {c["case_id"]: render_case(c, main) for c in cases}
        answers: dict[str, dict] = {}
        for n, i in enumerate(range(0, len(cases), BATCH), start=len(logs) + 1):
            rec = run_batch(cases[i : i + BATCH], images, n)
            logs.append(rec)
            if rec["ok"]:
                doc, _ = cor.vr.extract_json_object(Path(rec["answer"]).read_text())
                answers.update({str(x.get("case_id")): x for x in doc.get("cases", [])})
        rows = apply_answers(cases, answers)
        all_rows += rows
        for c in cases:
            a = answers.get(c["case_id"], {})
            c.update(decision=a.get("decision", "no answer"), reason=a.get("reason", ""))
        pd.DataFrame(
            [{k: v for k, v in c.items() if k not in ("component",)} for c in cases]
        ).astype({"A": str, "B": str, "centre": str}).to_parquet(
            OUT_DIR / f"cases_pass{pass_no}.parquet"
        )
        if not rows:
            break
        # every repair so far stays in the network for the next pass (cumulative)
        rep = gpd.GeoDataFrame(all_rows, crs=WORKING_CRS)
        extra = gpd.GeoDataFrame(
            pd.concat([joins[["geometry"]], rep[["geometry"]]], ignore_index=True), crs=WORKING_CRS
        )
    rep = gpd.GeoDataFrame(all_rows, crs=WORKING_CRS) if all_rows else None
    if rep is not None:
        rep["method_version"] = METHOD_VERSION
        rep.to_file(cor.ROADS_PATH, layer="repairs", driver="GPKG")
    (OUT_DIR / "repair_log.json").write_text(
        json.dumps({"passes": summary, "batches": logs}, indent=1, default=str) + "\n"
    )
    after = components(roads, extra)
    return {
        "passes": summary,
        "repairs": 0 if rep is None else len(rep),
        "repair_km": 0.0 if rep is None else round(rep.geometry.length.sum() / 1000, 2),
        "components_after": len(after),
        "main_share_after": round(after[0]["length"] / sum(c["length"] for c in after), 3),
        "batches_ok": sum(b["ok"] for b in logs),
        "batches": len(logs),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["run"])
    ap.parse_args(argv)
    t0 = time.perf_counter()
    print(json.dumps(repair(), default=str))
    print(f"compute {time.perf_counter() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
