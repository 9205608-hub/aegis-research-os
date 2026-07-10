"""Regression tests for orchestrator scenario-path fixes (AUDIT 2026-07).

Covers:
- AUDIT-A4 / E2: `cap_cumulative_growth_path` — the shared BUG-Y23 30×
  cumulative-growth cap, now applied on BOTH the base `_build_dcf_input`
  path and the bear/bull driver_deltas path. A Cambricon-style hyper-growth
  path (+453%/+106%/...) must be capped so cumulative scale ≤ 30× with the
  correct capped_year; ordinary paths must pass through untouched.
- AUDIT-A5 (orchestrator side): `renormalize_scenario_probabilities` —
  bear/base/bull weights must sum to 1.0 before pw_value, even when the
  LLM dropped/renamed a case and orchestrator defaults survived.
- AUDIT-A9 (BUG-Y20 third path): variant/valuation analysts' rule-based
  observation must not render a boundary-hit implied growth artifact.
"""

from __future__ import annotations

import math

import pytest

from aegis.core.orchestrator.auto_research import (
    MAX_TERMINAL_RATIO,
    cap_cumulative_growth_path,
    renormalize_scenario_probabilities,
)


def _cumulative_scale(path: list[float]) -> float:
    scale = 1.0
    for g in path:
        scale *= (1 + g)
    return scale


# ─────────────────────────────────────────────────────────────────
# AUDIT-A4 / E2: cumulative growth cap
# ─────────────────────────────────────────────────────────────────

class TestCapCumulativeGrowthPath:
    # Cambricon FY2025-style driver path: +453% Y1, +106% Y2, decaying but
    # still hyper-growth — uncapped this compounds to >>30× by mid-horizon.
    CAMBRICON_PATH = [4.53, 1.06, 0.80, 0.60, 0.45, 0.35, 0.28, 0.22, 0.18, 0.15]
    TG = 0.03

    def test_cambricon_path_is_capped(self):
        capped, capped_year = cap_cumulative_growth_path(self.CAMBRICON_PATH, self.TG)
        assert capped_year >= 0, "hyper-growth path must trip the cap"
        # Post-cap cumulative scale must not blow far past the threshold:
        # the cap year itself may overshoot (cap triggers on crossing), but
        # every subsequent year grows at ~terminal, so total scale stays
        # bounded by (threshold-crossing year value) × (1+tg+0.005)^rest.
        pre_cap_scale = _cumulative_scale(self.CAMBRICON_PATH[: capped_year + 1])
        remaining = len(self.CAMBRICON_PATH) - (capped_year + 1)
        bound = pre_cap_scale * (1 + self.TG + 0.005) ** remaining
        assert _cumulative_scale(capped) <= bound + 1e-9
        # And it must be drastically below the uncapped 60-100×+ blowup.
        assert _cumulative_scale(capped) < _cumulative_scale(self.CAMBRICON_PATH)

    def test_capped_year_is_first_year_crossing_threshold(self):
        capped, capped_year = cap_cumulative_growth_path(self.CAMBRICON_PATH, self.TG)
        # Recompute the expected first crossing by hand.
        cum = 1.0
        expected = -1
        for i, g in enumerate(self.CAMBRICON_PATH):
            cum *= (1 + g)
            if cum > MAX_TERMINAL_RATIO:
                expected = i
                break
        assert expected >= 0
        assert capped_year == expected
        # Years before the cap are untouched; years from the cap onward run
        # at terminal_growth + 0.5% premium (original BUG-Y23 convention).
        for i in range(capped_year):
            assert capped[i] == self.CAMBRICON_PATH[i]
        for i in range(capped_year, len(capped)):
            assert capped[i] == pytest.approx(round(self.TG + 0.005, 4))

    def test_ordinary_path_untouched(self):
        ordinary = [0.15, 0.12, 0.10, 0.08, 0.07, 0.06, 0.05, 0.045, 0.04, 0.035]
        assert _cumulative_scale(ordinary) < MAX_TERMINAL_RATIO
        capped, capped_year = cap_cumulative_growth_path(ordinary, self.TG)
        assert capped_year == -1
        assert capped == ordinary

    def test_does_not_mutate_input(self):
        original = list(self.CAMBRICON_PATH)
        cap_cumulative_growth_path(self.CAMBRICON_PATH, self.TG)
        assert self.CAMBRICON_PATH == original

    def test_exactly_at_threshold_not_capped(self):
        # Cumulative scale exactly == max_ratio must NOT trip (strict >).
        g = MAX_TERMINAL_RATIO ** (1 / 10) - 1  # 10 equal years → exactly 30×
        path = [g] * 10
        assert math.isclose(_cumulative_scale(path), MAX_TERMINAL_RATIO)
        _, capped_year = cap_cumulative_growth_path(path, self.TG)
        assert capped_year == -1

    def test_bear_tree_more_aggressive_than_capped_base_also_capped(self):
        # The A4 failure mode: base got capped at 30× while a bear tree
        # compounding 60×+ sailed through uncapped and inverted above base.
        # Both must now trip the cap and land in the same order of magnitude
        # (the crossing year itself may overshoot 30× — original BUG-Y23
        # convention — but subsequent years run at ~terminal, so no path can
        # keep compounding to the old 60-128× blowup).
        bear_path = [4.20, 1.00, 0.75, 0.55, 0.42, 0.33, 0.26, 0.20, 0.17, 0.14]
        assert _cumulative_scale(bear_path) > 60  # uncapped it explodes
        base_capped, base_year = cap_cumulative_growth_path(self.CAMBRICON_PATH, self.TG)
        bear_capped, bear_year = cap_cumulative_growth_path(bear_path, self.TG)
        assert base_year >= 0 and bear_year >= 0
        for capped, raw in ((base_capped, self.CAMBRICON_PATH), (bear_capped, bear_path)):
            # Bounded by crossing-year scale × terminal drift afterwards.
            year = cap_cumulative_growth_path(raw, self.TG)[1]
            crossing_scale = _cumulative_scale(raw[: year + 1])
            remaining = len(raw) - (year + 1)
            assert _cumulative_scale(capped) <= (
                crossing_scale * (1 + self.TG + 0.005) ** remaining + 1e-9
            )
            assert _cumulative_scale(capped) < 0.75 * _cumulative_scale(raw)


