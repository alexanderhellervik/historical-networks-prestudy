"""Network building: snapping, crossings, sheet footprints."""

from __future__ import annotations

import numpy as np
from shapely.geometry import LineString

from histnet import accessibility as acc
from histnet import network1960s as n60


def _components(lines: list[LineString]) -> int:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    xy, edges = acc.graph_from_lines(lines, 6.0)
    u = np.array([e[0] for e in edges])
    v = np.array([e[1] for e in edges])
    m = coo_matrix((np.ones(len(u)), (u, v)), shape=(len(xy), len(xy)))
    return connected_components(m, directed=False)[0]


def test_a_line_end_within_the_snapping_distance_joins_the_other_line() -> None:
    a = LineString([(0, 0), (1000, 0)])
    b = LineString([(500, 30), (500, 600)])  # ends 30 m from a, inside 40 m
    pieces, n = acc.snap_ends([a, b], snap_m=40.0)
    assert n == 1 and _components(pieces) == 1


def test_a_line_end_beyond_the_snapping_distance_stays_apart() -> None:
    a = LineString([(0, 0), (1000, 0)])
    b = LineString([(500, 60), (500, 600)])
    pieces, n = acc.snap_ends([a, b], snap_m=40.0)
    assert n == 0 and _components(pieces) == 2


def test_crossing_lines_are_joined_at_the_crossing() -> None:
    a = LineString([(0, 0), (1000, 0)])
    b = LineString([(500, -500), (500, 500)])
    pieces, _ = acc.snap_ends([a, b], snap_m=40.0)
    assert _components(pieces) == 1
    assert len(pieces) == 4  # both lines split at the crossing


def test_sheet_footprint_from_its_id_matches_the_georeferenced_sheet() -> None:
    b = n60.footprint_from_id("7C 9b").bounds
    assert [round(v) for v in b] == [352473, 6441007, 357529, 6446063]


def _edges(rows: list[tuple[int, int, float, str]]):
    import pandas as pd

    return pd.DataFrame(rows, columns=["u", "v", "length_m", "status_1960s"]).assign(mode="road")


def test_a_short_absent_gap_to_a_stranded_fragment_is_bridged() -> None:
    e = _edges([
        (1, 2, 5000, "present"), (2, 3, 120, "absent"), (3, 4, 180, "absent"),
        (4, 5, 800, "present"),
    ])  # fmt: skip
    assert n60.bridge_gaps(e, max_gap_m=300).tolist() == [False, True, True, False]


def test_a_long_absent_gap_is_not_bridged_and_nothing_else_is_kept() -> None:
    e = _edges([
        (1, 2, 5000, "present"), (2, 3, 200, "absent"), (3, 4, 200, "absent"),
        (4, 5, 800, "present"), (1, 6, 50, "absent"),
    ])  # fmt: skip
    assert not n60.bridge_gaps(e, max_gap_m=300).any()


def test_only_the_cheapest_chain_is_bridged() -> None:
    e = _edges([
        (1, 2, 5000, "present"), (2, 3, 100, "absent"), (2, 4, 250, "absent"),
        (3, 5, 800, "present"), (4, 5, 10, "present"),
    ])  # fmt: skip
    assert n60.bridge_gaps(e, max_gap_m=300).tolist() == [False, True, False, False, False]


def test_a_short_absent_link_that_saves_a_long_detour_is_kept() -> None:
    e = _edges([
        (1, 2, 1000, "present"), (2, 3, 150, "absent"), (3, 4, 1000, "present"),
        (1, 5, 3000, "present"), (5, 4, 3000, "present"),
    ])  # fmt: skip
    assert n60.bridge_cuts(e).tolist() == [False, True, False, False, False]


def test_a_short_absent_link_beside_a_short_kept_route_is_dropped() -> None:
    e = _edges([
        (1, 2, 1000, "present"), (2, 3, 150, "absent"), (3, 4, 1000, "present"),
        (2, 5, 500, "present"), (5, 3, 500, "present"),
    ])  # fmt: skip
    assert not n60.bridge_cuts(e).any()


def test_a_long_absent_chain_is_not_kept_as_a_cut() -> None:
    e = _edges([
        (1, 2, 1000, "present"), (2, 6, 200, "absent"), (6, 3, 200, "absent"),
        (3, 4, 1000, "present"), (1, 5, 3000, "present"), (5, 4, 3000, "present"),
    ])  # fmt: skip
    assert not n60.bridge_cuts(e).any()


def test_a_short_link_beside_a_long_absent_road_is_still_kept() -> None:
    e = _edges([
        (1, 2, 1000, "present"), (2, 3, 150, "absent"), (3, 4, 1000, "present"),
        (1, 5, 3000, "present"), (5, 4, 3000, "present"), (3, 6, 900, "absent"),
    ])  # fmt: skip
    assert n60.bridge_cuts(e).tolist() == [False, True, False, False, False, False]
