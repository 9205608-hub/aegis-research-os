"""数字清洗器三个已确认盲区的回归测试（2026-08-01）。

HANDOFF 2026-08-01 晚条目"遗留/下一步"第 5 条——Editor front_page_numbers
接线时如实报告的三个清洗器盲区：

盲区 1  BPS/EPS/DPS 类真实 per-share 值被误杀："每股净资产 ¥12.50" 的
        上下文命中 fair-value 关键词、金额不匹配任何情景值 → 被当编造
        目标价剔除。修法：per_share_sanctioned_values 从 meta_facts
        确定性派生白名单（±10% 容差，比情景 ±15% 更紧），strict 票同样
        生效（真实披露数据不受估值失配连坐）。

盲区 2  无方向裸 % 在 strict 票不设防："隐含 30% 的重估" 无已注册方向
        关键词，绕过 _has_dir 闸门。修法（保守，防误杀优先）：strict
        票下 40 字符窗口命中估值语境词且无运营/增长语境词、不在白名单
        → 清洗；常态（非 strict）票行为零改变。

盲区 3  logic_critic 中文净利率 % claim 缺失 + EN 绝对额单一类型：
        "分部净利率45%" 无 claim 类型；EN 绝对额只识别 operating income，
        "Cloud net profit of $50B" 完全绕过 ceiling。修法：新增 nm claim
        类型（归母净利 ceiling，meta_facts 缺失保守跳过）；EN 绝对额与
        zh 统一按最近利润关键词归类 oi/ni/gp。
"""

from unittest.mock import MagicMock

import pytest

from aegis.core.chief_analyst.report_editor import (
    ReportEditor,
    _scrub_front_page_numbers,
)
from aegis.core.chief_analyst.thesis_synthesizer import (
    _scrub_fair_value_claims,
    per_share_sanctioned_values,
)
from aegis.core.critics.logic_critic.critic import LogicCritic
from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    Inference,
    JudgmentContract,
    Observation,
)

MKT = {"current_price": 349.0}

# 正常口径：三档围绕市价 ±50% 内（同 test_front_page_scrub.SANE_SCEN）
SANE_SCEN = {
    "currency": "CNY",
    "bear_value": 250.0,
    "base_value": 380.0,
    "bull_value": 520.0,
    "probability_weighted_value": 390.0,
}

# 估值失配（strict 票）：三档全部在市价 8-15× 之外
MISMATCH_SCEN = {
    "currency": "CNY",
    "bear_value": 2800.0,
    "base_value": 4000.0,
    "bull_value": 5200.0,
    "probability_weighted_value": 4100.0,
}

# 派生基准：BPS = 125亿/10亿股 = 12.50；EPS = 23.5亿/10亿股 = 2.35
META_PS = {
    "total_equity": 125e8,
    "shares_outstanding": 10e8,
    "net_income": 23.5e8,
}


def _scrub(text, scenarios=SANE_SCEN, **kw):
    out, warns = _scrub_fair_value_claims(
        {"core_thesis": text}, dict(scenarios), dict(MKT), **kw
    )
    return out["core_thesis"], warns


# ═══ 盲区 1：真实 per-share 值白名单 ═══════════════════════════════════


class TestPerShareSanctionedValues:
    """meta_facts → BPS/EPS/DPS 派生（字段名依据 fact_bridge 实测）。"""

    def test_bps_eps_derived(self):
        vals = per_share_sanctioned_values(META_PS)
        assert any(abs(v - 12.5) < 1e-9 for v in vals)   # BPS
        assert any(abs(v - 2.35) < 1e-9 for v in vals)   # EPS

    def test_diluted_eps_and_dps_and_disclosed_eps(self):
        meta = dict(META_PS)
        meta.update({
            "diluted_shares": 10.5e8,
            "dividends_paid": -5e8,     # 现金流口径负号 → 取绝对值
            "eps_basic": 2.36,          # filing 直通值
        })
        vals = per_share_sanctioned_values(meta)
        assert any(abs(v - 23.5e8 / 10.5e8) < 1e-6 for v in vals)  # 稀释EPS
        assert any(abs(v - 0.5) < 1e-9 for v in vals)              # DPS
        assert any(abs(v - 2.36) < 1e-9 for v in vals)             # 直通EPS

    def test_missing_facts_yield_empty(self):
        # 宁缺毋滥：派生不出就传空，绝不猜测
        assert per_share_sanctioned_values({}) == []
        assert per_share_sanctioned_values(None) == []

    def test_no_shares_no_ratio_derivation(self):
        # 无股本 → BPS/EPS 不派生；披露 EPS 直通值仍可用
        vals = per_share_sanctioned_values(
            {"total_equity": 125e8, "net_income": 23.5e8, "eps_basic": 2.35}
        )
        assert vals == [2.35]


