"""LLM-as-Judge Critic — cross-checks agent narrative numbers against source data.

Uses a cheap LLM (deepseek-v4-flash, ~$0.004/run) to review every agent's
observations and inferences, comparing cited numbers against the ground-truth
data tables (meta_facts, computed_metrics, historical revenue/growth).

This catches errors that regex-based critics can't:
- "revenue grew 3.3% annually" (no CAGR keyword)
- "revenue doubled" when actual growth is 14%
- Inconsistent numbers across paragraphs
- Ambiguous segment-vs-total confusion

Severity rules:
- CAGR / growth rate mismatches → block (directly affect DCF projections)
- Dollar amount or ratio mismatches → warn (could be segment-level or rounding)

Fallback: if the LLM call fails, returns a single warn noting the failure.
Never blocks the pipeline due to infrastructure failure.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from aegis.core.critics.base import CriticBase
from aegis.data_contracts.critic_result_schema import CriticIssue, CriticResult
from aegis.data_contracts.judgment_schema import JudgmentContract


# ── System prompt ────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a numerical fact-checker for investment research reports. Your job is
to find numbers in analyst narratives that contradict the ground-truth data.

RULES:
1. Compare EVERY number in the analyst text against the GROUND TRUTH table.
2. Only flag numbers that CLEARLY refer to company-level (not segment-level)
   historical or current-period facts. Do NOT flag:
   - Segment/product revenue (e.g. "iPhone revenue $209B") — segments are subsets
   - Forward estimates or consensus projections (e.g. "FY2027 revenue $471B")
   - DCF model assumptions (growth rates, margins in projections)
   - Qualitative statements ("strong growth", "declining margins")
3. For CAGR / growth rates: check the window label matches the value.
   "3-year CAGR 3.3%" is WRONG if actual 3-year CAGR is 1.8%.
   "4-year CAGR 3.3%" is CORRECT if actual 4-year CAGR is 3.3%.
4. Tolerance: ±1pp for percentages/ratios, ±10% for dollar amounts.
5. severity MUST be "block" for CAGR/growth-rate errors (they affect DCF).
   Use "warn" for everything else.
6. If the text is clean (no errors), return an empty issues list.
7. Be CONSERVATIVE. When in doubt, do NOT flag. False positives are worse
   than false negatives here.

Use the report_issues tool to return your findings.
"""

# ── Tool schema ──────────────────────────────────────────────────────

_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "source_label": {
                        "type": "string",
                        "description": "e.g. obs[2] or inf[1]",
                    },
                    "claimed_text": {
                        "type": "string",
                        "description": "Quoted excerpt containing the wrong number",
                    },
                    "claimed_value": {"type": "string"},
                    "correct_value": {"type": "string"},
                    "issue_type": {
                        "type": "string",
                        "enum": [
                            "cagr_mismatch",
                            "growth_mismatch",
                            "metric_mismatch",
                            "margin_mismatch",
                            "other",
                        ],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["block", "warn"],
                    },
                    "explanation": {"type": "string"},
                },
                "required": [
                    "agent_name",
                    "source_label",
                    "claimed_text",
                    "claimed_value",
                    "correct_value",
                    "issue_type",
                    "severity",
                    "explanation",
                ],
            },
        },
    },
    "required": ["issues"],
}

# ── Ground-truth builder ─────────────────────────────────────────────

_MAX_CHARS_PER_AGENT = 6000  # Truncate to control token cost


