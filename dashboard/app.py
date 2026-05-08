"""Aegis Research OS — Streamlit Dashboard.

Interactive investment research dashboard.
Run: streamlit run dashboard/app.py
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import json
from datetime import datetime, timezone

st.set_page_config(
    page_title="Aegis Research OS",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ──
st.markdown("""
<style>
    .stApp { background-color: #0f172a; }
    .metric-card {
        background: #1e293b; border-radius: 12px; padding: 20px;
        border: 1px solid #334155; text-align: center;
    }
    .metric-value { font-size: 32px; font-weight: 700; }
    .metric-label { font-size: 12px; color: #94a3b8; text-transform: uppercase; }
    .bear { color: #ef4444; } .base { color: #3b82f6; }
    .bull { color: #22c55e; } .price { color: #eab308; }
    .badge {
        display: inline-block; padding: 4px 12px; border-radius: 12px;
        font-size: 12px; font-weight: 600; margin: 2px;
    }
</style>
""", unsafe_allow_html=True)


def run_pipeline(entity_id: str, mode: str):
    """Run the research pipeline and return all artifacts."""
    from aegis.core.governance.run_manifest import generate_run_id
    from aegis.core.truth.registry.metric_registry import MetricRegistry
    from aegis.core.truth.registry.seed_metrics import seed_core_metrics
    from aegis.core.truth.formulas.formula_engine import FormulaEngine
    from aegis.core.truth.scenario_engine.dcf_engine import DCFEngine, DCFInput
    from aegis.core.truth.scenario_engine.reverse_dcf_solver import ReverseDCFSolver
    from aegis.core.macro import MacroContextLayer
    from aegis.core.market_expectations import MarketExpectationsLayer
    from aegis.core.agents.base import AgentInput
    from aegis.core.agents import (
        AccountingAnalyst, BusinessAnalyst, SectorContextAgent,
        ManagementAnalyst, ValuationAnalyst, VariantAnalyst, RiskAnalyst,
        LLMAccountingAnalyst, LLMBusinessAnalyst, LLMSectorContextAgent,
        LLMManagementAnalyst, LLMValuationAnalyst, LLMVariantAnalyst, LLMRiskAnalyst,
    )
    from aegis.core.critics import (
        LogicCritic, AccountingCritic, EvidenceCritic, SectorCritic,
        CognitiveBiasCritic, MacroConsistencyCritic, MarketCritic,
    )
    from aegis.core.publish_gate import PublishGate
    from aegis.core.decision_engine import DecisionEngine
    from aegis.core.llm import LLMConfig, LLMMode
    from aegis.data_contracts.macro_snapshot_schema import MacroSnapshot
    from aegis.data_contracts.edge_assessment_schema import EdgeAssessment

    run_id = generate_run_id()

    # ── Meta FY2024 Data ──
    meta_facts = {
        "revenue": 164_710_000_000, "cost_of_revenue": 27_928_000_000,
        "gross_profit": 136_782_000_000, "operating_income": 69_381_000_000,
        "net_income": 62_360_000_000, "ebitda": 81_537_000_000,
        "sbc": 19_007_000_000, "capex": 39_200_000_000,
        "operating_cash_flow": 91_145_000_000, "free_cash_flow": 51_945_000_000,
        "total_assets": 256_166_000_000, "total_equity": 165_862_000_000,
        "total_debt": 28_826_000_000, "cash_and_equivalents": 58_069_000_000,
        "current_assets": 75_860_000_000, "current_liabilities": 31_480_000_000,
        "diluted_shares": 2_579_000_000, "basic_shares": 2_514_000_000,
        "dap": 3_350_000_000, "map": 3_980_000_000, "arpu_global": 41.39,
        "family_of_apps_revenue": 160_826_000_000,
        "reality_labs_revenue": 3_884_000_000,
        "reality_labs_operating_loss": -17_725_000_000,
    }
    market_data = {"current_price": 585.0, "market_cap": 1_510_000_000_000, "shares_outstanding": 2_579_000_000}

    # ── Compute Metrics ──
    f = meta_facts
    engine = FormulaEngine()
    computed = {}
    for def_id, inputs in [
        ("gross_margin_v1", {"gross_profit": f["gross_profit"], "revenue": f["revenue"]}),
        ("operating_margin_v1", {"operating_income": f["operating_income"], "revenue": f["revenue"]}),
        ("net_margin_v1", {"net_income": f["net_income"], "revenue": f["revenue"]}),
        ("ebitda_margin_v1", {"ebitda": f["ebitda"], "revenue": f["revenue"]}),
        ("roe_v1", {"net_income": f["net_income"], "avg_shareholders_equity": f["total_equity"]}),
        ("roa_v1", {"net_income": f["net_income"], "avg_total_assets": f["total_assets"]}),
        ("sbc_to_revenue_v1", {"sbc": f["sbc"], "revenue": f["revenue"]}),
    ]:
        fact_ids = {k: f"fact:{k}" for k in inputs}
        r = engine.compute(definition_id=def_id, formula_version=1, entity_id=entity_id,
                           period="FY2024", period_type="duration", currency="USD",
                           inputs=inputs, input_fact_ids=fact_ids)
        if r and r.value == r.value:
            computed[def_id.replace("_v1", "")] = r.value

    computed["capex_to_revenue"] = f["capex"] / f["revenue"]
    computed["current_ratio"] = f["current_assets"] / f["current_liabilities"]
    computed["net_debt"] = f["total_debt"] - f["cash_and_equivalents"]
    computed["net_debt_to_ebitda"] = computed["net_debt"] / f["ebitda"]
    computed["enterprise_value"] = market_data["market_cap"] + computed["net_debt"]
    computed["ev_to_ebitda"] = computed["enterprise_value"] / f["ebitda"]
    computed["ev_to_revenue"] = computed["enterprise_value"] / f["revenue"]
    computed["pe_ratio"] = market_data["current_price"] / (f["net_income"] / f["diluted_shares"])
    computed["fcf_simple"] = f["free_cash_flow"]
    computed["dilution_rate"] = (f["diluted_shares"] - f["basic_shares"]) / f["basic_shares"]
    computed["accruals_ratio"] = (f["net_income"] - f["operating_cash_flow"]) / f["total_assets"]
    computed["cfo_to_net_income"] = f["operating_cash_flow"] / f["net_income"]
    computed["roic"] = f["operating_income"] * 0.79 / (f["total_equity"] + computed["net_debt"])
    computed["nwc"] = f["current_assets"] - f["current_liabilities"]
    computed["arpu"] = f["arpu_global"]
    computed["dau_mau_ratio"] = f["dap"] / f["map"]

    # ── DCF ──
    dcf_input = DCFInput(
        base_revenue=f["revenue"],
        revenue_growth_path=[0.16,0.14,0.12,0.10,0.09,0.08,0.07,0.06,0.05,0.04],
        operating_margin_path=[0.42,0.43,0.43,0.44,0.44,0.44,0.44,0.44,0.44,0.44],
        effective_tax_rate=0.14, capex_to_revenue_path=[0.24]*10,
        nwc_to_revenue_delta=0.01, wacc=0.095, terminal_growth_rate=0.03,
        shares_outstanding=f["diluted_shares"], net_debt=computed["net_debt"],
        sbc_to_revenue=0.0, dilution_rate_annual=computed["dilution_rate"], horizon_years=10,
    )
    dcf = DCFEngine()
    base_out = dcf.compute_dcf(dcf_input)
    bear_out = dcf.compute_dcf(DCFInput(**{**dcf_input.__dict__,
        "revenue_growth_path": [0.10,0.08,0.06,0.05,0.04,0.04,0.03,0.03,0.03,0.03],
        "operating_margin_path": [0.38]*10}))
    bull_out = dcf.compute_dcf(DCFInput(**{**dcf_input.__dict__,
        "revenue_growth_path": [0.20,0.18,0.16,0.14,0.12,0.10,0.09,0.08,0.07,0.06],
        "operating_margin_path": [0.45,0.46,0.47,0.47,0.47,0.47,0.47,0.47,0.47,0.47]}))

    scenarios = {"bear_value": bear_out.per_share_value, "base_value": base_out.per_share_value,
                 "bull_value": bull_out.per_share_value}

    rdcf = ReverseDCFSolver().solve_implied_growth(
        current_price=market_data["current_price"], base_revenue=dcf_input.base_revenue,
        operating_margin_path=dcf_input.operating_margin_path, capex_to_revenue_path=dcf_input.capex_to_revenue_path,
        effective_tax_rate=dcf_input.effective_tax_rate, nwc_to_revenue_delta=dcf_input.nwc_to_revenue_delta,
        terminal_growth_rate=dcf_input.terminal_growth_rate, wacc=dcf_input.wacc,
        sbc_to_revenue=dcf_input.sbc_to_revenue, dilution_rate_annual=dcf_input.dilution_rate_annual,
        shares_outstanding=dcf_input.shares_outstanding, net_debt=dcf_input.net_debt, horizon_years=dcf_input.horizon_years,
    )
    implied_growth = rdcf.implied_value

    # ── Agents ──
    sector_pack = {
        "sector_pack_id": "sp_ad_platform_v1", "sector_name": "Ad Platform / Digital Advertising",
        "key_kpis": [{"metric": "arpu", "display": "ARPU", "importance": "critical"},
                     {"metric": "dau_mau_ratio", "display": "DAU/MAU", "importance": "high"}],
        "cycle_characteristics": {"cyclicality": "moderate", "primary_driver": "Ad spend ~ GDP",
                                  "leading_indicators": ["PMI"]},
        "competitive_dynamics": {"disruption_risks": ["Privacy regulation", "TikTok"]},
        "accounting_considerations": ["SBC material (10-15% rev)"],
    }
    agent_macro = {
        "cycle_phase": "late_expansion",
        "priced_in": {"implied_revenue_growth": implied_growth, "implied_terminal_growth": 0.03,
                      "revision_momentum": "positive", "pe_ratio_fwd": computed["pe_ratio"]},
        "scenarios": scenarios, "current_price": market_data["current_price"],
        "disagreements": [{"assumption": "revenue_growth_fy26", "market_implied": f"{implied_growth:.0%}",
                           "my_view": "16%", "this_is_the_variant": True}],
    }
    evidence = [
        {"evidence_id": "ev_meta_10k_fy2024", "assertion_type": "accounting_quality",
         "assertion_text": "Meta 10-K FY2024: clean audit opinion by EY"},
        {"evidence_id": "ev_meta_ai_capex", "assertion_type": "revenue_guidance",
         "assertion_text": "Management guided $60-65B capex for 2025"},
        {"evidence_id": "ev_meta_rl_loss", "assertion_type": "risk_factor",
         "assertion_text": "Reality Labs operating loss $17.7B in FY2024"},
    ]

    base_inp = AgentInput(entity_id=entity_id, run_id=run_id, question_id="q_full",
                          metric_results=computed, macro_context=agent_macro,
                          evidence_packets=evidence, sector_pack=sector_pack)

    if mode == "rule":
        agents = [AccountingAnalyst, BusinessAnalyst, ManagementAnalyst, ValuationAnalyst, VariantAnalyst, RiskAnalyst]
        kwargs = {}
        SectorCls = SectorContextAgent
    else:
        llm_config = LLMConfig(mode=LLMMode.SUBPROCESS if mode == "subprocess" else LLMMode.MOCK)
        agents = [LLMAccountingAnalyst, LLMBusinessAnalyst, LLMManagementAnalyst,
                  LLMValuationAnalyst, LLMVariantAnalyst, LLMRiskAnalyst]
        kwargs = {"llm_config": llm_config}
        SectorCls = LLMSectorContextAgent

    judgments = []
    for Cls in agents:
        out = Cls(**kwargs).run(base_inp)
        judgments.append(out.judgment)

    sector_inp = AgentInput(entity_id=entity_id, run_id=run_id, question_id="q_sector",
                            metric_results={"arpu": f["arpu_global"], "dau_mau_ratio": computed["dau_mau_ratio"]},
                            sector_pack=sector_pack)
    judgments.append(SectorCls(**kwargs).run(sector_inp).judgment)

    # ── Critics ──
    edge_dict = {
        "edge_assessment_id": f"ea_{entity_id}", "thesis_id": f"th_{entity_id}",
        "primary_edge_type": "analytical", "edge_source": "AI capex payback timeline",
        "edge_durability": "medium_term", "edge_decay_trigger": "Sell-side AI ROIC models",
        "edge_confidence": "medium", "why_market_is_wrong": "Consensus uses simple revenue/capex ratio",
        "what_would_change_my_mind": "Management AI revenue attribution matches consensus",
        "edge_uniqueness": "moderate",
    }
    critic_ctx = {"sector_pack": sector_pack, "cycle_phase": "late_expansion",
                  "priced_in": {"implied_revenue_growth": implied_growth},
                  "edge_assessment": edge_dict, "scenarios": scenarios}

    critic_results = [Cls().review(judgments, context=critic_ctx) for Cls in
                      [LogicCritic, AccountingCritic, EvidenceCritic, SectorCritic,
                       CognitiveBiasCritic, MacroConsistencyCritic, MarketCritic]]

    gate = PublishGate().evaluate(judgments, critic_results, context={"run_manifest_id": run_id})
    edge = EdgeAssessment(**edge_dict)
    decision = DecisionEngine().decide(entity_id, run_id, judgments, critic_results, gate.publishable,
        context={"edge_assessment": edge, "scenarios": scenarios,
                 "macro_dependency": "US late-cycle", "sector_cycle_position": "Moderate cyclicality"})

    return {
        "meta_facts": meta_facts, "market_data": market_data, "computed": computed,
        "scenarios": scenarios, "implied_growth": implied_growth,
        "judgments": judgments, "critic_results": critic_results,
        "gate": gate, "decision": decision, "edge": edge, "run_id": run_id,
        "dcf_projections": base_out.projections,
    }


# ══════════════════════════ SIDEBAR ══════════════════════════
with st.sidebar:
    st.title("🛡️ Aegis Research OS")
    st.divider()
    entity = st.selectbox("Entity", ["meta_platforms"], index=0)
    mode = st.radio("Agent Mode", ["rule", "mock", "subprocess"],
                    format_func=lambda x: {"rule": "📐 Rule-based", "mock": "🤖 LLM (Mock)", "subprocess": "🚀 LLM (Claude)"}[x])
    run_btn = st.button("▶️ Run Analysis", type="primary", use_container_width=True)
    st.divider()
    st.caption("v0.1.0 · 143 tests passing")

# ══════════════════════════ MAIN ══════════════════════════
if run_btn:
    with st.spinner("Running full pipeline..."):
        data = run_pipeline(entity, mode)
    st.session_state["data"] = data

if "data" not in st.session_state:
    st.title("Aegis Research OS")
    st.info("👈 Select an entity and click **Run Analysis** to start")
    st.stop()

data = st.session_state["data"]
d = data["decision"]
m = data["computed"]
s = data["scenarios"]
facts = data["meta_facts"]

# ── Header ──
col1, col2, col3, col4 = st.columns(4)
col1.metric("Status", d.publishing_status.upper())
col2.metric("Confidence", d.confidence_bucket.upper())
col3.metric("Bias Check", d.bias_check_status.upper())
col4.metric("Conflicts", len(d.unresolved_conflicts))

st.divider()

# ══════════════════════════ TABS ══════════════════════════
tab1, tab2, tab3, tab4 = st.tabs(["📊 Overview", "🤖 Agents", "🔍 Critics", "⚠️ Risk Monitor"])

# ── TAB 1: Overview ──
with tab1:
    # Valuation cards
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🔴 Bear Case", f"${s['bear_value']:.0f}")
    c2.metric("🔵 Base Case", f"${s['base_value']:.0f}")
    c3.metric("🟢 Bull Case", f"${s['bull_value']:.0f}")
    c4.metric("🟡 Current Price", f"${data['market_data']['current_price']:.0f}")

    col_l, col_r = st.columns(2)

    with col_l:
        # Scenario bar chart
        fig_sc = go.Figure()
        fig_sc.add_bar(x=["Bear", "Base", "Bull"],
                       y=[s["bear_value"], s["base_value"], s["bull_value"]],
                       marker_color=["#ef4444", "#3b82f6", "#22c55e"])
        fig_sc.add_hline(y=data["market_data"]["current_price"], line_dash="dash",
                         line_color="#eab308", annotation_text=f"Price: ${data['market_data']['current_price']:.0f}")
        fig_sc.update_layout(title="Scenario Valuation vs Price", template="plotly_dark",
                             height=350, yaxis_title="$/share", paper_bgcolor="#0f172a", plot_bgcolor="#1e293b")
        st.plotly_chart(fig_sc, use_container_width=True)

    with col_r:
        # Radar chart
        categories = ["Gross Margin", "Op Margin", "Net Margin", "ROIC", "ROE"]
        values = [m.get("gross_margin",0)*100, m.get("operating_margin",0)*100,
                  m.get("net_margin",0)*100, m.get("roic",0)*100, m.get("roe",0)*100]
        fig_radar = go.Figure()
        fig_radar.add_trace(go.Scatterpolar(r=values + [values[0]], theta=categories + [categories[0]],
                                            fill='toself', fillcolor='rgba(59,130,246,0.15)',
                                            line_color='#3b82f6', name=entity))
        fig_radar.update_layout(title="Key Metrics Radar", template="plotly_dark", height=350,
                                polar=dict(radialaxis=dict(visible=True, range=[0, 100])),
                                paper_bgcolor="#0f172a", plot_bgcolor="#1e293b")
        st.plotly_chart(fig_radar, use_container_width=True)

    # Executive Summary
    st.subheader("Executive Summary")
    st.markdown(f"**Core Thesis:** {d.core_thesis}")
    st.markdown(f"**Variant:** {d.my_variant}")
    st.markdown(f"**Counter Thesis:** {d.counter_thesis}")

    # Key financials
    st.subheader("Key Financials (FY2024)")
    fc1, fc2, fc3, fc4, fc5 = st.columns(5)
    fc1.metric("Revenue", f"${facts['revenue']/1e9:.1f}B")
    fc2.metric("Net Income", f"${facts['net_income']/1e9:.1f}B")
    fc3.metric("FCF", f"${facts['free_cash_flow']/1e9:.1f}B")
    fc4.metric("P/E", f"{m.get('pe_ratio',0):.1f}x")
    fc5.metric("EV/EBITDA", f"{m.get('ev_to_ebitda',0):.1f}x")

    # Edge Assessment
    st.subheader("Edge Assessment")
    ec1, ec2 = st.columns(2)
    edge = data["edge"]
    ec1.markdown(f"**Type:** {edge.primary_edge_type.value}")
    ec1.markdown(f"**Durability:** {edge.edge_durability.value}")
    ec2.markdown(f"**Why Market Wrong:** {edge.why_market_is_wrong}")
    ec2.markdown(f"**Decay Trigger:** {edge.edge_decay_trigger}")

# ── TAB 2: Agent Analysis ──
with tab2:
    for j in data["judgments"]:
        with st.expander(f"🤖 {j.agent_name.replace('_', ' ').title()} — {len(j.observations)} obs, {len(j.inferences)} inf"):
            st.markdown("**Observations:**")
            for obs in j.observations:
                st.markdown(f"- {obs.text}")
            st.markdown("**Inferences:**")
            for inf in j.inferences:
                emoji = "🟢" if inf.confidence == "high" else "🟡" if inf.confidence == "medium" else "🔴"
                st.markdown(f"- {emoji} [{inf.confidence}] {inf.text}")
            if j.counterarguments:
                st.markdown("**Counterarguments:**")
                for ca in j.counterarguments:
                    st.markdown(f"- *[{ca.strength}]* {ca.text}")

# ── TAB 3: Critic Review ──
with tab3:
    for cr in data["critic_results"]:
        label = cr.critic_type.replace("_", " ").title()
        status_emoji = "🔴" if cr.block_publish else "🟡" if cr.overall_risk == "medium" else "🟢"
        with st.expander(f"{status_emoji} {label} — {len(cr.issues)} issues ({cr.overall_risk} risk)"):
            if not cr.issues:
                st.success("No issues found")
            for iss in cr.issues:
                sev_color = {"block": "🔴", "warn": "🟡", "info": "🔵"}.get(iss.severity.value, "⚪")
                st.markdown(f"{sev_color} **{iss.issue_code}**: {iss.message}")
                if iss.recommended_action:
                    st.caption(f"→ {iss.recommended_action}")

    # Gate results
    st.subheader("Publish Gate")
    for check in data["gate"].checks:
        emoji = "✅" if check.passed else "❌"
        st.markdown(f"{emoji} **{check.gate_name}**: {check.message}")

# ── TAB 4: Risk Monitor ──
with tab4:
    st.subheader("Kill Criteria")
    for kc in d.kill_criteria:
        desc = kc.get("description", str(kc)) if isinstance(kc, dict) else str(kc)
        st.error(f"⛔ {desc}")

    st.subheader("Monitorables")
    for mon in d.monitorables[:10]:
        desc = mon.get("description", str(mon)) if isinstance(mon, dict) else str(mon)
        freq = mon.get("check_frequency", "") if isinstance(mon, dict) else ""
        st.warning(f"👁 {desc} ({freq})")

    st.subheader("Fragility Points")
    for fp in d.fragility_points[:8]:
        st.info(f"💎 {fp}")
