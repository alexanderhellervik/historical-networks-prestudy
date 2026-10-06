"""Population: FOLKNET parish and town series (CEDAR, Umeå University).

No population figure is stored in this repository. Export the FOLKNET series yourself (see
SOURCES.md) into ``inputs/folknet/``; ``population_long`` turns the export into one row per
unit and year. ``data/crosswalk_folknet_histmaps.csv`` links FOLKNET units to historical
parish codes (unit keys and names only, no values).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from histnet.paths import DATA, INPUTS

CROSSWALK = DATA / "crosswalk_folknet_histmaps.csv"


def population(year: int, keys: list[str]) -> pd.Series:
    """Population of the given FOLKNET units in year (summed per unit key)."""
    p = population_long(load_population_source())
    p = p[(p["year"] == year) & p["unit_key"].isin(keys)]
    return p.groupby("unit_key")["value"].sum().reindex(keys)


FOLKNET_DIR = INPUTS / "folknet"  # the FOLKNET population export (CSV) goes here


def folknet_file(folder: Path = FOLKNET_DIR) -> Path:
    """The newest FOLKNET CSV export in inputs/folknet (see SOURCES.md)."""
    files = sorted(folder.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"no FOLKNET export in {folder}; see SOURCES.md")
    return files[-1]


FOLKNET_PREAMBLE_LINES = 6  # title, source and citation lines before the header row


FOLKNET_KEY_COLUMNS = ("Län", "Lännamn", "Typ", "Namn")


# --------------------------------------------------------------------------------------
# FOLKNET import (verbatim, then a typed long table)
# --------------------------------------------------------------------------------------
def load_population_source(path: Path | None = None) -> pd.DataFrame:
    """The FOLKNET export read verbatim: every cell stays the string the file holds.

    The file is UTF-8 with a BOM, semicolon separated, with six preamble lines (title,
    source, citation) before the header row. Only the *column names* are stripped of the
    padding spaces the export writes around them; values, aggregate rows and empty cells
    are all kept. Nothing is parsed, rounded or dropped here.
    """
    path = path or folknet_file()
    frame = pd.read_csv(
        path,
        sep=";",
        skiprows=FOLKNET_PREAMBLE_LINES,
        encoding="utf-8-sig",
        dtype=str,
        keep_default_na=False,
        na_filter=False,
    )
    frame.columns = [str(c).strip() for c in frame.columns]
    missing = [c for c in FOLKNET_KEY_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{path.name}: expected columns {missing} are absent")
    frame.insert(0, "source_row", range(len(frame)))
    frame.insert(
        1,
        "unit_key",
        [
            f"folknet:{lan}:{typ}:{namn}"
            for lan, typ, namn in zip(frame["Län"], frame["Typ"], frame["Namn"], strict=True)
        ],
    )
    if not frame["unit_key"].is_unique:
        raise ValueError("FOLKNET (Län, Typ, Namn) is not unique; the unit key would collide")
    return frame


def year_columns(frame: pd.DataFrame) -> list[str]:
    return [c for c in frame.columns if c.startswith("År")]


def population_long(source: pd.DataFrame) -> pd.DataFrame:
    """One row per (unit, year). Empty cells become <NA>; no row is dropped.

    `source_estimation_status` is 'unknown': the export carries no flag distinguishing an
    observed count from an interpolated or estimated one, and this pipeline does not invent
    one. Values are the export's own integers.
    """
    years = year_columns(source)
    long = source.melt(
        id_vars=["unit_key", "source_row", *FOLKNET_KEY_COLUMNS],
        value_vars=years,
        var_name="year_column",
        value_name="raw_value",
    )
    long["year"] = long["year_column"].str.removeprefix("År").astype("int64")
    stripped = long["raw_value"].str.strip()
    parsed = pd.to_numeric(stripped.where(stripped != "", other=None), errors="raise")
    long["value"] = parsed.astype("Int64")
    long["source_estimation_status"] = "unknown"
    out = long.rename(columns={"Län": "lan", "Lännamn": "lannamn", "Typ": "typ", "Namn": "namn"})[
        [
            "unit_key",
            "source_row",
            "lan",
            "lannamn",
            "typ",
            "namn",
            "year",
            "value",
            "raw_value",
            "source_estimation_status",
        ]
    ]
    return out.sort_values(["unit_key", "year"], kind="stable").reset_index(drop=True)
