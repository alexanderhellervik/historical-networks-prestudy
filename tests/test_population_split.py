"""Population disaggregation keeps parish totals."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, box

from histnet import accessibility as acc


def test_parish_population_is_split_by_road_length_and_totals_are_kept() -> None:
    cells = gpd.GeoDataFrame(
        {"cell": ["a", "b", "c"]},
        geometry=[Point(125, 125), Point(375, 125), Point(625, 125)],
        crs="EPSG:3006",
    )
    pts = gpd.GeoDataFrame(
        {"place": ["p1"], "level": ["parish"], "polygon": [box(0, 0, 1000, 250)]},
        geometry=[Point(500, 125)],
        crs="EPSG:3006",
    )
    road = np.array([0.0, 150.0, 450.0])  # metres of road per cell
    pop = pd.Series({"p1": 1000.0})
    out = acc.disaggregate(cells, road, pts, pop)
    assert abs(out.sum() - 1000.0) < 1e-9
    w = road + acc.FLOOR_M
    assert np.allclose(out, 1000.0 * w / w.sum())
