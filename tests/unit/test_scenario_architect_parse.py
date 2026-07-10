"""Regression tests for ScenarioArchitect._parse hardening (AUDIT-A5a/A5b).

Covers the 2026-07 parse-side validation:
  - AUDIT-A5a: delta arrays padded/truncated to exactly 10 numeric elements
    (short arrays from truncation-repair used to zip-shorten the DCF growth
    path and crash dcf_engine's length assert)
  - AUDIT-A5b: case-name normalization ("Bearish " / "悲观" → "bear")
  - AUDIT-A5b: incomplete structure (≠3 cases / missing name / non-numeric
    probability) raises ValueError → orchestrator mechanical fallback
  - AUDIT-A5b: three-way probability renormalization to sum 1.0
"""

import pytest
from unittest.mock import MagicMock

from aegis.core.chief_analyst.scenario_architect import (
    ScenarioArchitect,
    _coerce_delta_path,
    _normalize_case_name,
)


def _case(name, prob, growth=None, margin=None, **extra):
    c = {
        "name": name,
        "probability": prob,
        "narrative": f"{name} scenario narrative long enough for tests",
        "key_driver": f"{name} key driver",
        "revenue_growth_delta": growth if growth is not None else [0.0] * 10,
        "margin_delta": margin if margin is not None else [0.0] * 10,
    }
    c.update(extra)
    return c


def _raw(cases):
    return {
        "scenarios": cases,
        "key_disagreements": ["disagreement 1", "disagreement 2"],
        "primary_swing_factor": "swing factor",
    }


def _parse(raw):
    return ScenarioArchitect(llm_client=MagicMock())._parse(raw)


class TestDeltaPadding:
    """AUDIT-A5a: all delta arrays coerced to exactly 10 numeric elements."""

    def test_short_growth_delta_padded_to_10(self):
        """The audited failure: truncation-repair leaves a 3-element array."""
        bp = _parse(_raw([
            _case("bear", 0.25, growth=[-0.05, -0.04, -0.03]),
            _case("base", 0.50),
            _case("bull", 0.25),
        ]))
        bear = bp.get_case("bear")
        assert len(bear.revenue_growth_delta) == 10
        assert bear.revenue_growth_delta[:3] == [-0.05, -0.04, -0.03]
        assert bear.revenue_growth_delta[3:] == [0.0] * 7

    def test_short_margin_delta_padded_to_10(self):
        bp = _parse(_raw([
            _case("bear", 0.25, margin=[-0.02]),
            _case("base", 0.50),
            _case("bull", 0.25),
        ]))
        bear = bp.get_case("bear")
        assert len(bear.margin_delta) == 10
        assert bear.margin_delta[0] == -0.02
        assert bear.margin_delta[1:] == [0.0] * 9

    def test_long_delta_truncated_to_10(self):
        bp = _parse(_raw([
            _case("bear", 0.25, growth=[-0.01] * 14),
            _case("base", 0.50),
            _case("bull", 0.25),
        ]))
        assert len(bp.get_case("bear").revenue_growth_delta) == 10

    def test_driver_deltas_padded_to_10(self):
        bp = _parse(_raw([
            _case("bear", 0.25, driver_deltas={"DAU": [-0.01, -0.02]}),
            _case("base", 0.50),
            _case("bull", 0.25),
        ]))
        dau = bp.get_case("bear").driver_deltas["DAU"]
        assert len(dau) == 10
        assert dau[:2] == [-0.01, -0.02]
        assert dau[2:] == [0.0] * 8

    def test_non_numeric_elements_coerced_or_dropped(self):
        """Numeric strings coerced; garbage dropped; result still 10 long."""
        bp = _parse(_raw([
            _case("bear", 0.25, growth=["-0.05", "n/a", -0.03, None]),
            _case("base", 0.50),
            _case("bull", 0.25),
        ]))
        bear = bp.get_case("bear")
        assert len(bear.revenue_growth_delta) == 10
        assert bear.revenue_growth_delta[:2] == [-0.05, -0.03]

    def test_coerce_delta_path_helper(self):
        assert _coerce_delta_path(None) == [0.0] * 10
        assert _coerce_delta_path([1, "2.5", "x", True]) == [1.0, 2.5] + [0.0] * 8
        assert len(_coerce_delta_path([0.1] * 20)) == 10


