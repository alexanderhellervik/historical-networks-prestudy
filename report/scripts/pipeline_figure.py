"""Draw the pipeline overview used in the report (report/figures/pipeline.png)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "figures" / "pipeline.png"

ROWS = [
    ("1890s", "#8c5a2b", [
        "Härads-\nekonomiska\nkartan", "1 km tiles", "AI reading\n(Codex)",
        "AI review\n(Claude)", "read every tile\nin the core area", "stitch, snap,\nAI repairs",
    ]),
    ("1960s", "#2b6a8c", [
        "Ekonomiska\nkartan", "today's roads\n(OSM)", "check roads\nagainst map ink",
        "drop roads\nnot on the map", "keep realigned;\nbridge gaps\n≤ 300 m", "",
    ]),
    ("today", "#4a7a3a", ["OpenStreetMap", "", "", "", "", "roads only"]),
]  # fmt: skip
SHARED = ["routable\nnetwork", "population on\n250 m cells",
          "accessibility\n(HierX 0.1.1\nor exact)", "maps"]  # fmt: skip


def box(ax, x: float, y: float, text: str, color: str, w: float = 1.55, h: float = 0.62) -> None:
    ax.add_patch(
        FancyBboxPatch(
            (x - w / 2, y - h / 2),
            w,
            h,
            boxstyle="round,pad=0.03,rounding_size=0.08",
            fc=color,
            ec="none",
            alpha=0.92,
        )  # fmt: skip
    )
    ax.text(x, y, text, ha="center", va="center", color="white", fontsize=8.2)


def main() -> None:
    fig, ax = plt.subplots(figsize=(15, 4.6))
    ys = [2.2, 1.1, 0.0]
    for (label, color, steps), y in zip(ROWS, ys, strict=True):
        ax.text(-0.45, y, label, ha="right", va="center", fontsize=11, weight="bold", color=color)
        xs = [i * 1.8 + 0.5 for i, s in enumerate(steps) if s]
        for i, s in enumerate(steps):
            if s:
                box(ax, i * 1.8 + 0.5, y, s, color)
        for a, b in zip(xs[:-1], xs[1:], strict=True):
            ax.annotate("", (b - 0.8, y), (a + 0.8, y), arrowprops={"arrowstyle": "->", "lw": 1})
        ax.annotate("", (11.15, 1.1), (xs[-1] + 0.8, y),
                    arrowprops={"arrowstyle": "->", "lw": 1, "color": "#777"})  # fmt: skip
    for i, s in enumerate(SHARED):
        box(ax, 12.0 + i * 1.8, 1.1, s, "#444444")
        if i:
            ax.annotate("", (12.0 + i * 1.8 - 0.8, 1.1), (12.0 + (i - 1) * 1.8 + 0.8, 1.1),
                        arrowprops={"arrowstyle": "->", "lw": 1})  # fmt: skip
    box(ax, 12.0 + 1.8, 2.2, "FOLKNET parish\npopulation\nof the period", "#7a6a9a")
    ax.annotate("", (12.0 + 1.8, 1.1 + 0.33), (12.0 + 1.8, 2.2 - 0.33),
                arrowprops={"arrowstyle": "->", "lw": 1})  # fmt: skip
    box(ax, 12.0, -0.2, "VGJ railway,\n1963 timetable", "#c0162c")
    ax.annotate("", (12.0, 1.1 - 0.33), (12.0, -0.2 + 0.33),
                arrowprops={"arrowstyle": "->", "lw": 1, "ls": "--"})  # fmt: skip
    ax.text(12.0 + 0.85, -0.2, "1960s walking\nvariant only", ha="left", va="center",
            fontsize=7.5, color="#c0162c")  # fmt: skip
    ax.set_xlim(-1.4, 18.2)
    ax.set_ylim(-0.7, 2.8)
    ax.axis("off")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=150, bbox_inches="tight", facecolor="white")


if __name__ == "__main__":
    main()