class TestRealPerShareSurvives:
    """真实 BPS/EPS 引用存活；无白名单时被误杀（锁定盲区本体）。"""

    def test_bps_killed_without_whitelist(self):
        # 盲区本体：每股+估值语境命中 fair-value 关键词 → 被当编造剔除
        text, warns = _scrub("每股净资产 ¥12.50，为估值提供底部支撑")
        assert "¥12.50" not in text
        assert any("VALUATION CONSISTENCY" in w for w in warns)

    def test_bps_survives_with_whitelist(self):
        text, warns = _scrub(
            "每股净资产 ¥12.50，为估值提供底部支撑",
            extra_sanctioned_per_share=per_share_sanctioned_values(META_PS),
        )
        assert "¥12.50" in text
        assert warns == []

    def test_eps_survives_with_whitelist(self):
        text, warns = _scrub(
            "全年EPS ¥2.35，对应当前估值并不昂贵",
            extra_sanctioned_per_share=[12.5, 2.35],
        )
        assert "¥2.35" in text
        assert warns == []

    def test_tolerance_absorbs_share_count_口径差(self):
        # 稀释/加权平均股本口径差：¥2.42 vs 派生 2.35（+3%）→ ±10% 容差内
        text, _ = _scrub(
            "每股收益 ¥2.42，估值合理",
            extra_sanctioned_per_share=[2.35],
        )
        assert "¥2.42" in text

    def test_outside_tolerance_still_scrubbed(self):
        # ¥14.50 vs BPS 12.50 = +16% > ±10% → 仍视为编造
        text, _ = _scrub(
            "每股价值应达 ¥14.50",
            extra_sanctioned_per_share=[12.5],
        )
        assert "¥14.50" not in text

    def test_fabricated_target_still_scrubbed_with_whitelist(self):
        # 白名单不放行编造目标价：¥25 距 BPS/EPS/情景值/市价全都不沾边
        text, warns = _scrub(
            "合理估值应达 ¥25.00/股",
            extra_sanctioned_per_share=[12.5, 2.35],
        )
        assert "¥25.00" not in text
        assert any("VALUATION CONSISTENCY" in w for w in warns)

    def test_bps_survives_strict_ticket(self):
        # strict 票 sanctioned 情景集清空，但真实披露派生值不连坐
        text, _ = _scrub(
            "每股净资产 ¥12.50，为估值提供底部支撑",
            scenarios=MISMATCH_SCEN,
            strict=True,
            extra_sanctioned_per_share=[12.5],
        )
        assert "¥12.50" in text

    def test_front_page_bps_entry_survives(self):
        entry = {
            "label": "每股净资产", "value": "¥12.50",
            "context": "每股净资产¥12.50，估值底部支撑",
        }
        # 无白名单 → 整条剔除（value 违规不留空壳）
        kept, _ = _scrub_front_page_numbers([dict(entry)], SANE_SCEN, MKT)
        assert kept == []
        # 白名单透传 → 存活
        kept, warns = _scrub_front_page_numbers(
            [dict(entry)], SANE_SCEN, MKT,
            extra_sanctioned_per_share=[12.5],
        )
        assert len(kept) == 1
        assert kept[0]["value"] == "¥12.50"
        assert warns == []


class TestEditorWiringPerShare:
    """ReportEditor.edit() 全链路：meta_facts 派生白名单接进 try 块。"""

    def _edit(self, meta_facts):
        e = ReportEditor()
        e._llm = MagicMock()
        e._llm.call_structured.return_value = {
            "headline": "标题",
            "front_page_numbers": [{
                "label": "每股净资产", "value": "¥12.50",
                "context": "每股净资产¥12.50，估值底部支撑",
            }],
        }
        return e.edit(
            entity_name="测试公司",
            synthesized_thesis=MagicMock(unresolved_tensions=[]),
            directive=None,
            computed_metrics={}, market_data=dict(MKT),
            scenarios=dict(SANE_SCEN),
            meta_facts=meta_facts, segment_detail=None,
        )

    def test_bps_entry_survives_end_to_end(self):
        edited = self._edit(dict(META_PS))
        assert len(edited.front_page_numbers) == 1
        assert edited.front_page_numbers[0]["value"] == "¥12.50"

    def test_without_meta_facts_still_dropped(self):
        # 对照组：派生不出白名单时行为不变（接线未放松原有防线）
        edited = self._edit({})
        assert edited.front_page_numbers == []


