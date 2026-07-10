"""Logic Critic — Section 20.1.

Checks for:
- Inferences without observation support
- Circular reasoning
- Contradictory inferences across judgments
- Double counting (SBC + dilution, revenue + GMV, etc.)
"""

from __future__ import annotations

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult, Remediation
from aegis.data_contracts.judgment_schema import JudgmentContract


class LogicCritic(CriticBase):
    """Reviews judgments for logical consistency."""

    CRITIC_TYPE = "logic_critic"

    # Known double-counting pairs: if both appear in a single judgment, flag
    DOUBLE_COUNT_PAIRS = [
        ({"sbc_to_revenue"}, {"dilution_rate"},
         "SBC expense deduction AND dilution adjustment applied — potential double counting"),
    ]

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        issues: list[CriticIssue] = []

        for j in judgments:
            issues.extend(self._check_inference_grounding(j))
            issues.extend(self._check_double_counting(j, context))
            issues.extend(self._check_observation_source(j))

        # Cross-judgment contradiction check
        issues.extend(self._check_cross_judgment_contradictions(judgments))

        # BUG-47: segment-margin consistency — reject LLM-fabricated per-segment
        # operating margins that are mathematically incompatible with the
        # consolidated operating income.
        issues.extend(self._check_segment_margin_consistency(judgments, context))

        return CriticResult(
            critic_id=f"critic_logic_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=self._any_block(issues),
            overall_risk=self._overall_risk(issues),
        )

    def _check_inference_grounding(self, j: JudgmentContract) -> list[CriticIssue]:
        issues = []
        obs_count = len(j.observations)
        for i, inf in enumerate(j.inferences):
            # Check out-of-bounds references
            for idx in inf.based_on_observation_indices:
                if idx < 0 or idx >= obs_count:
                    issues.append(self._make_issue(
                        code="LOGIC_UNGROUNDED_INFERENCE",
                        severity="block",
                        message=f"Inference[{i}] references observation index {idx} "
                                f"which does not exist (only {obs_count} observations)",
                        judgment_ids=[j.judgment_id],
                        action="Fix observation reference or add missing observation",
                        remediation=Remediation(
                            steps=[
                                f"In agent '{j.agent_name}', fix inference[{i}].based_on_observation_indices "
                                f"to reference valid indices (0 to {obs_count - 1})",
                                "Alternatively, add the missing observation to the agent's output",
                                "Re-run the agent with corrected observation references",
                            ],
                            target_component=j.agent_name,
                            target_field="inferences[].based_on_observation_indices",
                            auto_fixable=False,
                            rerun_required=True,
                        ),
                    ))

            # Check if inference has any grounding
            if not inf.based_on_observation_indices:
                issues.append(self._make_issue(
                    code="LOGIC_UNGROUNDED_INFERENCE",
                    severity="block",
                    message=f"Inference[{i}] has no observation references — "
                            "inference must be grounded in observations",
                    judgment_ids=[j.judgment_id],
                    action="Link inference to at least one observation",
                    remediation=Remediation(
                        steps=[
                            f"In agent '{j.agent_name}', add based_on_observation_indices "
                            f"to inference[{i}]: '{inf.text[:80]}...'",
                            "Each inference must reference at least one observation by index",
                            "If no existing observation supports it, add one with proper source_ids",
                        ],
                        target_component=j.agent_name,
                        target_field=f"inferences[{i}].based_on_observation_indices",
                        auto_fixable=False,
                        rerun_required=True,
                    ),
                ))
        return issues

    def _check_double_counting(self, j: JudgmentContract, ctx: dict | None = None) -> list[CriticIssue]:
        issues = []
        used_metrics = set(j.used_metric_ids)

        # If orchestrator explicitly set a safe SBC treatment mode, skip
        # this check entirely — engine-level guarantee supersedes metric usage.
        c = ctx or {}
        sbc_mode = c.get("sbc_treatment", "")
        if sbc_mode in ("dilution_only", "expense_in_fcf"):
            return issues

        for set_a, set_b, message in self.DOUBLE_COUNT_PAIRS:
            if set_a & used_metrics and set_b & used_metrics:
                # Check if the judgment text explicitly acknowledges the double-count risk
                all_text = " ".join(inf.text for inf in j.inferences)
                acknowledged = "double" in all_text.lower() or "double-count" in all_text.lower()
                remediation = None if acknowledged else Remediation(
                    steps=[
                        "Set DCFInput.sbc_treatment to 'expense_in_fcf' (deduct SBC from FCFF, "
                        "use basic shares) OR 'dilution_only' (no SBC deduction, use diluted shares)",
                        "Remove conflicting metric from used_metric_ids",
                        "Re-run DCF engine and valuation analyst with corrected treatment",
                    ],
                    target_component="dcf_engine",
                    target_field="sbc_treatment",
                    auto_fixable=True,
                    rerun_required=True,
                )
                issues.append(self._make_issue(
                    code="LOGIC_DOUBLE_COUNTING",
                    severity="warn" if acknowledged else "block",
                    message=message,
                    judgment_ids=[j.judgment_id],
                    action="Use EITHER SBC deduction OR diluted shares, not both",
                    remediation=remediation,
                ))
        return issues

    def _check_observation_source(self, j: JudgmentContract) -> list[CriticIssue]:
        issues = []
        for i, obs in enumerate(j.observations):
            if not obs.source_ids:
                issues.append(self._make_issue(
                    code="LOGIC_UNSOURCED_OBSERVATION",
                    severity="block",
                    message=f"Observation[{i}] has no source_ids — "
                            "observations must trace to facts or evidence",
                    judgment_ids=[j.judgment_id],
                    action="Add source_ids tracing to facts or evidence packets",
                    remediation=Remediation(
                        steps=[
                            f"In agent '{j.agent_name}', add source_ids to observation[{i}]",
                            "Source IDs should reference fact IDs (e.g., 'fact:revenue:entity:FY2024') "
                            "or evidence IDs (e.g., 'ev_entity_xxxx')",
                            "Every observation must be traceable to an input data point",
                        ],
                        target_component=j.agent_name,
                        target_field=f"observations[{i}].source_ids",
                        auto_fixable=False,
                        rerun_required=True,
                    ),
                ))
        return issues

    def _check_cross_judgment_contradictions(
        self, judgments: list[JudgmentContract]
    ) -> list[CriticIssue]:
        """Detect potential contradictions between judgments.

        Simple heuristic: if one judgment says "strong" and another says "weak"
        about the same topic, flag for review.
        """
        issues = []
        # Group inferences by topic keywords
        positive_signals: dict[str, list[str]] = {}
        negative_signals: dict[str, list[str]] = {}

        # BUG-Y46 (2026-05-06): keyword sets were EN-only. Chinese
        # narratives use 护城河 / 竞争优势 / 定价权 / 盈利质量 / 应计 /
        # 资本配置 / 管理层 — none of which matched, so cross-agent
        # contradictions on these topics never fired in CN runs. Now both
        # alphabets are recognised. Topic keys are stable English IDs so
        # downstream message rendering doesn't change.
        topic_keywords = {
            "moat": ["moat", "competitive advantage", "pricing power",
                     "护城河", "竞争优势", "定价权", "壁垒"],
            "earnings_quality": ["earnings quality", "accrual",
                                  "盈利质量", "应计", "现金流转化"],
            "management": ["capital allocation", "management",
                           "资本配置", "管理层", "管理团队", "战略纪律"],
        }
        EN_POS = ("strong", "durable", "sound", "excellent", "robust", "resilient")
        ZH_POS = ("强劲", "稳健", "出色", "优秀", "坚挺", "韧性", "卓越")
        EN_NEG = ("weak", "poor", "below par", "deteriorat", "fragile")
        # AUDIT (2026-07, logic_critic:202): Y46's single-char negatives
        # 弱/差 and the quantifier 不足 substring-matched neutral phrasing
        # ("差异化定位", "减弱幅度有限", "误差", "市占率不足5%") and fired
        # false LOGIC_CONTRADICTION warns. Single-char CJK words are too
        # ambiguous — multi-char unambiguous phrases only (same lesson as
        # the Y47 decision_engine negation window).
        ZH_NEG = ("疲弱", "走弱", "偏弱", "转弱", "疲软", "较差", "变差",
                  "恶化", "脆弱", "薄弱", "不及预期", "低于预期", "堪忧", "乏力")

        for j in judgments:
            for inf in j.inferences:
                text_lower = inf.text.lower()
                text_orig = inf.text
                for topic, keywords in topic_keywords.items():
                    matches_topic = any(
                        (kw in text_lower if kw.isascii() else kw in text_orig)
                        for kw in keywords
                    )
                    if matches_topic:
                        is_pos = (
                            any(w in text_lower for w in EN_POS)
                            or any(w in text_orig for w in ZH_POS)
                        )
                        is_neg = (
                            any(w in text_lower for w in EN_NEG)
                            or any(w in text_orig for w in ZH_NEG)
                        )
                        if is_pos and not is_neg:
                            positive_signals.setdefault(topic, []).append(j.judgment_id)
                        elif is_neg and not is_pos:
                            negative_signals.setdefault(topic, []).append(j.judgment_id)

        for topic in set(positive_signals) & set(negative_signals):
            all_ids = list(set(positive_signals[topic] + negative_signals[topic]))
            issues.append(self._make_issue(
                code="LOGIC_CONTRADICTION",
                severity="warn",
                message=f"Contradictory signals on '{topic}' — "
                        "positive and negative assessments across judgments",
                judgment_ids=all_ids,
                action="Reconcile conflicting assessments or explain why both are valid",
            ))

        return issues

    def _check_segment_margin_consistency(
        self,
        judgments: list[JudgmentContract],
        context: dict | None,
    ) -> list[CriticIssue]:
        """BUG-47: flag LLM-fabricated per-segment operating margins that
        are mathematically incompatible with the consolidated operating
        income ceiling.

        Strategy: scan each judgment's observations and narrative for text
        patterns like 'Google Services 62%' or 'Services operating margin
        of 62%'. Extract (segment_name, margin_pct) pairs. For each pair,
        look up the segment revenue from context['segment_detail'] and
        compute implied operating income. Sum implied opinc across all
        extracted pairs. If sum > 1.05 × consolidated total_operating_income,
        issue a block. Tolerance accounts for allocation / unallocated items.
        """
        issues: list[CriticIssue] = []
        if not context:
            return issues
        meta_facts = context.get("meta_facts") or {}
        segment_detail = context.get("segment_detail") or {}
        total_opinc = meta_facts.get("operating_income")
        total_gross_profit = meta_facts.get("gross_profit")
        if not total_opinc or total_opinc <= 0 or not segment_detail:
            return issues

        # Flatten segments → {normalized_name: revenue}
        # BUG-Y39 (2026-05-06): SEC fact_bridge groups segments into multiple
        # categories (product / business_segment / geographic). The
        # operating-income margin check should ONLY consider product /
        # reporting segments — `geographic` regions aren't margin centers
        # in the operating-income sense. NVDA segment_detail has us / tw /
        # china_including_hong_kong / il in `geographic`; LLM citing
        # "U.S. region 65% margin" was being matched against a geographic
        # entry, then summed with product-segment margins, producing a
        # phantom $251B vs $130B operating-income oversum BLOCK. Now the
        # critic only flattens product/business segments. Counter: if the
        # LLM truly fabricates a geographic segment opinc, it'll fly under
        # this check — but that's a less common error and we'd rather not
        # block real margin checks because of category confusion.
        _PRODUCT_LIKE = {"product", "business_segment", "segment", "operating_segment"}
        seg_rev_lookup: dict[str, float] = {}
        for category, segs in segment_detail.items():
            if not isinstance(segs, dict):
                continue
            cat_norm = (category or "").lower().strip()
            if cat_norm and cat_norm not in _PRODUCT_LIKE:
                # Skip `geographic`, `customer`, `channel`, etc.
                continue
            for seg_id, seg_data in segs.items():
                if not isinstance(seg_data, dict):
                    continue
                rev = seg_data.get("revenue", 0)
                if rev > 0:
                    # Normalize: strip common prefixes and underscores, lowercase
                    normalized = seg_id.replace("_", " ").lower().strip()
                    seg_rev_lookup[normalized] = rev
                    # Also register without common word prefixes
                    for prefix in ("google ", "apple ", "microsoft ", "amazon "):
                        if normalized.startswith(prefix):
                            seg_rev_lookup[normalized[len(prefix):]] = rev

        if not seg_rev_lookup:
            return issues

        import re

        # TODO-Y6 (2026-05-06): pick the currency unit for absolute-OI claims
        # off `meta_facts.__display`. CNY uses ¥X亿 (亿 = 1e8); USD uses $XB
        # (B = 1e9). Without this branch, A-share LLM output that fabricates
        # `¥3亿净利润` totally bypasses the segment ceiling check, exactly the
        # mirror of BUG-A25 in synthesizer's `_scrub_fair_value_claims`.
        _display = meta_facts.get("__display") or {}
        _currency = (_display.get("currency") or meta_facts.get("__currency") or "USD").upper()
        is_cny = _currency in ("CNY", "RMB", "¥") or _display.get("symbol") == "¥"

        # Strategy: find every "<pct>%" token that sits within ~60 chars of
        # the word "margin" and scan the surrounding window for a known
        # segment name. Much more robust than a single compound regex.
        pct_margin_pattern = re.compile(r"(\d{1,3}(?:\.\d)?)\s*%", re.IGNORECASE)
        # Absolute $ opinc / gross profit patterns ($NN.NB, $NN billion)
        abs_dollar_pattern = re.compile(
            r"\$\s*(\d{1,4}(?:\.\d+)?)\s*(?:B\b|billion\b)",
            re.IGNORECASE,
        )
        # ¥ opinc / 亿 patterns: `¥3.5亿` or `3.5亿` near "operating income".
        # We require the explicit ¥ to avoid catching share-count "X亿股".
        abs_yuan_pattern = re.compile(
            r"¥\s*(\d{1,4}(?:\.\d+)?)\s*亿",
        )
        abs_pattern = abs_yuan_pattern if is_cny else abs_dollar_pattern
        abs_unit_scale = 1e8 if is_cny else 1e9
        sigil = "¥" if is_cny else "$"
        big_unit = "亿" if is_cny else "B"
        # Match Chinese "经营利润" / "营业利润" alongside English forms so the
        # window check below works for A-share narrative.
        # AUDIT (2026-07, logic_critic:338): "营业收入" removed — under CAS
        # it is REVENUE, not operating profit. With it in the list, a real
        # segment-revenue citation ("云端产品线营业收入¥60.0亿") implied
        # OI > consolidated OI and was mis-blocked as ABS_OI_IMPOSSIBLE.
        # Revenue-level claims are narrative_fact_critic's job.
        opinc_keywords = (
            ("operating income", "operating profit", "经营利润", "营业利润")
            if is_cny else ("operating income", "operating profit")
        )
        seg_names_sorted = sorted(seg_rev_lookup.keys(), key=len, reverse=True)

        claimed_pairs: list[tuple[str, float, float, str, str]] = []
        # (seg_name, pct_or_raw, implied_oi, jid, claim_type)
        # claim_type ∈ {"opm", "gm", "abs_oi"}

        for j in judgments:
            texts: list[str] = []
            for obs in j.observations:
                texts.append(getattr(obs, "text", "") or "")
            for inf in j.inferences:
                texts.append(getattr(inf, "text", "") or "")
            full_text = " ".join(texts)
            full_lower = full_text.lower()

            seen_in_this_j: set[tuple[str, str, float]] = set()

            # === Pattern A: percentage margin claims ===
            for m in pct_margin_pattern.finditer(full_text):
                try:
                    pct = float(m.group(1))
                except ValueError:
                    continue
                if not (5 <= pct <= 95):
                    continue
                start, end = m.span()
                window_start = max(0, start - 80)
                window_end = min(len(full_text), end + 40)
                window = full_lower[window_start:window_end]
                # Distinguish operating margin vs gross margin vs other
                is_opm = (
                    "operating margin" in window
                    or "operating margins" in window
                    or " om " in window
                    or " om:" in window
                )
                is_gm = (
                    "gross margin" in window
                    or "gross margins" in window
                )
                if not is_opm and not is_gm:
                    continue
                # Find the longest matching segment name in the window
                matched_seg = None
                matched_rev = None
                for seg_name in seg_names_sorted:
                    if seg_name in window:
                        matched_seg = seg_name
                        matched_rev = seg_rev_lookup[seg_name]
                        break
                if matched_seg is None or matched_rev is None:
                    continue
                claim_type = "opm" if is_opm else "gm"
                key = (matched_seg, claim_type, pct)
                if key in seen_in_this_j:
                    continue
                seen_in_this_j.add(key)
                implied = matched_rev * pct / 100
                claimed_pairs.append(
                    (matched_seg, pct, implied, j.judgment_id, claim_type)
                )

            # === Pattern B: absolute operating income claims ===
            # TODO-Y6: scale + scan window depend on currency. CNY: ¥X亿
            # (1e8); USD: $XB (1e9).
            for m in abs_pattern.finditer(full_text):
                try:
                    amount_b = float(m.group(1))
                except ValueError:
                    continue
                if amount_b < 0.1 or amount_b > 20000:
                    continue
                start, end = m.span()
                window_start = max(0, start - 80)
                window_end = min(len(full_text), end + 40)
                window = full_lower[window_start:window_end]
                if not any(kw in window for kw in opinc_keywords):
                    continue
                matched_seg = None
                for seg_name in seg_names_sorted:
                    if seg_name in window:
                        matched_seg = seg_name
                        break
                if matched_seg is None:
                    continue
                implied = amount_b * abs_unit_scale
                key = (matched_seg, "abs_oi", amount_b)
                if key in seen_in_this_j:
                    continue
                seen_in_this_j.add(key)
                claimed_pairs.append(
                    (matched_seg, amount_b, implied, j.judgment_id, "abs_oi")
                )

        if not claimed_pairs:
            return issues

        # ── Individual check ────────────────────────────────────────────
        # Any single segment claim that exceeds its consolidated ceiling
        # (operating income or gross profit) is mathematically impossible.
        # TODO-Y6: helper to format an absolute amount in the active currency.
        def _fmt_big(v: float) -> str:
            return f"{sigil}{v/abs_unit_scale:.1f}{big_unit}"

        for seg_name, raw_val, implied, jid, ctype in claimed_pairs:
            if ctype == "opm":
                if implied > total_opinc * 1.05:
                    issues.append(self._make_issue(
                        code="LOGIC_SEGMENT_MARGIN_IMPOSSIBLE",
                        severity="block",
                        message=(
                            f"Segment '{seg_name}' claimed operating margin "
                            f"{raw_val:.0f}% implies operating income "
                            f"{_fmt_big(implied)}, which exceeds consolidated "
                            f"total operating income {_fmt_big(total_opinc)}. "
                            f"Mathematically impossible — segment opinc was "
                            f"not disclosed and the value was fabricated."
                        ),
                        judgment_ids=[jid],
                        action=(
                            "Remove the specific margin percentage. Segment "
                            "operating income is not disclosed in the filing; "
                            "discuss profitability qualitatively only."
                        ),
                    ))
            elif ctype == "gm" and total_gross_profit and total_gross_profit > 0:
                if implied > total_gross_profit * 1.05:
                    issues.append(self._make_issue(
                        code="LOGIC_SEGMENT_GROSS_MARGIN_IMPOSSIBLE",
                        severity="block",
                        message=(
                            f"Segment '{seg_name}' claimed gross margin "
                            f"{raw_val:.0f}% implies gross profit "
                            f"{_fmt_big(implied)}, which exceeds consolidated "
                            f"total gross profit {_fmt_big(total_gross_profit)}. "
                            f"Mathematically impossible — segment gross profit "
                            f"not disclosed and the value was fabricated."
                        ),
                        judgment_ids=[jid],
                        action="Remove the specific gross margin percentage.",
                    ))
            elif ctype == "abs_oi":
                if implied > total_opinc * 1.05:
                    issues.append(self._make_issue(
                        code="LOGIC_SEGMENT_ABS_OI_IMPOSSIBLE",
                        severity="block",
                        message=(
                            f"Segment '{seg_name}' claimed operating income "
                            f"{sigil}{raw_val:.1f}{big_unit} exceeds consolidated total "
                            f"{_fmt_big(total_opinc)}. Mathematically impossible."
                        ),
                        judgment_ids=[jid],
                        action=f"Remove the specific {sigil}-denominated figure.",
                    ))

        # ── Aggregate check (operating margin only) ─────────────────────
        # Highest OPM claim per segment, sum across unique segments.
        by_segment: dict[str, tuple[float, float, str]] = {}
        for seg_name, raw_val, implied, jid, ctype in claimed_pairs:
            if ctype != "opm":
                continue
            key = seg_name.lower().strip()
            prev = by_segment.get(key)
            if prev is None or raw_val > prev[0]:
                by_segment[key] = (raw_val, implied, jid)

        total_implied = sum(oi for _, oi, _ in by_segment.values())
        if total_implied > total_opinc * 1.05 and len(by_segment) >= 2:
            issues.append(self._make_issue(
                code="LOGIC_SEGMENT_MARGIN_OVERSUM",
                severity="block",
                message=(
                    f"Σ implied segment operating income from claimed margins "
                    f"= {_fmt_big(total_implied)}, exceeds consolidated total "
                    f"{_fmt_big(total_opinc)} by "
                    f"{(total_implied/total_opinc - 1)*100:.1f}%. "
                    f"Segments cited: "
                    f"{', '.join(f'{k}({v[0]:.0f}%)' for k, v in by_segment.items())}. "
                    f"At least one segment margin is fabricated."
                ),
                judgment_ids=list({v[2] for v in by_segment.values()}),
                action=(
                    "Remove or correct per-segment margin citations. The "
                    "consolidated operating margin is an upper bound on the "
                    "revenue-weighted mean of segment margins."
                ),
            ))

        return issues
