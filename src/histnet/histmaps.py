"""Historical parish boundaries from histmaps (Riksarkivet data, packaged by J. Junkka).

Reads the R data files of the histmaps package and rebuilds the unit polygons in EPSG:3006,
with each unit's validity interval. Download the package data into ``inputs/histmaps/data``.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rdata
from shapely.geometry import MultiPolygon, Polygon

from histnet.paths import INPUTS, WORKING_CRS
from histnet.util import require_crs

RAW = INPUTS  # the histmaps package data lives under inputs/histmaps/data

GEOM_SP_REL = "histmaps/data/geom_sp.rda"


GEOM_META_REL = "histmaps/data/geom_meta.rda"


GEOM_RELATIONS_REL = "histmaps/data/geom_relations.rda"


OPEN_END = 9999  # histmaps sentinel: interval still open


UNBOUNDED_START = 0  # histmaps sentinel: in force before the record begins


VERIFIED_TYPES = frozenset({"parish", "county"})  # per raw/histmaps/README.md


def _read_rda(path: Path, name: str) -> pd.DataFrame:
    """Read one R data file. rdata warns about every sf class it has no constructor for;
    the classes are irrelevant here because the geometry is rebuilt from the coordinates."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Missing constructor for R class", category=UserWarning
        )
        converted = rdata.conversion.convert(rdata.parser.parse_file(path))
    if name not in converted:
        raise ValueError(f"{path} does not contain object {name!r} (has {sorted(converted)})")
    return converted[name]


def _ring(coords: object) -> np.ndarray:
    return np.asarray(coords, dtype=float)


def _is_ring(candidate: object) -> bool:
    """True when `candidate` is a single ring, i.e. a sequence of (x, y) pairs."""
    return np.asarray(candidate[0], dtype=float).ndim == 1


def sfg_to_shapely(sfg: list) -> Polygon | MultiPolygon:
    """Rebuild a shapely geometry from an sf simple-feature geometry.

    In `geom_sp` a POLYGON arrives as a list of (n, 2) coordinate arrays (exterior ring
    first, then holes) and a MULTIPOLYGON as a list of such lists. rdata leaves no class
    tag behind, so the nesting depth is what distinguishes the two.
    """
    if len(sfg) == 0:
        raise ValueError("empty simple-feature geometry")
    if _is_ring(sfg[0]):
        return Polygon(_ring(sfg[0]), [_ring(h) for h in sfg[1:]])
    return MultiPolygon([(_ring(part[0]), [_ring(h) for h in part[1:]]) for part in sfg])


# --------------------------------------------------------------------------------------
# unit table
# --------------------------------------------------------------------------------------
def _semicolon(values: Iterable) -> str:
    return ";".join(str(int(v)) for v in sorted({int(x) for x in values}))


def _relation_maps(relations: pd.DataFrame) -> tuple[dict[int, str], dict[int, str]]:
    """geom_id -> ';'-joined predecessor / successor geom_ids (see module docstring)."""
    pre: dict[int, set[int]] = {}
    succ: dict[int, set[int]] = {}
    for g1, g2, rel in zip(
        relations["g1"].to_numpy(),
        relations["g2"].to_numpy(),
        relations["relation"].astype(str),
        strict=True,
    ):
        if pd.isna(g1) or pd.isna(g2):
            continue
        target = pre if rel == "pre" else succ
        target.setdefault(int(g1), set()).add(int(g2))
    return (
        {k: _semicolon(v) for k, v in pre.items()},
        {k: _semicolon(v) for k, v in succ.items()},
    )


def load_histmaps_units(raw_root: Path = RAW) -> gpd.GeoDataFrame:
    """Every histmaps administrative unit-version as a GeoDataFrame in EPSG:3006.

    All unit types are kept, each with its validity interval. `geom_id` is the stable id
    (`unit_id` is the same value namespaced by source); nothing is dissolved or chosen.
    """
    sp = _read_rda(raw_root / GEOM_SP_REL, "geom_sp")
    meta = _read_rda(raw_root / GEOM_META_REL, "geom_meta")
    relations = _read_rda(raw_root / GEOM_RELATIONS_REL, "geom_relations")

    geometry = [sfg_to_shapely(g) for g in sp["geometry"]]
    frame = pd.DataFrame(
        {
            "geom_id": sp["geom_id"].astype("int64"),
            "ref_code": sp["ref_code"].astype("string").astype(str),
            "name": sp["name"].astype("string").astype(str),
            "type": sp["type"].astype(str),
            "type_id": sp["type_id"].astype(str),
            "start": sp["start"].astype("int64"),
            "end": sp["end"].astype("int64"),
        }
    )
    frame["unit_id"] = "histmaps:" + frame["geom_id"].astype(str)
    frame["start_is_unbounded"] = frame["start"] == UNBOUNDED_START
    frame["end_is_open"] = frame["end"] == OPEN_END
    frame["type_verified_upstream"] = frame["type_id"].isin(VERIFIED_TYPES)

    meta_indexed = meta.set_index(meta["geom_id"].astype("int64"))
    frame["meta_ref_code"] = (
        frame["geom_id"].map(meta_indexed["ref_code"].astype("string")).astype("string")
    )
    frame["meta_name_x"] = (
        frame["geom_id"].map(meta_indexed["name.x"].astype("string")).astype("string")
    )
    frame["meta_name_y"] = (
        frame["geom_id"].map(meta_indexed["name.y"].astype("string")).astype("string")
    )
    frame["county_code"] = frame["geom_id"].map(meta_indexed["county"]).astype("Float64")
    frame["has_join_keys"] = frame["geom_id"].isin(meta_indexed.index)
    frame["ref_code_disagrees"] = frame["meta_ref_code"].notna() & (
        frame["meta_ref_code"] != frame["ref_code"]
    )

    pre_map, succ_map = _relation_maps(relations)
    frame["predecessor_geom_ids"] = frame["geom_id"].map(pre_map).fillna("")
    frame["successor_geom_ids"] = frame["geom_id"].map(succ_map).fillna("")

    gdf = gpd.GeoDataFrame(frame, geometry=geometry, crs=WORKING_CRS)
    gdf["geometry_kind"] = gdf.geometry.geom_type
    gdf["n_parts"] = [len(g.geoms) if isinstance(g, MultiPolygon) else 1 for g in gdf.geometry]
    gdf["geometry_valid"] = gdf.geometry.is_valid
    gdf["area_km2"] = (gdf.geometry.make_valid().area / 1e6).round(6)

    gdf = gdf.sort_values(["ref_code", "start", "geom_id"], kind="stable").reset_index(drop=True)
    return require_crs(gdf)
