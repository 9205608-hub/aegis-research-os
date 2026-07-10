"""Chinese-narrative regression tests for critic keyword paths (AUDIT E4).

BUG-Y41..Y48 fixed seven separate "dead code for CN" bugs in the critics,
but until now no test fed actual Chinese narrative text through the keyword
matchers — a refactor could silently regress the whole A-share critic
pipeline back to a false-negative/false-positive state. This file is the
first batch of regression protection, covering the 2026-07 audit fixes:

- narrative_fact_critic: 万亿 unit parsing (AUDIT-C3, was 10,000× low),
  为/达到/约 connectors without a leading space, 复合增长率 CAGR aliases
- logic_critic: contradiction detection not fooled by 差异化/减弱幅度有限,
  segment ceiling check not blocking real 营业收入 (=revenue) citations
- thesis_synthesizer % scrub: 估值回归 classified as UPSIDE (longest-match
  beats the "回归" downside substring), 调整后 excluded, real downside fires
- numeric_consistency_critic: 即/减 implicit Chinese equations
"""

import pytest

from aegis.core.chief_analyst.thesis_synthesizer import (
    _direction_of,
    _scrub_fair_value_claims,
)
from aegis.core.critics.logic_critic.critic import LogicCritic
from aegis.core.critics.narrative_fact_critic.critic import (
    NarrativeFactCritic,
    _parse_dollar,
)
from aegis.core.critics.numeric_consistency_critic.critic import (
    NumericConsistencyCritic,
)
from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    Inference,
    JudgmentContract,
    Observation,
)


CNY_DISPLAY = {"currency": "CNY", "symbol": "¥"}


def _judgment(
    jid: str,
    obs: list[str],
    infs: list[str] | None = None,
    agent_name: str = "test_agent",
) -> JudgmentContract:
    """Minimal well-formed judgment: every obs sourced, every inf grounded."""
    infs = infs if infs is not None else ["综合观察，结论中性。"]
    return JudgmentContract(
        judgment_id=jid,
        agent_name=agent_name,
        agent_version="v1_test",
        question_id="q_test",
        run_id="run_test",
        judgment_status="complete",
        observations=[
            Observation(text=t, source_ids=["fact:test"]) for t in obs
        ],
        inferences=[
            Inference(text=t, confidence="medium", based_on_observation_indices=[0])
            for t in infs
        ],
        cognitive_bias_self_check=CognitiveBiasSelfCheck(
            anchoring_risk="low",
            confirmation_bias_risk="low",
            recency_bias_risk="low",
            narrative_fallacy_risk="low",
        ),
    )


# ── narrative_fact_critic: 万亿 unit parsing (AUDIT-C3) ──────────────


class TestParseDollarUnits:
    """AUDIT-C3: '万亿' ends in '亿' — tail-search matched the 1e8 entry
    first and parsed 2.4万亿 as 2.4e8 (10,000× low)."""

    @pytest.mark.parametrize("num,unit,expected", [
        ("2.4", "万亿", 2.4e12),   # the C3 bug: was 2.4e8
        ("2.4", "亿", 2.4e8),      # must not regress
        ("2.4", "万", 2.4e4),      # must not regress
        ("416.2", "B", 416.2e9),
        ("5", "billion", 5e9),
        ("120", "M", 120e6),
    ])
    def test_unit_suffixes(self, num, unit, expected):
        assert _parse_dollar(num, unit) == pytest.approx(expected)


class TestNarrativeFactTrillion:
    """End-to-end: trillion-CNY company cited correctly must NOT warn."""

    def _ctx(self):
        return {
            "meta_facts": {
                "revenue": 2.4e12,
                "__historical_revenue": {2024: 2.4e12},
                "__display": dict(CNY_DISPLAY),
            },
            "computed_metrics": {},
            "market_data": {},
        }

    def test_correct_trillion_citation_not_flagged(self):
        j = _judgment(
            "j_zh_001",
            ["公司FY2024 营收 2.4万亿，总营收2.4万亿创历史新高。"],
        )
        res = NarrativeFactCritic().review([j], self._ctx())
        bad = [i for i in res.issues
               if i.issue_code in ("METRIC_MISMATCH", "FY_REVENUE_MISMATCH")]
        assert bad == [], [i.message for i in bad]

    def test_wrong_trillion_citation_flagged(self):
        # 1.2万亿 vs actual 2.4万亿 — detection must work at 1e12 scale
        j = _judgment("j_zh_002", ["总营收1.2万亿，规模领先。"])
        res = NarrativeFactCritic().review([j], self._ctx())
        assert any(i.issue_code == "METRIC_MISMATCH" for i in res.issues)


