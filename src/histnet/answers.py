"""Model answers: parse, normalise and validate a reader's answer (pass A) and a reviewer's
answer (pass C), turn them into map lines, and assemble the reviewed product.

The reviewed product keeps lines the reviewer agrees with, replaces courses the reviewer
corrects, removes lines judged not a road or undecidable, and adds roads the reviewer drew.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString

from histnet.maptiles import (
    DISPLAY_PX,
    TileSpec,
    image_polyline_to_map,
    image_to_map,
    repo_relative,
)
from histnet.paths import PROMPTS, WORKING_CRS, rel
from histnet.util import sha256_file

METHOD_VERSION = "vision-roads-0.1"


# The Haradskartan guide's legend page (page 4 of
# raw/nv_haradskartan/guide-haradskartan-och-stomkartorna.pdf) shows *land cover and
# nature* symbols only; it carries no road table, and says a full legend lives on
# Lantmateriet's web pages. Two entries there do bear on roads ("Park med parkvagar",
# "Alleteckning"); the rest of this vocabulary therefore describes the drawn symbol's
# *form*, not an official road class, and a model that cannot tell must answer "unknown".
CLASS_VOCABULARY: tuple[str, ...] = (
    "double_line_wide",  # two parallel casing lines, clearly separated
    "double_line_narrow",  # two parallel casing lines, close together
    "single_line",  # one drawn line, no casing
    "dashed_line",  # a broken or dotted line
    "avenue_lined",  # a road flanked by tree symbols (legend: "Alleteckning")
    "park_path",  # a path inside park or garden symbology (legend: "Park med parkvagar")
    "unknown",
)


ENDPOINT_FLAGS: tuple[str, ...] = ("edge_of_tile", "dead_end", "junction", "unknown")


PASSES: tuple[str, ...] = ("A", "B", "C")


CANDIDATE_FIELDS: tuple[str, ...] = (
    "feature_id",
    "tile_id",
    "pass",
    "model_family",
    "waypoints_uv",
    "proposed_class",
    "junction_ids",
    "endpoint_flags",
    "evidence_note",
    "uncertainty_note",
    "alternatives",
    "provenance",
)


PROVENANCE_FIELDS: tuple[str, ...] = ("prompt_sha256", "image_sha256", "model_id", "run_id")


@dataclass(frozen=True)
class Rejection:
    feature_id: str
    tile_id: str
    reason: str


def validate_candidates(
    records: list[dict[str, Any]], tiles: list[TileSpec]
) -> tuple[list[dict[str, Any]], list[Rejection]]:
    """Check model output against the schema and the tile frame. Nothing is repaired.

    Every rejection is returned with its reason and counted by the caller: a record that
    does not pass is a result, not something to be quietly dropped or corrected.
    """
    by_id = {t.tile_id: t for t in tiles}
    accepted: list[dict[str, Any]] = []
    rejected: list[Rejection] = []
    seen: set[tuple[str, str]] = set()
    for rec in records:
        fid = str(rec.get("feature_id", ""))
        tid = str(rec.get("tile_id", ""))
        missing = [f for f in CANDIDATE_FIELDS if f not in rec and f not in ("alternatives",)]
        if missing:
            rejected.append(Rejection(fid, tid, f"missing_fields:{','.join(missing)}"))
            continue
        if tid not in by_id:
            rejected.append(Rejection(fid, tid, "unknown_tile_id"))
            continue
        if (tid, fid) in seen:
            rejected.append(Rejection(fid, tid, "duplicate_feature_id"))
            continue
        seen.add((tid, fid))
        tile = by_id[tid]
        if rec["pass"] not in PASSES:
            rejected.append(Rejection(fid, tid, f"pass_not_in_{PASSES}"))
            continue
        if rec["proposed_class"] not in CLASS_VOCABULARY:
            rejected.append(Rejection(fid, tid, "class_not_in_vocabulary"))
            continue
        flags = rec.get("endpoint_flags") or []
        if not isinstance(flags, list) or any(f not in ENDPOINT_FLAGS for f in flags):
            rejected.append(Rejection(fid, tid, "endpoint_flag_not_in_vocabulary"))
            continue
        prov = rec.get("provenance") or {}
        if not isinstance(prov, dict) or [f for f in PROVENANCE_FIELDS if f not in prov]:
            rejected.append(Rejection(fid, tid, "incomplete_provenance"))
            continue
        pts = rec.get("waypoints_uv")
        if not isinstance(pts, list) or len(pts) < 2 or any(len(p) != 2 for p in pts):
            rejected.append(Rejection(fid, tid, "waypoints_not_a_polyline"))
            continue
        arr = np.asarray(pts, dtype=float)
        if not np.isfinite(arr).all():
            rejected.append(Rejection(fid, tid, "non_finite_coordinate"))
            continue
        if (
            (arr[:, 0] < 0).any()
            or (arr[:, 0] > tile.display_w).any()
            or (arr[:, 1] < 0).any()
            or (arr[:, 1] > tile.display_h).any()
        ):
            rejected.append(Rejection(fid, tid, "coordinate_outside_map_face"))
            continue
        line = LineString([(float(u), float(v)) for u, v in arr])
        if line.length <= 0:
            rejected.append(Rejection(fid, tid, "zero_length"))
            continue
        if not line.is_simple:
            rejected.append(Rejection(fid, tid, "self_intersection"))
            continue
        accepted.append(rec)
    return accepted, rejected


def candidates_to_gdf(records: list[dict[str, Any]], tiles: list[TileSpec]) -> gpd.GeoDataFrame:
    """Validated candidates as EPSG:3006 LineStrings, using each tile's own transform."""
    by_id = {t.tile_id: t for t in tiles}
    rows: list[dict[str, Any]] = []
    geoms: list[LineString] = []
    for rec in sorted(records, key=lambda r: (str(r["tile_id"]), str(r["feature_id"]))):
        tile = by_id[str(rec["tile_id"])]
        geom = image_polyline_to_map(tile, rec["waypoints_uv"])
        prov = rec.get("provenance") or {}
        rows.append(
            {
                "feature_id": rec["feature_id"],
                "tile_id": rec["tile_id"],
                "pass": rec["pass"],
                "model_family": rec["model_family"],
                "model_id": prov.get("model_id", ""),
                "run_id": prov.get("run_id", ""),
                "prompt_sha256": prov.get("prompt_sha256", ""),
                "image_sha256": json.dumps(prov.get("image_sha256"), sort_keys=True),
                "proposed_class": rec["proposed_class"],
                "proposed_class_raw": rec.get("proposed_class_raw", ""),
                "junction_ids": json.dumps(rec.get("junction_ids") or []),
                "endpoint_flags": json.dumps(rec.get("endpoint_flags") or []),
                "endpoint_flags_raw": json.dumps(rec.get("endpoint_flags_raw") or []),
                "evidence_note": rec.get("evidence_note", ""),
                "uncertainty_note": rec.get("uncertainty_note", ""),
                "alternatives": json.dumps(rec.get("alternatives") or []),
                "waypoints_uv": json.dumps(rec["waypoints_uv"]),
                "n_waypoints": len(rec["waypoints_uv"]),
                "length_m": geom.length,
                "method_version": METHOD_VERSION,
            }
        )
        geoms.append(geom)
    return gpd.GeoDataFrame(rows, geometry=geoms, crs=WORKING_CRS)


