"""Draw the three routable networks side by side (report/figures/networks.png).

Reads what `make demo` writes to work/ (1890s road lines and repairs, the 1960s network) and the
shipped modern network. Same extent and scale in every map, for general impressions.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from histnet import accessibility as acc  # noqa: E402
from histnet import corridor as cor  # noqa: E402
from histnet import network1960s as n60  # noqa: E402
from histnet.paths import DATA, WORKING_CRS  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "figures" / "networks.png"
ROAD = "#2b2b2b"
GUESS = "#e07b00"
RAIL = "#c0162c"
LW = 0.25


def segments_from_lines(geoms) -> list[np.ndarray]:
    out = []
    for g in geoms:
        if g is None or g.is_empty:
            continue
        for part in getattr(g, "geoms", [g]):
            out.append(np.asarray(part.coords)[:, :2])
    return out


def node_segments(edges: pd.DataFrame, pos: pd.DataFrame) -> np.ndarray:
    e = edges[edges["u"].isin(pos.index) & edges["v"].isin(pos.index)]
    return np.stack([pos.loc[e["u"]].to_numpy(), pos.loc[e["v"]].to_numpy()], axis=1)


def inside(segs: list[np.ndarray] | np.ndarray, area) -> list[np.ndarray]:
    """Segments whose midpoint lies in the mapped area (all maps show the same area)."""
    import shapely

    segs = list(segs)
    if not segs:
        return segs
    mid = np.array([s[len(s) // 2] if len(s) > 2 else s.mean(axis=0) for s in segs])
    keep = shapely.contains_xy(area, mid[:, 0], mid[:, 1])
    return [s for s, k in zip(segs, keep, strict=True) if k]


def main() -> Path:
    pts = acc.places()
    parishes = pts[pts["level"] == "parish"]
    outline = gpd.GeoSeries([unary_union(list(parishes["polygon"]))], crs=WORKING_CRS)
    bounds = outline.total_bounds
    area = outline.iloc[0]
    town = pts[pts["place"] == acc.ALINGSAS].geometry.iloc[0]
    read_area = gpd.read_file(cor.ROADS_PATH, layer="tiles").union_all()

    nodes = pd.read_parquet(DATA / "modern_network" / "nodes.parquet")
    nodes = nodes[nodes["in_graph_ring"]].set_index("node_id")[["x", "y"]]

    # 1890s: read lines and seam joins; AI repairs (loose pieces and missing links)
    r90 = segments_from_lines(gpd.read_file(cor.ROADS_PATH, layer="roads").geometry)
    r90 += segments_from_lines(gpd.read_file(cor.ROADS_PATH, layer="seam_joins").geometry)
    g90 = segments_from_lines(gpd.read_file(cor.ROADS_PATH, layer="repairs").geometry)

    # 1960s: today's roads kept by the 1960s sheets; bridged gaps; the VGJ (walking variant)
    e60 = pd.read_parquet(n60.EDGES_PATH)
    road60 = e60[e60["mode"] == "road"]
    kept = node_segments(road60[road60["status_1960s"] != n60.GAP_STATUS], nodes)
    gap = node_segments(road60[road60["status_1960s"] == n60.GAP_STATUS], nodes)
    con = pd.read_parquet(DATA / "vgj_1963" / "rail_connectors.parquet")
    con = con[con["grafsnas_candidate"] == "SC-01"].drop_duplicates("station_key")
    st_xy = {r.station_key: tuple(nodes.loc[r.road_node_id]) for r in con.itertuples()
             if r.road_node_id in nodes.index}  # fmt: skip
    st_xy["grafsnas"] = n60.GRAFSNAS_XY
    st = pd.read_parquet(DATA / "vgj_1963" / "state_edges.parquet")
    st = st[st["grafsnas_candidate"] == "SC-01"]
    pairs = zip(st["from_key"], st["to_key"], strict=True)
    vgj = [np.array([st_xy[a], st_xy[b]]) for a, b in pairs if a in st_xy and b in st_xy]

    # today: OpenStreetMap roads, no railways
    me = pd.read_parquet(DATA / "modern_network" / "edges.parquet", columns=["u", "v", "mode"])
    today = node_segments(me[me["mode"] == "road"], nodes)

    fig, axes = plt.subplots(1, 3, figsize=(15, 6.4))
    maps = [
        ("1890s", "roads read from Häradsekonomiska kartan by AI models",
         [(r90, ROAD, "read roads"), (g90, GUESS, "AI repairs (best guesses)")],
         "Roads read from Häradsekonomiska kartan · map origin: Lantmäteriet"),
        ("1960s", "today's roads checked against Ekonomiska kartan 1963–74",
         [(list(kept), ROAD, "roads on the 1960s map"), (list(gap), GUESS, "short gaps bridged")],
         "© OpenStreetMap contributors (ODbL), checked against Ekonomiska kartan · map origin: "
         "Lantmäteriet"),
        ("today", "OpenStreetMap roads (no railways)", [(list(today), ROAD, "roads")],
         "© OpenStreetMap contributors (ODbL)"),
    ]  # fmt: skip
    for ax, (label, sub, layers, badge) in zip(axes, maps, strict=True):
        outline.plot(ax=ax, facecolor="#f4f1ea", edgecolor="#9a9a9a", linewidth=0.6)
        parishes_b = gpd.GeoSeries(list(parishes["polygon"]), crs=WORKING_CRS).boundary
        parishes_b.plot(ax=ax, color="#c9c4b8", linewidth=0.35)
        gpd.GeoSeries([read_area], crs=WORKING_CRS).boundary.plot(
            ax=ax, color="#5a7fb0", linewidth=0.8, linestyle=(0, (4, 2))
        )
        handles = []
        for segs, colour, name in layers:
            lw = LW if colour == ROAD else 0.9
            segs = inside(segs, area)
            ax.add_collection(LineCollection(segs, colors=colour, linewidths=lw))
            handles.append(Line2D([], [], color=colour, lw=1.5, label=name))
        if label == "1960s" and vgj:
            ax.add_collection(LineCollection(vgj, colors=RAIL, linewidths=1.6, linestyles="--"))
            xs, ys = zip(*[v for k, v in st_xy.items() if k != "goteborg_c"], strict=True)
            ax.scatter(xs, ys, s=10, color=RAIL, zorder=5)
            handles.append(Line2D([], [], color=RAIL, lw=1.5, ls="--", marker="o", ms=3,
                                  label="VGJ, stations (walking variant only)"))  # fmt: skip
        ax.scatter([town.x], [town.y], s=60, marker="s", facecolor="white", edgecolor="black",
                   zorder=6)  # fmt: skip
        ax.annotate("Alingsås", (town.x, town.y), xytext=(6, -10), textcoords="offset points",
                    fontsize=8, fontweight="bold")  # fmt: skip
        handles.append(Line2D([], [], color="#5a7fb0", lw=1, ls="--",
                              label="area read for the 1890s"))  # fmt: skip
        ax.set_xlim(bounds[0] - 1500, bounds[2] + 1500)
        ax.set_ylim(bounds[1] - 1500, bounds[3] + 1500)
        ax.set_aspect("equal")
        ax.set_axis_off()
        ax.set_title(f"{label}\n{sub}", fontsize=10)
        ax.legend(handles=handles, loc="upper left", fontsize=7, frameon=True, framealpha=0.9)
        x0, y0 = bounds[0], bounds[1] - 500  # 10 km scale bar
        ax.plot([x0, x0 + 10_000], [y0, y0], color="black", lw=2)
        ax.text(x0 + 5_000, y0 + 700, "10 km", ha="center", fontsize=7)
        ax.text(0.0, -0.03, badge, transform=ax.transAxes, fontsize=6, color="#333333",
                bbox={"facecolor": "white", "edgecolor": "#888888",
                      "boxstyle": "round,pad=0.25"})  # fmt: skip
    fig.suptitle("The three routable networks (same extent and scale; north up)", fontsize=12)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.88, bottom=0.05, wspace=0.03)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=170, facecolor="white")
    return OUT


if __name__ == "__main__":
    print(main())