class TestNarrativeFactCnConnectors:
    """AUDIT (narrative:140): connector group required \\s+ before 为/达到/约,
    so the most common CN phrasings never matched — dead check."""

    def _ctx(self):
        return {
            "meta_facts": {
                "revenue": 65e8,
                "net_income": 10.5e8,
                "__display": dict(CNY_DISPLAY),
            },
            "computed_metrics": {},
            "market_data": {},
        }

    @pytest.mark.parametrize("text", [
        "总营收为80.0亿元，超出预期。",
        "总营收达到80.0亿。",
        "总营收约80.0亿。",
        "总营收约为80.0亿。",
        "净利润为15.0亿。",
        "净利润达到15.0亿。",
    ])
    def test_connector_mismatch_fires(self, text):
        # Cited values are ~23-43% off ground truth → must warn.
        j = _judgment("j_zh_010", [text])
        res = NarrativeFactCritic().review([j], self._ctx())
        assert any(i.issue_code == "METRIC_MISMATCH" for i in res.issues), text

    def test_connector_correct_values_pass(self):
        j = _judgment(
            "j_zh_011",
            ["总营收为65.0亿元，净利润达到10.5亿。"],
        )
        res = NarrativeFactCritic().review([j], self._ctx())
        assert [i for i in res.issues if i.issue_code == "METRIC_MISMATCH"] == []


class TestNarrativeFactCnCagr:
    """AUDIT (narrative:140): _CAGR_PATTERN only knew the literal 'CAGR' —
    复合增长率 / 年均复合增长率 / 复合增速 aliases were invisible."""

    def _ctx(self):
        # FY2021 100亿 → FY2024 195.3125亿: exact 3-year CAGR = 25.0%
        return {
            "meta_facts": {
                "__historical_revenue": {2021: 1.0e10, 2024: 1.953125e10},
                "__display": dict(CNY_DISPLAY),
            },
            "computed_metrics": {},
            "market_data": {},
        }

    @pytest.mark.parametrize("text", [
        "过去3年复合增长率为10.0%。",
        "3年年均复合增长率10.0%。",
        "3年营收复合增速约10.0%。",
        "3年CAGR为10.0%。",
    ])
    def test_cagr_alias_mismatch_blocks(self, text):
        # Claimed 10% vs actual 25% → block-level CAGR window mismatch.
        j = _judgment("j_zh_020", [text])
        res = NarrativeFactCritic().review([j], self._ctx())
        hits = [i for i in res.issues if i.issue_code == "CAGR_WINDOW_MISMATCH"]
        assert hits, text
        assert hits[0].severity == "block"

    def test_cagr_alias_correct_value_passes(self):
        j = _judgment("j_zh_021", ["过去3年复合增长率为25.0%，增长稳健。"])
        res = NarrativeFactCritic().review([j], self._ctx())
        assert [i for i in res.issues if i.issue_code == "CAGR_WINDOW_MISMATCH"] == []


# ── logic_critic: CN contradiction + segment ceiling ─────────────────


class TestLogicCriticCnContradiction:
    """AUDIT (logic:202): single-char negatives 弱/差 substring-matched
    neutral words (差异化 / 减弱幅度有限) → false LOGIC_CONTRADICTION."""

    @pytest.mark.parametrize("neutral_text", [
        "估值溢价来自护城河的差异化定位。",
        "行业逆风下护城河减弱幅度有限。",
        "护城河宽度的测算存在一定误差。",
    ])
    def test_neutral_zh_words_no_false_contradiction(self, neutral_text):
        j1 = _judgment("j_zh_101", ["数据来源于年报。"],
                       ["公司护城河强劲，定价权突出。"])
        j2 = _judgment("j_zh_102", ["数据来源于年报。"], [neutral_text])
        res = LogicCritic().review([j1, j2], {})
        assert [i for i in res.issues if i.issue_code == "LOGIC_CONTRADICTION"] == [], \
            neutral_text

    def test_real_zh_contradiction_still_fires(self):
        j1 = _judgment("j_zh_103", ["数据来源于年报。"],
                       ["公司护城河强劲，定价权突出。"])
        j2 = _judgment("j_zh_104", ["数据来源于年报。"],
                       ["护城河持续恶化，竞争壁垒被侵蚀。"])
        res = LogicCritic().review([j1, j2], {})
        assert any(i.issue_code == "LOGIC_CONTRADICTION" for i in res.issues)


