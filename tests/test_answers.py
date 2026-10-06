"""Model answers: JSON extraction and key variants a reader actually used."""

from __future__ import annotations

from histnet import answers


def test_json_is_extracted_from_a_fenced_answer() -> None:
    doc, wrapped = answers.extract_json_object('Here it is:\n```json\n{"features": []}\n```\n')
    assert doc == {"features": []} and wrapped


def test_known_key_variants_are_recognised() -> None:
    assert answers.FEATURE_KEY_ALIASES["points"] == "waypoints_uv"
    assert answers.JUNCTION_KEY_ALIASES["point"] == "uv"
    assert answers.JUNCTION_KEY_ALIASES["coordinates"] == "uv"