def _sample_points(line: LineString, step_m: float = 1.0) -> np.ndarray:
    n = max(2, int(math.ceil(line.length / step_m)) + 1)
    d = np.linspace(0.0, line.length, n)
    return np.array([[p.x, p.y] for p in (line.interpolate(float(v)) for v in d)])


NORMALISER_VERSION = "pass-a-normalise-0.1"


PASS_A_SCHEMA_PATH = PROMPTS / "pass_a_schema.json"


PASS_A_PROMPT_PATH = PROMPTS / "pass_a_interpretation.md"


# The keys the schema uses, and the key names delivered answers have been seen to use
# instead. Renaming is the only thing done to them; no value is touched.
FEATURE_KEY_ALIASES: dict[str, str] = {
    "id": "feature_id",
    "class": "proposed_class",
    "polyline": "waypoints_uv",
    "points": "waypoints_uv",  # seen 2026-09-30 in a Codex answer for tile t07c
    "waypoints": "waypoints_uv",
    "evidence": "evidence_note",
}


JUNCTION_KEY_ALIASES: dict[str, str] = {
    "id": "junction_id",
    "coordinate": "uv",
    "point": "uv",  # seen 2026-09-30 in Codex answers for t07 (arm L) and t07c (arm B)
    "coordinates": "uv",  # seen 2026-09-30 in a Codex answer for t07 (arm K, r3)
    "branch_count": "n_branches",
    "evidence": "evidence_note",
}


ALTERNATIVE_KEY_ALIASES: dict[str, str] = {
    "class": "proposed_class",
    "polyline": "waypoints_uv",
    "waypoints": "waypoints_uv",
}


TOP_REQUIRED: tuple[str, ...] = ("tile_id", "features", "junctions")


TOP_OPTIONAL: tuple[str, ...] = ("unreadable_areas", "overall_note")


FEATURE_REQUIRED: tuple[str, ...] = (
    "feature_id",
    "waypoints_uv",
    "proposed_class",
    "junction_ids",
    "endpoint_flags",
    "evidence_note",
    "uncertainty_note",
)


FEATURE_OPTIONAL: tuple[str, ...] = ("alternatives",)


JUNCTION_REQUIRED: tuple[str, ...] = ("junction_id", "uv", "n_branches", "evidence_note")


JUNCTION_OPTIONAL: tuple[str, ...] = ("uncertainty_note",)


ALTERNATIVE_REQUIRED: tuple[str, ...] = ("kind", "note")


ALTERNATIVE_OPTIONAL: tuple[str, ...] = ("proposed_class", "waypoints_uv")


# Defaults, used only where a delivery left a key out altogether. Each one is recorded as
# a ``missing_key`` deviation and each is a declared "not stated", never a guess at what
# the model meant: an absent endpoint flag becomes ``unknown``, the vocabulary's own word
# for "not decided".
FEATURE_DEFAULTS: dict[str, Any] = {
    "junction_ids": [],
    "endpoint_flags": ["unknown", "unknown"],
    "evidence_note": "",
    "uncertainty_note": "",
}


JUNCTION_DEFAULTS: dict[str, Any] = {"evidence_note": "", "uncertainty_note": ""}


NOTE_MAX_CHARS = 300  # prompts/pass_a_schema.json maxLength on the note fields


FEATURE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass(frozen=True)
class Deviation:
    """One departure of a delivered answer from ``prompts/pass_a_schema.json``.

    A deviation is a measured property of the delivery route - the client, whether a
    schema could be enforced, what the model was able to read - and not a judgement on the
    reading it carries.
    """

    tile_id: str
    family: str
    scope: str  # top | feature | junction | alternative
    object_id: str
    key: str
    kind: str
    detail: str


def extract_json_object(text: str) -> tuple[dict[str, Any], bool]:
    """Parse an answer that may be wrapped in prose or a fenced block.

    Returns the object and whether anything had to be stripped to reach it. That the
    wrapping was there is a schema-adherence fact and is recorded; the object inside is
    not altered.
    """
    stripped = text.strip()
    try:
        return json.loads(stripped), False
    except json.JSONDecodeError:
        pass
    for block in re.findall(r"```(?:json)?\s*(.+?)```", stripped, flags=re.S):
        try:
            return json.loads(block.strip()), True
        except json.JSONDecodeError:
            continue
    first, last = stripped.find("{"), stripped.rfind("}")
    if first >= 0 and last > first:
        return json.loads(stripped[first : last + 1]), True
    raise ValueError("no JSON object found in the answer")


def _rename_keys(
    obj: dict[str, Any],
    aliases: dict[str, str],
    required: tuple[str, ...],
    optional: tuple[str, ...],
    record: Any,
) -> dict[str, Any]:
    """Apply the alias map, then report any key that is still not in the schema."""
    out: dict[str, Any] = {}
    known = set(required) | set(optional)
    for key, value in obj.items():
        target = aliases.get(key, key)
        if target != key:
            record("renamed_key", key, f"{key} -> {target}")
        if target not in known:
            record("extra_key", key, "not a property of this schema object")
        out[target] = value
    return out


def _split_extra(obj: dict[str, Any], known: tuple[str, ...]) -> dict[str, Any]:
    """Move keys the schema does not define into their own dict. Nothing is discarded."""
    return {k: obj.pop(k) for k in [k for k in obj if k not in known]}


def _check_note(value: Any, key: str, record: Any) -> None:
    if not isinstance(value, str):
        record("wrong_type", key, f"expected a string, got {type(value).__name__}")
    elif len(value) > NOTE_MAX_CHARS:
        record("note_too_long", key, f"{len(value)} chars, schema maxLength {NOTE_MAX_CHARS}")