# ═══ 盲区 2：strict 票无方向裸 % 设防 ══════════════════════════════════


class TestStrictBarePctGuard:
    BARE = "现价隐含30%的重估机会，条件详见正文"

    def test_bare_valuation_pct_scrubbed_in_strict(self):
        # "重估"单独不在方向关键词表 → 原先绕过 _has_dir 闸门
        text, warns = _scrub(self.BARE, scenarios=MISMATCH_SCEN, strict=True)
        assert "30%" not in text
        assert "〔估值失配·幅度结论已停用〕" in text
        assert any("% RETURN CONSISTENCY" in w for w in warns)

    def test_normal_ticket_zero_change(self):
        # 常态票行为零改变：同一文本非 strict → 原样保留
        text, warns = _scrub(self.BARE, scenarios=SANE_SCEN, strict=False)
        assert text == self.BARE
        assert warns == []

    def test_operating_context_survives_strict(self):
        # 估值词（重估/空间）与运营词（市占率）同窗 → 保留（防误杀优先）
        src = "重估空间取决于市占率30%能否守住"
        text, warns = _scrub(src, scenarios=MISMATCH_SCEN, strict=True)
        assert text == src
        assert warns == []

    @pytest.mark.parametrize("src", [
        "毛利率30%保持稳定",           # 运营语境词
        "产能利用率30%仍在爬坡",       # 运营语境词
        "营收增速30%超出预期",         # 增长语境（Y21 豁免对齐）
    ])
    def test_operating_metrics_untouched_in_strict(self, src):
        text, warns = _scrub(src, scenarios=MISMATCH_SCEN, strict=True)
        assert text == src
        assert warns == []

    def test_whitelisted_pct_survives_strict(self):
        # 红线 9：白名单（如前沿隐含增速）照常豁免
        text, _ = _scrub(
            self.BARE, scenarios=MISMATCH_SCEN, strict=True,
            extra_sanctioned_pcts=[30.0],
        )
        assert "30%" in text

    def test_directional_pct_still_scrubbed_in_strict(self):
        # 既有行为回归：方向性 % 在 strict 票仍被清洗
        text, _ = _scrub(
            "较现价存在70%的下行空间", scenarios=MISMATCH_SCEN, strict=True,
        )
        assert "70%" not in text

    def test_no_valuation_context_untouched_in_strict(self):
        # 无估值语境的裸 %（概率/覆盖率类）不清洗
        src = "经营现金流对净利润的覆盖为68%"
        text, warns = _scrub(src, scenarios=MISMATCH_SCEN, strict=True)
        assert text == src
        assert warns == []


# ═══ 盲区 3：logic_critic 净利率 claim + EN 绝对额扩展 ═══════════════


def _judgment(jid: str, obs: list[str]) -> JudgmentContract:
    """最小合法判断：观察有 source、推理有 grounding（同 test_critics_zh）。"""
    return JudgmentContract(
        judgment_id=jid,
        agent_name="test_agent",
        agent_version="v1_test",
        question_id="q_test",
        run_id="run_test",
        judgment_status="complete",
        observations=[
            Observation(text=t, source_ids=["fact:test"]) for t in obs
        ],
        inferences=[
            Inference(
                text="综合观察，结论中性。",
                confidence="medium",
                based_on_observation_indices=[0],
            )
        ],
        cognitive_bias_self_check=CognitiveBiasSelfCheck(
            anchoring_risk="low",
            confirmation_bias_risk="low",
            recency_bias_risk="low",
            narrative_fallacy_risk="low",
        ),
    )


def _seg_issues(text: str, ctx) -> list:
    res = LogicCritic().review([_judgment("j_bs", [text])], ctx)
    return [i for i in res.issues if i.issue_code.startswith("LOGIC_SEGMENT")]


