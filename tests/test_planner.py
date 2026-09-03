from __future__ import annotations

from compressor import (
    PlannedAsset,
    PlannedVariant,
    _plan_variant_selection,
    _selection_stream_bytes,
)


def _variant(score: int, size: int) -> PlannedVariant:
    return PlannedVariant(score, 1.0, 80, 300, b"x" * size)


def test_global_planner_respects_budget_and_common_quality_floor() -> None:
    variants = (
        _variant(0, 10),
        _variant(100, 20),
        _variant(200, 50),
    )
    assets = [
        PlannedAsset(("figure", 0, 0), "figure", 0, variants),
        PlannedAsset(("figure", 0, 1), "figure", 0, variants),
    ]

    selection = _plan_variant_selection(assets, 70)

    assert min(selection) >= 1
    assert max(selection) == 2
    assert _selection_stream_bytes(assets, selection) <= 70


def test_global_planner_falls_back_to_minimum_variants() -> None:
    variants = (_variant(0, 10), _variant(100, 20))
    assets = [
        PlannedAsset(("image", 0), "image", 0, variants),
        PlannedAsset(("figure", 0, 0), "figure", 0, variants),
    ]

    assert _plan_variant_selection(assets, 15) == (0, 0)