def _check_coordinate(value: Any, key: str, where: str, record: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        record("non_numeric_coordinate", key, f"{where}={value!r}")
    elif not 0.0 <= float(value) <= float(DISPLAY_PX):
        record("coordinate_out_of_range", key, f"{where}={value}")


def _check_waypoints(pts: Any, key: str, record: Any) -> None:
    """Type and range checks on coordinates. Nothing is clamped, moved or dropped."""
    if not isinstance(pts, list) or len(pts) < 2:
        record("wrong_type", key, "expected an array of at least two [u, v] pairs")
        return
    for i, p in enumerate(pts):
        if not isinstance(p, list | tuple) or len(p) != 2:
            record("wrong_type", key, f"waypoint {i} is not a two-element array")
            continue
        for axis, val in zip(("u", "v"), p, strict=True):
            _check_coordinate(val, key, f"waypoint {i} {axis}", record)


def _normalise_class(value: Any, record: Any, key: str = "proposed_class") -> tuple[str, str]:
    """Map a delivered class string onto the vocabulary, or keep it raw and answer unknown.

    Only an exact match after lower-casing counts. Anything else keeps the model's own
    string in ``proposed_class_raw`` and answers ``unknown``: this code does not decide
    what a model meant by a word the vocabulary does not contain.
    """
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    if isinstance(value, str) and value.lower() in CLASS_VOCABULARY:
        if value != value.lower():
            record("class_case_normalised", key, f"{value!r} -> {value.lower()!r}")
        return value.lower(), raw
    record("class_not_in_vocabulary", key, f"{raw!r} kept raw; proposed_class set to unknown")
    return "unknown", raw


def _normalise_alternatives(value: Any, record: Any) -> list[dict[str, Any]]:
    """Alternatives, with a bare string kept verbatim as the alternative's note."""
    if value is None:
        return []
    if not isinstance(value, list):
        record("wrong_type", "alternatives", f"expected an array, got {type(value).__name__}")
        return []
    out: list[dict[str, Any]] = []
    for i, alt in enumerate(value):
        if isinstance(alt, str):
            record("alternative_not_an_object", "alternatives", f"item {i} is a bare string")
            out.append({"note": alt, "raw": alt})
            continue
        if not isinstance(alt, dict):
            record("wrong_type", "alternatives", f"item {i} is a {type(alt).__name__}")
            continue

        def alt_record(kind: str, key: str, detail: str, i: int = i) -> None:
            """Keys inside an alternative are reported as ``alternatives[i].<key>``."""
            record(kind, f"alternatives[{i}].{key}", detail)

        clean = _rename_keys(
            alt, ALTERNATIVE_KEY_ALIASES, ALTERNATIVE_REQUIRED, ALTERNATIVE_OPTIONAL, alt_record
        )
        for key in ALTERNATIVE_REQUIRED:
            if key not in clean:
                record("missing_key", f"alternatives[{i}].{key}", "required by the schema")
        out.append(clean)
    return out


def _normalise_feature(raw_feature: dict[str, Any], record: Any) -> dict[str, Any]:
    """One feature onto the canonical keys, recording every departure on the way."""
    feat = _rename_keys(
        raw_feature, FEATURE_KEY_ALIASES, FEATURE_REQUIRED, FEATURE_OPTIONAL, record
    )
    extra = _split_extra(feat, FEATURE_REQUIRED + FEATURE_OPTIONAL)
    for key in FEATURE_REQUIRED:
        if key not in feat:
            record("missing_key", key, "required by the schema")
            if key in FEATURE_DEFAULTS:
                feat[key] = json.loads(json.dumps(FEATURE_DEFAULTS[key]))
    fid = str(feat.get("feature_id", ""))
    if not FEATURE_ID_PATTERN.match(fid):
        record("feature_id_pattern", "feature_id", f"{fid!r} does not match the schema pattern")
    _check_waypoints(feat.get("waypoints_uv"), "waypoints_uv", record)
    feat["proposed_class"], feat["proposed_class_raw"] = _normalise_class(
        feat.get("proposed_class"), record
    )
    flags = feat.get("endpoint_flags")
    if not isinstance(flags, list) or len(flags) != 2:
        record("wrong_type", "endpoint_flags", f"expected two flags, got {flags!r}")
        flags = (
            list(flags) if isinstance(flags, list) else list(FEATURE_DEFAULTS["endpoint_flags"])
        )
    clean_flags: list[str] = []
    for flag in flags:
        if isinstance(flag, str) and flag.lower() in ENDPOINT_FLAGS:
            clean_flags.append(flag.lower())
        else:
            record(
                "endpoint_flag_not_in_vocabulary",
                "endpoint_flags",
                f"{flag!r} kept raw; this flag set to unknown",
            )
            clean_flags.append("unknown")
    feat["endpoint_flags"] = clean_flags
    feat["endpoint_flags_raw"] = [f if isinstance(f, str) else json.dumps(f) for f in flags]
    if not isinstance(feat.get("junction_ids"), list):
        record(
            "wrong_type", "junction_ids", f"expected an array, got {feat.get('junction_ids')!r}"
        )
        feat["junction_ids"] = []
    for key in ("evidence_note", "uncertainty_note"):
        _check_note(feat.get(key, ""), key, record)
    feat["alternatives"] = _normalise_alternatives(feat.get("alternatives"), record)
    if extra:
        feat["extra"] = extra
    return feat


def _normalise_junction(raw_junction: dict[str, Any], record: Any) -> dict[str, Any]:
    """One junction onto the canonical keys, recording every departure on the way."""
    junc = _rename_keys(
        raw_junction, JUNCTION_KEY_ALIASES, JUNCTION_REQUIRED, JUNCTION_OPTIONAL, record
    )
    extra = _split_extra(junc, JUNCTION_REQUIRED + JUNCTION_OPTIONAL)
    for key in JUNCTION_REQUIRED:
        if key not in junc:
            record("missing_key", key, "required by the schema")
            if key in JUNCTION_DEFAULTS:
                junc[key] = JUNCTION_DEFAULTS[key]
    uv = junc.get("uv")
    if not isinstance(uv, list | tuple) or len(uv) != 2:
        record("wrong_type", "uv", f"expected a two-element array, got {uv!r}")
    else:
        for axis, val in zip(("u", "v"), uv, strict=True):
            _check_coordinate(val, "uv", axis, record)
    n_branches = junc.get("n_branches")
    if isinstance(n_branches, bool) or not isinstance(n_branches, int):
        record("wrong_type", "n_branches", f"expected an integer, got {n_branches!r}")
    elif n_branches < 3:
        record("value_out_of_range", "n_branches", f"{n_branches} < schema minimum 3")
    for key in ("evidence_note", "uncertainty_note"):
        if key in junc:
            _check_note(junc.get(key, ""), key, record)
    if extra:
        junc["extra"] = extra
    return junc


def normalise_pass_a(
    raw_doc: dict[str, Any],
    *,
    tile_id: str,
    family: str,
    source: dict[str, str] | None = None,
    was_wrapped: bool = False,
) -> tuple[dict[str, Any], list[Deviation]]:
    """Map one delivered pass-A answer onto the canonical schema, recording each deviation.

    Key names are renamed, keys that are absent altogether are filled with a declared "not
    stated" default, and a class string outside the vocabulary falls back to ``unknown``
    with the original kept in ``proposed_class_raw``. No coordinate, note or class string
    is otherwise changed, and nothing the model sent is thrown away: keys the schema does
    not define are preserved under ``extra``.
    """
    deviations: list[Deviation] = []

    def make_record(scope: str, object_id: str) -> Any:
        def record(kind: str, key: str, detail: str) -> None:
            deviations.append(Deviation(tile_id, family, scope, object_id, key, kind, detail))

        return record

    top = make_record("top", tile_id)
    if was_wrapped:
        top("answer_not_bare_json", "<document>", "JSON had to be extracted from around it")
    doc = _rename_keys(raw_doc, {}, TOP_REQUIRED, TOP_OPTIONAL, top)
    for key in TOP_REQUIRED:
        if key not in doc:
            top("missing_key", key, "required by the schema")
    if str(doc.get("tile_id", "")) != tile_id:
        top("tile_id_mismatch", "tile_id", f"{doc.get('tile_id')!r} != requested {tile_id!r}")
    doc["tile_id"] = doc.get("tile_id", tile_id)

    features_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw_feature in enumerate(doc.get("features") or []):
        if not isinstance(raw_feature, dict):
            make_record("feature", f"<item-{i:02d}>")(
                "wrong_type", "<feature>", f"expected an object, got {type(raw_feature).__name__}"
            )
            continue
        fid = str(raw_feature.get("feature_id", raw_feature.get("id", f"<unnamed-{i:02d}>")))
        record = make_record("feature", fid)
        if fid in seen:
            record("duplicate_feature_id", "feature_id", f"{fid!r} used more than once")
        seen.add(fid)
        features_out.append(_normalise_feature(raw_feature, record))
    doc["features"] = features_out

    junctions_out: list[dict[str, Any]] = []
    for i, raw_junction in enumerate(doc.get("junctions") or []):
        if not isinstance(raw_junction, dict):
            make_record("junction", f"<item-{i:02d}>")(
                "wrong_type",
                "<junction>",
                f"expected an object, got {type(raw_junction).__name__}",
            )
            continue
        jid = str(raw_junction.get("junction_id", raw_junction.get("id", f"<unnamed-{i:02d}>")))
        junctions_out.append(_normalise_junction(raw_junction, make_record("junction", jid)))
    doc["junctions"] = junctions_out

    doc["provenance_note"] = {
        "note": (
            "Normalised copy of a delivered pass-A answer. Key names were mapped onto "
            "prompts/pass_a_schema.json, keys absent altogether were filled with a declared "
            "'not stated' default, and class strings outside the vocabulary were replaced by "
            "'unknown' with the original kept in proposed_class_raw. No coordinate, note or "
            "class string was otherwise changed and nothing was dropped; every change is "
            "listed in metrics/pass_a_schema_adherence.parquet. Not an interpretation, not a "
            "reference."
        ),
        "family": family,
        "tile_id": tile_id,
        "normaliser_version": NORMALISER_VERSION,
        "method_version": METHOD_VERSION,
        "schema": repo_relative(PASS_A_SCHEMA_PATH),
        "n_deviations": len(deviations),
        "source": source or {},
    }
    return doc, deviations


def pass_a_candidate_records(
    doc: dict[str, Any], *, tile_id: str, family: str, provenance: dict[str, Any]
) -> list[dict[str, Any]]:
    """Canonical candidate records for :func:`validate_candidates`, one per feature."""
    return [
        {
            "feature_id": str(feat.get("feature_id", "")),
            "tile_id": tile_id,
            "pass": "A",
            "model_family": family,
            "waypoints_uv": feat.get("waypoints_uv"),
            "proposed_class": feat.get("proposed_class", "unknown"),
            "proposed_class_raw": feat.get("proposed_class_raw", ""),
            "junction_ids": feat.get("junction_ids") or [],
            "endpoint_flags": feat.get("endpoint_flags") or [],
            "endpoint_flags_raw": feat.get("endpoint_flags_raw") or [],
            "evidence_note": feat.get("evidence_note", ""),
            "uncertainty_note": feat.get("uncertainty_note", ""),
            "alternatives": feat.get("alternatives") or [],
            "provenance": provenance,
        }
        for feat in doc.get("features") or []
    ]


def junctions_to_gdf(
    docs: list[tuple[str, str, dict[str, Any]]], tiles: list[TileSpec]
) -> gpd.GeoDataFrame:
    """Reported junctions as EPSG:3006 points, one row per (tile, family, junction)."""
    by_id = {t.tile_id: t for t in tiles}
    rows: list[dict[str, Any]] = []
    xs: list[float] = []
    ys: list[float] = []
    for tile_id, family, doc in sorted(docs, key=lambda d: (d[0], d[1])):
        tile = by_id[tile_id]
        for junc in sorted(doc.get("junctions") or [], key=lambda j: str(j.get("junction_id"))):
            uv = junc.get("uv")
            if not isinstance(uv, list | tuple) or len(uv) != 2:
                continue
            try:
                u, v = float(uv[0]), float(uv[1])
            except (TypeError, ValueError):
                continue
            if not (0.0 <= u <= tile.display_w and 0.0 <= v <= tile.display_h):
                continue
            x, y = image_to_map(tile, u, v)
            xs.append(x)
            ys.append(y)
            extra = junc.get("extra") or {}
            rows.append(
                {
                    "junction_id": str(junc.get("junction_id", "")),
                    "tile_id": tile_id,
                    "model_family": family,
                    "pass": "A",
                    "n_branches": junc.get("n_branches"),
                    "u": u,
                    "v": v,
                    "feature_ids": json.dumps(extra.get("feature_ids") or []),
                    "evidence_note": junc.get("evidence_note", ""),
                    "uncertainty_note": junc.get("uncertainty_note", ""),
                    "alternatives": json.dumps(extra.get("alternatives") or []),
                    "method_version": METHOD_VERSION,
                }
            )
    if not rows:
        return gpd.GeoDataFrame(
            {"junction_id": [], "tile_id": [], "model_family": []}, geometry=[], crs=WORKING_CRS
        )
    return gpd.GeoDataFrame(
        pd.DataFrame(rows), geometry=gpd.points_from_xy(xs, ys), crs=WORKING_CRS
    )


NORMALISER_VERSION_C = "pass-c-normalise-0.1"


PASS_C_SCHEMA_PATH = PROMPTS / "pass_c_schema.json"


PASS_C_PROMPT_PATH = PROMPTS / "pass_c_review.md"


VERDICTS: tuple[str, ...] = (
    "agree",
    "agree_with_different_class",
    "partly_agree_course_differs",
    "disagree_not_a_road",
    "cannot_tell",
)


# The verdicts whose proposal enters the reviewed product with its course unchanged.
VERDICTS_KEPT_AS_DRAWN: tuple[str, ...] = ("agree", "agree_with_different_class")


# ``unknown`` is not in the schema's enum: it is this code's declared "not stated", used
# when a delivery route asked for free text instead of the enum. The model's own words are
# never thrown away - they are kept in ``what_is_disputed_note``.
CONTESTED_KINDS_SCHEMA: tuple[str, ...] = (
    "branch_count",
    "which_lines_meet",
    "position",
    "exists_at_all",
)


REVIEW_KEY_ALIASES: dict[str, str] = {
    "id": "reviewed_feature_id",
    "feature_id": "reviewed_feature_id",
    "class": "proposed_class",
    "evidence": "evidence_note",
    "corrected_polyline": "corrected_waypoints_uv",
    "corrected_waypoints": "corrected_waypoints_uv",
}


CONTESTED_KEY_ALIASES: dict[str, str] = {
    # the pass-C runner script asked for "what_is_contested"; the schema calls it
    # "what_is_disputed". Both families answered with the runner's key, so every delivered
    # contested junction carries this rename - a property of the delivery route.
    "what_is_contested": "what_is_disputed",
    "coordinate": "uv",
    "evidence": "evidence_note",
}


TOP_C_REQUIRED: tuple[str, ...] = ("tile_id", "reviews", "features", "contested_junctions")


TOP_C_OPTIONAL: tuple[str, ...] = ("overall_note",)


REVIEW_REQUIRED: tuple[str, ...] = ("reviewed_feature_id", "verdict", "evidence_note")


REVIEW_OPTIONAL: tuple[str, ...] = (
    "proposed_class",
    "corrected_waypoints_uv",
    "uncertainty_note",
)


CONTESTED_REQUIRED: tuple[str, ...] = ("uv", "what_is_disputed", "evidence_note")


CONTESTED_OPTIONAL: tuple[str, ...] = ()


REVIEW_DEFAULTS: dict[str, Any] = {"evidence_note": "", "uncertainty_note": ""}


def _normalise_review(raw_review: dict[str, Any], record: Any) -> dict[str, Any]:
    """One review entry onto the canonical keys, recording every departure on the way."""
    rev = _rename_keys(raw_review, REVIEW_KEY_ALIASES, REVIEW_REQUIRED, REVIEW_OPTIONAL, record)
    extra = _split_extra(rev, REVIEW_REQUIRED + REVIEW_OPTIONAL)
    for key in REVIEW_REQUIRED:
        if key not in rev:
            record("missing_key", key, "required by the schema")
            if key in REVIEW_DEFAULTS:
                rev[key] = REVIEW_DEFAULTS[key]
    verdict = rev.get("verdict")
    rev["verdict_raw"] = (
        verdict if isinstance(verdict, str) else json.dumps(verdict, sort_keys=True)
    )
    if isinstance(verdict, str) and verdict.lower() in VERDICTS:
        if verdict != verdict.lower():
            record("verdict_case_normalised", "verdict", f"{verdict!r} -> {verdict.lower()!r}")
        rev["verdict"] = verdict.lower()
    else:
        record(
            "verdict_not_in_vocabulary",
            "verdict",
            f"{rev['verdict_raw']!r} kept raw; verdict set to cannot_tell",
        )
        rev["verdict"] = "cannot_tell"
    if "proposed_class" in rev:
        rev["proposed_class"], rev["proposed_class_raw"] = _normalise_class(
            rev.get("proposed_class"), record
        )
    else:
        rev["proposed_class"], rev["proposed_class_raw"] = "", ""
    pts = rev.get("corrected_waypoints_uv")
    if pts is None or (isinstance(pts, list) and not pts):
        rev["corrected_waypoints_uv"] = []
    else:
        _check_waypoints(pts, "corrected_waypoints_uv", record)
    if rev["verdict"] == "partly_agree_course_differs" and not rev["corrected_waypoints_uv"]:
        record(
            "correction_missing",
            "corrected_waypoints_uv",
            "verdict says the course differs but no corrected course was given",
        )
    for key in ("evidence_note", "uncertainty_note"):
        if key in rev:
            _check_note(rev.get(key, ""), key, record)
    if extra:
        rev["extra"] = extra
    return rev


def _normalise_contested(raw_cj: dict[str, Any], record: Any) -> dict[str, Any]:
    """One contested junction onto the canonical keys. The dispute text is never rewritten."""
    cj = _rename_keys(
        raw_cj, CONTESTED_KEY_ALIASES, CONTESTED_REQUIRED, CONTESTED_OPTIONAL, record
    )
    extra = _split_extra(cj, CONTESTED_REQUIRED + CONTESTED_OPTIONAL)
    for key in CONTESTED_REQUIRED:
        if key not in cj:
            record("missing_key", key, "required by the schema")
            cj[key] = [] if key == "uv" else ""
    uv = cj.get("uv")
    if not isinstance(uv, list | tuple) or len(uv) != 2:
        record("wrong_type", "uv", f"expected a two-element array, got {uv!r}")
    else:
        for axis, val in zip(("u", "v"), uv, strict=True):
            _check_coordinate(val, "uv", axis, record)
    what = cj.get("what_is_disputed")
    cj["what_is_disputed_note"] = (
        what if isinstance(what, str) else json.dumps(what, sort_keys=True)
    )
    if isinstance(what, str) and what.lower() in CONTESTED_KINDS_SCHEMA:
        cj["what_is_disputed"] = what.lower()
    else:
        record(
            "contested_kind_not_in_vocabulary",
            "what_is_disputed",
            "free text kept verbatim in what_is_disputed_note; what_is_disputed set to unknown",
        )
        cj["what_is_disputed"] = "unknown"
    _check_note(cj.get("evidence_note", ""), "evidence_note", record)
    if extra:
        cj["extra"] = extra
    return cj


def normalise_pass_c(
    raw_doc: dict[str, Any],
    *,
    tile_id: str,
    reviewer: str,
    producer: str,
    source: dict[str, str] | None = None,
    was_wrapped: bool = False,
) -> tuple[dict[str, Any], list[Deviation]]:
    """Map one delivered pass-C answer onto ``prompts/pass_c_schema.json``, recording each
    departure.

    The same rules as :func:`normalise_pass_a`: keys are renamed, absent keys are filled
    with a declared "not stated", a value outside a vocabulary falls back to that
    vocabulary's own "not decided" word with the original kept beside it, and nothing else
    is changed or dropped. Missed features go through :func:`_normalise_feature`, so a
    reviewer's own road is held to exactly the pass-A feature shape.
    """
    deviations: list[Deviation] = []
    family = reviewer

    def make_record(scope: str, object_id: str) -> Any:
        def record(kind: str, key: str, detail: str) -> None:
            deviations.append(Deviation(tile_id, family, scope, object_id, key, kind, detail))

        return record

    top = make_record("top", tile_id)
    if was_wrapped:
        top("answer_not_bare_json", "<document>", "JSON had to be extracted from around it")
    doc = _rename_keys(raw_doc, {}, TOP_C_REQUIRED, TOP_C_OPTIONAL, top)
    for key in TOP_C_REQUIRED:
        if key not in doc:
            top("missing_key", key, "required by the schema")
    if str(doc.get("tile_id", "")) != tile_id:
        top("tile_id_mismatch", "tile_id", f"{doc.get('tile_id')!r} != requested {tile_id!r}")
    doc["tile_id"] = doc.get("tile_id", tile_id)

    reviews_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw_review in enumerate(doc.get("reviews") or []):
        if not isinstance(raw_review, dict):
            make_record("review", f"<item-{i:02d}>")(
                "wrong_type", "<review>", f"expected an object, got {type(raw_review).__name__}"
            )
            continue
        rid = str(
            raw_review.get("reviewed_feature_id", raw_review.get("id", f"<unnamed-{i:02d}>"))
        )
        record = make_record("review", rid)
        if rid in seen:
            record("duplicate_reviewed_feature_id", "reviewed_feature_id", f"{rid!r} used twice")
        seen.add(rid)
        reviews_out.append(_normalise_review(raw_review, record))
    doc["reviews"] = reviews_out

    features_out: list[dict[str, Any]] = []
    for i, raw_feature in enumerate(doc.get("features") or []):
        if not isinstance(raw_feature, dict):
            make_record("feature", f"<item-{i:02d}>")(
                "wrong_type", "<feature>", f"expected an object, got {type(raw_feature).__name__}"
            )
            continue
        fid = str(raw_feature.get("feature_id", raw_feature.get("id", f"<unnamed-{i:02d}>")))
        features_out.append(_normalise_feature(raw_feature, make_record("feature", fid)))
    doc["features"] = features_out

    contested_out: list[dict[str, Any]] = []
    for i, raw_cj in enumerate(doc.get("contested_junctions") or []):
        if not isinstance(raw_cj, dict):
            make_record("contested_junction", f"<item-{i:02d}>")(
                "wrong_type", "<contested_junction>", f"got {type(raw_cj).__name__}"
            )
            continue
        contested_out.append(
            _normalise_contested(raw_cj, make_record("contested_junction", f"cj{i:02d}"))
        )
    doc["contested_junctions"] = contested_out

    doc["provenance_note"] = {
        "note": (
            "Normalised copy of a delivered pass-C answer. Key names were mapped onto "
            "prompts/pass_c_schema.json, keys absent altogether were filled with a declared "
            "'not stated' default, and a value outside a vocabulary was replaced by that "
            "vocabulary's 'not decided' word with the original kept beside it. No coordinate, "
            "note or verdict string was otherwise changed and nothing was dropped; every "
            "change is listed in metrics/pass_c_schema_adherence.parquet. Not an "
            "interpretation, not a reference, and not an adjudication between the families."
        ),
        "reviewing_family": reviewer,
        "reviewed_family": producer,
        "tile_id": tile_id,
        "normaliser_version": NORMALISER_VERSION_C,
        "method_version": METHOD_VERSION,
        "schema": repo_relative(PASS_C_SCHEMA_PATH),
        "n_deviations": len(deviations),
        "source": source or {},
    }
    return doc, deviations


def pass_c_correction_records(
    doc: dict[str, Any],
    *,
    tile_id: str,
    reviewer: str,
    producer: str,
    provenance: dict[str, Any],
) -> list[dict[str, Any]]:
    """Corrected courses as candidate records, so they meet the same validation as pass A.

    The record keeps the reviewed proposal's id in ``reviewed_feature_id``; the candidate's
    own id is that id with ``-corr`` appended, which is unique inside one reviewer's answer.
    """
    out: list[dict[str, Any]] = []
    for rev in doc.get("reviews") or []:
        pts = rev.get("corrected_waypoints_uv") or []
        if not pts:
            continue
        rid = str(rev.get("reviewed_feature_id", ""))
        out.append(
            {
                "feature_id": f"{rid}-corr",
                "reviewed_feature_id": rid,
                "tile_id": tile_id,
                "pass": "C",
                "model_family": reviewer,
                "role": "course_correction",
                "reviewed_family": producer,
                "verdict": rev.get("verdict", ""),
                "waypoints_uv": pts,
                "proposed_class": rev.get("proposed_class") or "unknown",
                "proposed_class_raw": rev.get("proposed_class_raw", ""),
                "junction_ids": [],
                "endpoint_flags": ["unknown", "unknown"],
                "endpoint_flags_raw": [],
                "evidence_note": rev.get("evidence_note", ""),
                "uncertainty_note": rev.get("uncertainty_note", ""),
                "alternatives": [],
                "provenance": provenance,
            }
        )
    return out


def pass_c_missed_records(
    doc: dict[str, Any],
    *,
    tile_id: str,
    reviewer: str,
    producer: str,
    provenance: dict[str, Any],
) -> list[dict[str, Any]]:
    """Roads the reviewer says the proposal missed, as candidate records for validation."""
    return [
        {
            **rec,
            "role": "missed_by_proposal",
            "reviewed_family": producer,
            "reviewed_feature_id": "",
            "verdict": "",
        }
        for rec in pass_a_candidate_records(
            doc, tile_id=tile_id, family=reviewer, provenance=provenance
        )
    ]


def _with_record_columns(
    gdf: gpd.GeoDataFrame, records: list[dict[str, Any]], keys: tuple[str, ...]
) -> gpd.GeoDataFrame:
    """Carry extra record keys onto the geometry frame, matched on (tile_id, feature_id)."""
    lookup = {(str(r["tile_id"]), str(r["feature_id"])): r for r in records}
    for key in keys:
        gdf[key] = [
            str(lookup[(str(t), str(f))].get(key, ""))
            for t, f in zip(gdf["tile_id"], gdf["feature_id"], strict=True)
        ]
    return gdf


def course_offset(
    proposal: LineString, correction: LineString, step_m: float = 1.0
) -> tuple[float, float]:
    """How far the reviewer moved the course: mean sample offset, and the symmetric maximum.

    ``mean`` is the average distance from a 1 m sample of the corrected course to the
    proposal; ``max`` is the Hausdorff distance, so a correction that agrees along most of
    its length but leaves the proposal at one end still shows that end.
    """
    if proposal.is_empty or correction.is_empty or correction.length == 0:
        return float("nan"), float("nan")
    pts = _sample_points(correction, step_m)
    dist = shapely.distance(shapely.points(pts[:, 0], pts[:, 1]), proposal)
    return float(dist.mean()), float(shapely.hausdorff_distance(proposal, correction))


def reviews_to_gdf(
    docs: list[tuple[str, str, str, dict[str, Any]]],
    proposals: gpd.GeoDataFrame,
    corrections: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, list[dict[str, Any]]]:
    """Each review joined to the geometry of the proposal it judges, with its verdict.

    Returns the joined frame and the reviews whose ``reviewed_feature_id`` names no
    proposal - those are counted, never quietly dropped.
    """
    prop_idx = (
        {(str(r["tile_id"]), str(r["feature_id"])): r for _, r in proposals.iterrows()}
        if len(proposals)
        else {}
    )
    corr_idx = (
        {
            (str(r["tile_id"]), str(r["model_family"]), str(r["reviewed_feature_id"])): r
            for _, r in corrections.iterrows()
        }
        if len(corrections)
        else {}
    )
    rows: list[dict[str, Any]] = []
    geoms: list[LineString] = []
    unmatched: list[dict[str, Any]] = []
    for tile_id, reviewer, producer, doc in sorted(docs, key=lambda d: (d[0], d[1])):
        for rev in sorted(
            doc.get("reviews") or [], key=lambda r: str(r.get("reviewed_feature_id", ""))
        ):
            rid = str(rev.get("reviewed_feature_id", ""))
            prop = prop_idx.get((tile_id, rid))
            if prop is None:
                unmatched.append(
                    {
                        "tile_id": tile_id,
                        "reviewing_family": reviewer,
                        "reviewed_family": producer,
                        "reviewed_feature_id": rid,
                        "verdict": str(rev.get("verdict", "")),
                        "reason": "reviewed_feature_id names no accepted proposal",
                    }
                )
                continue
            corr = corr_idx.get((tile_id, reviewer, rid))
            geom = prop.geometry
            if corr is not None:
                mean_off, max_off = course_offset(geom, corr.geometry)
            else:
                mean_off, max_off = float("nan"), float("nan")
            rows.append(
                {
                    "tile_id": tile_id,
                    "reviewing_family": reviewer,
                    "reviewed_family": producer,
                    "feature_id": rid,
                    "verdict": str(rev.get("verdict", "")),
                    "verdict_raw": str(rev.get("verdict_raw", "")),
                    "proposed_class_producer": str(prop["proposed_class"]),
                    "proposed_class_reviewer": str(rev.get("proposed_class", "")),
                    "class_differs": bool(
                        rev.get("proposed_class")
                        and str(rev.get("proposed_class")) != str(prop["proposed_class"])
                    ),
                    "length_m": float(geom.length),
                    "has_correction": corr is not None,
                    "correction_feature_id": str(corr["feature_id"]) if corr is not None else "",
                    "correction_length_m": float(corr.geometry.length)
                    if corr is not None
                    else float("nan"),
                    "offset_mean_m": mean_off,
                    "offset_max_m": max_off,
                    "evidence_note": str(rev.get("evidence_note", "")),
                    "uncertainty_note": str(rev.get("uncertainty_note", "")),
                    "producer_evidence_note": str(prop.get("evidence_note", "")),
                    "method_version": METHOD_VERSION,
                    "note": "a verdict is one family's reading of another's, with no "
                    "reference behind it",
                }
            )
            geoms.append(geom)
    if not rows:
        return (
            gpd.GeoDataFrame(
                {"tile_id": [], "reviewing_family": [], "reviewed_family": [], "feature_id": []},
                geometry=[],
                crs=WORKING_CRS,
            ),
            unmatched,
        )
    return gpd.GeoDataFrame(pd.DataFrame(rows), geometry=geoms, crs=WORKING_CRS), unmatched


def build_reviewed_product(
    producer: str,
    reviewer: str,
    proposals: gpd.GeoDataFrame,
    reviews: gpd.GeoDataFrame,
    corrections: gpd.GeoDataFrame,
    missed: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """One family's pass-A reading after the other family's blind review.

    Returns the product and the table of proposals it leaves out, with the reason for each.
    Every feature carries where it came from in ``provenance_kind``, so the reviewed product
    can always be taken apart again into what one family drew and what the other changed.
    """
    rows: list[dict[str, Any]] = []
    geoms: list[LineString] = []
    excluded: list[dict[str, Any]] = []
    props = proposals[proposals["model_family"] == producer] if len(proposals) else proposals
    rv_idx = (
        {
            (str(r["tile_id"]), str(r["feature_id"])): r
            for _, r in reviews.iterrows()
            if str(r["reviewed_family"]) == producer
        }
        if len(reviews)
        else {}
    )
    corr_idx = (
        {
            (str(r["tile_id"]), str(r["reviewed_feature_id"])): r
            for _, r in corrections.iterrows()
            if str(r["model_family"]) == reviewer
        }
        if len(corrections)
        else {}
    )
    for _, prop in props.iterrows():
        key = (str(prop["tile_id"]), str(prop["feature_id"]))
        rev = rv_idx.get(key)
        verdict = str(rev["verdict"]) if rev is not None else ""
        if rev is None:
            excluded.append(
                {
                    "tile_id": key[0],
                    "producer_family": producer,
                    "reviewing_family": reviewer,
                    "feature_id": key[1],
                    "verdict": "",
                    "length_m": float(prop.geometry.length),
                    "reason": "no_review_returned",
                }
            )
            continue
        if verdict in VERDICTS_KEPT_AS_DRAWN:
            kind = (
                "pass_a_kept_class_contested"
                if verdict == "agree_with_different_class"
                else "pass_a_kept_agreed"
            )
            geom = prop.geometry
        elif verdict == "partly_agree_course_differs":
            corr = corr_idx.get(key)
            if corr is None:
                excluded.append(
                    {
                        "tile_id": key[0],
                        "producer_family": producer,
                        "reviewing_family": reviewer,
                        "feature_id": key[1],
                        "verdict": verdict,
                        "length_m": float(prop.geometry.length),
                        "reason": "course_differs_but_no_correction_given",
                    }
                )
                continue
            kind = "course_corrected_by_reviewer"
            geom = corr.geometry
        else:
            excluded.append(
                {
                    "tile_id": key[0],
                    "producer_family": producer,
                    "reviewing_family": reviewer,
                    "feature_id": key[1],
                    "verdict": verdict,
                    "length_m": float(prop.geometry.length),
                    "reason": "reviewer_says_not_a_road"
                    if verdict == "disagree_not_a_road"
                    else "reviewer_cannot_tell",
                }
            )
            continue
        rows.append(
            {
                "product_feature_id": f"{prop['tile_id']}:{producer}:{prop['feature_id']}",
                "feature_id": str(prop["feature_id"]),
                "tile_id": str(prop["tile_id"]),
                "model_family": producer,
                "product": "reviewed",
                "provenance_kind": kind,
                "geometry_from": producer if kind != "course_corrected_by_reviewer" else reviewer,
                "class_from": producer,
                "reviewing_family": reviewer,
                "verdict": verdict,
                "proposed_class": str(prop["proposed_class"]),
                "proposed_class_reviewer": str(rev["proposed_class_reviewer"]),
                "class_contested": bool(rev["class_differs"]),
                "evidence_note": str(prop.get("evidence_note", "")),
                "reviewer_evidence_note": str(rev["evidence_note"]),
                "method_version": METHOD_VERSION,
            }
        )
        geoms.append(geom)
    miss = missed[missed["model_family"] == reviewer] if len(missed) else missed
    for _, m in miss.iterrows():
        if str(m.get("reviewed_family", producer)) != producer:
            continue
        rows.append(
            {
                "product_feature_id": f"{m['tile_id']}:{reviewer}:{m['feature_id']}",
                "feature_id": str(m["feature_id"]),
                "tile_id": str(m["tile_id"]),
                "model_family": producer,
                "product": "reviewed",
                "provenance_kind": "added_by_reviewer",
                "geometry_from": reviewer,
                "class_from": reviewer,
                "reviewing_family": reviewer,
                "verdict": "",
                "proposed_class": str(m["proposed_class"]),
                "proposed_class_reviewer": str(m["proposed_class"]),
                "class_contested": False,
                "evidence_note": "",
                "reviewer_evidence_note": str(m.get("evidence_note", "")),
                "method_version": METHOD_VERSION,
            }
        )
        geoms.append(m.geometry)
    if not rows:
        empty = gpd.GeoDataFrame(
            {"product_feature_id": [], "tile_id": [], "model_family": [], "proposed_class": []},
            geometry=[],
            crs=WORKING_CRS,
        )
        return empty, pd.DataFrame(excluded)
    gdf = gpd.GeoDataFrame(pd.DataFrame(rows), geometry=geoms, crs=WORKING_CRS)
    gdf["length_m"] = gdf.geometry.length
    if gdf["product_feature_id"].duplicated().any():
        dupes = sorted(gdf.loc[gdf["product_feature_id"].duplicated(), "product_feature_id"])
        raise ValueError(f"reviewed product has duplicate feature ids: {dupes}")
    gdf = gdf.sort_values(["tile_id", "product_feature_id"], kind="stable").reset_index(drop=True)
    return gdf, pd.DataFrame(excluded)


METHOD_VERSION = "reference-scoring-0.1"


NOT_SCORED = "not_scored"


VISION_COARSE: dict[str, str] = {
    "double_line_wide": "major",
    "double_line_narrow": "minor",
    "single_line": "track_path",
    "dashed_line": "track_path",
    "park_path": "track_path",
    "avenue_lined": NOT_SCORED,
    "unknown": NOT_SCORED,
}


def _with_coarse(
    lines: gpd.GeoDataFrame, class_column: str, crosswalk: dict[str, str]
) -> tuple[gpd.GeoDataFrame, list[str]]:
    lines = lines.copy()
    lines["product_class"] = lines[class_column].fillna("unknown").astype(str)
    unmapped = sorted(set(lines["product_class"]) - set(crosswalk))
    lines["coarse_class"] = lines["product_class"].map(crosswalk).fillna(NOT_SCORED)
    return lines, unmapped


def _empty_lines() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"feature_id": [], "tile_id": [], "model_family": [], "proposed_class": []},
        geometry=[],
        crs=WORKING_CRS,
    )