class TestLogicCriticNetMarginZh:
    """中文净利率/归母净利率 % claim vs 归母净利 ceiling。"""

    def _ctx(self, with_ni: bool = True):
        meta = {
            "operating_income": 30e8,
            "gross_profit": 45e8,
            "__display": {"currency": "CNY", "symbol": "¥"},
        }
        if with_ni:
            meta["net_income"] = 20e8   # 归母口径（fact_bridge Step 2b）
        return {
            "meta_facts": meta,
            "segment_detail": {"product": {"云端产品线": {"revenue": 60e8}}},
        }

    def test_net_margin_impossible_blocked(self):
        # 45% × 60亿 = 27亿 > 归母净利 20亿 × 1.05
        hits = _seg_issues("云端产品线净利率45%，盈利含金量高。", self._ctx())
        assert [i.issue_code for i in hits] == [
            "LOGIC_SEGMENT_NET_MARGIN_IMPOSSIBLE"
        ]
        assert hits[0].severity == "block"

    def test_guimu_net_margin_form_blocked(self):
        hits = _seg_issues("云端产品线归母净利率45%创新高。", self._ctx())
        assert [i.issue_code for i in hits] == [
            "LOGIC_SEGMENT_NET_MARGIN_IMPOSSIBLE"
        ]

    def test_net_margin_within_ceiling_not_flagged(self):
        # 30% × 60亿 = 18亿 < 21亿 → 不误报
        assert _seg_issues("云端产品线净利率30%。", self._ctx()) == []

    def test_conservative_skip_without_net_income(self):
        # meta_facts 缺归母净利 → 保守跳过（净利润不受营业利润上界约束）
        assert _seg_issues(
            "云端产品线净利率45%。", self._ctx(with_ni=False)
        ) == []


class TestLogicCriticEnAbsoluteExpansion:
    """EN 绝对额路径：net profit / gross profit 的 $X B 表述纳入。"""

    def _ctx(self, with_ni: bool = True):
        meta = {
            "operating_income": 30e9,
            "gross_profit": 45e9,
            "__display": {"currency": "USD", "symbol": "$"},
        }
        if with_ni:
            meta["net_income"] = 20e9
        return {
            "meta_facts": meta,
            "segment_detail": {"product": {"cloud": {"revenue": 60e9}}},
        }

    def test_en_abs_net_profit_blocked(self):
        hits = _seg_issues(
            "Cloud segment net profit of $50B dwarfs peers.", self._ctx()
        )
        assert [i.issue_code for i in hits] == [
            "LOGIC_SEGMENT_ABS_NI_IMPOSSIBLE"
        ]
        assert hits[0].severity == "block"

    def test_en_abs_gross_profit_blocked(self):
        # $50B > 毛利 $45B × 1.05
        hits = _seg_issues(
            "Cloud gross profit of $50 billion, per our estimate.", self._ctx()
        )
        assert [i.issue_code for i in hits] == [
            "LOGIC_SEGMENT_ABS_GP_IMPOSSIBLE"
        ]

    def test_en_abs_operating_income_regression(self):
        # 既有行为回归：EN operating income 绝对额仍命中
        hits = _seg_issues(
            "Cloud operating income of $50B, best in class.", self._ctx()
        )
        assert [i.issue_code for i in hits] == [
            "LOGIC_SEGMENT_ABS_OI_IMPOSSIBLE"
        ]

    def test_en_abs_net_profit_within_ceiling(self):
        assert _seg_issues(
            "Cloud segment net profit of $18B.", self._ctx()
        ) == []

    def test_en_conservative_skip_without_net_income(self):
        assert _seg_issues(
            "Cloud segment net profit of $50B.", self._ctx(with_ni=False)
        ) == []

    def test_en_net_margin_pct_blocked(self):
        # EN 净利率 % 路径与 zh 对称：45% × $60B = $27B > $21B
        hits = _seg_issues(
            "Cloud segment net margin of 45%, best in class.", self._ctx()
        )
        assert [i.issue_code for i in hits] == [
            "LOGIC_SEGMENT_NET_MARGIN_IMPOSSIBLE"
        ]


# ═══════════════════════════════════════════════════════════════════
# 审计处方一 2（2026-08-28）：占位符文案按触发原因分档 + 相对估值表
# 倍数白名单（300502 对抗性审计回归锁）
# ═══════════════════════════════════════════════════════════════════

