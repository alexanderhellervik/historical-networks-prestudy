"""Cut repair for the corridor 1890s network (after the cumulative repair).

The cumulative repair joins separate pieces to the main network. This pass looks inside the main
network: a dead end with another part of the network within ``MAX_GAP_M`` in a straight line but
more than ``DETOUR_M`` away along the network becomes a case, shown to the same Opus 5.5 agent on a
crop of the sheet mosaic. Connections are appended to the repairs layer with their own label; no
reviewed line is moved or deleted.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely import STRtree
from shapely.geometry import MultiLineString, Point
from shapely.ops import nearest_points

from histnet import corridor as cor
from histnet import repair as rep
from histnet.accessibility import snap_ends
from histnet.paths import PROMPTS, WORKING_CRS

METHOD_VERSION = "corridor-cut-repair-0.1"
LABEL = "agent cut repair (best guess)"
OUT_DIR = cor.OUT_DIR / "cut_repair"
TASK_FILE = PROMPTS / "cut_repair_task.md"
MAX_GAP_M = 400.0
DETOUR_M = 2000.0
DEDUPE_M = 150.0
MAX_TARGETS = 2


def main_graph() -> tuple[nx.Graph, list[Any]]:
    """The largest connected part of roads + seam joins + repairs, after end snapping."""
    roads = gpd.read_file(cor.ROADS_PATH, layer="roads")
    lines = [g for g in roads.geometry if g is not None and not g.is_empty]
    for layer in ("seam_joins", "repairs"):
        try:
            lines += list(gpd.read_file(cor.ROADS_PATH, layer=layer).geometry)
        except Exception:  # noqa: BLE001 - an absent layer is a valid state
            pass
    pieces, _ = snap_ends(lines)
    g = nx.Graph()
    for i, p in enumerate(pieces):
        a, b = tuple(np.round(p.coords[0], 1)), tuple(np.round(p.coords[-1], 1))
        if a != b:
            g.add_edge(a, b, piece=i, length=p.length)
    main = max(nx.connected_components(g), key=lambda c: sum(
        d["length"] for _, _, d in g.subgraph(c).edges(data=True)))  # fmt: skip
    return g.subgraph(main).copy(), pieces


def make_cut_cases(g: nx.Graph, pieces: list[Any]) -> list[dict[str, Any]]:
    edges = list(g.edges(data=True))
    geoms = [pieces[d["piece"]] for _, _, d in edges]
    tree = STRtree(geoms)
    main_geom = MultiLineString(geoms)
    ends = sorted(n for n in g.nodes if g.degree(n) == 1)
    cases: list[dict[str, Any]] = []
    for e in ends:
        pe = Point(e)
        if any(pe.distance(Point(c["A"][0])) < DEDUPE_M for c in cases):
            continue
        near = nx.single_source_dijkstra_path_length(g, e, cutoff=DETOUR_M, weight="length")
        cand = []
        for k in tree.query(pe.buffer(MAX_GAP_M)):
            a, b, _d = edges[k]
            if a in near or b in near:
                continue  # reachable within the detour limit (or the edge touches that area)
            dist = geoms[k].distance(pe)
            if dist <= MAX_GAP_M:
                cand.append((dist, k))
        if not cand:
            continue
        cand.sort()
        targets: list[Point] = []
        for _dist, k in cand:
            t = nearest_points(pe, geoms[k])[1]
            if all(t.distance(q) > 25 for q in targets):
                targets.append(t)
            if len(targets) == MAX_TARGETS:
                break
        own = [geoms[i] for i, (a, b, _) in enumerate(edges) if a in near and b in near]
        own = [x for x in own if x.distance(pe) <= MAX_GAP_M * 1.5]
        gap = cand[0][0]
        cases.append(
            {
                "case_id": f"K{len(cases) + 1:03d}",
                "gap_m": round(gap, 1),
                "component_m": DETOUR_M,  # the network distance is more than this
                "A": [(pe.x, pe.y)],
                "B": [(t.x, t.y) for t in targets],
                "centre": ((pe.x + targets[0].x) / 2, (pe.y + targets[0].y) / 2),
                "component": MultiLineString(own) if own else MultiLineString([]),
            }
        )
    for c in cases:
        c["main"] = main_geom
    return cases


def run(dry: bool = False) -> dict[str, Any]:
    g, pieces = main_graph()
    cases = make_cut_cases(g, pieces)
    summary: dict[str, Any] = {
        "main_km": round(sum(d["length"] for _, _, d in g.edges(data=True)) / 1000, 1),
        "dead_ends": sum(1 for n in g.nodes if g.degree(n) == 1),
        "cases": len(cases),
    }
    if dry or not cases:
        return summary
    # the repair module's image, agent and answer code, pointed at this pass's folder and prompt
    rep.OUT_DIR, rep.CASES_DIR, rep.TASK_FILE, rep.LABEL = (
        OUT_DIR, OUT_DIR / "cases", TASK_FILE, LABEL)  # fmt: skip
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    images = {c["case_id"]: rep.render_case(c, c["main"]) for c in cases}
    answers: dict[str, dict] = {}
    logs = []
    for n, i in enumerate(range(0, len(cases), rep.BATCH), start=1):
        rec = rep.run_batch(cases[i : i + rep.BATCH], images, n)
        logs.append(rec)
        if rec["ok"]:
            doc, _ = cor.vr.extract_json_object(Path(rec["answer"]).read_text())
            answers.update({str(x.get("case_id")): x for x in doc.get("cases", [])})
    rows = rep.apply_answers(cases, answers)
    for c in cases:
        a = answers.get(c["case_id"], {})
        c.update(decision=a.get("decision", "no answer"), reason=a.get("reason", ""))
    pd.DataFrame(
        [{k: v for k, v in c.items() if k not in ("component", "main")} for c in cases]
    ).astype({"A": str, "B": str, "centre": str}).to_parquet(OUT_DIR / "cases.parquet")
    before = gpd.read_file(cor.ROADS_PATH, layer="repairs")
    if rows:
        new = gpd.GeoDataFrame(rows, crs=WORKING_CRS)
        new["method_version"] = METHOD_VERSION
        both = gpd.GeoDataFrame(pd.concat([before, new], ignore_index=True), crs=WORKING_CRS)
        both.to_file(cor.ROADS_PATH, layer="repairs", driver="GPKG")
    summary.update(
        connections=len(rows),
        connection_km=round(sum(r["geometry"].length for r in rows) / 1000, 2),
        batches=len(logs),
        batches_ok=sum(b["ok"] for b in logs),
        models=sorted({m for b in logs for m in b.get("models", [])}),
    )
    (OUT_DIR / "cut_repair_log.json").write_text(
        json.dumps({"summary": summary, "batches": logs}, indent=1, default=str) + "\n"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["count", "run"])
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    print(json.dumps(run(dry=args.cmd == "count"), default=str))
    print(f"compute {time.perf_counter() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
