"""Map tiles: a 1 km window of a georeferenced sheet rendered for a reader.

Each tile has a bare view (the map face, 1000 x 1000 px), a gridded view (the same face with a
100-pixel grid and labelled margins) and a 3 km context view, plus an exact image-to-map
transform so that a reader's image coordinates (u right, v down) map to EPSG:3006.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from affine import Affine
from PIL import Image, ImageDraw, ImageFont
from rasterio.enums import Resampling
from rasterio.windows import Window
from shapely.geometry import LineString, Polygon

from histnet.paths import WORK, WORKING_CRS, rel
from histnet.util import sha256_file

SELECTION_SEED = 20260910


VISION_DIR = WORK / "tiles"


TILES_DIR = VISION_DIR


MANIFEST_PATH = VISION_DIR / "manifest.jsonl"


RIGHTS_NOTE = (
    "Naturvardsverket Haradskartan package; Lantmateriet copyright waived per its "
    "information page; terms to confirm"
)


DISPLAY_PX = 1000  # the map face is exactly this, in both directions


CONTEXT_SIDE_M = 3000.0


CONTEXT_PX = 600


GRID_PX = 100  # grid spacing in *image pixels*; see coordinate_contract.md


# margins of the gridded view, in canvas pixels; the map face never overlaps them
MARGIN_LEFT = 76


MARGIN_TOP = 52


MARGIN_RIGHT = 52


MARGIN_BOTTOM = 104


# Overlay line width, in image pixels. One pixel, not two: at 2 px the drawn line hides
# the map symbol it claims to follow, which a pass-C reviewer has to see to judge it.
OVERLAY_LINE_WIDTH = 1


OVERLAY_COLOURS: tuple[tuple[int, int, int], ...] = (
    (220, 30, 40),
    (20, 90, 220),
    (0, 150, 90),
    (230, 120, 0),
    (140, 40, 200),
    (0, 160, 190),
    (190, 0, 130),
    (90, 110, 0),
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if path.is_file():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def repo_relative(path: Path) -> str:
    """Path as written in the manifest: relative to the repository when it is inside it."""
    return rel(path)


@dataclass(frozen=True)
class TileSpec:
    """One 1 km window of a sheet, and everything needed to move between its frames.

    Image coordinates ``(u, v)`` are continuous, u to the right and v downwards, with
    ``(0, 0)`` at the **outer corner** of the top-left pixel of the map face and
    ``(display_w, display_h)`` at the outer corner of the bottom-right pixel. The centre
    of image pixel ``(i, j)`` is therefore ``(i + 0.5, j + 0.5)``. Parent pixel
    coordinates follow the same convention (GDAL's), so the window origin
    ``(col_off, row_off)`` is an outer corner too.
    """

    tile_id: str
    split: str
    block: str
    series: str
    source_raster: str
    source_sha256: str
    col_off: int
    row_off: int
    width: int
    height: int
    display_w: int
    display_h: int
    canvas_offset: tuple[int, int]
    canvas_size: tuple[int, int]
    parent_transform: tuple[float, float, float, float, float, float]
    image_transform: tuple[float, float, float, float, float, float]
    crs: str
    grid_spacing_px: int
    grid_spacing_m: tuple[float, float]
    ground_size_m: tuple[float, float]
    rights_note: str
    selection: dict[str, Any] = field(default_factory=dict)
    renders: dict[str, dict[str, str]] = field(default_factory=dict)
    paired_tile_id: str | None = None

    # -- frames ------------------------------------------------------------------------

    @property
    def image_affine(self) -> Affine:
        """Affine from continuous image coordinates ``(u, v)`` to EPSG:3006 ``(x, y)``."""
        return Affine(*self.image_transform)

    @property
    def parent_affine(self) -> Affine:
        return Affine(*self.parent_transform)

    @property
    def face_polygon(self) -> Polygon:
        """The map face in EPSG:3006 (a rectangle: the parent rasters are north-up)."""
        corners = [
            (0.0, 0.0),
            (self.display_w, 0.0),
            (self.display_w, self.display_h),
            (0.0, self.display_h),
        ]
        return Polygon([image_to_map(self, u, v) for u, v in corners])

    def as_json(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("canvas_offset", "canvas_size", "grid_spacing_m", "ground_size_m"):
            d[key] = list(d[key])
        for key in ("parent_transform", "image_transform"):
            d[key] = list(d[key])
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TileSpec:
        d = dict(d)
        for key in ("canvas_offset", "canvas_size", "grid_spacing_m", "ground_size_m"):
            d[key] = tuple(d[key])
        for key in ("parent_transform", "image_transform"):
            d[key] = tuple(float(v) for v in d[key])
        return cls(**d)


def derive_image_transform(
    parent_transform: Affine,
    col_off: int,
    row_off: int,
    width: int,
    height: int,
    display_w: int,
    display_h: int,
) -> Affine:
    """Compose window offset and display resampling onto the parent affine.

    ``(u, v) -> (col, row) -> (x, y)``: the window origin is an outer pixel corner, and
    one image pixel is ``width / display_w`` parent pixels wide, so the composition is
    exact and involves no half-pixel fudge. Pixel *centres* are handled by the caller
    adding 0.5, never by this transform.
    """
    return (
        parent_transform
        @ Affine.translation(col_off, row_off)
        @ Affine.scale(width / display_w, height / display_h)
    )


def image_to_map(tile: TileSpec, u: Any, v: Any) -> tuple[Any, Any]:
    """Image coordinates (u right, v down, origin top-left outer corner) -> EPSG:3006."""
    a, b, c, d, e, f = tile.image_transform
    u_arr = np.asarray(u, dtype=float)
    v_arr = np.asarray(v, dtype=float)
    x = a * u_arr + b * v_arr + c
    y = d * u_arr + e * v_arr + f
    if x.ndim == 0:
        return (float(x), float(y))
    return (x, y)


def image_polyline_to_map(tile: TileSpec, waypoints_uv: list[list[float]]) -> LineString:
    return LineString([image_to_map(tile, float(u), float(v)) for u, v in waypoints_uv])


def read_manifest(path: Path = MANIFEST_PATH) -> list[TileSpec]:
    out: list[TileSpec] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(TileSpec.from_json(json.loads(line)))
    return out


def tile_by_id(tiles: list[TileSpec], tile_id: str) -> TileSpec:
    for t in tiles:
        if t.tile_id == tile_id:
            return t
    raise KeyError(f"no tile {tile_id!r} in the manifest")


@dataclass(frozen=True)
class Candidate:
    block: str
    split: str
    col_off: int
    row_off: int
    width: int
    height: int
    draw: float
    valid_fraction: float
    road_length_m: float
    junction_count: int
    absent_uncertain_m: float
    status: str = ""
    reason: str = ""

    @property
    def key(self) -> str:
        return f"{self.block}_{self.col_off}_{self.row_off}"


def read_window_rgb(
    raster_path: Path, col_off: int, row_off: int, width: int, height: int, out_px: int
) -> np.ndarray:
    """A parent window resampled to ``out_px`` square, padded white outside the raster."""
    canvas = np.full((out_px, out_px, 3), 255, dtype=np.uint8)
    with rasterio.open(raster_path) as ds:
        c0 = max(0, col_off)
        r0 = max(0, row_off)
        c1 = min(ds.width, col_off + width)
        r1 = min(ds.height, row_off + height)
        if c1 <= c0 or r1 <= r0:
            return canvas
        sx = out_px / width
        sy = out_px / height
        ox0 = int(round((c0 - col_off) * sx))
        oy0 = int(round((r0 - row_off) * sy))
        ox1 = int(round((c1 - col_off) * sx))
        oy1 = int(round((r1 - row_off) * sy))
        arr = ds.read(
            [1, 2, 3],
            window=Window(c0, r0, c1 - c0, r1 - r0),
            out_shape=(3, max(1, oy1 - oy0), max(1, ox1 - ox0)),
            resampling=Resampling.cubic,
        )
    patch = np.transpose(arr, (1, 2, 0)).astype(np.uint8)
    canvas[oy0 : oy0 + patch.shape[0], ox0 : ox0 + patch.shape[1]] = patch
    return canvas


PixelSource = Callable[[Affine, int, int], np.ndarray]


def render_bare(
    tile: TileSpec, raster_path: Path, out_png: Path, source: PixelSource | None = None
) -> Path:
    """(a) the map face alone, exactly ``display_w`` x ``display_h`` px, no annotation."""
    if source is None:
        arr = read_window_rgb(
            raster_path, tile.col_off, tile.row_off, tile.width, tile.height, tile.display_w
        )
    else:
        arr = source(tile.image_affine, tile.display_w, tile.display_h)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(out_png)
    return out_png


def _dashed_line(
    draw: ImageDraw.ImageDraw,
    p0: tuple[float, float],
    p1: tuple[float, float],
    colour: tuple[int, int, int],
    on: int = 7,
    off: int = 9,
) -> None:
    length = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    if length == 0:
        return
    ux, uy = (p1[0] - p0[0]) / length, (p1[1] - p0[1]) / length
    t = 0.0
    while t < length:
        t2 = min(length, t + on)
        draw.line(
            [(p0[0] + ux * t, p0[1] + uy * t), (p0[0] + ux * t2, p0[1] + uy * t2)],
            fill=colour,
            width=1,
        )
        t += on + off


def render_gridded(tile: TileSpec, bare_png: Path, out_png: Path) -> Path:
    """(b) the same face with a grid and, outside it on a white margin, ticks and scales.

    The map face stays exactly ``display_w`` x ``display_h`` at ``canvas_offset``; every
    annotation except the grid itself is drawn in the margin.
    """
    face = Image.open(bare_png).convert("RGB")
    ox, oy = tile.canvas_offset
    cw, ch = tile.canvas_size
    canvas = Image.new("RGB", (cw, ch), (255, 255, 255))
    canvas.paste(face, (ox, oy))
    d = ImageDraw.Draw(canvas)
    grid_colour = (25, 70, 210)
    small = _font(13)
    tiny = _font(11)
    med = _font(15)
    step = tile.grid_spacing_px
    for u in range(step, tile.display_w, step):
        _dashed_line(d, (ox + u, oy), (ox + u, oy + tile.display_h), grid_colour)
    for v in range(step, tile.display_h, step):
        _dashed_line(d, (ox, oy + v), (ox + tile.display_w, oy + v), grid_colour)
    d.rectangle([ox, oy, ox + tile.display_w, oy + tile.display_h], outline=(0, 0, 0), width=1)
    for u in range(0, tile.display_w + 1, step):
        d.line([(ox + u, oy - 8), (ox + u, oy - 1)], fill=(0, 0, 0), width=1)
        label = str(u)
        w = d.textlength(label, font=small)
        d.text((ox + u - w / 2, oy - 24), label, fill=(0, 0, 0), font=small)
    for v in range(0, tile.display_h + 1, step):
        d.line([(ox - 8, oy + v), (ox - 1, oy + v)], fill=(0, 0, 0), width=1)
        label = str(v)
        w = d.textlength(label, font=small)
        d.text((ox - 12 - w, oy + v - 7), label, fill=(0, 0, 0), font=small)
    d.text((ox, oy - 44), "u (image px), right ->", fill=(0, 0, 0), font=med)
    strip = Image.new("RGB", (170, 18), (255, 255, 255))
    ImageDraw.Draw(strip).text((0, 0), "v (image px), down ->", fill=(0, 0, 0), font=med)
    canvas.paste(strip.rotate(90, expand=True), (6, oy))
    # scale bar, in the bottom margin: 200 m, using the tile's own ground scale
    bar_y = oy + tile.display_h + 34
    m_per_px = tile.ground_size_m[0] / tile.display_w
    bar_px = 200.0 / m_per_px
    for i in range(4):
        x0 = ox + bar_px * i / 4
        x1 = ox + bar_px * (i + 1) / 4
        d.rectangle(
            [x0, bar_y, x1, bar_y + 9],
            fill=(0, 0, 0) if i % 2 == 0 else (255, 255, 255),
            outline=(0, 0, 0),
        )
    d.text((ox, bar_y + 13), "0", fill=(0, 0, 0), font=tiny)
    d.text((ox + bar_px - 14, bar_y + 13), "200 m", fill=(0, 0, 0), font=tiny)
    # north arrow, in the bottom margin
    ax = ox + tile.display_w - 40
    d.polygon(
        [(ax, bar_y - 6), (ax - 8, bar_y + 16), (ax, bar_y + 10), (ax + 8, bar_y + 16)],
        fill=(0, 0, 0),
    )
    d.text((ax - 4, bar_y + 20), "N", fill=(0, 0, 0), font=tiny)
    d.text((ax - 74, bar_y + 36), "grid north, EPSG:3006", fill=(0, 0, 0), font=tiny)
    caption = (
        f"{tile.tile_id}  |  face {tile.display_w}x{tile.display_h} px at canvas offset "
        f"({ox}, {oy})  |  grid {step} image px = "
        f"{tile.grid_spacing_m[0]:.1f} m E-W, {tile.grid_spacing_m[1]:.1f} m N-S"
    )
    d.text((ox, bar_y + 56), caption, fill=(0, 0, 0), font=tiny)
    d.text(
        (ox, bar_y + 70),
        "image coordinates: u right, v down, (0,0) = top-left outer corner of the map face",
        fill=(0, 0, 0),
        font=tiny,
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png)
    return out_png


def render_context(
    tile: TileSpec,
    raster_path: Path,
    out_png: Path,
    side_m: float = CONTEXT_SIDE_M,
    size_px: int = CONTEXT_PX,
    source: PixelSource | None = None,
) -> Path:
    """(c) a ``side_m`` square around the tile at ``size_px``, with the tile outlined."""
    a = tile.parent_affine
    px_x, px_y = abs(a.a), abs(a.e)
    w = round(side_m / px_x)
    h = round(side_m / px_y)
    c0 = tile.col_off + tile.width // 2 - w // 2
    r0 = tile.row_off + tile.height // 2 - h // 2
    if source is None:
        arr = read_window_rgb(raster_path, c0, r0, w, h, size_px)
    else:
        out_affine = a @ Affine.translation(c0, r0) @ Affine.scale(w / size_px, h / size_px)
        arr = source(out_affine, size_px, size_px)
    im = Image.fromarray(arr)
    d = ImageDraw.Draw(im)
    sx = size_px / w
    sy = size_px / h
    x0 = (tile.col_off - c0) * sx
    y0 = (tile.row_off - r0) * sy
    x1 = (tile.col_off + tile.width - c0) * sx
    y1 = (tile.row_off + tile.height - r0) * sy
    d.rectangle([x0, y0, x1, y1], outline=(220, 0, 0), width=2)
    d.rectangle([0, 0, size_px - 1, 15], fill=(255, 255, 255))
    d.text(
        (3, 2),
        f"{tile.tile_id} - context {side_m / 1000:.0f} km, tile outlined",
        fill=(0, 0, 0),
        font=_font(12),
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_png)
    return out_png


def render_overlay(
    tile: TileSpec,
    records: list[dict[str, Any]],
    bare_png: Path,
    out_png: Path,
    ids_only: bool = False,
    line_width: int = OVERLAY_LINE_WIDTH,
) -> Path:
    """Candidate polylines on the bare tile: thin coloured lines with feature ids.

    ``ids_only`` draws the ids at the line midpoints and no lines, so a reviewer can see
    what was claimed where without the drawing hiding the symbol underneath. ``line_width``
    is kept at one pixel for the same reason: the symbol under the claim must stay visible.
    """
    im = Image.open(bare_png).convert("RGB")
    d = ImageDraw.Draw(im)
    font = _font(14)
    for i, rec in enumerate(sorted(records, key=lambda r: str(r["feature_id"]))):
        colour = OVERLAY_COLOURS[i % len(OVERLAY_COLOURS)]
        pts = [(float(u), float(v)) for u, v in rec["waypoints_uv"]]
        if not ids_only and len(pts) > 1:
            d.line(pts, fill=colour, width=line_width, joint="curve")
        centre = LineString(pts).interpolate(0.5, normalized=True)
        mid = (centre.x, centre.y)
        label = str(rec["feature_id"])
        w = d.textlength(label, font=font)
        d.rectangle([mid[0] - 2, mid[1] - 9, mid[0] + w + 2, mid[1] + 9], fill=(255, 255, 255))
        d.text((mid[0], mid[1] - 8), label, fill=colour, font=font)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_png)
    return out_png


def _make_tile(
    candidate: Candidate,
    index: int,
    raster_path: Path,
    raster_sha: str,
    series: str,
    tile_id: str,
    paired_tile_id: str | None = None,
) -> TileSpec:
    with rasterio.open(raster_path) as ds:
        transform = ds.transform
    image_affine = derive_image_transform(
        transform,
        candidate.col_off,
        candidate.row_off,
        candidate.width,
        candidate.height,
        DISPLAY_PX,
        DISPLAY_PX,
    )
    ground_w = candidate.width * abs(transform.a)
    ground_h = candidate.height * abs(transform.e)
    return TileSpec(
        tile_id=tile_id,
        split=candidate.split,
        block=candidate.block,
        series=series,
        source_raster=repo_relative(raster_path),
        source_sha256=raster_sha,
        col_off=int(candidate.col_off),
        row_off=int(candidate.row_off),
        width=int(candidate.width),
        height=int(candidate.height),
        display_w=DISPLAY_PX,
        display_h=DISPLAY_PX,
        canvas_offset=(MARGIN_LEFT, MARGIN_TOP),
        canvas_size=(
            DISPLAY_PX + MARGIN_LEFT + MARGIN_RIGHT,
            DISPLAY_PX + MARGIN_TOP + MARGIN_BOTTOM,
        ),
        parent_transform=tuple(float(v) for v in tuple(transform)[:6]),
        image_transform=tuple(float(v) for v in tuple(image_affine)[:6]),
        crs=WORKING_CRS,
        grid_spacing_px=GRID_PX,
        grid_spacing_m=(GRID_PX * ground_w / DISPLAY_PX, GRID_PX * ground_h / DISPLAY_PX),
        ground_size_m=(ground_w, ground_h),
        rights_note=RIGHTS_NOTE,
        selection={
            "index": index,
            "seed": SELECTION_SEED,
            "draw": candidate.draw,
            "road_length_m": round(candidate.road_length_m, 1),
            "junction_count": candidate.junction_count,
            "absent_or_uncertain_length_m": round(candidate.absent_uncertain_m, 1),
            "valid_fraction": candidate.valid_fraction,
            "rule": "one tile per block, smallest seeded draw among eligible candidates",
        },
        paired_tile_id=paired_tile_id,
    )


def _render_all(
    tile: TileSpec,
    raster_path: Path,
    source: PixelSource | None = None,
    tiles_dir: Path = TILES_DIR,
) -> TileSpec:
    bare = tiles_dir / f"{tile.tile_id}_bare.png"
    gridded = tiles_dir / f"{tile.tile_id}_grid.png"
    context = tiles_dir / f"{tile.tile_id}_context.png"
    render_bare(tile, raster_path, bare, source=source)
    render_gridded(tile, bare, gridded)
    render_context(tile, raster_path, context, source=source)
    renders = {
        name: {"path": repo_relative(p), "sha256": sha256_file(p)}
        for name, p in (("bare", bare), ("gridded", gridded), ("context", context))
    }
    return TileSpec(**{**asdict(tile), "renders": renders})


SERIES_1890S = "hk_1890s"  # series label recorded on tile specs