def _candidates(records: list[dict[str, Any]], tile: Any, keys: tuple[str, ...] = ()) -> Any:

    if not records:
        gdf = _empty_lines()
        for k in keys:
            gdf[k] = []
        return gdf
    gdf = candidates_to_gdf(records, [tile])
    return _with_record_columns(gdf, records, keys) if keys else gdf


def _read_pass_a(
    tile: Any,
    family: str,
    path: Path,
    prompt_path: Path | None = None,
    run_id: str | None = None,
    normalised_out: Path | None = None,
) -> tuple[Any, list, dict[str, Any], Any]:
    """Normalise, validate and vectorise one pass-A answer in memory.

    Nothing is written unless ``normalised_out`` names a file for the normalised copy (the
    informed-reading experiment keeps one next to each answer, as pass B does). The prompt
    and run id default to the t06 vision-roads ones and only enter the candidates'
    provenance.
    """

    raw_doc, wrapped = extract_json_object(path.read_text(encoding="utf-8"))
    source = {"path": _rel(path), "sha256": sha256_file(path)}
    doc, devs = normalise_pass_a(
        raw_doc, tile_id=tile.tile_id, family=family, source=source, was_wrapped=wrapped
    )
    if normalised_out is not None:
        normalised_out.write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    prompt = prompt_path or PASS_A_PROMPT_PATH
    prompt_sha = sha256_file(prompt) if prompt.is_file() else "unknown"
    recs = pass_a_candidate_records(
        doc,
        tile_id=tile.tile_id,
        family=family,
        provenance={
            "prompt_sha256": prompt_sha,
            "image_sha256": {k: v["sha256"] for k, v in tile.renders.items()},
            "model_id": family,
            "run_id": run_id or f"{tile.tile_id}_{family}_passA",
        },
    )
    accepted, rejected = validate_candidates(recs, [tile])
    lines = _candidates(accepted, tile)
    junctions = junctions_to_gdf([(tile.tile_id, family, doc)], [tile])
    counts = {
        "returned": len(recs),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "rejection_reasons": sorted({r.reason for r in rejected}),
        "schema_deviations": len(devs),
        "junctions_reported": len(doc.get("junctions") or []),
    }
    return lines, devs, counts, junctions


