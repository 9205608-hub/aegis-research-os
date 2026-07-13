"""Decision Engine — Section 21.

Responsibilities:
1. Aggregate observations and supported inferences
2. Explicitly list unresolved conflicts
3. Determine publishing status via Publish Gate
4. Generate standardized thesis object (with edge assessment)
5. Generate scenario matrix reference
6. Generate portfolio signal
7. Register monitorables and kill criteria

Principles:
- No pseudo-mathematical total score
- Only integrate validated and reviewed objects
- Unresolved conflicts must be explicitly exposed
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from aegis.data_contracts.critic_result_schema import CriticResult
from aegis.data_contracts.edge_assessment_schema import EdgeAssessment
from aegis.data_contracts.judgment_schema import JudgmentContract
from aegis.data_contracts.scenario_schema import ScenarioMatrix


@dataclass
class UnresolvedConflict:
    """A conflict between agent judgments that was not resolved."""

    topic: str
    conflicting_judgment_ids: list[str]
    description: str
    resolution_suggestion: str


# ── AUDIT 2026-07-12 (B3): evidence-gap detection ────────────────────────
# Grok's cross-cutting finding: "信息缺口的位置正好是 edge 声称的位置" —
# the thesis cites variables as established edge while its own
# open_questions admit they are unknown. Nothing in the pipeline checked
# this overlap. Deterministic CJK-bigram / latin-token overlap between the
# edge-bearing narrative fields and each open question; strong overlap on
# ≥ EVIDENCE_GAP_CONFLICT_THRESHOLD questions becomes an UnresolvedConflict
# → status "downgraded" → confidence capped medium (existing semantics).

_CJK_SEG_RE = re.compile(r"[一-鿿]+")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
# Generic bigrams that would fire on any thesis/question pair.
_STOP_BIGRAMS = frozenset({
    "我们", "市场", "公司", "是否", "没有", "可以", "进行", "相关",
    "当前", "一个", "这个", "对于", "如果", "以及", "还是", "什么",
    "具体", "多少", "如何", "怎样", "数据", "需要", "分析", "判断",
    "情况", "问题", "目前", "未来", "持续", "变化", "趋势", "水平",
})
# R4-2 (AUDIT 2026-07-12): sensitivity retuned against Grok round-3 ground
# truth — 002371 (downgraded, judged correct) vs 300750 (published with
# segment/overseas gaps, judged 发布级别过高). Old (4 shared, ≥2 hits)
# missed the CATL case; 3 shared + 1 hit catches it while unrelated
# questions still fail the 0.28 ratio bar.
EVIDENCE_GAP_MIN_SHARED = 3      # shared informative tokens per question
EVIDENCE_GAP_MIN_RATIO = 0.28    # …as a share of the question's tokens
EVIDENCE_GAP_CONFLICT_THRESHOLD = 1  # hit questions → unresolved conflict

# 合流 2026-07-13（Grok 仲裁蓝图 §5.3）：edge 主张字段清单收口成单一真源。
# 此前同一份清单在 engine.decide / 合成期镜像 / orchestrator 形态盖章三处
# 手抄，阈值与字段漂移风险由此产生。
EVIDENCE_GAP_CLAIM_FIELDS = (
    "core_thesis", "my_variant", "edge_source",
    "key_assumption_disagreement", "why_market_is_wrong",
)


def edge_claim_blob(src: Any) -> str:
    """拼接 edge 主张叙事字段（dict 或对象皆可），供 gap 检查消费。"""
    def _val(f: str) -> str:
        if isinstance(src, dict):
            return str(src.get(f) or "")
        return str(getattr(src, f, "") or "")
    return " ".join(_val(f) for f in EVIDENCE_GAP_CLAIM_FIELDS)


def _gap_tokens(text: str) -> set[str]:
    toks: set[str] = set()
    for seg in _CJK_SEG_RE.findall(text or ""):
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg not in _STOP_BIGRAMS:
                toks.add(bg)
    for w in _LATIN_TOKEN_RE.findall((text or "").lower()):
        toks.add(w)
    return toks


def evidence_gap_hits(
    edge_text_blob: str,
    open_questions: list[Any],
    min_shared: int = EVIDENCE_GAP_MIN_SHARED,
    min_ratio: float = EVIDENCE_GAP_MIN_RATIO,
) -> list[dict]:
    """Open questions whose subject matter overlaps the claimed edge.

    Returns one dict per hit: {"question", "shared" (sample), "ratio"}.
    """
    edge_toks = _gap_tokens(edge_text_blob)
    if not edge_toks:
        return []
    hits: list[dict] = []
    for q in open_questions or []:
        q_text = q.get("question") if isinstance(q, dict) else q
        q_text = str(q_text or "").strip()
        if not q_text:
            continue
        q_toks = _gap_tokens(q_text)
        if not q_toks:
            continue
        shared = q_toks & edge_toks
        ratio = len(shared) / len(q_toks)
        if len(shared) >= min_shared and ratio >= min_ratio:
            hits.append({
                "question": q_text,
                "shared": sorted(shared)[:8],
                "ratio": round(ratio, 2),
            })
    return hits


@dataclass
class ThesisDecision:
    """The decision engine's output — everything needed to construct a thesis."""

    entity_id: str
    run_id: str
    publishable: bool
    publishing_status: str  # "published", "blocked", "downgraded"

    # Aggregated content
    core_thesis: str
    my_variant: str
    variant_magnitude: str
    why_now: str
    market_implied_story: str
    counter_thesis: str
    key_assumption_disagreement: str

    # Edge
    edge_assessment: EdgeAssessment | None = None

    # Scenarios
    scenario_matrix_id: str = ""
    bear_case_value: float | None = None
    base_case_value: float | None = None
    bull_case_value: float | None = None
    scenario_narratives: dict[str, str] = field(default_factory=dict)
    scenario_probabilities: dict[str, float] = field(default_factory=dict)
    probability_weighted_value: float | None = None
    primary_swing_factor: str = ""

    # Risk
    kill_criteria: list[dict] = field(default_factory=list)
    monitorables: list[dict] = field(default_factory=list)
    fragility_points: list[str] = field(default_factory=list)
    # AUDIT 2026-07-12 (B1): agent falsification triggers under their honest
    # name. Previously these were only surfaced renamed as kill_criteria,
    # while the contract's disconfirming_triggers field got a one-line
    # "what_would_change_my_mind" — the label swap behind Grok's
    # "Kill 名实倒置" finding.
    disconfirming_triggers: list[str] = field(default_factory=list)

    # Quality
    unresolved_conflicts: list[UnresolvedConflict] = field(default_factory=list)
    critic_summary: dict[str, str] = field(default_factory=dict)
    confidence_bucket: str = "medium"
    bias_check_status: str = "passed"

    # Forecast bridge (DCF projections + assumptions)
    dcf_projections: list[dict] = field(default_factory=list)
    dcf_assumptions: dict = field(default_factory=dict)
    tv_pct: float | None = None
    sensitivity_rankings: list[dict] = field(default_factory=list)
    sensitivity_table: dict | None = None

    # Variant decomposition
    variant_decomposition: list[dict] = field(default_factory=list)

    # Open research questions from agent follow-ups
    open_questions: list[dict] = field(default_factory=list)

    # Metadata
    macro_dependency: str = ""
    sector_cycle_position: str = ""
    management_quality_summary: str = ""
    capital_allocation_assessment: str = ""