class TestNameNormalization:
    """AUDIT-A5b: LLM name variants normalized so get_case() matches."""

    def test_bearish_with_trailing_space(self):
        bp = _parse(_raw([
            _case("Bearish ", 0.25, growth=[-0.05] * 10),
            _case("base", 0.50),
            _case("bull", 0.25),
        ]))
        bear = bp.get_case("bear")
        assert bear is not None
        assert bear.name == "bear"

    def test_chinese_names(self):
        bp = _parse(_raw([
            _case("悲观", 0.25),
            _case("中性", 0.50),
            _case("乐观", 0.25),
        ]))
        assert bp.get_case("bear") is not None
        assert bp.get_case("base") is not None
        assert bp.get_case("bull") is not None

    def test_base_case_variant_gets_zeroed_deltas(self):
        """Normalization runs before base-delta zeroing, so 'Base Case'
        with non-zero deltas still comes out all-zero."""
        bp = _parse(_raw([
            _case("bear", 0.25),
            _case("Base Case", 0.50, growth=[0.01] * 10, margin=[0.02] * 10),
            _case("bull", 0.25),
        ]))
        base = bp.get_case("base")
        assert all(d == 0.0 for d in base.revenue_growth_delta)
        assert all(d == 0.0 for d in base.margin_delta)

    def test_normalize_case_name_helper(self):
        assert _normalize_case_name("Bullish") == "bull"
        assert _normalize_case_name("牛市") == "bull"
        assert _normalize_case_name("熊市") == "bear"
        assert _normalize_case_name("bear scenario") == "bear"
        assert _normalize_case_name("neutral") == "base"
        assert _normalize_case_name(None) is None
        assert _normalize_case_name("sideways") is None


class TestIncompleteStructureRaises:
    """AUDIT-A5b: broken structure raises → orchestrator mechanical fallback."""

    def test_missing_case_raises(self):
        """Truncation-repair dropping the bull case must not pass silently."""
        with pytest.raises(ValueError):
            _parse(_raw([_case("bear", 0.25), _case("base", 0.50)]))

    def test_duplicate_case_raises(self):
        with pytest.raises(ValueError):
            _parse(_raw([
                _case("bear", 0.25), _case("bear", 0.25), _case("base", 0.50),
            ]))

    def test_missing_name_raises(self):
        cases = [_case("bear", 0.25), _case("base", 0.50), _case("bull", 0.25)]
        del cases[2]["name"]
        with pytest.raises(ValueError):
            _parse(_raw(cases))

    def test_unrecognized_name_raises(self):
        with pytest.raises(ValueError):
            _parse(_raw([
                _case("sideways", 0.25), _case("base", 0.50), _case("bull", 0.25),
            ]))

    def test_non_numeric_probability_raises(self):
        with pytest.raises(ValueError):
            _parse(_raw([
                _case("bear", "high"), _case("base", 0.50), _case("bull", 0.25),
            ]))

    def test_missing_probability_raises(self):
        cases = [_case("bear", 0.25), _case("base", 0.50), _case("bull", 0.25)]
        del cases[0]["probability"]
        with pytest.raises(ValueError):
            _parse(_raw(cases))

    def test_numeric_string_probability_coerced(self):
        bp = _parse(_raw([
            _case("bear", "0.25"), _case("base", 0.50), _case("bull", 0.25),
        ]))
        assert bp.get_case("bear").probability == 0.25

    def test_empty_scenarios_raises(self):
        with pytest.raises(ValueError):
            _parse(_raw([]))


class TestProbabilityRenormalization:
    """AUDIT-A5b: three-way renormalization inside _parse."""

    def test_over_unity_probabilities_renormalized(self):
        bp = _parse(_raw([
            _case("bear", 0.3), _case("base", 0.5), _case("bull", 0.4),
        ]))
        total = sum(s.probability for s in bp.scenarios)
        assert abs(total - 1.0) < 1e-9
        # Ratios preserved: base = 0.5 / 1.2
        assert abs(bp.get_case("base").probability - 0.5 / 1.2) < 1e-9

    def test_exact_probabilities_untouched(self):
        bp = _parse(_raw([
            _case("bear", 0.25), _case("base", 0.50), _case("bull", 0.25),
        ]))
        assert bp.get_case("bear").probability == 0.25
        assert bp.get_case("base").probability == 0.50


class TestEndToEnd:
    """Messy-but-recoverable response survives the full architect() path."""

    def test_architect_with_messy_response(self):
        mock_llm = MagicMock()
        mock_llm.call_structured.return_value = _raw([
            _case("Bearish", 0.3, growth=[-0.05, -0.04, -0.03]),
            _case("基准", 0.5),
            _case("Bull ", 0.4, margin=["0.02", 0.01]),
        ])
        architect = ScenarioArchitect(llm_client=mock_llm)
        bp = architect.architect(
            entity_id="test", entity_name="TEST",
            base_dcf_assumptions={"revenue_growth_path": [0.10] * 10},
            meta_facts={}, computed_metrics={},
        )
        assert sorted(s.name for s in bp.scenarios) == ["base", "bear", "bull"]
        assert all(len(s.revenue_growth_delta) == 10 for s in bp.scenarios)
        assert all(len(s.margin_delta) == 10 for s in bp.scenarios)
        assert abs(sum(s.probability for s in bp.scenarios) - 1.0) < 1e-9