# ─────────────────────────────────────────────────────────────────
# AUDIT-A5: scenario probability renormalization
# ─────────────────────────────────────────────────────────────────

class TestRenormalizeScenarioProbabilities:
    def test_well_formed_weights_unchanged(self):
        probs = {"bear": 0.25, "base": 0.50, "bull": 0.25}
        out = renormalize_scenario_probabilities(probs)
        assert out == pytest.approx(probs)

    def test_dropped_bull_case_renormalized(self):
        # LLM returned only bear+base (normalized to 1.0 between them);
        # orchestrator default 0.25 survives for bull → sum 1.25.
        probs = {"bear": 0.40, "base": 0.60, "bull": 0.25}
        out = renormalize_scenario_probabilities(probs)
        assert sum(out.values()) == pytest.approx(1.0)
        # Relative ordering preserved.
        assert out["base"] > out["bear"] > out["bull"] - 1e-9

    def test_warns_when_drift_exceeds_one_percent(self):
        logs: list[str] = []
        renormalize_scenario_probabilities(
            {"bear": 0.40, "base": 0.60, "bull": 0.25}, log=logs.append,
        )
        assert any("renormalizing" in line for line in logs)

    def test_no_warning_within_tolerance(self):
        logs: list[str] = []
        renormalize_scenario_probabilities(
            {"bear": 0.25, "base": 0.50, "bull": 0.25}, log=logs.append,
        )
        assert logs == []

    def test_degenerate_total_falls_back_to_default(self):
        out = renormalize_scenario_probabilities({"bear": 0.0, "base": 0.0, "bull": 0.0})
        assert out == {"bear": 0.25, "base": 0.50, "bull": 0.25}

    def test_pw_value_uses_unit_weights(self):
        # End-to-end arithmetic: with the 1.25-sum weights, pw over
        # (50, 100, 200) used to inflate; renormalized it stays a convex
        # combination inside [bear, bull].
        probs = renormalize_scenario_probabilities(
            {"bear": 0.40, "base": 0.60, "bull": 0.25},
        )
        pw = probs["bear"] * 50 + probs["base"] * 100 + probs["bull"] * 200
        assert 50 <= pw <= 200


# ─────────────────────────────────────────────────────────────────
# AUDIT-A9: boundary-hit implied growth must not become an Observation
# ─────────────────────────────────────────────────────────────────

