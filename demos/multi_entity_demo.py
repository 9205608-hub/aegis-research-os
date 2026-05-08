"""Aegis Research OS — Multi-Entity Comparison Demo.

Compares META vs GOOGL vs SNAP using the same sector pack (Ad Platform).
Demonstrates: per-entity analysis → ComparativeAnalyst → CrossEntityCritic.

Usage:
  python demos/multi_entity_demo.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.core.governance.run_manifest import generate_run_id
from aegis.core.truth.registry.metric_registry import MetricRegistry
from aegis.core.truth.registry.seed_metrics import seed_core_metrics
from aegis.core.truth.formulas.formula_engine import FormulaEngine
from aegis.core.truth.scenario_engine.dcf_engine import DCFEngine, DCFInput
from aegis.core.agents.base import AgentInput
from aegis.core.agents import (
    AccountingAnalyst, BusinessAnalyst, SectorContextAgent,
    ManagementAnalyst, ValuationAnalyst, VariantAnalyst, RiskAnalyst,
)
from aegis.core.agents.comparative_analyst.agent import ComparativeAnalyst, ComparativeInput
from aegis.core.critics import (
    LogicCritic, AccountingCritic, EvidenceCritic, SectorCritic,
    CognitiveBiasCritic, MacroConsistencyCritic, MarketCritic,
)
from aegis.core.critics.cross_entity_critic.critic import CrossEntityCritic
from aegis.core.reports import ReportSerializer


def separator(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


# ================================================================
# ENTITY DATA — simulated from 10-K filings
# ================================================================
ENTITIES = {
    "meta_platforms": {
        "ticker": "META",
        "revenue": 164_710_000_000,
        "cost_of_revenue": 27_928_000_000,
        "gross_profit": 136_782_000_000,
        "operating_income": 69_381_000_000,
        "net_income": 62_360_000_000,
        "ebitda": 81_537_000_000,
        "total_assets": 256_166_000_000,
        "total_equity": 165_862_000_000,
        "total_debt": 28_826_000_000,
        "cash_and_equivalents": 58_069_000_000,
        "current_assets": 75_860_000_000,
        "current_liabilities": 31_480_000_000,
        "operating_cash_flow": 91_145_000_000,
        "capex": 39_200_000_000,
        "free_cash_flow": 51_945_000_000,
        "sbc": 19_007_000_000,
        "diluted_shares": 2_579_000_000,
        "basic_shares": 2_514_000_000,
        "depreciation_amortization": 12_156_000_000,
        "dap": 3_350_000_000,
        "arpu_global": 41.39,
        "current_price": 585.0,
        "market_cap": 1_510_000_000_000,
    },
    "googl": {
        "ticker": "GOOGL",
        "revenue": 350_018_000_000,
        "cost_of_revenue": 148_164_000_000,
        "gross_profit": 201_854_000_000,
        "operating_income": 112_387_000_000,
        "net_income": 100_681_000_000,
        "ebitda": 127_000_000_000,
        "total_assets": 432_428_000_000,
        "total_equity": 315_517_000_000,
        "total_debt": 13_253_000_000,
        "cash_and_equivalents": 95_741_000_000,
        "current_assets": 171_530_000_000,
        "current_liabilities": 86_083_000_000,
        "operating_cash_flow": 112_647_000_000,
        "capex": 52_529_000_000,
        "free_cash_flow": 60_118_000_000,
        "sbc": 23_069_000_000,
        "diluted_shares": 12_324_000_000,
        "basic_shares": 12_116_000_000,
        "depreciation_amortization": 14_613_000_000,
        "dap": 2_000_000_000,
        "arpu_global": 43.75,
        "current_price": 185.0,
        "market_cap": 2_280_000_000_000,
    },
    "snap": {
        "ticker": "SNAP",
        "revenue": 5_366_000_000,
        "cost_of_revenue": 2_537_000_000,
        "gross_profit": 2_829_000_000,
        "operating_income": -734_000_000,
        "net_income": -703_000_000,
        "ebitda": -334_000_000,
        "total_assets": 7_462_000_000,
        "total_equity": 1_482_000_000,
        "total_debt": 3_735_000_000,
        "cash_and_equivalents": 1_337_000_000,
        "current_assets": 2_541_000_000,
        "current_liabilities": 1_685_000_000,
        "operating_cash_flow": 193_000_000,
        "capex": 125_000_000,
        "free_cash_flow": 68_000_000,
        "sbc": 1_531_000_000,
        "diluted_shares": 1_681_000_000,
        "basic_shares": 1_646_000_000,
        "depreciation_amortization": 400_000_000,
        "dap": 414_000_000,
        "arpu_global": 3.24,
        "current_price": 12.50,
        "market_cap": 21_000_000_000,
    },
}


def compute_metrics(facts: dict) -> dict[str, float]:
    """Compute standard metrics from facts."""
    m: dict[str, float] = {}
    safe_div = lambda a, b: a / b if b else 0

    m["gross_margin"] = safe_div(facts["gross_profit"], facts["revenue"])
    m["operating_margin"] = safe_div(facts["operating_income"], facts["revenue"])
    m["net_margin"] = safe_div(facts["net_income"], facts["revenue"])
    m["ebitda_margin"] = safe_div(facts["ebitda"], facts["revenue"])
    m["roe"] = safe_div(facts["net_income"], facts["total_equity"])
    m["roa"] = safe_div(facts["net_income"], facts["total_assets"])
    m["sbc_to_revenue"] = safe_div(facts["sbc"], facts["revenue"])
    m["capex_to_revenue"] = safe_div(facts["capex"], facts["revenue"])
    m["current_ratio"] = safe_div(facts["current_assets"], facts["current_liabilities"])
    m["net_debt"] = facts["total_debt"] - facts["cash_and_equivalents"]
    m["net_debt_to_ebitda"] = safe_div(m["net_debt"], facts["ebitda"]) if facts["ebitda"] > 0 else 0
    m["fcf_margin"] = safe_div(facts["free_cash_flow"], facts["revenue"])
    m["revenue_growth"] = 0.15  # Simplified — would come from YoY data

    # Market metrics
    if facts.get("current_price") and facts.get("net_income") and facts["net_income"] > 0:
        eps = facts["net_income"] / facts["diluted_shares"]
        m["pe_ratio"] = safe_div(facts["current_price"], eps)

    if facts.get("market_cap"):
        ev = facts["market_cap"] + m["net_debt"]
        m["enterprise_value"] = ev
        m["ev_to_revenue"] = safe_div(ev, facts["revenue"])
        m["ev_to_ebitda"] = safe_div(ev, facts["ebitda"]) if facts["ebitda"] > 0 else 0

    nd = m["net_debt"]
    invested = facts["total_equity"] + nd
    m["roic"] = safe_div(facts["operating_income"] * 0.79, invested)

    if facts.get("arpu_global"):
        m["arpu"] = facts["arpu_global"]
    if facts.get("dap"):
        m["dau"] = facts["dap"]

    m["dilution_rate"] = safe_div(
        facts["diluted_shares"] - facts["basic_shares"], facts["basic_shares"],
    )

    return m


def main():
    separator("AEGIS RESEARCH OS — MULTI-ENTITY COMPARISON DEMO")
    print("  Entities: META vs GOOGL vs SNAP (Ad Platform sector)")
    run_id = generate_run_id()
    print(f"  Run ID: {run_id}")

    sector_pack = {
        "sector_pack_id": "sp_ad_platform_v1",
        "sector_name": "Ad Platform / Digital Advertising",
        "key_kpis": [
            {"metric": "arpu", "display": "Average Revenue Per User", "importance": "critical"},
            {"metric": "dau_mau_ratio", "display": "DAU/MAU Ratio", "importance": "high"},
        ],
        "cycle_characteristics": {"cyclicality": "moderate"},
        "competitive_dynamics": {},
        "accounting_considerations": ["SBC is material"],
    }

    # ================================================================
    # STEP 1: Per-Entity Analysis
    # ================================================================
    per_entity_metrics: dict[str, dict[str, float]] = {}
    per_entity_judgments: dict[str, list] = {}
    per_entity_dcf: dict[str, float] = {}

    for entity_id, facts in ENTITIES.items():
        separator(f"STEP 1: Per-Entity Analysis — {facts['ticker']}")

        # Compute metrics
        metrics = compute_metrics(facts)
        per_entity_metrics[entity_id] = metrics

        print(f"  Revenue:         ${facts['revenue']/1e9:.1f}B")
        print(f"  Gross Margin:    {metrics['gross_margin']:.1%}")
        print(f"  Operating Margin:{metrics['operating_margin']:.1%}")
        print(f"  FCF Margin:      {metrics['fcf_margin']:.1%}")
        print(f"  ROIC:            {metrics['roic']:.1%}")
        if metrics.get("pe_ratio"):
            print(f"  P/E:             {metrics['pe_ratio']:.1f}x")
        if metrics.get("ev_to_revenue"):
            print(f"  EV/Revenue:      {metrics['ev_to_revenue']:.1f}x")

        # DCF (simplified)
        revenue = facts["revenue"]
        if facts["operating_income"] > 0:
            dcf_input = DCFInput(
                base_revenue=revenue,
                revenue_growth_path=[0.12, 0.10, 0.09, 0.08, 0.07, 0.06, 0.05, 0.05, 0.04, 0.04],
                operating_margin_path=[metrics["operating_margin"]] * 10,
                capex_to_revenue_path=[metrics["capex_to_revenue"]] * 10,
                effective_tax_rate=0.21,
                nwc_to_revenue_delta=0.01,
                wacc=0.095,
                terminal_growth_rate=0.03,
                shares_outstanding=facts["diluted_shares"],
                net_debt=metrics["net_debt"],
                sbc_to_revenue=0.0,
                dilution_rate_annual=metrics["dilution_rate"],
                horizon_years=10,
                sbc_treatment="expense_in_fcf",
                buyback_yield_annual=0.0,
                base_depreciation=facts["depreciation_amortization"],
                capex_useful_life_years=5.0,
            )
            dcf_output = DCFEngine().compute_dcf(dcf_input)
            per_entity_dcf[entity_id] = dcf_output.per_share_value
            print(f"  DCF Value:       ${dcf_output.per_share_value:.0f}/share")
        else:
            per_entity_dcf[entity_id] = 0
            print(f"  DCF Value:       N/A (negative operating income)")

        # Run agents
        agent_macro = {
            "cycle_phase": "late_expansion",
            "priced_in": {"implied_revenue_growth": 0.10},
            "scenarios": {"bear_value": 0, "base_value": 0, "bull_value": 0},
            "current_price": facts.get("current_price", 0),
        }
        base_inp = AgentInput(
            entity_id=entity_id, run_id=run_id,
            question_id=f"q_{entity_id}",
            facts=facts,
            metric_results=metrics,
            macro_context=agent_macro,
            sector_pack=sector_pack,
        )
        judgments = []
        for AgentCls in [AccountingAnalyst, BusinessAnalyst, ManagementAnalyst,
                         ValuationAnalyst, VariantAnalyst, RiskAnalyst, SectorContextAgent]:
            out = AgentCls().run(base_inp)
            judgments.append(out.judgment)

        per_entity_judgments[entity_id] = judgments
        print(f"  Agents:          {len(judgments)} ran")

    # ================================================================
    # STEP 2: Comparative Analysis
    # ================================================================
    separator("STEP 2: Comparative Analysis")

    entity_ids = list(ENTITIES.keys())
    comp_input = ComparativeInput(
        entity_ids=entity_ids,
        run_id=run_id,
        theme="Digital Advertising / Ad Platform",
        per_entity_metrics=per_entity_metrics,
        per_entity_judgments=per_entity_judgments,
        comparison_dimensions=[
            "gross_margin", "operating_margin", "fcf_margin",
            "roic", "ev_to_revenue", "sbc_to_revenue",
        ],
        valuation_metric="ev_to_revenue",
    )

    comp_analyst = ComparativeAnalyst()
    comparison = comp_analyst.analyze(comp_input)

    print(f"\n  Theme: {comparison.theme}")
    print(f"  Entities: {', '.join(comparison.entity_ids)}")
    print(f"\n  --- Dimension Rankings ---")
    for dim in comparison.dimensions:
        print(f"\n  {dim.dimension}:")
        for eid in entity_ids:
            rank = dim.rankings.get(eid, "N/A")
            val = dim.values.get(eid)
            ticker = ENTITIES[eid]["ticker"]
            if val is not None:
                print(f"    #{rank} {ticker:>6}: {val:.2%}" if abs(val) < 10 else f"    #{rank} {ticker:>6}: {val:.1f}x")
            else:
                print(f"    #{rank} {ticker:>6}: N/A")

    print(f"\n  --- Relative Valuation ---")
    print(f"  Metric: {comparison.relative_valuation.metric}")
    for eid in entity_ids:
        val = comparison.relative_valuation.values.get(eid, 0)
        ticker = ENTITIES[eid]["ticker"]
        print(f"    {ticker:>6}: {val:.1f}x")
    print(f"  Sector Median: {comparison.relative_valuation.sector_median:.1f}x")

    print(f"\n  --- Top Picks ---")
    for pick in comparison.top_picks:
        ticker = ENTITIES[pick]["ticker"]
        print(f"    {ticker}")
    print(f"  Rationale: {comparison.top_pick_rationale}")

    if comparison.cross_entity_risks:
        print(f"\n  --- Cross-Entity Risks ---")
        for risk in comparison.cross_entity_risks:
            print(f"    ! {risk}")

    # ================================================================
    # STEP 3: Cross-Entity Critic
    # ================================================================
    separator("STEP 3: Cross-Entity Critic")

    all_judgments = []
    for jlist in per_entity_judgments.values():
        all_judgments.extend(jlist)

    cross_critic = CrossEntityCritic()
    cross_result = cross_critic.review(all_judgments, context={
        "entity_standards": {eid: "US_GAAP" for eid in entity_ids},
        "entity_currencies": {eid: "USD" for eid in entity_ids},
    })

    status = "BLOCK" if cross_result.block_publish else "PASS"
    print(f"  [{status}] {cross_result.critic_type}: {len(cross_result.issues)} issues")
    for issue in cross_result.issues:
        print(f"    [{issue.severity}] {issue.issue_code}: {issue.message[:80]}")

    # ================================================================
    # STEP 4: Comparison Table
    # ================================================================
    separator("STEP 4: Comparison Table Output")

    serializer = ReportSerializer()
    table = serializer.comparison_table(comparison)
    print(json.dumps(table, indent=2, default=str))

    # ================================================================
    # SUMMARY
    # ================================================================
    separator("MULTI-ENTITY COMPARISON COMPLETE")

    print(f"\n  {'Entity':>20}  {'Revenue':>10}  {'Op Margin':>10}  {'FCF Margin':>10}  {'ROIC':>8}  {'EV/Rev':>8}  {'DCF':>8}")
    print(f"  {'------':>20}  {'-------':>10}  {'---------':>10}  {'----------':>10}  {'----':>8}  {'------':>8}  {'---':>8}")
    for eid in entity_ids:
        ticker = ENTITIES[eid]["ticker"]
        m = per_entity_metrics[eid]
        dcf = per_entity_dcf[eid]
        print(f"  {ticker:>20}  ${ENTITIES[eid]['revenue']/1e9:>8.1f}B"
              f"  {m['operating_margin']:>9.1%}"
              f"  {m['fcf_margin']:>9.1%}"
              f"  {m['roic']:>7.1%}"
              f"  {m.get('ev_to_revenue', 0):>7.1f}x"
              f"  ${dcf:>6.0f}")

    print(f"\n  Top Picks: {', '.join(ENTITIES[p]['ticker'] for p in comparison.top_picks)}")
    print(f"  Rationale: {comparison.top_pick_rationale}")


if __name__ == "__main__":
    main()
