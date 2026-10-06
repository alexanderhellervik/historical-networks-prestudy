"""Map-ink evidence for modern roads on a historical sheet (the 1960s method).

Each modern road edge is sampled across its width on the sheet; the fill-and-casing pattern of a
drawn road, or a near-continuous dark line, makes it present, weak evidence uncertain, and
nothing absent. Parameters were set once on Ekonomiska kartan sheet 7C 9b and are applied
unchanged to the other sheets of the series.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from scipy.ndimage import maximum_filter1d, minimum_filter1d, uniform_filter1d
from shapely.geometry import LineString, Point, Polygon

from histnet.paths import WORK
from histnet.util import require_crs

METHOD_VERSION = "stage3-historical-roads-0.1"


# below this length an edge carries too few perpendicular samples to be trusted on its own
SHORT_EDGE_M = 15.0


MAPS_DIR = WORK / "maps"


@dataclass(frozen=True)
class SheetSpec:
    """One historical map state, with everything needed to reproduce its geometry."""

    sheet_id: str
    label: str
    date_text: str
    raster: Path
    source_raw: str  # relative path under raw/, for the provenance stamp
    # profile thresholds, calibrated per sheet (see calibrate notes in diagnostics/roads.md)
    fill_min_grey: float
    fill_min_bright: float
    fill_grey_over_background: float
    casing_contrast: float
    dark_line_contrast: float
    # per-sheet decision thresholds; None falls back to EvidenceParams
    dark_present_threshold: float | None = None
    absent_threshold: float | None = None


ECON_1962 = SheetSpec(
    sheet_id="ekon_7C9b_1962",
    label="7C 9b 1962",
    date_text="aerial photography 1959, mapping completed 1962",
    raster=MAPS_DIR / "ekon_7C9b_1962_3006.tif",
    source_raw="lm_economic_map/52414b5f4a3133332d374339623634_1_2.tiff",
    fill_min_grey=0.33,
    fill_min_bright=0.44,
    fill_grey_over_background=0.06,
    casing_contrast=0.09,
    dark_line_contrast=0.18,
)


def build_edges(
    lines: gpd.GeoDataFrame,
    footprint: Polygon,
    class_column: str,
    max_edge_m: float = 250.0,
    min_edge_m: float = 1.0,
) -> gpd.GeoDataFrame:
    """Clip to the sheet, then cut into edges of at most ``max_edge_m``.

    Deterministic: ways are processed in ascending OSM id, parts in their stored order,
    and each part is divided into equal pieces. ``edge_id`` is ``<way id>_<part>_<piece>``.

    ``min_edge_m`` is deliberately small. Short pieces are the connectors at junctions, and
    dropping them shatters the network: at a 6 m cut-off 422 pieces (1.8 of 100.9 km)
    disappear and the edge graph falls from 4 components to 160. They are kept and instead
    marked ``confidence = low`` when they are too short to carry reliable evidence.
    """
    require_crs(lines)
    clipped = gpd.clip(lines.sort_values("id"), footprint)
    rows: list[dict[str, Any]] = []
    for _, r in clipped.iterrows():
        geom = r.geometry
        if geom is None or geom.is_empty:
            continue
        parts = [geom] if geom.geom_type == "LineString" else list(getattr(geom, "geoms", []))
        for pi, part in enumerate(parts):
            if part.geom_type != "LineString" or part.length < min_edge_m:
                continue
            n = max(1, int(np.ceil(part.length / max_edge_m)))
            cuts = np.linspace(0.0, part.length, n + 1)
            for k in range(n):
                piece = _substring(part, float(cuts[k]), float(cuts[k + 1]))
                if piece is None or piece.length < min_edge_m:
                    continue
                rows.append(
                    {
                        "edge_id": f"{int(r['id'])}_{pi}_{k}",
                        "osm_way_id": int(r["id"]),
                        "modern_class": r[class_column],
                        "geometry": piece,
                    }
                )
    if not rows:
        empty = gpd.GeoDataFrame(
            {
                c: pd.Series(dtype=t)
                for c, t in (
                    ("edge_id", "object"),
                    ("osm_way_id", "int64"),
                    ("modern_class", "object"),
                    ("length_m", "float64"),
                )
            },
            geometry=gpd.GeoSeries([], dtype="geometry"),
            crs=lines.crs,
        )
        return empty
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=lines.crs)
    out = out.sort_values("edge_id").reset_index(drop=True)
    out["length_m"] = out.geometry.length
    return out


def _substring(line: LineString, start: float, end: float) -> LineString | None:
    """Portion of ``line`` between two distances along it (shapely-version independent)."""
    if end - start <= 0:
        return None
    coords = [line.interpolate(start)]
    acc = 0.0
    pts = list(line.coords)
    for a, b in zip(pts[:-1], pts[1:], strict=False):
        seg = float(np.hypot(b[0] - a[0], b[1] - a[1]))
        if acc + seg > start and acc < end:
            if start < acc + seg < end:
                coords.append(Point(b))
        acc += seg
    coords.append(line.interpolate(end))
    uniq = [coords[0]]
    for p in coords[1:]:
        if p.distance(uniq[-1]) > 1e-6:
            uniq.append(p)
    if len(uniq) < 2:
        return None
    return LineString([(p.x, p.y) for p in uniq])


@dataclass(frozen=True)
class EvidenceParams:
    """Everything that turns a raster into an evidence score. Recorded in provenance."""

    step_m: float = 4.0
    core_band_m: float = 6.0  # centre offsets searched for the classification
    slack_band_m: float = 15.0  # wider band, reported as positional slack
    profile_res_m: float = 1.0
    half_widths_m: tuple[int, ...] = (2, 3, 4, 5, 6)
    offset_tolerance_m: float = 2.0  # wobble allowed around the constant edge offset
    present_threshold: float = 0.50  # share of sample points that must show the fill
    dark_present_threshold: float = 0.80  # a bare dark line has to be near-continuous
    absent_threshold: float = 0.30  # below -> absent; between -> uncertain
    major_min_half_width_m: float = 3.5


@dataclass
class SheetRaster:
    """A sheet's raster held in memory as the two features the detector uses."""

    grey: np.ndarray  # min(R,G,B)/255  -- how un-coloured a pixel is
    bright: np.ndarray  # max(R,G,B)/255
    valid: np.ndarray
    transform: Any
    shape: tuple[int, int] = field(init=False)

    def __post_init__(self) -> None:
        self.shape = self.grey.shape