class TestLogicCriticCnSegment:
    """AUDIT (logic:338): '营业收入' is revenue under CAS, not operating
    profit — real segment-revenue citations were mis-blocked as
    'mathematically impossible' operating income."""

    def _ctx(self):
        return {
            "meta_facts": {
                "operating_income": 30e8,
                "gross_profit": 45e8,
                "__display": dict(CNY_DISPLAY),
            },
            "segment_detail": {
                "product": {"云端产品线": {"revenue": 60e8}},
            },
        }

    def test_segment_revenue_citation_not_blocked(self):
        # Real disclosed segment revenue (60亿 > consolidated OI 30亿 is
        # perfectly normal — margins are <100%). Must NOT block.
        j = _judgment("j_zh_110", ["云端产品线营业收入¥60.0亿，同比增长45%。"])
        res = LogicCritic().review([j], self._ctx())
        seg = [i for i in res.issues if i.issue_code.startswith("LOGIC_SEGMENT")]
        assert seg == [], [i.message for i in seg]

    def test_fabricated_segment_oi_still_blocked(self):
        # Fabricated segment OI 45亿 > consolidated 30亿 → impossible.
        j = _judgment("j_zh_111", ["云端产品线经营利润高达¥45.0亿，盈利能力突出。"])
        res = LogicCritic().review([j], self._ctx())
        hits = [i for i in res.issues
                if i.issue_code == "LOGIC_SEGMENT_ABS_OI_IMPOSSIBLE"]
        assert hits
        assert hits[0].severity == "block"


# ── thesis_synthesizer: % return scrub direction keywords ────────────


class TestThesisScrubDirectionZh:
    """AUDIT (synthesizer:106/:112): '回归'(down) ⊂ '估值回归'(up) with
    downside priority → upside re-rating claims signed negative → false
    '% RETURN CONSISTENCY OVERRIDE' warnings appended to the report."""

    def test_direction_longest_match(self):
        assert _direction_of("盈利驱动估值回归，隐含") == 1   # 估值回归 beats 回归
        assert _direction_of("股价面临回归风险，") == -1      # bare 回归 still down
        assert _direction_of("估值修复空间打开，") == 1
        assert _direction_of("存在明显下行空间，") == -1
        assert _direction_of("调整后EBITDA利润率") == 0       # 调整后 excluded
        assert _direction_of("估值面临调整压力，") == -1      # bare 调整 still down
        assert _direction_of("下行 81-89% vs 上行仅") == 0    # tie → undecidable

    def test_upside_rerating_pct_no_false_warning(self):
        # 茅台-like: price 100, scenarios → sanctioned returns +33/+165/+431.
        # "估值回归…35%" must be read as +35 (≈ +33) → no warning.
        scen = {"bear_value": 133.0, "base_value": 265.0,
                "bull_value": 531.0, "currency": "CNY"}
        out, warns = _scrub_fair_value_claims(
            {"core_thesis": "盈利驱动估值回归，隐含35%空间。"},
            scen,
            {"current_price": 100.0},
            fields=("core_thesis",),
        )
        assert warns == [], warns

    def test_bogus_downside_pct_still_scrubbed(self):
        # Sanctioned returns -33/-10/+10; claimed 下行80% diverges >10pt →
        # the downside keyword path must still classify and warn.
        scen = {"bear_value": 67.0, "base_value": 90.0,
                "bull_value": 110.0, "currency": "CNY"}
        out, warns = _scrub_fair_value_claims(
            {"core_thesis": "模型显示下行空间约80%，风险敞口大。"},
            scen,
            {"current_price": 100.0},
            fields=("core_thesis",),
        )
        assert warns and "RETURN CONSISTENCY" in warns[0]

    def test_adjusted_ebitda_margin_not_scrubbed(self):
        # "调整后EBITDA利润率18%" is a margin metric, not a return claim.
        scen = {"bear_value": 133.0, "base_value": 265.0,
                "bull_value": 531.0, "currency": "CNY"}
        out, warns = _scrub_fair_value_claims(
            {"core_thesis": "调整后EBITDA利润率18%，同比改善。"},
            scen,
            {"current_price": 100.0},
            fields=("core_thesis",),
        )
        assert warns == [], warns