class TestImpliedGrowthUnreliableGate:
    def _inp(self, priced_in: dict):
        from aegis.core.agents.base import AgentInput
        return AgentInput(
            entity_id="TEST", run_id="r1", question_id="q1",
            macro_context={"priced_in": priced_in},
        )

    def test_variant_analyst_skips_unreliable_value(self):
        from aegis.core.agents.variant_analyst.agent import VariantAnalyst
        obs = VariantAnalyst()._extract_observations(self._inp(
            {"implied_revenue_growth": 0.50, "implied_growth_unreliable": True},
        ))
        assert not any("implied" in o.text.lower() for o in obs)

    def test_variant_analyst_keeps_reliable_value(self):
        from aegis.core.agents.variant_analyst.agent import VariantAnalyst
        obs = VariantAnalyst()._extract_observations(self._inp(
            {"implied_revenue_growth": 0.12, "implied_growth_unreliable": False},
        ))
        assert any("12.00%" in o.text for o in obs)

    def test_valuation_analyst_skips_unreliable_value(self):
        from aegis.core.agents.valuation_analyst.agent import ValuationAnalyst
        inp = self._inp(
            {"implied_revenue_growth": 0.50, "implied_growth_unreliable": True},
        )
        agent = ValuationAnalyst()
        obs = agent._extract_observations(inp)
        assert not any("implied revenue growth" in o.text.lower() for o in obs)
        # And the "aggressive" inference (fires on >25%) must not appear.
        infs = agent._derive_inferences(obs, inp)
        assert not any("aggressive" in i.text.lower() for i in infs)

    def test_valuation_analyst_keeps_reliable_value(self):
        from aegis.core.agents.valuation_analyst.agent import ValuationAnalyst
        obs = ValuationAnalyst()._extract_observations(self._inp(
            {"implied_revenue_growth": 0.30},
        ))
        assert any("30.00%" in o.text for o in obs)


# ─────────────────────────────────────────────────────────────────
# AUDIT-A10: clamp / growth-cap disclosure in the report dict
# ─────────────────────────────────────────────────────────────────

class TestScenarioClampDisclosure:
    def _report(self, scenarios: dict, meta_facts: dict | None = None):
        from aegis.core.reports.html_report_v2 import build_report_dict
        return build_report_dict(
            entity_id="600519",  # A-share → 中文 footnotes
            entity_name="测试公司",
            scenarios=scenarios,
            meta_facts=meta_facts or {"revenue": 1e9},
        )

    BASE_SC = {
        "currency": "CNY",
        "bear_value": 50.0, "base_value": 100.0, "bull_value": 200.0,
        "bear_probability": 0.25, "base_probability": 0.50, "bull_probability": 0.25,
        "bear_narrative": "悲观叙事", "base_narrative": "基准叙事", "bull_narrative": "乐观叙事",
    }

    def test_clamped_bear_gets_footnote_with_raw_value(self):
        sc = {**self.BASE_SC, "bear_clamped": True, "bear_raw_value": 12.34}
        report = self._report(sc)
        bear = next(c for c in report["scenarios"] if c["key"] == "bear")
        assert bear["clamped"] is True
        assert "夹逼" in bear["footnote"]
        assert "12.34" in bear["footnote"]
        # Footnote must actually reach the rendered narrative text.
        assert "夹逼" in bear["narrative"]
        assert bear["rawPx"] == 12.34

    def test_unclamped_cells_have_no_footnote(self):
        report = self._report(dict(self.BASE_SC))
        for cell in report["scenarios"]:
            assert cell["clamped"] is False
            assert "footnote" not in cell
            assert "夹逼" not in cell["narrative"]

    def test_growth_path_cap_footnote_in_dcf_block(self):
        report = self._report(
            dict(self.BASE_SC),
            meta_facts={"revenue": 1e9, "__growth_path_capped": True,
                        "__growth_path_capped_year": 6},
        )
        assert report["dcf"]["growthPathCapped"] is True
        assert report["dcf"]["growthPathCappedYear"] == 6
        assert "30×" in report["dcf"]["paragraphHtml"]
        assert "Y6" in report["dcf"]["paragraphHtml"]

    def test_no_growth_cap_no_footnote(self):
        report = self._report(dict(self.BASE_SC))
        assert "growthPathCapped" not in report["dcf"]
        assert "30×" not in report["dcf"]["paragraphHtml"]

    def test_none_sensitivity_cells_survive_rounding(self):
        # dcf-core handoff: infeasible two-way cells arrive as None and must
        # not crash `round()` in the renderer.
        sc = dict(self.BASE_SC)
        report_builder_input = {
            "variable_1": "wacc", "variable_2": "terminal_growth_rate",
            "var1_values": [0.08, 0.09], "var2_values": [0.02, 0.03],
            "matrix": [[100.0, None], [None, 80.0]],
        }
        from aegis.core.reports.html_report_v2 import build_report_dict
        report = build_report_dict(
            entity_id="600519", entity_name="测试公司",
            scenarios=sc, meta_facts={"revenue": 1e9},
            sensitivity_table=report_builder_input,
        )
        assert report["sensitivity"]["matrix"][0] == [100.0, None]
        assert report["sensitivity"]["matrix"][1] == [None, 80.0]