def load_sheet_raster(path: Path) -> SheetRaster:
    with rasterio.open(path) as ds:
        arr = ds.read()[:3].astype(np.float32)
        tr = ds.transform
    grey = arr.min(axis=0) / 255.0
    bright = arr.max(axis=0) / 255.0
    valid = arr.sum(axis=0) > 0  # the delivered rasters use 0,0,0 as nodata padding
    return SheetRaster(grey=grey, bright=bright, valid=valid, transform=tr)


def perpendicular_profiles(
    raster: SheetRaster, line: LineString, params: EvidenceParams
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sample ``grey`` and ``bright`` on perpendiculars every ``step_m`` along ``line``.

    Returns ``(grey, bright, valid, offsets)`` with shape ``(n_samples, n_offsets)``.
    """
    n = max(2, int(np.ceil(line.length / params.step_m)) + 1)
    d = np.linspace(0.0, line.length, n)
    pts = np.array([[p.x, p.y] for p in (line.interpolate(float(v)) for v in d)])
    tangent = np.gradient(pts, axis=0)
    normal = np.stack([-tangent[:, 1], tangent[:, 0]], axis=1)
    norm = np.linalg.norm(normal, axis=1)
    norm[norm == 0] = 1.0
    normal = normal / norm[:, None]
    offsets = np.arange(
        -params.slack_band_m, params.slack_band_m + params.profile_res_m, params.profile_res_m
    )
    x = pts[:, None, 0] + normal[:, None, 0] * offsets[None, :]
    y = pts[:, None, 1] + normal[:, None, 1] * offsets[None, :]
    inv = ~raster.transform
    col = np.rint(inv.a * x + inv.b * y + inv.c).astype(int)
    row = np.rint(inv.d * x + inv.e * y + inv.f).astype(int)
    inside = (col >= 0) & (col < raster.shape[1]) & (row >= 0) & (row < raster.shape[0])
    col = np.clip(col, 0, raster.shape[1] - 1)
    row = np.clip(row, 0, raster.shape[0] - 1)
    ok = inside & raster.valid[row, col]
    return raster.grey[row, col], raster.bright[row, col], ok, offsets


def _sliding_mean(a: np.ndarray, w: int) -> np.ndarray:
    return uniform_filter1d(a, size=w, axis=-1, mode="nearest")


def _sliding_min(a: np.ndarray, w: int) -> np.ndarray:
    return minimum_filter1d(a, size=w, axis=-1, mode="nearest")


def _shift(a: np.ndarray, k: int, fill: float) -> np.ndarray:
    """``out[..., i] = a[..., i + k]``, padded with ``fill``."""
    out = np.full_like(a, fill)
    if k == 0:
        return a.copy()
    if k > 0:
        out[..., :-k] = a[..., k:]
    else:
        out[..., -k:] = a[..., :k]
    return out


def pattern_hits(
    grey: np.ndarray,
    bright: np.ndarray,
    ok: np.ndarray,
    sheet: SheetSpec,
    params: EvidenceParams,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Where, in a stack of perpendicular profiles, each drawn-road pattern is found.

    A drawn road on the economic map is a light, *un-coloured* fill between two darker
    casing lines; on the Haradskartan sheet it is one or two dark lines on pale paper.
    Both patterns are searched at every centre offset and every half width.

    Returns ``(fill_hit, dark_hit, fill_half_width)``, each of shape
    ``(n_samples, n_offsets)``; the half width is 0 where there is no fill hit.
    """
    g = np.where(ok, grey, 0.0).astype(np.float64)
    b = np.where(ok, bright, 1.0).astype(np.float64)
    bg = np.median(g, axis=1, keepdims=True)
    fill_hit = np.zeros(g.shape, dtype=bool)
    fill_w = np.zeros(g.shape)
    best_contrast = np.full(g.shape, -np.inf)
    smin3 = _sliding_min(b, 3)
    smean3 = _sliding_mean(b, 3)
    for h in params.half_widths_m:
        w = max(1, 2 * h - 1)
        mean_g = _sliding_mean(g, w)
        mean_b = _sliding_mean(b, w)
        contrast = np.minimum(
            mean_b - _shift(smin3, -(h + 1), 1.0), mean_b - _shift(smin3, h + 1, 1.0)
        )
        hit = (
            (mean_g >= sheet.fill_min_grey)
            & (mean_b >= sheet.fill_min_bright)
            & (mean_g >= bg + sheet.fill_grey_over_background)
            & (contrast >= sheet.casing_contrast)
        )
        # the half width recorded is the one whose casing contrast is sharpest: a window
        # narrower than the symbol sees no casing, a wider one swallows it
        better = hit & (contrast > best_contrast)
        fill_w = np.where(better, float(h), fill_w)
        best_contrast = np.where(better, contrast, best_contrast)
        fill_hit |= hit
    dark_hit = np.zeros(g.shape, dtype=bool)
    for h in (1, 2, 3):
        run_min = _sliding_min(b, 2 * h + 1)
        shoulder = np.minimum(_shift(smean3, -(h + 2), 1.0), _shift(smean3, h + 2, 1.0))
        dark_hit |= shoulder - run_min >= sheet.dark_line_contrast
    edge = np.zeros(g.shape[1], dtype=bool)
    edge[:2] = edge[-2:] = True  # filters are unreliable at the profile ends
    fill_hit[:, edge] = False
    dark_hit[:, edge] = False
    return fill_hit, dark_hit, fill_w


def _offset_profile(hit: np.ndarray, tol_px: int) -> np.ndarray:
    """Share of samples with a hit at each candidate *constant* offset (+/- tol)."""
    widened = maximum_filter1d(hit.astype(np.float64), size=2 * tol_px + 1, axis=-1)
    return widened.mean(axis=0)


def _best_offset_index(frac: np.ndarray, allowed: np.ndarray) -> int:
    """Index of the best constant offset; ties are broken by the middle of the tied run."""
    masked = np.where(allowed, frac, -1.0)
    best = masked.max()
    tied = np.flatnonzero(masked >= best - 1e-12)
    return int(tied[len(tied) // 2])


def edge_evidence(
    raster: SheetRaster, line: LineString, sheet: SheetSpec, params: EvidenceParams
) -> dict[str, float]:
    """Evidence that a road is drawn along one edge.

    The positional slack is applied as a *constant* offset for the whole edge, not
    per sample point: a real drawn road lies at a consistent distance from the modern
    line, while scattered dark specks do not line up. This is what stops the map's
    field boundaries and stipple from scoring as roads.
    """
    grey, bright, ok, offsets = perpendicular_profiles(raster, line, params)
    fill_hit, dark_hit, fill_w = pattern_hits(grey, bright, ok, sheet, params)
    tol = int(round(params.offset_tolerance_m / params.profile_res_m))
    frac_fill = _offset_profile(fill_hit, tol)
    frac_dark = _offset_profile(fill_hit | dark_hit, tol)
    core = np.abs(offsets) <= params.core_band_m + 1e-9
    slack = np.abs(offsets) <= params.slack_band_m + 1e-9
    i_fill = _best_offset_index(frac_fill, slack)
    i_dark = _best_offset_index(frac_dark, slack)
    lo, hi = max(0, i_fill - tol), min(len(offsets), i_fill + tol + 1)
    w = fill_w[:, lo:hi]
    widths = w[w > 0]
    return {
        "n_samples": float(grey.shape[0]),
        "valid_fraction": float(ok.any(axis=1).mean()),
        "evidence_score": float(frac_fill[core].max()),
        "evidence_score_dark": float(frac_dark[core].max()),
        "evidence_score_slack": float(frac_fill[i_fill]),
        "evidence_score_slack_dark": float(frac_dark[i_dark]),
        "best_offset_m": float(offsets[i_fill]) if frac_fill[i_fill] > 0 else float("nan"),
        "best_offset_dark_m": float(offsets[i_dark]) if frac_dark[i_dark] > 0 else float("nan"),
        "median_half_width_m": float(np.median(widths)) if widths.size else 0.0,
    }


def classify_edge(
    ev: dict[str, float], sheet: SheetSpec, params: EvidenceParams
) -> dict[str, Any]:
    """Present / absent / uncertain, plus a historical class from the line symbology.

    The fill-and-casing pattern is the strong evidence. A bare dark line is weak on the
    economic map, where field and property boundaries are drawn as dark lines too, so it
    only yields "present" when it is near-continuous along the edge, and then with low
    confidence and the non-committal class ``track_or_path``.
    """
    fill = ev["evidence_score_slack"]
    any_line = ev["evidence_score_slack_dark"]
    dark_present = (
        sheet.dark_present_threshold
        if sheet.dark_present_threshold is not None
        else params.dark_present_threshold
    )
    absent_below = (
        sheet.absent_threshold if sheet.absent_threshold is not None else params.absent_threshold
    )
    if fill >= params.present_threshold:
        conf = "high" if fill >= 0.75 else "medium"
        hist = (
            "major_double_casing"
            if ev["median_half_width_m"] >= params.major_min_half_width_m
            else "minor_cased"
        )
        return {"present": "present", "confidence": conf, "hist_class": hist}
    if any_line >= dark_present:
        return {"present": "present", "confidence": "low", "hist_class": "track_or_path"}
    if any_line >= absent_below:
        return {"present": "uncertain", "confidence": "low", "hist_class": "track_or_path"}
    return {
        "present": "absent",
        "confidence": "high" if any_line <= 0.10 else "medium",
        "hist_class": "none",
    }


def extract_sheet(
    sheet: SheetSpec,
    edges: gpd.GeoDataFrame,
    params: EvidenceParams | None = None,
    raster: SheetRaster | None = None,
) -> gpd.GeoDataFrame:
    """Score and classify every edge against one sheet. Deterministic, edge_id-sorted."""
    params = params or EvidenceParams()
    raster = raster or load_sheet_raster(sheet.raster)
    recs: list[dict[str, Any]] = []
    for _, r in edges.iterrows():
        ev = edge_evidence(raster, r.geometry, sheet, params)
        cls = classify_edge(ev, sheet, params)
        recs.append(
            {
                "edge_id": r["edge_id"],
                "osm_way_id": r["osm_way_id"],
                "modern_class": r["modern_class"],
                "length_m": float(r.geometry.length),
                **ev,
                "present": cls["present"],
                "hist_class": cls["hist_class"],
                "confidence": (
                    "low" if float(r.geometry.length) < SHORT_EDGE_M else cls["confidence"]
                ),
                "source_sheet": sheet.label,
                "method_version": METHOD_VERSION,
                "geometry": r.geometry,
            }
        )
    out = gpd.GeoDataFrame(recs, geometry="geometry", crs=edges.crs)
    return out.sort_values("edge_id").reset_index(drop=True)


def crop_edge(
    geom: LineString,
    rasters: list[tuple[str, Path]],
    out_png: Path,
    pad_m: float = 90.0,
    max_px: int = 520,
    outline: tuple[int, int, int] = (230, 0, 190),
) -> Path:
    """One PNG per edge: the same window from each sheet, side by side, edge outlined.

    The edge is drawn as a thin outline so the symbol under it stays readable; a filled
    or thick line would hide exactly what the owner is being asked to judge.
    """
    from PIL import Image, ImageDraw
    from rasterio.windows import from_bounds

    minx, miny, maxx, maxy = geom.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    half = max((maxx - minx) / 2, (maxy - miny) / 2, 40.0) + pad_m
    bounds = (cx - half, cy - half, cx + half, cy + half)
    tiles = []
    for label, path in rasters:
        with rasterio.open(path) as ds:
            win = from_bounds(*bounds, ds.transform)
            arr = ds.read(window=win, boundless=True, fill_value=255)[:3]
            tr = ds.window_transform(win)
        im = Image.fromarray(np.transpose(arr, (1, 2, 0)).astype("uint8"))
        size = min(max_px, max(200, int(2 * half)))
        sx, sy = size / max(1, im.width), size / max(1, im.height)
        im = im.resize((size, size), Image.LANCZOS)
        d = ImageDraw.Draw(im)
        inv = ~tr
        parts = [geom] if geom.geom_type == "LineString" else list(getattr(geom, "geoms", []))
        for part in parts:
            pts = [
                tuple(v * s for v, s in zip(inv * (x, y), (sx, sy), strict=False))
                for x, y in part.coords
            ]
            if len(pts) > 1:
                d.line(pts, fill=outline, width=1)
        d.rectangle([0, 0, size - 1, 13], fill=(255, 255, 255))
        d.text((3, 2), label, fill=(0, 0, 0))
        tiles.append(im)
    total_w = sum(p.width for p in tiles) + 6 * (len(tiles) - 1)
    sheet = Image.new("RGB", (total_w, tiles[0].height), (255, 255, 255))
    x = 0
    for tile in tiles:
        sheet.paste(tile, (x, 0))
        x += tile.width + 6
    out_png.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_png)
    return out_png