def _build_ground_truth(
    meta_facts: dict,
    computed_metrics: dict,
    market_data: dict,
) -> str:
    """Build a concise ground-truth table from source data."""
    lines = ["=== GROUND TRUTH DATA (authoritative — agent narratives may contradict these) ==="]

    # Fiscal period
    fy = meta_facts.get("__fiscal_year") or ""
    if fy:
        lines.append(f"Fiscal Year: FY{fy}")

    currency = meta_facts.get("__currency", "USD")
    sym = "¥" if currency == "CNY" else "$"
    div = 1e8 if currency == "CNY" else 1e9
    unit = "亿" if currency == "CNY" else "B"

    # Key dollar metrics
    for key, label in [
        ("revenue", "Revenue"),
        ("net_income", "Net Income"),
        ("operating_income", "Operating Income"),
        ("ebitda", "EBITDA"),
        ("free_cash_flow", "Free Cash Flow"),
        ("net_debt", "Net Debt (positive = net debtor, negative = net cash)"),
        ("total_debt", "Total Debt"),
        ("total_cash_and_investments", "Cash & Investments"),
        ("depreciation_amortization", "D&A"),
    ]:
        val = meta_facts.get(key)
        if val:
            lines.append(f"{label}: {sym}{val/div:.1f}{unit}")

    # Computed dollar metrics (may differ from meta_facts)
    for key, label in [
        ("net_debt", "Net Debt (computed)"),
    ]:
        val = computed_metrics.get(key)
        if val is not None and key not in [k for k, _ in [("revenue", ""), ("net_income", "")]]:
            if not any(key in line for line in lines):
                lines.append(f"{label}: {sym}{val/div:.1f}{unit}")

    # Key ratios (decimal → %)
    for key, label in [
        ("operating_margin", "Operating Margin"),
        ("gross_margin", "Gross Margin"),
        ("net_margin", "Net Margin"),
        ("roic", "ROIC"),
        ("roe", "ROE"),
    ]:
        val = computed_metrics.get(key)
        if val is not None:
            lines.append(f"{label}: {val*100:.1f}%")

    # P/E
    pe = computed_metrics.get("pe_ratio")
    if pe:
        lines.append(f"P/E (FY): {pe:.1f}x")

    # Market data
    price = market_data.get("current_price", 0)
    if price:
        lines.append(f"Current Price: {sym}{price:.2f}")
        mcap = market_data.get("market_cap", 0)
        if mcap:
            lines.append(f"Market Cap: {sym}{mcap/div:.0f}{unit}")

    # Historical revenue + CAGR
    hist_rev = meta_facts.get("__historical_revenue", {})
    if hist_rev:
        sorted_yrs = sorted(hist_rev.keys())
        rev_str = ", ".join(f"FY{y}={sym}{hist_rev[y]/div:.1f}{unit}" for y in sorted_yrs)
        lines.append(f"Revenue by Year: {rev_str}")

        # Compute all meaningful CAGRs
        latest = sorted_yrs[-1]
        for n in range(2, len(sorted_yrs) + 1):
            base_yr = latest - n
            if base_yr in hist_rev and hist_rev[base_yr] > 0:
                cagr = (hist_rev[latest] / hist_rev[base_yr]) ** (1 / n) - 1
                lines.append(f"Revenue CAGR ({n}-year, FY{base_yr}→FY{latest}): {cagr:.1%}")

    # Historical growth
    hist_growth = meta_facts.get("__historical_growth", {})
    if hist_growth:
        g_str = ", ".join(f"FY{y}={hist_growth[y]:.1%}" for y in sorted(hist_growth))
        lines.append(f"YoY Revenue Growth: {g_str}")

    # EPS
    eps = meta_facts.get("earnings_per_share")
    if not eps:
        ni = meta_facts.get("net_income", 0)
        sh = meta_facts.get("diluted_shares", 1)
        if ni and sh > 1e6:
            eps = ni / sh
    if eps:
        lines.append(f"EPS: {sym}{eps:.2f}")

    return "\n".join(lines)


def _build_agent_text(judgments: list[JudgmentContract]) -> str:
    """Serialize agent narratives for the LLM to review."""
    parts = []
    for j in judgments:
        agent_lines = [f"\n=== Agent: {j.agent_name} ==="]
        for i, obs in enumerate(j.observations):
            text = obs.text[:_MAX_CHARS_PER_AGENT]
            agent_lines.append(f"[obs[{i}]] {text}")
        for i, inf in enumerate(j.inferences):
            text = inf.text[:_MAX_CHARS_PER_AGENT]
            agent_lines.append(f"[inf[{i}]] {text}")
        # Narrative supplement
        narrative = getattr(j, "narrative", None)
        if narrative and isinstance(narrative, str):
            agent_lines.append(f"[narrative] {narrative[:_MAX_CHARS_PER_AGENT]}")
        parts.append("\n".join(agent_lines))
    return "\n".join(parts)


# ── Critic class ─────────────────────────────────────────────────────

# Issue types that always get block severity (override LLM's judgment)
_BLOCK_TYPES = {"cagr_mismatch", "growth_mismatch"}