def _reviewed_product(
    tile: Any,
    producer: str,
    reviewer: str,
    proposals: Any,
    c_path: Path,
    prompt_path: Path | None = None,
    run_id: str | None = None,
    normalised_out: Path | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Assemble the reviewed product exactly as the review pass does, in memory.

    ``prompt_path``, ``run_id`` and ``normalised_out`` as in :func:`_read_pass_a`.
    """

    raw_doc, wrapped = extract_json_object(c_path.read_text(encoding="utf-8"))
    source = {"path": _rel(c_path), "sha256": sha256_file(c_path)}
    doc, devs = normalise_pass_c(
        raw_doc,
        tile_id=tile.tile_id,
        reviewer=reviewer,
        producer=producer,
        source=source,
        was_wrapped=wrapped,
    )
    if normalised_out is not None:
        normalised_out.write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    prompt = prompt_path or PASS_C_PROMPT_PATH
    prompt_sha = sha256_file(prompt) if prompt.is_file() else "unknown"
    prov = {
        "prompt_sha256": prompt_sha,
        "image_sha256": {k: v["sha256"] for k, v in tile.renders.items()},
        "model_id": reviewer,
        "run_id": run_id or f"{tile.tile_id}_{reviewer}_reviews_{producer}_passC",
    }
    kw = {"tile_id": tile.tile_id, "reviewer": reviewer, "producer": producer, "provenance": prov}
    recs = pass_c_correction_records(doc, **kw) + pass_c_missed_records(doc, **kw)
    accepted, rejected = validate_candidates(recs, [tile])
    keys = ("role", "reviewed_family", "reviewed_feature_id", "verdict")
    corr = [r for r in accepted if r["role"] == "course_correction"]
    miss = [r for r in accepted if r["role"] != "course_correction"]
    corrections = _candidates(corr, tile, keys)
    missed = _candidates(miss, tile, keys)
    props = proposals.drop(columns=["product_class", "coarse_class"], errors="ignore")
    reviews, unmatched = reviews_to_gdf(
        [(tile.tile_id, reviewer, producer, doc)], props, corrections
    )
    product, excluded = build_reviewed_product(
        producer, reviewer, props, reviews, corrections, missed
    )
    counts = {
        "reviews": len(doc.get("reviews") or []),
        "unmatched_reviews": len(unmatched),
        "corrections_accepted": len(corr),
        "missed_accepted": len(miss),
        "pass_c_rejected": len(rejected),
        "left_out": len(excluded),
        "left_out_m": float(excluded["length_m"].sum()) if len(excluded) else 0.0,
        "contested_junctions": len(doc.get("contested_junctions") or []),
        "schema_deviations_pass_c": len(devs),
    }
    return product, counts


def verdict(value: float, limit: float, kind: str) -> str:
    """'pass' | 'fail' | 'not_measurable' for one metric against its threshold."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "not_measurable"
    ok = value >= limit if kind == "min" else value <= limit
    return "pass" if ok else "fail"


def _rel(path: Path) -> str:
    return rel(path)
