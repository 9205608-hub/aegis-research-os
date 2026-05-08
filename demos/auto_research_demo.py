"""Aegis Research OS — Auto Research Demo.

End-to-end automated research: ticker → EDGAR fetch → analysis → report.
No hardcoded financial data — everything flows from SEC EDGAR XBRL.

Usage:
  python demos/auto_research_demo.py META                      # Default (FY2024, no price)
  python demos/auto_research_demo.py META --price 585           # With market price
  python demos/auto_research_demo.py AAPL --period FY2023       # Different period
  python demos/auto_research_demo.py META --price 585 --sector sp_ad_platform_v1

Note: Requires internet access to fetch from SEC EDGAR API.
      Set SEC_USER_AGENT="Your Name your@email.com" for SEC fair access.
"""

import argparse
import os
import sys
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Aegis Auto Research — ticker to report")
    parser.add_argument("ticker", help="Stock ticker (e.g., META, AAPL, GOOGL)")
    parser.add_argument("--period", default="latest",
                        help="Fiscal period (default: 'latest' = auto-detect most recent 10-K). "
                             "Or specify e.g. 'FY2025' to pin a specific year.")
    parser.add_argument("--price", type=float, default=None, help="Current stock price")
    parser.add_argument("--market-cap", type=float, default=None, help="Market cap in USD")
    parser.add_argument("--sector", default=None, help="Sector pack ID (e.g., sp_ad_platform_v1)")
    parser.add_argument("--wacc", type=float, default=0.095, help="WACC (default: 0.095)")
    parser.add_argument("--tg", type=float, default=0.03, help="Terminal growth rate (default: 0.03)")
    parser.add_argument("--no-html", action="store_true", help="Skip HTML report generation")
    parser.add_argument("--output-dir", default=None, help="Output directory for reports")
    parser.add_argument("--llm", action="store_true", help="Use LLM-powered agents (auto-detects best backend)")
    parser.add_argument("--model", default="sonnet", help="LLM model: sonnet, opus, haiku (default: sonnet)")
    parser.add_argument("--backend", default="auto", choices=["auto", "sdk", "subprocess", "kimi", "deepseek"],
                        help="LLM backend: auto (default), sdk (Anthropic API), subprocess (claude CLI), kimi (Moonshot), deepseek (DeepSeek)")
    parser.add_argument("--kimi-key", default=None, help="Kimi API key (or set KIMI_API_KEY env var)")
    parser.add_argument("--kimi-model", default="k2.6", help="Kimi model: k2.6 (default, latest 2026-04-13), k2.5, moonshot-v1-8k/32k/128k")
    parser.add_argument("--deepseek-key", default=None, help="DeepSeek API key (or set DEEPSEEK_API_KEY env var)")
    parser.add_argument("--deepseek-model", default="deepseek-v4-pro", help="DeepSeek model: deepseek-v4-pro (default, flagship), deepseek-v4-flash (faster/cheaper)")
    parser.add_argument("--fast-agents", action="store_true",
                        help="Use cheaper model (k2.5) for specialist agents, keep k2.6 for synthesizer/editor")
    parser.add_argument("--fast-agent-model", default="k2.5",
                        help="Model for specialist agents in fast mode (default: k2.5)")
    parser.add_argument("--fast", action="store_true",
                        help="Fast pipeline: override Director's DEEP designations to standard "
                             "(skips narrative_supplement) and runs iterative re-analysis at "
                             "standard depth. Cuts wall-clock from ~40 min → ~12 min on V4-pro. "
                             "Use for iteration / debugging; full reports still benefit from DEEP.")
    parser.add_argument("--split-prompts", action="store_true",
                        help="Split each agent's single LLM call into two shorter calls "
                             "(observations then inferences). Reduces per-call thinking overhead "
                             "on long A-share DEEP prompts. Doubles round-trips, so net benefit "
                             "depends on how output-thinking-bound each call is.")
    parser.add_argument("--fmp-key", default=None, help="FMP API key for consensus estimates (or set FMP_API_KEY env var)")
    parser.add_argument("--fred-key", default=None, help="FRED API key for macro data (or set FRED_API_KEY env var)")
    parser.add_argument("--no-openbb", action="store_true", help="Disable OpenBB data enrichment")
    parser.add_argument("--strict-llm", action="store_true",
                        help="Fail hard if any agent's LLM exhausts retries (no silent mock fallback). "
                             "Use for production runs where mock-mixed output is worse than no report.")
    parser.add_argument("--smoke", action="store_true",
                        help="Smoke-test mode: skip ALL LLM calls (rule-based agents only), still run "
                             "data fetch / DCF / HTML render. Targets <5 min completion to validate "
                             "non-LLM bug fixes without paying the 25-75 min LLM pipeline cost.")
    args = parser.parse_args()

    # TODO-1 (2026-04-24): smoke mode forces non-LLM paths for fast iteration.
    if args.smoke:
        if args.llm:
            print("  [SMOKE] --smoke overrides --llm; LLM agents disabled.", file=sys.stderr)
        args.llm = False
        if args.strict_llm:
            print("  [SMOKE] --strict-llm has no effect in smoke mode.", file=sys.stderr)
            args.strict_llm = False

    from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator, ResearchConfig

    config = ResearchConfig(
        ticker=args.ticker.upper(),
        period=args.period,
        current_price=args.price,
        market_cap=args.market_cap,
        sector_pack_id=args.sector,
        wacc=args.wacc,
        terminal_growth_rate=args.tg,
        generate_html=not args.no_html,
        output_dir=args.output_dir,
        sec_user_agent=os.environ.get("SEC_USER_AGENT") or os.environ.get("EDGAR_USER_AGENT"),
        use_llm=args.llm,
        llm_model=args.model,
        llm_backend=args.backend,
        kimi_api_key=args.kimi_key,
        kimi_model=args.kimi_model,
        deepseek_api_key=args.deepseek_key,
        deepseek_model=args.deepseek_model,
        fast_agents=args.fast_agents,
        fast_agent_model=args.fast_agent_model,
        fast_pipeline=args.fast,
        split_prompts=args.split_prompts,
        fmp_api_key=args.fmp_key,
        fred_api_key=args.fred_key,
        enable_openbb=not args.no_openbb,
        strict_llm=args.strict_llm,
        smoke_mode=args.smoke,
    )

    print(f"{'='*70}")
    if args.smoke:
        print(f"  AEGIS AUTO RESEARCH — {config.ticker} {config.period}  [SMOKE MODE]")
        print(f"  ⚠ rule-based agents only — report content is for plumbing validation, not analysis")
    else:
        print(f"  AEGIS AUTO RESEARCH — {config.ticker} {config.period}")
    print(f"{'='*70}")
    print(f"  Price: ${config.current_price}" if config.current_price else "  Price: not provided")
    print(f"  WACC: {config.wacc:.1%}  TG: {config.terminal_growth_rate:.1%}")
    if config.use_llm:
        print(f"  LLM Mode: {config.llm_model} (backend: {config.llm_backend})")
        if config.fast_agents:
            print(f"  Fast Agents: {config.fast_agent_model} (premium: {config.kimi_model})")
    print()

    orchestrator = AutoResearchOrchestrator()

    try:
        result = orchestrator.run(config)
    except Exception as e:
        print(f"\n  ERROR: {e}")
        print(f"\n  Troubleshooting:")
        print(f"    - Check internet connectivity (SEC EDGAR requires HTTP access)")
        print(
            "    - Set SEC_USER_AGENT env var: "
            "export SEC_USER_AGENT='Name email@example.com'"
        )
        print(f"    - Verify ticker is in SEC Entity Registry")
        sys.exit(1)

    # Print results
    print(f"\n{'='*70}")
    print(f"  RESULTS")
    print(f"{'='*70}")
    print(f"  Entity:           {result.entity_id}")
    print(f"  Run ID:           {result.run_id}")
    print(f"  Meta Facts:       {len(result.meta_facts)} fields")
    print(f"  Metrics Computed: {len(result.computed_metrics)}")
    print()

    # Key financials
    f = result.meta_facts
    # Refactor 2 (2026-05-04): consume the structured __display context
    # written by fact_bridge so we don't re-derive currency rules here.
    # Falls back to USD defaults for caches predating the refactor.
    _disp = (f.get("__display") or {}) if isinstance(f.get("__display"), dict) else {}
    _csym = _disp.get("symbol") or ("¥" if f.get("__currency") == "CNY" else "$")
    _scale = _disp.get("scale") or (1e8 if f.get("__currency") == "CNY" else 1e9)
    _unit = _disp.get("unit") or ("亿" if f.get("__currency") == "CNY" else "B")
    print(f"  --- Key Financials ---")
    for label, key in [
        ("Revenue", "revenue"),
        ("Net Income", "net_income"),
        ("Operating Income", "operating_income"),
        ("Free Cash Flow", "free_cash_flow"),
        ("Total Assets", "total_assets"),
        ("Cash", "cash_and_equivalents"),
        ("Total Debt", "total_debt"),
    ]:
        val = f.get(key)
        if val is not None:
            print(f"  {label:>20}: {_csym}{val/_scale:.1f}{_unit}")

    # Key metrics
    m = result.computed_metrics
    # Refactor 3 (2026-05-04): orchestrator's _compute_metrics omits ratios
    # that are mathematically meaningless (P/E w/ negative earnings,
    # EV/EBITDA w/ EBITDA ≤ 0, Net-Debt/EBITDA w/ EBITDA ≤ 0). Renderer
    # just skips the row when the key isn't present — no n/m branching.
    print(f"\n  --- Key Metrics ---")
    for label, key, fmt in [
        ("Gross Margin", "gross_margin", "{:.1%}"),
        ("Operating Margin", "operating_margin", "{:.1%}"),
        ("Net Margin", "net_margin", "{:.1%}"),
        ("ROE", "roe", "{:.1%}"),
        ("ROIC", "roic", "{:.1%}"),
        ("Net Debt/EBITDA", "net_debt_to_ebitda", "{:.1f}x"),
        ("P/E Ratio", "pe_ratio", "{:.1f}x"),
        ("EV/EBITDA", "ev_to_ebitda", "{:.1f}x"),
    ]:
        val = m.get(key)
        if val is None:
            continue
        print(f"  {label:>20}: {fmt.format(val)}")

    # Valuation
    print(f"\n  --- Valuation ---")
    if result.dcf_per_share is None or result.dcf_per_share <= 0:
        print(f"  DCF Base Value:   n/m ({_csym}{result.dcf_per_share:.2f} — DCF 对持续亏损公司无意义)")
    else:
        print(f"  DCF Base Value:   {_csym}{result.dcf_per_share:.2f}/share")
    bear_v = result.scenarios['bear_value']; base_v = result.scenarios['base_value']; bull_v = result.scenarios['bull_value']
    print(f"  Scenario Range:   {_csym}{bear_v:.2f} / {_csym}{base_v:.2f} / {_csym}{bull_v:.2f}")
    if result.implied_growth:
        # BUG-Y13 follow-up: when ReverseDCFSolver hit a boundary, the value
        # is meaningless (e.g. 50.00% = exact growth_high). Show n/a + reason
        # rather than a fake-clean number.
        if f.get("__implied_growth_unreliable"):
            print(f"  Implied Growth:   n/a (reverse-DCF non-monotonic; bisection hit "
                  f"{f.get('__implied_growth_boundary_hit', '?')} bound)")
        else:
            print(f"  Implied Growth:   {result.implied_growth:.2%}")

    # Decision
    print(f"\n  --- Decision ---")
    print(f"  Status:           {result.decision.publishing_status}")
    print(f"  Confidence:       {result.decision.confidence_bucket}")
    print(f"  Kill Criteria:    {len(result.decision.kill_criteria)}")
    print(f"  Monitorables:     {len(result.decision.monitorables)}")

    # Signal
    print(f"\n  --- Portfolio Signal ---")
    print(f"  Direction:        {result.signal.direction}")
    print(f"  Conviction:       {result.signal.conviction}")
    print(f"  Sizing Tier:      {result.signal.sizing_tier}")

    # OpenBB data
    if result.consensus_estimates:
        print(f"\n  --- Consensus Estimates ({len(result.consensus_estimates)} items) ---")
        # BUG-Y15 (2026-05-06): consensus print used to hardcode `$X.XB`
        # regardless of currency. For A-shares yfinance/OpenBB returns CNY-
        # denominated raw values that, when formatted with `$X.XB`, look like
        # huge USD numbers (Cambricon FY_Current consensus shown as $23.7B
        # was actually ¥237亿). Use the same _csym/_scale/_unit derived from
        # __display further up so the formatting is consistent with the Key
        # Financials block above.
        for est in result.consensus_estimates[:6]:
            v = est.consensus_mean
            if abs(v) > 1e6:
                disp_val = f"{_csym}{v/_scale:.1f}{_unit}"
            else:
                disp_val = f"{_csym}{v:.2f}"
            print(f"  {est.metric:>10} {est.period}: {disp_val} ({est.analyst_count} analysts)")
    if result.earnings_history:
        print(f"\n  --- Earnings History ({len(result.earnings_history)} quarters) ---")
        for eh in result.earnings_history[:4]:
            surp = eh.eps_surprise_pct
            tag = f" ({surp:+.1%})" if surp is not None else ""
            print(f"  {eh.report_date[:10]}: EPS ${eh.eps_actual:.2f} vs ${eh.eps_consensus:.2f}{tag}" if eh.eps_actual and eh.eps_consensus else f"  {eh.report_date[:10]}: data incomplete")
    if result.peer_fundamentals:
        print(f"\n  --- Peer Comparison ({len(result.peer_fundamentals)} peers) ---")
        for p in result.peer_fundamentals[:5]:
            name = p.name or p.symbol
            print(f"  {name[:15]:>15}: GM={p.gross_margin*100:.1f}% OM={p.operating_margin*100:.1f}% PE={p.pe_trailing:.1f}x" if p.pe_trailing else f"  {name[:15]:>15}: GM={p.gross_margin*100:.1f}% OM={p.operating_margin*100:.1f}%")

    # Warnings
    if result.bridge_warnings:
        print(f"\n  --- Warnings ---")
        for w in result.bridge_warnings:
            print(f"  ! {w}")

    # Output
    if result.html_path:
        print(f"\n  HTML Report:      {result.html_path}")
        print(f"  Open in browser:  file://{Path(result.html_path).resolve()}")

    # Pipeline log
    print(f"\n  --- Pipeline Log ---")
    for entry in result.pipeline_log:
        print(f"  {entry}")

    print(f"\n{'='*70}")
    print(f"  PIPELINE COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