class LLMJudgeCritic(CriticBase):
    """Cross-checks agent narratives against source data using an LLM judge."""

    CRITIC_TYPE = "llm_judge_critic"

    def __init__(self, model: str = "deepseek-v4-flash"):
        # Cheap judge tier: fact-checking needs precision, not depth, so the
        # flash tier is the default. Only consulted on the DeepSeek path;
        # Grok/SDK fallbacks use their own defaults.
        self._model = model

    def review(
        self,
        judgments: list[JudgmentContract],
        context: dict | None = None,
    ) -> CriticResult:
        issues: list[CriticIssue] = []
        ctx = context or {}

        meta_facts = ctx.get("meta_facts", {})
        computed_metrics = ctx.get("computed_metrics", {})
        market_data = ctx.get("market_data", {})

        if not judgments:
            return CriticResult(
                critic_id=f"critic_llm_judge_{id(self)}",
                critic_type=self.CRITIC_TYPE,
                issues=[],
                block_publish=False,
                overall_risk="low",
            )

        # Build the prompt
        ground_truth = _build_ground_truth(meta_facts, computed_metrics, market_data)
        agent_text = _build_agent_text(judgments)
        user_message = (
            f"Review the following analyst narratives for numerical errors. "
            f"Compare every cited number against the ground truth.\n\n"
            f"{ground_truth}\n\n"
            f"=== ANALYST NARRATIVES TO REVIEW ===\n{agent_text}"
        )

        # BUG-Y40 (2026-05-06): if the orchestrator passes its own LLM
        # client via critic_context, reuse it so the call is counted in
        # the run-level cost summary. Falls through to backend auto-pick
        # (Y31 fix) when no shared client is available.
        shared_client = ctx.get("shared_llm_client")

        # Call LLM
        try:
            llm_issues = self._call_llm(user_message, shared_client=shared_client)
        except Exception as e:
            # Graceful degradation — never block due to infra failure
            return CriticResult(
                critic_id=f"critic_llm_judge_{id(self)}",
                critic_type=self.CRITIC_TYPE,
                issues=[self._make_issue(
                    code="LLM_JUDGE_FAILED",
                    severity="warn",
                    message=f"LLM judge critic failed: {e}. Narrative numbers were NOT cross-checked.",
                    action="Set DEEPSEEK_API_KEY / GROK_API_KEY / CLAUDE_CODE_OAUTH_TOKEN and check network.",
                )],
                block_publish=False,
                overall_risk="medium",
            )

        # AUDIT 2026-07-12 (B4): block-level findings must reproduce before
        # they can flip the publish decision. With critic_gate threshold=1,
        # a single stochastic block used to flip published↔blocked run-to-run
        # (茅台 翻盘 — Grok 20-audit 元发现). When the first pass found any
        # block-type issue, rerun the judge once and keep block severity only
        # for (issue_type, agent) pairs that appear in BOTH passes;
        # non-reproduced blocks demote to warn. Infra failure on the rerun
        # keeps the original blocks (a network hiccup must not un-gate).
        _blocks_present = any(
            raw.get("issue_type", "other") in _BLOCK_TYPES for raw in llm_issues
        )
        _confirmed_block_keys: set[tuple[str, str]] | None = None
        if _blocks_present:
            try:
                _rerun = self._call_llm(user_message, shared_client=shared_client)
                _confirmed_block_keys = {
                    (r.get("issue_type", "other"), r.get("agent_name", "unknown"))
                    for r in _rerun
                    if r.get("issue_type", "other") in _BLOCK_TYPES
                }
            except Exception:
                _confirmed_block_keys = None

        # Map LLM output to CriticIssues
        for raw in llm_issues:
            issue_type = raw.get("issue_type", "other")
            # Override severity: CAGR/growth errors always block
            severity = "block" if issue_type in _BLOCK_TYPES else "warn"
            _demoted_unreproduced = False
            if (
                severity == "block"
                and _confirmed_block_keys is not None
                and (issue_type, raw.get("agent_name", "unknown"))
                not in _confirmed_block_keys
            ):
                severity = "warn"
                _demoted_unreproduced = True

            agent_name = raw.get("agent_name", "unknown")
            source_label = raw.get("source_label", "")
            claimed = raw.get("claimed_value", "?")
            correct = raw.get("correct_value", "?")
            explanation = raw.get("explanation", "")
            claimed_text = raw.get("claimed_text", "")

            msg = (
                f"{agent_name} {source_label}: "
                f"claims \"{claimed_text}\" "
                f"(cited: {claimed}, actual: {correct}). "
                f"{explanation}"
            )
            if _demoted_unreproduced:
                msg += (
                    " [block→warn: finding did not reproduce on confirmation "
                    "rerun — treated as sampling noise, not a gate-flipping "
                    "fact error]"
                )

            # Find matching judgment_id
            jid = None
            for j in judgments:
                if j.agent_name == agent_name:
                    jid = j.judgment_id
                    break

            issues.append(self._make_issue(
                code=f"LLM_JUDGE_{issue_type.upper()}",
                severity=severity,
                message=msg,
                judgment_ids=[jid] if jid else [],
                action=raw.get("explanation", "Verify number against source data."),
            ))

        return CriticResult(
            critic_id=f"critic_llm_judge_{id(self)}",
            critic_type=self.CRITIC_TYPE,
            issues=issues,
            block_publish=self._any_block(issues),
            overall_risk=self._overall_risk(issues),
        )

    def _call_llm(self, user_message: str, shared_client: Any = None) -> list[dict]:
        """Call any available structured-output LLM backend for fact-checking.

        BUG-Y31 (2026-05-06): previously hardcoded one specific backend
        client and emitted "LLM judge critic failed: no API key" warnings on
        EVERY production run whenever that backend's key was absent. The
        whole critic was effectively dead — firing one warn per run telling
        operators it didn't run. Try backends in order of availability:
        DeepSeek → Grok → Anthropic SDK.

        BUG-Y40 (2026-05-06): when the orchestrator passes its own
        already-configured LLM client via the `shared_client` arg (sourced
        from `critic_context["shared_llm_client"]`), reuse it. That keeps
        the call billed against the run-level cost_tracker rather than a
        fresh CostTracker that the orchestrator's "LLM cost: ..." log will
        never see.
        """
        if shared_client is not None:
            try:
                result = shared_client.call_structured(
                    system_prompt=_SYSTEM_PROMPT,
                    user_message=user_message,
                    tool_schema=_TOOL_SCHEMA,
                    tool_name="report_issues",
                    role="critic",
                )
                issues = result.get("issues", [])
                return issues if isinstance(issues, list) else []
            except Exception:
                # Fall through to backend auto-pick if shared client fails
                # (e.g. content-filter rejection that's specific to this
                # prompt). Don't propagate — the backend fallback may save us.
                pass
        client = None
        last_err: Exception | None = None
        try_order: list[tuple[str, callable]] = [
            ("deepseek", lambda: __import__("aegis.core.llm.deepseek_client",
                fromlist=["DeepSeekClient"]).DeepSeekClient(model=self._model)
                if __import__("aegis.core.llm.deepseek_client",
                    fromlist=["DeepSeekClient"]).DeepSeekClient.is_available() else None),
            ("grok", lambda: __import__("aegis.core.llm.grok_client",
                fromlist=["GrokClient"]).GrokClient()
                if __import__("aegis.core.llm.grok_client",
                    fromlist=["GrokClient"]).GrokClient.is_available() else None),
            ("sdk", lambda: __import__("aegis.core.llm.sdk_client",
                fromlist=["SDKClient"]).SDKClient(model="haiku")
                if __import__("aegis.core.llm.sdk_client",
                    fromlist=["SDKClient"]).SDKClient.is_available() else None),
        ]
        for name, factory in try_order:
            try:
                _client = factory()
                if _client is not None:
                    client = _client
                    break
            except Exception as e:
                last_err = e
                continue

        if client is None:
            raise RuntimeError(
                f"No LLM backend available for llm_judge_critic. "
                f"Set DEEPSEEK_API_KEY, GROK_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN. "
                f"Last error: {last_err}"
            )

        result = client.call_structured(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_message,
            tool_schema=_TOOL_SCHEMA,
            tool_name="report_issues",
            role="critic",
        )

        issues = result.get("issues", [])
        if not isinstance(issues, list):
            return []
        return issues