from aegis.core.chief_analyst.thesis_synthesizer import (  # noqa: E402
    relative_valuation_sanctioned_multiples,
)


class TestStrictTagByReason:
    """strict 占位符不许对读者撒谎：估值失配字样只许失配触发时用。"""

    RAW = {"core_thesis": "重估空间约 45%，安全边际充足。"}

    def test_evidence_gap_strict_uses_neutral_tag(self):
        # 300502 实锤：mismatch=False 的证据缺口票，占位符却写「估值失配」
        out, _ = _scrub_fair_value_claims(
            dict(self.RAW), SANE_SCEN, MKT,
            strict=True, strict_reason="evidence_gap",
        )
        assert "〔未经核准的数字已略去〕" in out["core_thesis"]
        assert "估值失配" not in out["core_thesis"]

    def test_mismatch_strict_keeps_mismatch_tag(self):
        out, _ = _scrub_fair_value_claims(
            dict(self.RAW), MISMATCH_SCEN, MKT,
            strict=True, strict_reason="mismatch",
        )
        assert "〔估值失配·幅度结论已停用〕" in out["core_thesis"]

    def test_default_reason_is_mismatch_for_editor_compat(self):
        # report_editor 的 strict 只由失配触发（不传 strict_reason），
        # 默认值必须保持失配文案——否则 editor 路径文案回归。
        out, _ = _scrub_fair_value_claims(
            dict(self.RAW), MISMATCH_SCEN, MKT, strict=True,
        )
        assert "〔估值失配·幅度结论已停用〕" in out["core_thesis"]

    def test_non_strict_tag_unchanged(self):
        raw = {"core_thesis": "下行空间 81-89%，风险显著。"}
        out, _ = _scrub_fair_value_claims(dict(raw), SANE_SCEN, MKT)
        assert "〔回报口径详见DCF情景〕" in out["core_thesis"]
        assert "估值失配" not in out["core_thesis"]


class TestStrictMultipleWhitelist:
    """相对估值表公开渲染的倍数不得一处展示一处删除（红线 9 同则）。"""

    def test_relval_multiples_survive_strict(self):
        raw = {"core_thesis": (
            "TTM市盈率43.5倍处于同业第20分位（同业中位数114.1倍），"
            "估值折价明显。"
        )}
        out, _ = _scrub_fair_value_claims(
            raw, MISMATCH_SCEN, MKT, strict=True,
            extra_sanctioned_multiples=[43.5, 114.1],
        )
        assert "43.5" in out["core_thesis"]
        assert "114.1" in out["core_thesis"]

    def test_unsanctioned_multiple_still_scrubbed(self):
        raw = {"core_thesis": "PE 从22倍到30倍的重估空间清晰可见。"}
        out, _ = _scrub_fair_value_claims(
            raw, MISMATCH_SCEN, MKT, strict=True,
            extra_sanctioned_multiples=[43.5, 114.1],
        )
        assert "22倍" not in out["core_thesis"] or "30倍" not in out["core_thesis"]

    def test_whitelist_tolerance_matches_rendering_rounding(self):
        # 表渲染四舍五入到 1 位小数（43.5），叙事可能引 43.47/43.5 —— 两者都豁免
        raw = {"core_thesis": "估值锚：市盈率43.47倍，重估空间有限。"}
        out, _ = _scrub_fair_value_claims(
            raw, MISMATCH_SCEN, MKT, strict=True,
            extra_sanctioned_multiples=[43.5],
        )
        assert "43.47" in out["core_thesis"]


class TestRelvalMultiplesHelper:

    def test_rounding_follows_table_rendering(self):
        relval = {
            "insufficient_peers": False,
            "target_pe_ttm": 43.47886709, "peer_pe_median": 114.06818949,
            "target_pb": 23.47597917, "peer_pb_median": 15.849179,
        }
        assert relative_valuation_sanctioned_multiples(relval) == [
            15.85, 23.48, 43.5, 114.1,
        ]

    def test_insufficient_peers_gate(self):
        assert relative_valuation_sanctioned_multiples(
            {"insufficient_peers": True, "target_pe_ttm": 43.5}) == []
        assert relative_valuation_sanctioned_multiples(None) == []
        assert relative_valuation_sanctioned_multiples({}) == []