class DecisionEngine:
    """Aggregates agent judgments and critic reviews into a thesis decision.

    Section 21: no pseudo-math scores, only structured aggregation.
    """

    def decide(
        self,
        entity_id: str,
        run_id: str,
        judgments: list[JudgmentContract],
        critic_results: list[CriticResult],
        publish_gate_passed: bool,
        context: dict[str, Any] | None = None,
        synthesized_thesis: Any | None = None,
    ) -> ThesisDecision:
        ctx = context or {}

        # Detect unresolved conflicts
        conflicts = self._detect_conflicts(judgments)

        # AUDIT 2026-07-12 (B3): edge built on unclosed evidence is an
        # unresolved conflict. When the synthesized edge/variant narrative
        # substantially overlaps ≥N of its own open_questions, the thesis is
        # claiming as edge what it admits it does not know — it must not
        # publish clean ("结论跑在证据前面", Grok 20-audit ~全中).
        if synthesized_thesis is not None:
            _gap_hits = evidence_gap_hits(
                edge_claim_blob(synthesized_thesis),
                ctx.get("open_questions") or [])
            if len(_gap_hits) >= EVIDENCE_GAP_CONFLICT_THRESHOLD:
                _sample = _gap_hits[0]["question"][:60]
                conflicts.append(UnresolvedConflict(
                    topic="evidence_gap",
                    conflicting_judgment_ids=[],
                    description=(
                        f"edge/variant 引用的关键变量仍在 open_questions 中未闭合"
                        f"（{len(_gap_hits)} 处重叠，如「{_sample}…」）——"
                        "论点把自承未知的数据当已证实事实使用"
                    ),
                    resolution_suggestion=(
                        "补齐关键数据闭合 open_questions，或把 edge 降级为"
                        "待验证假设（幅度与置信同步下调）后再发布"
                    ),
                ))

        # Determine publishing status
        if not publish_gate_passed:
            status = "blocked"
        elif conflicts:
            status = "downgraded"
        else:
            status = "published"

        # Aggregate bias check
        bias_status = self._aggregate_bias_status(critic_results)

        # Use LLM-synthesized thesis if available, otherwise fall back to keyword extraction
        if synthesized_thesis is not None:
            summaries = self._from_synthesized_thesis(synthesized_thesis)
        else:
            summaries = self._extract_summaries(judgments)

        # Build edge assessment from context (or synthesized thesis)
        edge = ctx.get("edge_assessment")

        # If we have a synthesized thesis, upgrade edge assessment with LLM-generated content
        if synthesized_thesis is not None and edge is not None:
            # EdgeAssessment is a frozen model — use model_copy to update fields
            updates: dict = {}
            synth_fields = [
                ('why_market_is_wrong', 'why_market_is_wrong'),
                ('what_would_change_my_mind', 'what_would_change_my_mind'),
                ('edge_source', 'edge_source'),
                ('edge_durability', 'edge_durability'),
            ]
            for edge_field, synth_field in synth_fields:
                synth_val = getattr(synthesized_thesis, synth_field, None)
                if synth_val:
                    updates[edge_field] = synth_val
            if updates:
                if isinstance(edge, dict):
                    # dict-based edge from auto_research
                    for k, v in updates.items():
                        edge[k] = v
                else:
                    # Pydantic frozen model — must use model_copy
                    try:
                        edge = edge.model_copy(update=updates)
                    except Exception:
                        # Fallback: convert to dict, update, leave as dict
                        edge_dict = edge.model_dump() if hasattr(edge, 'model_dump') else dict(edge)
                        edge_dict.update(updates)
                        edge = edge_dict

        # Scenarios from context
        scenarios = ctx.get("scenarios", {})

        # Build critic summary
        critic_summary = {}
        for cr in critic_results:
            critic_summary[cr.critic_type] = (
                f"{'BLOCK' if cr.block_publish else 'PASS'} "
                f"({len(cr.issues)} issues, risk={cr.overall_risk})"
            )

        return ThesisDecision(
            entity_id=entity_id,
            run_id=run_id,
            publishable=publish_gate_passed and not any(
                c for c in conflicts if c.topic == "critical"
            ),
            publishing_status=status,
            core_thesis=summaries.get("core_thesis", ""),
            my_variant=summaries.get("variant", ""),
            variant_magnitude=summaries.get("variant_magnitude", ""),
            why_now=summaries.get("why_now", ""),
            market_implied_story=summaries.get("market_implied", ""),
            counter_thesis=summaries.get("counter_thesis", ""),
            key_assumption_disagreement=summaries.get("key_disagreement", ""),
            edge_assessment=edge,
            scenario_matrix_id=scenarios.get("matrix_id", ""),
            bear_case_value=scenarios.get("bear_value"),
            base_case_value=scenarios.get("base_value"),
            bull_case_value=scenarios.get("bull_value"),
            scenario_narratives={
                "bear": scenarios.get("bear_narrative", ""),
                "base": scenarios.get("base_narrative", ""),
                "bull": scenarios.get("bull_narrative", ""),
            },
            scenario_probabilities={
                "bear": scenarios.get("bear_probability", 0.25),
                "base": scenarios.get("base_probability", 0.50),
                "bull": scenarios.get("bull_probability", 0.25),
            },
            probability_weighted_value=scenarios.get("probability_weighted_value"),
            primary_swing_factor=scenarios.get("primary_swing_factor", ""),
            kill_criteria=(_kills := self._extract_kill_criteria(
                judgments, synthesized_thesis)),
            monitorables=self._extract_monitorables(judgments),
            fragility_points=self._extract_fragility_points(judgments),
            # R3-2: 跨清单调和——与 kill 同主题的触发器不再在 disconfirm
            # 里以另一套阈值重复出现（Grok round-2："Kill/触发器多阈值互相
            # 打架"）。kill 是量化定稿，disconfirm 保留其余观察方向。
            disconfirming_triggers=self._reconcile_disconfirm_with_kills(
                self._extract_disconfirming_triggers(judgments), _kills),
            unresolved_conflicts=conflicts,
            critic_summary=critic_summary,
            confidence_bucket=self._determine_confidence(
                judgments, critic_results,
                publishing_status=status,
                publish_gate_passed=publish_gate_passed,
                gate_skipped_count=len(ctx.get("gate_skipped_names") or []),
            ),
            bias_check_status=bias_status,
            dcf_projections=ctx.get("dcf_projections_base", []),
            dcf_assumptions=ctx.get("dcf_assumptions", {}),
            tv_pct=ctx.get("tv_pct"),
            sensitivity_rankings=ctx.get("sensitivity_rankings", []),
            sensitivity_table=ctx.get("sensitivity_table"),
            variant_decomposition=ctx.get("variant_decomposition", []),
            open_questions=ctx.get("open_questions", []),
            macro_dependency=ctx.get("macro_dependency", ""),
            sector_cycle_position=ctx.get("sector_cycle_position", ""),
            management_quality_summary=summaries.get("management", ""),
            capital_allocation_assessment=summaries.get("capital_allocation", ""),
        )

    def _from_synthesized_thesis(self, st: Any) -> dict[str, str]:
        """Extract summaries from LLM-synthesized thesis (replaces keyword matching)."""
        return {
            "core_thesis": st.core_thesis,
            "variant": st.my_variant,
            "variant_magnitude": st.variant_magnitude,
            "why_now": st.why_now,
            "market_implied": st.market_implied_story,
            "counter_thesis": st.counter_thesis,
            "key_disagreement": st.key_assumption_disagreement,
            "management": st.management_quality_summary,
            "capital_allocation": st.capital_allocation_assessment,
        }

    def _detect_conflicts(
        self, judgments: list[JudgmentContract]
    ) -> list[UnresolvedConflict]:
        """Detect contradictory inferences across agents.

        The previous implementation used naive bag-of-words matching: if an
        inference contained ANY topic keyword AND ANY sentiment keyword, it
        was labeled pos or neg on that topic. This produced false conflicts:
          - accounting_analyst: "if export controls force a greater revenue
            mix to [the] upside, margins compress" — labeled POS-growth
            because 'growth' + 'upside' both appear, but the inference is
            actually describing a downside risk.
          - One agent saying "historical growth was strong" + another saying
            "future growth will decelerate" — not a conflict, these are
            describing different time periods.

        New logic:
        1. Proximity: sentiment word must appear within 40 chars of the
           topic keyword (same clause / short phrase). This prevents "upside"
           in one clause from being glued to "growth" in another.
        2. Negation skip: if "not", "no", "never", or "without" appears
           within 10 chars before the sentiment word, ignore this inference.
        3. Noise floor: require ≥ 2 agents on each side before calling it a
           conflict. A single contrarian is not an "unresolved conflict",
           it's a minority view.
        """
        import re
        conflicts = []

        # BUG-Y47 (2026-05-06): keyword extraction was EN-only (matched
        # NVIDIA's English narratives but never CN runs). When synthesizer
        # fails for an A-share entity, this fallback path determined the
        # decision engine's contradiction detection — silently skipped.
        # Add 中文 equivalents for topics, pos/neg sentiment, and negation
        # prefixes. Topic IDs stay English so downstream rendering doesn't
        # change.
        positive: dict[str, set[str]] = {}
        negative: dict[str, set[str]] = {}
        topics_en = {
            "moat": ["moat", "competitive advantage", "pricing power"],
            "earnings_quality": ["earnings quality", "accrual quality"],
            "growth": ["growth", "accelerat", "decelerat"],
            "leverage": ["leverage", "balance sheet", "debt load"],
        }
        topics_zh = {
            "moat": ["护城河", "竞争优势", "定价权", "壁垒"],
            "earnings_quality": ["盈利质量", "应计", "现金流转化", "利润含金量"],
            "growth": ["增长", "增速", "扩张", "提速", "放缓", "降速"],
            "leverage": ["杠杆", "负债", "资产负债表", "偿债", "净负债"],
        }
        pos_en = ("strong", "durable", "sound", "robust", "healthy", "net cash")
        pos_zh = ("强劲", "稳健", "出色", "优秀", "坚挺", "韧性", "净现金", "稳固")
        neg_en = ("weak", "poor", "below par", "elevated risk", "overleveraged")
        neg_zh = ("弱", "差", "不足", "脆弱", "薄弱", "高杠杆", "过度举债")
        neg_prefixes_en = ("not ", "no ", "never ", "without ", "isn't ",
                           "is not ", "won't ", "cannot ")
        neg_prefixes_zh = ("不", "无", "并非", "并未", "并无", "毫无",
                            "尚未", "没有", "缺乏", "缺少")

        def _label(text: str, keyword: str, sentiment_en: tuple,
                   sentiment_zh: tuple) -> bool:
            """True if any sentiment word appears within 40 chars of keyword,
            and is not preceded by a negation prefix within 10 chars."""
            for km in re.finditer(re.escape(keyword), text):
                kstart = km.start()
                window_start = max(0, kstart - 40)
                window_end = min(len(text), kstart + len(keyword) + 40)
                window = text[window_start:window_end]
                # English sentiment (text is already lowered)
                for sw in sentiment_en:
                    sm = window.find(sw)
                    if sm == -1:
                        continue
                    neg_win = window[max(0, sm - 10):sm]
                    if any(p in neg_win for p in neg_prefixes_en):
                        continue
                    return True
                # Chinese sentiment (also in lowered text — CJK is case-invariant
                # so .lower() doesn't break Chinese matching)
                for sw in sentiment_zh:
                    sm = window.find(sw)
                    if sm == -1:
                        continue
                    neg_win = window[max(0, sm - 6):sm]  # CJK 1-2 chars
                    if any(p in neg_win for p in neg_prefixes_zh):
                        continue
                    return True
            return False

        for j in judgments:
            for inf in j.inferences:
                text = inf.text.lower()
                # Iterate combined English + Chinese topic keys per topic
                for topic in topics_en:
                    keywords = list(topics_en[topic]) + list(topics_zh[topic])
                    for kw in keywords:
                        if kw not in text:
                            continue
                        if _label(text, kw, pos_en, pos_zh):
                            positive.setdefault(topic, set()).add(j.agent_name)
                            break
                        if _label(text, kw, neg_en, neg_zh):
                            negative.setdefault(topic, set()).add(j.agent_name)
                            break

        # Noise floor: require ≥ 2 distinct agents on each side
        for topic in set(positive) & set(negative):
            pos_agents = positive[topic]
            neg_agents = negative[topic]
            if len(pos_agents) < 2 or len(neg_agents) < 2:
                continue
            all_agents = list(pos_agents | neg_agents)
            conflicts.append(UnresolvedConflict(
                topic=topic,
                conflicting_judgment_ids=all_agents,
                description=(
                    f"Conflicting signals on '{topic}': "
                    f"{len(pos_agents)} positive, {len(neg_agents)} negative"
                ),
                resolution_suggestion="Review which agents' analyses are better supported by evidence",
            ))

        return conflicts

    def _aggregate_bias_status(self, critic_results: list[CriticResult]) -> str:
        for cr in critic_results:
            if cr.critic_type == "cognitive_bias_critic":
                if cr.block_publish:
                    return "blocked"
                if cr.overall_risk in ("medium", "high"):
                    return "warned"
        return "passed"

    def _extract_summaries(self, judgments: list[JudgmentContract]) -> dict[str, str]:
        summaries: dict[str, str] = {}
        for j in judgments:
            if j.agent_name == "variant_analyst":
                for inf in j.inferences:
                    if "variant" in inf.text.lower():
                        summaries["variant"] = inf.text
                    if "upside" in inf.text.lower() or "downside" in inf.text.lower():
                        summaries["variant_magnitude"] = inf.text
            elif j.agent_name == "valuation_analyst":
                for inf in j.inferences:
                    if "market" in inf.text.lower() and "implies" in inf.text.lower():
                        summaries["market_implied"] = inf.text
            elif j.agent_name == "business_analyst":
                for inf in j.inferences:
                    if "moat" in inf.text.lower() or "business" in inf.text.lower():
                        summaries["core_thesis"] = inf.text
            elif j.agent_name == "management_analyst":
                for inf in j.inferences:
                    if "capital allocation" in inf.text.lower():
                        summaries["management"] = inf.text
                        summaries["capital_allocation"] = inf.text
            elif j.agent_name == "risk_analyst":
                counterargs = [ca.text for ca in j.counterarguments]
                if counterargs:
                    summaries["counter_thesis"] = counterargs[0]
        return summaries

    # AUDIT 2026-07-12 (B1): a kill criterion must carry an executable,
    # quantified threshold. Matches either a comparator phrase followed by a
    # number ("低于80%", "跌破¥100亿", "falls below 0.6") or a duration
    # pattern ("连续两个季度"). Bare qualitative triggers ("增长放缓") never
    # qualify — they stay as disconfirming triggers / monitorables.
    _QUANT_THRESHOLD_RE = re.compile(
        r"(?:低于|高于|超过|超出|跌破|突破|不足|少于|大于|小于|升至|降至|回落至|达到|"
        r"下降超|上升超|环比[升降]|同比[升降]|"
        r"below|above|under|over|exceeds?|falls?\s+below|drops?\s+below|[<>≥≤])"
        r"[^。;；，,]{0,20}?"
        r"\d+(?:[.,]\d+)?\s*(?:%|pp|个百分点|亿|万|元|倍|天|日|周|bp|bps|x|×)?"
        r"|连续\s*[两三四五六0-9]+\s*[个]?\s*(?:季度|月|年|周)"
        r"|\d+(?:[.,]\d+)?\s*(?:%|pp|个百分点|亿元?|万元?|倍|bp|bps)\s*(?:以[上下内]|或以[上下])"
    )

    # Binary observable events are executable kills even without a number —
    # the event either happened or it didn't (credit downgrade, covenant
    # breach, regulatory investigation…). Confirmation-style signals ("获得
    # 订单/认证") deliberately do NOT appear here.
    _BINARY_EVENT_RE = re.compile(
        r"评级下调|展望转负|违约|豁免|退市|立案|调查|处罚|吊销|召回|"
        r"流失|终止合作|资产冻结|停产|断供|减值|商誉爆雷|"
        r"downgrade|covenant\s+breach|waiver|investigation|delisting|"
        r"recall|suspension|impairment",
        re.IGNORECASE,
    )

    def _extract_kill_criteria(
        self,
        judgments: list[JudgmentContract],
        synthesized_thesis: Any = None,
    ) -> list[dict]:
        """AUDIT 2026-07-12 (B1 + R2-2): thesis-direction-aware kills.

        Primary source: the SYNTHESIZER's own structured kill_criteria — it
        knows the final thesis stance, so its kills carry the right polarity
        (Grok round-1 caught the deeper bug: risk-analyst disconfirming
        triggers falsify the RISK view, which for a bullish thesis makes
        them thesis-POSITIVE confirmations — "Kill 写成多头确认").
        Every candidate still passes the executability check: a quantified
        threshold or a binary observable event.

        Fallback (no/invalid synthesizer kills): the quantified/binary
        subset of the risk analyst's triggers (B1 behavior). Non-qualifying
        triggers remain in disconfirming_triggers / monitorables.
        """
        st_kills: list[dict] = []
        for item in (getattr(synthesized_thesis, "kill_criteria", None) or []):
            if not isinstance(item, dict):
                continue
            desc = str(item.get("description") or "").strip()
            if not desc:
                continue
            probe = f"{desc} {item.get('threshold') or ''}"
            m = self._QUANT_THRESHOLD_RE.search(probe)
            ev = None if m else self._BINARY_EVENT_RE.search(probe)
            if not (m or ev):
                continue
            threshold = str(item.get("threshold") or "").strip()
            if not threshold:
                threshold = m.group(0).strip() if m else f"事件触发：{ev.group(0)}"
            st_kills.append({
                "description": desc,
                "threshold": threshold,
                "check_frequency": str(item.get("check_frequency") or "quarterly"),
            })
        if st_kills:
            # R4-4：kill 内部一致性——同指标去重（第一条胜出=synthesizer
            # 排序即优先级）+ 检查频率与阈值期限匹配（"全年营收低于X"配
            # quarterly 是 Grok R3 点名的"年度 Kill 配季度频率"缺陷）。
            deduped: list[dict] = []
            seen_toks: list[set[str]] = []
            for k in st_kills:
                toks = _gap_tokens(k["description"])
                dup = any(
                    toks and ktoks
                    and (
                        len(toks & ktoks) / (len(toks | ktoks) or 1) >= 0.4
                        or len(toks & ktoks) / (min(len(toks), len(ktoks)) or 1) >= 0.55
                    )
                    for ktoks in seen_toks
                )
                if dup:
                    continue
                seen_toks.append(toks)
                blob = f"{k['description']} {k['threshold']}"
                if re.search(r"年报|全年|年度|financial year|annual", blob) and \
                        k.get("check_frequency") == "quarterly":
                    k = dict(k, check_frequency="annually")
                elif re.search(r"半年报|中报|H1|上半年", blob) and \
                        k.get("check_frequency") == "quarterly":
                    k = dict(k, check_frequency="semiannually")
                deduped.append(k)
            return deduped[:6]

        criteria = []
        for j in judgments:
            if j.agent_name != "risk_analyst":
                continue
            for dt in j.disconfirming_triggers:
                text = dt.text or ""
                m = self._QUANT_THRESHOLD_RE.search(text)
                if m:
                    threshold = m.group(0).strip()
                else:
                    ev = self._BINARY_EVENT_RE.search(text)
                    if not ev:
                        continue
                    threshold = f"事件触发：{ev.group(0)}"
                criteria.append({
                    "description": text,
                    "threshold": threshold,
                    "check_frequency": dt.check_frequency,
                })
        return criteria

    # R2-3: 7 agents × ~5 triggers produced a 27-35 entry "未去重的 agent
    # 垃圾场" (Grok round-1) with conflicting thresholds for the same metric.
    # Near-duplicates are clustered by token overlap; the quantified variant
    # wins its cluster; hard cap keeps the list an investment-committee
    # artifact rather than a dump.
    _DISCONFIRM_CAP = 12

    def _extract_disconfirming_triggers(
        self, judgments: list[JudgmentContract],
    ) -> list[str]:
        """AUDIT 2026-07-12 (B1 + R2-3): all agents' falsification triggers
        under their honest name — clustered, threshold-reconciled (quantified
        variant preferred), capped at ``_DISCONFIRM_CAP``."""
        clusters: list[tuple[set[str], str, bool]] = []  # (tokens, text, quantified)
        for j in judgments:
            for dt in j.disconfirming_triggers:
                text = (dt.text or "").strip()
                if not text:
                    continue
                toks = _gap_tokens(text)
                quant = bool(self._QUANT_THRESHOLD_RE.search(text))
                matched = False
                for i, (ctoks, ctext, cquant) in enumerate(clusters):
                    if not toks or not ctoks:
                        if text.lower() == ctext.lower():
                            matched = True
                            break
                        continue
                    inter = len(toks & ctoks)
                    # 宁并勿滥：同指标多套阈值正是 Grok 判"不可执行"的点，
                    # 轻度过合并（量化版本存活）好于三套阈值并存。
                    if (
                        inter / (len(toks | ctoks) or 1) >= 0.4
                        or inter / (min(len(toks), len(ctoks)) or 1) >= 0.55
                    ):
                        matched = True
                        # Threshold reconciliation: quantified beats vague;
                        # among equals keep the incumbent (first agent wins).
                        if quant and not cquant:
                            clusters[i] = (toks, text, True)
                        break
                if not matched:
                    clusters.append((toks, text, quant))
        return [text for _, text, _ in clusters[: self._DISCONFIRM_CAP]]

    def _reconcile_disconfirm_with_kills(
        self, triggers: list[str], kills: list[dict],
    ) -> list[str]:
        """R3-2: drop disconfirming triggers that duplicate a kill's subject.

        The kill list carries the single reconciled threshold per metric;
        a same-subject trigger with a different cutoff surviving in
        disconfirming_triggers recreates the "同一指标多套阈值" defect."""
        kill_tok_sets = [
            _gap_tokens(f"{k.get('description', '')} {k.get('threshold', '')}")
            for k in kills or []
        ]
        if not kill_tok_sets:
            return triggers
        out = []
        for text in triggers:
            toks = _gap_tokens(text)
            dup = False
            for ktoks in kill_tok_sets:
                if not toks or not ktoks:
                    continue
                inter = len(toks & ktoks)
                if (
                    inter / (len(toks | ktoks) or 1) >= 0.4
                    or inter / (min(len(toks), len(ktoks)) or 1) >= 0.55
                ):
                    dup = True
                    break
            if not dup:
                out.append(text)
        return out or triggers[:1]

    def _extract_monitorables(self, judgments: list[JudgmentContract]) -> list[dict]:
        monitorables = []
        seen_descriptions: set[str] = set()
        for j in judgments:
            for dt in j.disconfirming_triggers:
                if dt.monitorable:
                    desc_key = dt.text.strip().lower()
                    if desc_key not in seen_descriptions:
                        seen_descriptions.add(desc_key)
                        monitorables.append({
                            "description": dt.text,
                            "check_frequency": dt.check_frequency,
                            "source_agent": j.agent_name,
                        })
        return monitorables

    def _extract_fragility_points(self, judgments: list[JudgmentContract]) -> list[str]:
        points = []
        for j in judgments:
            for unc in j.self_reported_uncertainties:
                points.append(unc)
        return points[:10]  # Cap at 10

    def _determine_confidence(
        self,
        judgments: list[JudgmentContract],
        critic_results: list[CriticResult],
        publishing_status: str = "published",
        publish_gate_passed: bool = True,
        gate_skipped_count: int = 0,
    ) -> str:
        """Determine confidence bucket based on evidence and critic results.

        Uses a scoring system instead of binary block/pass:
        - Blocks reduce confidence but don't hard-floor to very_low
        - Evidence strength can offset moderate issues
        - Only extreme block counts force very_low

        BUG-37: confidence now takes publishing_status into account. A
        "downgraded" or "blocked" run cannot emit `high` confidence because
        the combination "I am highly confident in a thesis the system
        decided not to publish" is internally contradictory.
        """
        total_warns = sum(
            sum(1 for i in cr.issues if i.severity == "warn")
            for cr in critic_results
        )
        total_blocks = sum(
            sum(1 for i in cr.issues if i.severity == "block")
            for cr in critic_results
        )

        # Evidence and narrative strength
        total_evidence = sum(len(j.used_evidence_ids) for j in judgments)
        total_observations = sum(len(j.observations) for j in judgments)
        has_narratives = sum(1 for j in judgments if getattr(j, 'narrative_supplement', None))

        # Scoring: start at 70, adjust based on quality signals.
        # Block penalty is moderate (2pts) because many blocks come from
        # mock fallback observations lacking source_ids — these are systemic
        # false positives, not analytical failures.
        score = 70
        score -= total_blocks * 2        # Each block costs 2 points
        score -= total_warns * 0.5       # Each warn costs 0.5 points
        score += min(total_evidence, 20) * 1   # Evidence adds up to 20 points
        score += min(total_observations, 60) * 0.3  # Observations add up to 18
        score += has_narratives * 5      # Each narrative adds 5 points (high quality signal)

        if score >= 80:
            bucket = "high"
        elif score >= 60:
            bucket = "medium"
        elif score >= 40:
            bucket = "low"
        else:
            bucket = "very_low"

        # BUG-37: cap confidence based on publishing status.
        # - blocked  → cap at "low" (pipeline refused to publish)
        # - downgraded → cap at "medium" (pipeline flagged unresolved conflicts)
        # - published → no cap
        # Also: if publish_gate itself failed, the decision is structurally
        # unreliable regardless of score.
        order = ["very_low", "low", "medium", "high"]
        def cap(cur: str, ceiling: str) -> str:
            return cur if order.index(cur) <= order.index(ceiling) else ceiling

        if not publish_gate_passed or publishing_status == "blocked":
            bucket = cap(bucket, "low")
        elif publishing_status == "downgraded":
            bucket = cap(bucket, "medium")

        # AUDIT 2026-07-12 (B4): integrity gates that SKIPPED for missing
        # inputs are not passes. "published + high" while DCF artifacts /
        # sensitivity tables were absent is exactly the 新易盛/沈飞 failure
        # Grok called 发布门槛失守 — missing data caps confidence at medium.
        if gate_skipped_count > 0:
            bucket = cap(bucket, "medium")

        return bucket