# ── numeric_consistency_critic: CN implicit equations ────────────────


class TestNumericCriticZh:
    """AUDIT (numeric:147-171): equations phrased with Chinese copulas
    (即/为/是) and word operators (减/加) bypassed the literal-'=' regex."""

    def test_cn_implicit_equation_broken_fires(self):
        # 75 - 15 = 60 ≠ 47 → warn, even without a literal '='.
        j = _judgment("j_zh_201", ["净负债47亿元，即总债务75亿减现金15亿。"])
        res = NumericConsistencyCritic().review([j])
        assert any(i.issue_code == "NUMERIC_BROKEN_EQUATION" for i in res.issues)

    def test_cn_implicit_equation_consistent_passes(self):
        j = _judgment("j_zh_202", ["净负债60亿元，即总债务75亿减现金15亿。"])
        res = NumericConsistencyCritic().review([j])
        assert res.issues == []

    def test_cn_range_is_not_an_equation(self):
        # "为100亿-120亿" is a range — hyphen operators stay '='-only.
        j = _judgment("j_zh_203", ["目标市值为100亿-120亿区间。"])
        res = NumericConsistencyCritic().review([j])
        assert res.issues == []

    def test_literal_eq_form_still_fires(self):
        j = _judgment("j_zh_204", ["净负债 47 亿 = 总债务 75 亿 − 现金 15 亿"])
        res = NumericConsistencyCritic().review([j])
        assert any(i.issue_code == "NUMERIC_BROKEN_EQUATION" for i in res.issues)


class TestFairValueContextGate:
    """AUDIT follow-up (2026-07-10, 康达新材 LLM run): bare currency figures
    in ratio/illustration prose must not be scrubbed as fair-value claims,
    and the rewrite tag must be Chinese in CN reports."""

    SCEN = {"bear_value": 1.07, "base_value": 2.15,
            "bull_value": 4.30, "currency": "CNY"}
    MD = {"current_price": 13.76}

    def test_cash_burn_ratio_not_scrubbed(self):
        # The exact Kangda sentence that got rewritten into mid-sentence
        # garbage: ¥10 is CFO/NI≈-9.56x illustration, not a value claim.
        text = "换言之，公司每确认¥1账面利润，实际要烧掉近¥10现金。"
        out, warns = _scrub_fair_value_claims(
            {"executive_summary": text}, self.SCEN, self.MD,
            fields=("executive_summary",),
        )
        assert out["executive_summary"] == text
        assert warns == [], warns

    def test_rogue_target_price_still_scrubbed_with_zh_tag(self):
        # A genuine off-scenario fair-value claim near valuation context
        # must still be caught — and rewritten with the Chinese tag.
        text = "我们认为合理估值应达¥10，对应显著上行。"
        out, warns = _scrub_fair_value_claims(
            {"executive_summary": text}, self.SCEN, self.MD,
            fields=("executive_summary",),
        )
        assert "〔详见DCF情景估值〕" in out["executive_summary"]
        assert "¥10" not in out["executive_summary"]
        assert warns and "VALUATION CONSISTENCY" in warns[0]

    def test_english_rogue_claim_keeps_english_tag(self):
        scen = {"bear_value": 60.0, "base_value": 90.0,
                "bull_value": 120.0, "currency": "USD"}
        text = "We believe fair value is $200 per share."
        out, warns = _scrub_fair_value_claims(
            {"executive_summary": text}, scen, {"current_price": 100.0},
            fields=("executive_summary",),
        )
        assert "[see DCF scenarios]" in out["executive_summary"]
        assert warns and "VALUATION CONSISTENCY" in warns[0]

    def test_scenario_matching_value_near_context_not_scrubbed(self):
        # Sanctioned base 2.15 quoted with valuation context → legitimate.
        text = "DCF基准情景对应每股价值¥2.15，较现价存在明显落差。"
        out, warns = _scrub_fair_value_claims(
            {"executive_summary": text}, self.SCEN, self.MD,
            fields=("executive_summary",),
        )
        assert out["executive_summary"] == text
        assert warns == [], warns
