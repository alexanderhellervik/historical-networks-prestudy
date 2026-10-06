"""`make demo`: rebuild the three networks and their accessibility maps without calling a model.

Uses the files shipped in ``data/`` (the models' answers for the 1890s tiles and their image-to-map
transforms, the agent's repairs, the 1960s scored road edges, the VGJ edges, the corridor's modern
network) plus your own downloads in ``inputs/`` (FOLKNET population, histmaps boundaries; see
SOURCES.md). Writes to ``work/``.

Steps: 1890s road lines from the answers (stitched, snapped, repaired); the 1960s network; then for
each period 250 m cells, population along the roads, accessibility with HierX 0.1.1, exact values
where a map uses them, and the maps.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time

import geopandas as gpd

from histnet import accessibility as acc
from histnet import corridor as cor
from histnet import network1960s as n60
from histnet.paths import DATA

PERIODS_HIERX = ("today", "1960s")  # car-scale maps use HierX
PERIODS_EXACT = ("1890s", "1960s_walk", "1960s_walk_novgj")  # walking speed: exact values


def stage_1890s() -> dict:
    """Copy the shipped 1890s answers into work/ and rebuild the road lines with repairs."""
    src = DATA / "corridor_1890s"
    cor.OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.jsonl", "search_state.json"):
        shutil.copy2(src / name, cor.OUT_DIR / name)
    out = cor.OUTPUTS
    out.mkdir(parents=True, exist_ok=True)
    for f in (src / "model_outputs").glob("*.json"):
        shutil.copy2(f, out / f.name)
    summary = cor.assemble()
    repairs = gpd.read_file(src / "repairs.gpkg", layer="repairs")
    repairs.to_file(cor.ROADS_PATH, layer="repairs", driver="GPKG")
    summary["repairs"] = len(repairs)
    return summary


def stage_1960s() -> dict:
    n60.OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DATA / "network_1960s" / "scored_edges.gpkg", n60.SCORED_PATH)
    return n60.build_network()


def stage_accessibility(exact_for_car: bool = False) -> dict:
    out = {}
    for period in (*PERIODS_HIERX, *PERIODS_EXACT):
        t0 = time.perf_counter()
        info = acc.write_surface_inputs(period)
        info["hierx"] = acc.hierx_surface(period)
        if period in PERIODS_EXACT or exact_for_car:
            info["exact"] = acc.exact_all(period)
        else:
            info["exact_sample"] = acc.exact_sample(period)
        col = "rel_exact" if period in PERIODS_EXACT else "rel_hierx"
        info["map"] = str(acc.draw_surface(period, acc.relative_scores(period), col=col))
        info["seconds"] = round(time.perf_counter() - t0, 1)
        out[period] = info
    out["vgj_gain_map"] = str(acc.draw_vgj_difference())
    out["minutes_to_alingsas"] = acc.travel_time_table().round(1).to_dict()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exact-for-car", action="store_true", help="also exact values for car maps")
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    report = {
        "1890s": stage_1890s(),
        "1960s": stage_1960s(),
        "accessibility": stage_accessibility(args.exact_for_car),
    }
    print(json.dumps(report, indent=1, default=str))
    print(f"compute {time.perf_counter() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
