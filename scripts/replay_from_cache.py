"""Replay pipeline from cached state — fast iteration for downstream logic.

Loads the pickle dumped by orchestrator after all expensive LLM steps complete,
then re-runs only the cheap business logic (PublishGate, DecisionEngine,
PortfolioIntegration, ReportEditor, HTML report) with fresh code from disk.

Use this when debugging:
- PublishGate thresholds and gate logic
- DecisionEngine confidence scoring
- Report Editor formatting
- HTML report rendering

Typical iteration cycle: ~2-5 seconds (vs ~25 minutes for full pipeline).

Usage:
    python scripts/replay_from_cache.py NVDA              # default, skips editor
    python scripts/replay_from_cache.py NVDA --editor     # re-run editor (~2 min)
    python scripts/replay_from_cache.py NVDA --verbose    # dump all warnings
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Sensitivity recompute helpers (HANDOFF 待办: replay 敏感性表缺参)
#
# These mirror orchestrator Step 7 (auto_research.py) so a replayed report
# carries the same sensitivity artifacts a fresh run would. Kept at module
# level so unit tests can exercise them without a full state pickle.
# ---------------------------------------------------------------------------

def _centered_range(center: float, step: float, width: int,
                    floor: float | None = None,
                    ceiling: float | None = None) -> list[float]:
    """Symmetric value grid around `center` — identical to auto_research.py."""
    values = []
    for i in range(-width, width + 1):
        v = center + i * step
        if floor is not None:
            v = max(v, floor)
        if ceiling is not None:
            v = min(v, ceiling)
        values.append(round(v, 4))
    return list(dict.fromkeys(values))


def _recompute_sensitivity_flat(dcf_input_flat) -> tuple[list[dict], dict | None]:
    """Recompute sensitivity rankings + WACC × TGR two-way table (flat DCF).

    BUG (HANDOFF: replay 敏感性表缺参): the old inline code called
    `sa.two_way_table(dcf_input_flat, "wacc", "terminal_growth_rate")` —
    missing both required range arguments (real signature:
    two_way_table(base_inputs, var1_name, var1_range, var2_name, var2_range)).
    The resulting TypeError was swallowed by a bare `except: pass`, so the
    STALE cached table survived every replay while dcf_output was recomputed
    → dcf_integrity_gate mismatch/skip → confidence loss. It also assigned
    the raw SensitivityTable dataclass on success paths elsewhere, which
    html_report_v2 (isinstance dict check) silently drops.

    Returns (rankings, table_dict). `table_dict` is None when the two-way
    table could not be computed — caller should keep the cached one.
    """
    from aegis.core.truth.scenario_engine.sensitivity_analyzer import SensitivityAnalyzer

    sa = SensitivityAnalyzer()
    rankings = [
        {"assumption": r.assumption, "impact_pct": r.impact_pct,
         "signed_impact_pct": getattr(r, "signed_impact_pct", r.impact_pct),
         "base_per_share": r.base_per_share,
         "shocked_per_share": r.shocked_per_share}
        for r in sa.rank_assumptions(dcf_input_flat)
    ]
    table: dict | None = None
    try:
        # Same grids as orchestrator Step 7: WACC ±3×0.5pp (floor 3%),
        # terminal growth ±2×0.5pp (floor 0, ceiling wacc − 0.5pp).
        wacc_range = _centered_range(dcf_input_flat.wacc, 0.005, 3, floor=0.03)
        terminal_growth_range = _centered_range(
            dcf_input_flat.terminal_growth_rate, 0.005, 2,
            floor=0.0, ceiling=max(dcf_input_flat.wacc - 0.005, 0.0),
        )
        two_way = sa.two_way_table(
            dcf_input_flat,
            "wacc", wacc_range,
            "terminal_growth_rate", terminal_growth_range,
        )
        # Dict shape mirrors auto_research.py — required by html_report_v2,
        # the publish gate's dcf_integrity_gate, and the decision contract.
        table = {
            "variable_1": two_way.variable_1,
            "variable_2": two_way.variable_2,
            "var1_values": two_way.var1_values,
            "var2_values": two_way.var2_values,
            "matrix": two_way.matrix,
        }
    except Exception as exc:
        print(f"  [recompute] ⚠ two-way sensitivity table failed: {exc}")
    return rankings, table


def _scale_sensitivity_for_segment(rankings, table, share_ratio: float,
                                   ) -> tuple[list[dict], dict | None]:
    """Scale cached sensitivity artifacts to a new per-share base (segment DCF).

    Segment-DCF tickers keep their cached table (the flat input has different
    growth assumptions) and only rescale dollar values. None cells in the
    matrix (infeasible WACC/g combos, AUDIT 2026-07) must be preserved — the
    old in-place code did `cell * share_ratio` unconditionally and crashed
    the whole recompute block with a TypeError on the first None cell.
    """
    scaled_rankings = [
        {**r,
         "base_per_share": (r.get("base_per_share") or 0) * share_ratio,
         "shocked_per_share": (r.get("shocked_per_share") or 0) * share_ratio}
        for r in (rankings or [])
    ]
    scaled_table = table
    if isinstance(table, dict) and table.get("matrix"):
        scaled_table = {
            **table,
            "matrix": [
                [None if cell is None else round(cell * share_ratio, 2)
                 for cell in row]
                for row in table["matrix"]
            ],
        }
    return scaled_rankings, scaled_table


def _build_gate_context(state: dict) -> dict:
    """Build the PublishGate context exactly like orchestrator Step 12.

    The old replay passed only {"run_manifest_id"} — every integrity gate
    (dcf_integrity / terminal_value / valuation_sanity / capex_attribution)
    then "skipped: missing inputs". Skipped gates feed gate_skipped_names →
    DecisionEngine._determine_confidence caps the bucket at medium (AUDIT
    2026-07-12 B4), so replayed reports diverged from fresh runs: the gates
    never actually ran, and any faithful skip accounting would have cost
    confidence for a purely replay-side omission.
    """
    meta_facts = state.get("meta_facts") or {}
    return {
        "run_manifest_id": state.get("run_id"),
        "__data_quality_issues": meta_facts.get("__data_quality_issues", []),
        "meta_facts": meta_facts,
        "computed_metrics": state.get("computed_metrics"),
        "market_data": state.get("market_data"),
        "segment_detail": state.get("segment_detail"),
        "segment_projections": state.get("segment_projections_data"),
        "scenarios": state.get("scenarios"),
        "dcf_input": state.get("dcf_input_flat"),
        "dcf_output": state.get("dcf_output"),
        "sensitivity_table": state.get("sensitivity_table"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay pipeline from cache")
    parser.add_argument("ticker", help="Stock ticker (must have cached state)")
    parser.add_argument("--editor", action="store_true",
                        help="Re-run Report Editor LLM call (~2 min). Default skips.")
    parser.add_argument("--verbose", action="store_true",
                        help="Print all warnings and critic issues")
    parser.add_argument("--cache-dir", default=".cache",
                        help="Cache directory (default: .cache)")
    parser.add_argument("--allow-stale", action="store_true",
                        help="Bypass stale-cache guard (NOT recommended)")
    parser.add_argument("--rerun-critics", action="store_true",
                        help="Re-run critics against cached judgments with fresh critic code. "
                             "Use when debugging critic logic without re-running agents.")
    args = parser.parse_args()

    cache_file = Path(args.cache_dir) / f"{args.ticker.lower()}_replay_state.pkl"
    if not cache_file.exists():
        print(f"❌ No cache found at {cache_file}")
        print(f"   Run full pipeline first: python demos/auto_research_demo.py {args.ticker} --price 110 --llm --backend deepseek")
        return 1

    t0 = time.time()
    print(f"{'='*70}")
    print(f"  REPLAY FROM CACHE — {args.ticker.upper()}")
    print(f"{'='*70}")
    print(f"  Cache file: {cache_file} ({cache_file.stat().st_size // 1024}KB)")

    # ── Stale cache guard ──
    # If any orchestrator/DCF/agent file has been modified AFTER this cache
    # was written, the replay will produce results based on stale upstream
    # state (segments, scenarios, dcf_output) — masking real bug fixes.
    cache_mtime = cache_file.stat().st_mtime
    project_root = Path(__file__).resolve().parent.parent
    watched = [
        project_root / "aegis" / "core" / "orchestrator" / "auto_research.py",
        project_root / "aegis" / "core" / "dcf",
        project_root / "aegis" / "core" / "chief_analyst" / "thesis_synthesizer.py",
        project_root / "aegis" / "core" / "chief_analyst" / "scenario_architect.py",
        project_root / "aegis" / "core" / "agents",
    ]
    # Files that only affect post-cache rendering (HTML, publish gate, critics,
    # connectors) should not trigger staleness — the cached data is still valid.
    render_only_whitelist = {
        "html_report.py", "gate.py", "__init__.py", "replay_from_cache.py",
        "catalyst_calendar.py", "cninfo_connector.py", "akshare_connector.py",
        "openbb_connector.py",
    }
    newer: list[tuple[Path, float]] = []
    for w in watched:
        if not w.exists():
            continue
        if w.is_file():
            if w.name in render_only_whitelist:
                continue
            mt = w.stat().st_mtime
            if mt > cache_mtime:
                newer.append((w, mt))
        else:
            for p in w.rglob("*.py"):
                if p.name in render_only_whitelist:
                    continue
                mt = p.stat().st_mtime
                if mt > cache_mtime:
                    newer.append((p, mt))
    if newer:
        import datetime as _dt
        cache_str = _dt.datetime.fromtimestamp(cache_mtime).strftime("%Y-%m-%d %H:%M")
        print()
        print(f"  ⚠️  STALE CACHE — {len(newer)} upstream file(s) modified after "
              f"cache ({cache_str}):")
        for p, mt in sorted(newer, key=lambda x: -x[1])[:5]:
            ts = _dt.datetime.fromtimestamp(mt).strftime("%H:%M:%S")
            rel = p.relative_to(project_root)
            print(f"      {ts}  {rel}")
        if len(newer) > 5:
            print(f"      ... and {len(newer) - 5} more")
        print()
        print("  Replay will run on STALE upstream state. Results (scenarios,")
        print("  dcf_output, segments) reflect the OLD code path, not current.")
        if not args.allow_stale:
            print()
            print("  Refusing to replay. Re-run full pipeline to refresh cache:")
            print(f"      ./run_research.sh {args.ticker}")
            print(f"  Or pass --allow-stale to bypass this guard.")
            return 2
        else:
            print("  --allow-stale set, continuing anyway...")
        print()

    with cache_file.open("rb") as f:
        state = pickle.load(f)
    print(f"  Loaded state in {time.time()-t0:.2f}s")

    # ── Cache backfills for fields added after the cache was written ──
    # P/E TTM unification (2026-04-15): orchestrator now stores
    # `computed_metrics["pe_ratio_ttm"]` from historical_valuation.pe_stats.
    # Old caches don't have it; backfill from the cached historical_valuation
    # so the peer comparison and Key Financials show the corrected value.
    cm = state.get("computed_metrics", {}) or {}
    if "pe_ratio_ttm" not in cm:
        hv = state.get("historical_valuation") or {}
        ttm = (hv.get("pe_stats") or {}).get("current") if isinstance(hv, dict) else None
        if ttm and ttm > 0:
            cm["pe_ratio_ttm"] = ttm
            state["computed_metrics"] = cm
            pe_fy = cm.get("pe_ratio", 0)
            print(f"  [backfill] pe_ratio_ttm={ttm:.1f}x "
                  f"(was missing; FY-static was {pe_fy:.1f}x)")

    # BUG-23 backfill: pe_stats["current"] was computed from sorted(series)[-1]
    # (i.e. the MAX), not the latest time-series value. Fix by re-deriving
    # "current" from the actual pe_ratio series' last valid entry.
    hv = state.get("historical_valuation") or {}
    for stats_key, series_key in [("pe_stats", "pe_ratio"), ("ev_ebitda_stats", "ev_ebitda")]:
        stats = hv.get(stats_key)
        series = hv.get(series_key, [])
        if stats and series:
            latest = 0.0
            for v in reversed(series):
                if v and float(v) > 0 and float(v) < 500:
                    latest = float(v)
                    break
            if latest > 0 and abs(latest - stats.get("current", 0)) > 0.5:
                old_cur = stats.get("current", 0)
                stats["current"] = round(latest, 1)
                hv[stats_key] = stats
                print(f"  [backfill] {stats_key}[current]: {old_cur:.1f}x → {latest:.1f}x (was sorted max, now latest)")
    state["historical_valuation"] = hv

    # D&A component derivation (2026-04-15): fact_bridge now derives
    # `depreciation_amortization` from `depreciation` + `amortization` when
    # the combined field is missing (Alphabet-style filings). Backfill the
    # same logic into cached meta_facts so old caches stop reporting D&A=0.
    mf = state.get("meta_facts") or {}
    if mf and not mf.get("depreciation_amortization"):
        dep = mf.get("depreciation") or 0
        amort = mf.get("amortization") or 0
        if dep or amort:
            mf["depreciation_amortization"] = dep + amort
            state["meta_facts"] = mf
            print(f"  [backfill] depreciation_amortization={dep+amort:,.0f} "
                  f"(derived from depreciation={dep:,.0f}+amortization={amort:,.0f})")

    # Refactor 2 backfill (2026-05-04): __display block (currency / scale /
    # unit / symbol) is now written by fact_bridge. Old caches don't have
    # it; backfill from `__currency` so the renderer's display_ctx lookup
    # works without per-renderer fallbacks.
    mf_pre = state.get("meta_facts") or {}
    if isinstance(mf_pre, dict) and "__display" not in mf_pre and mf_pre.get("__currency"):
        _curr = mf_pre.get("__currency")
        _DISPLAY_TABLE = {
            "CNY": {"symbol": "¥", "scale": 1e8, "unit": "亿",
                    "big_scale": 1e12, "big_unit": "万亿"},
            "USD": {"symbol": "$", "scale": 1e9, "unit": "B",
                    "big_scale": 1e12, "big_unit": "T"},
            "EUR": {"symbol": "€", "scale": 1e9, "unit": "B",
                    "big_scale": 1e12, "big_unit": "T"},
            "GBP": {"symbol": "£", "scale": 1e9, "unit": "B",
                    "big_scale": 1e12, "big_unit": "T"},
            "JPY": {"symbol": "¥", "scale": 1e8, "unit": "億",
                    "big_scale": 1e12, "big_unit": "兆"},
        }
        mf_pre["__display"] = dict(_DISPLAY_TABLE.get(_curr, _DISPLAY_TABLE["USD"]))
        mf_pre["__display"]["currency"] = _curr
        state["meta_facts"] = mf_pre
        print(f"  [backfill] __display block from __currency={_curr}")

    # Refactor 3 backfill (2026-05-04): old caches stored EBITDA-based
    # multiples computed from negative EBITDA / negative earnings. The
    # orchestrator now omits those keys entirely; legacy caches still
    # have them, so the renderer would happily display "-82.7×" again.
    # Strip the offending keys here so replay matches a fresh run.
    cm = state.get("computed_metrics", {}) or {}
    mf = state.get("meta_facts", {}) or {}
    if cm and mf:
        _ebitda = mf.get("ebitda")
        _ni = mf.get("net_income")
        _scrubbed = []
        if (_ebitda is None or _ebitda <= 0):
            for k in ("ev_to_ebitda", "net_debt_to_ebitda"):
                if k in cm:
                    cm.pop(k, None)
                    _scrubbed.append(k)
        if cm.get("pe_ratio", 0) and cm["pe_ratio"] < 0:
            cm.pop("pe_ratio", None)
            _scrubbed.append("pe_ratio")
        if cm.get("pe_ratio_ttm", 0) and cm["pe_ratio_ttm"] < 0:
            cm.pop("pe_ratio_ttm", None)
            _scrubbed.append("pe_ratio_ttm")
        if _scrubbed:
            state["computed_metrics"] = cm
            print(f"  [backfill] scrubbed n/m ratios from cache: {', '.join(_scrubbed)}")

    # BUG-A6 backfill: when the cached pipeline run had a Synthesizer
    # failure, synthesized_thesis was stored as None. The new orchestrator
    # builds a Director-anchored fallback in this case, but old caches miss
    # it — without a thesis, ReportEditor is skipped and the rendered HTML
    # has empty headline / lede / executive paragraphs / thesis grid.
    # Replicate the fallback here so replay always renders something.
    if state.get("synthesized_thesis") is None and state.get("research_directive") is not None:
        try:
            from aegis.core.chief_analyst.thesis_synthesizer import SynthesizedThesis
            _dir = state["research_directive"]
            _opening = getattr(_dir, "opening_angle", "") or ""
            _hyp = getattr(_dir, "initial_hypothesis", "") or ""
            _consensus = getattr(_dir, "what_consensus_likely_believes", "") or ""
            _why_now = getattr(_dir, "why_now", "") or ""
            _key_vars = getattr(_dir, "key_variables", []) or []
            _controversy = getattr(_dir, "key_controversy", "") or ""
            _variant_text = ""
            _counter_text = ""
            _all_judgments = state.get("all_judgments") or {}
            for _name in ("variant_analyst", "risk_analyst", "valuation_analyst"):
                _j = _all_judgments.get(_name) if isinstance(_all_judgments, dict) else None
                if _j is None:
                    continue
                _infs = getattr(_j, "inferences", None) or (
                    _j.get("inferences", []) if isinstance(_j, dict) else []
                )
                if _infs:
                    _first = _infs[0]
                    # BUG-Y36 (2026-05-06): Inference schema uses `text`,
                    # not `claim`. The previous code asked for `.claim` on
                    # both the dataclass and dict paths — `getattr` returned
                    # None and `.get("claim")` returned "". So
                    # `_variant_text` and `_counter_text` were ALWAYS empty
                    # in the replay fallback, leaving `core_thesis` as the
                    # only filled field on the rebuilt SynthesizedThesis.
                    _txt = (getattr(_first, "text", None)
                            or (_first.get("text", "") if isinstance(_first, dict) else "")
                            or "")
                    if _txt and not _variant_text:
                        _variant_text = _txt
                    elif _txt and not _counter_text:
                        _counter_text = _txt
            state["synthesized_thesis"] = SynthesizedThesis(
                core_thesis=_opening or _hyp or "（合成失败：缺少核心论点）",
                my_variant=_variant_text,
                variant_magnitude="",
                variant_decomposition_narrative="",
                why_now=_why_now,
                market_implied_story=_consensus,
                key_assumption_disagreement=_controversy,
                counter_thesis=_counter_text,
                why_market_is_wrong="",
                what_would_change_my_mind="；".join(_key_vars[:3]) if _key_vars else "",
                edge_source="research_director_fallback",
                edge_durability="short_term",
                unresolved_tensions=list(_key_vars[:4]),
                conviction_narrative="（缓存中 Synthesizer 缺失，由 Director 摘要兜底）",
                hypothesis_validated=False,
                hypothesis_evolution="原 cache 无 SynthesizedThesis（Synthesizer 当时调用失败）",
            )
            print("  [backfill] synthesized_thesis built from research_directive (Director-anchored fallback)")
        except Exception as e:
            print(f"  [backfill] synthesized_thesis fallback failed: {e}")

    # BUG-28 backfill: FCF = OCF - abs(capex). Old caches computed
    # FCF = OCF - capex, which gave wrong sign when capex was negative
    # (A-share convention). Re-derive FCF from cached OCF and capex.
    if mf:
        ocf = mf.get("operating_cash_flow")
        cap = mf.get("capex") or mf.get("capital_expenditures")
        if ocf is not None and cap is not None:
            correct_fcf = ocf - abs(cap)
            old_fcf = mf.get("free_cash_flow")
            if old_fcf is not None and abs(correct_fcf - old_fcf) > abs(old_fcf) * 0.05:
                mf["free_cash_flow"] = correct_fcf
                state["meta_facts"] = mf
                _div = 1e8 if abs(correct_fcf) < 1e11 else 1e9
                _unit = "亿" if _div == 1e8 else "B"
                print(f"  [backfill] free_cash_flow: {old_fcf/_div:.1f}{_unit} → "
                      f"{correct_fcf/_div:.1f}{_unit} (OCF - abs(capex))")

    # Data-quality alerts (2026-04-15): fact_bridge now writes
    # `__data_quality_issues` into meta_facts at normalization time.
    # Backfill for old caches by re-running the checker against cached facts.
    if mf and "__data_quality_issues" not in mf:
        try:
            from aegis.core.acquisition.fact_bridge import _run_data_quality_checks
            issues = _run_data_quality_checks(mf)
            if issues:
                mf["__data_quality_issues"] = issues
                state["meta_facts"] = mf
                print(f"  [backfill] data_quality_issues: {len(issues)} found")
                for i in issues:
                    print(f"    [{i['severity']:5s}] {i['code']}: {i['message'][:120]}")
        except Exception as _dqe:
            print(f"  [backfill] DQ scan failed: {_dqe}")
    # BUG-25 backfill: historical EV/EBITDA series may have been scaled from
    # a bad yfinance snapshot.  If computed_metrics has a reliable ev_to_ebitda
    # and the historical series' last value diverges >50%, rescale the series.
    hv = state.get("historical_valuation") or {}
    ev_series = hv.get("ev_ebitda", [])
    computed_ev = (state.get("computed_metrics") or {}).get("ev_to_ebitda")
    if ev_series and computed_ev and computed_ev > 0:
        # last valid value in series
        last_ev = 0
        for v in reversed(ev_series):
            if v and v > 0:
                last_ev = float(v)
                break
        if last_ev > 0 and abs(last_ev - computed_ev) / computed_ev > 0.50:
            scale = computed_ev / last_ev
            hv["ev_ebitda"] = [round(float(v) * scale, 1) if v and v > 0 else v
                               for v in ev_series]
            # Recompute stats
            clean = sorted([v for v in hv["ev_ebitda"] if v and v > 0 and v < 500])
            if len(clean) >= 3:
                import statistics
                latest_rescaled = 0
                for v in reversed(hv["ev_ebitda"]):
                    if v and v > 0:
                        latest_rescaled = v
                        break
                hv["ev_ebitda_stats"] = {
                    "min": round(min(clean), 1), "max": round(max(clean), 1),
                    "median": round(statistics.median(clean), 1),
                    "mean": round(statistics.mean(clean), 1),
                    "p25": round(clean[len(clean) // 4], 1),
                    "p75": round(clean[3 * len(clean) // 4], 1),
                    "current": round(latest_rescaled, 1),
                }
            state["historical_valuation"] = hv
            print(f"  [backfill] EV/EBITDA rescaled: {last_ev:.1f}x → {computed_ev:.1f}x "
                  f"(×{scale:.2f})")

    print()

    # ── DCF Recompute (2026-04-16) ──
    # When dcf_engine.py or fact_bridge.py has been updated (D&A fix,
    # shares methodology, FCF formula), we need to recompute DCF from
    # the cached dcf_input_flat to get correct per-share values.
    try:
        from aegis.core.truth.scenario_engine.dcf_engine import DCFEngine, DCFInput, ConsolidatedDCFOutput

        dcf_input_flat = state["dcf_input_flat"]
        # Backfill D&A into DCF input if it was added to meta_facts
        if mf.get("depreciation_amortization") and dcf_input_flat.base_depreciation == 0:
            dcf_input_flat = DCFInput(**{
                **{k: getattr(dcf_input_flat, k) for k in dcf_input_flat.__dataclass_fields__},
                "base_depreciation": mf["depreciation_amortization"],
            })
            state["dcf_input_flat"] = dcf_input_flat
            state["_da_backfilled"] = True  # Force flat recompute even for segment DCF
            print(f"  [recompute] DCF input: backfilled base_depreciation={mf['depreciation_amortization']:,.0f}")

        dcf = DCFEngine()

        # Detect segment-based DCF: ConsolidatedDCFOutput has no .projections
        # at the top level (it has .segment_outputs instead). For segment DCF
        # tickers, dcf_input_flat has a different (much more aggressive) growth
        # path than what segments individually use, so we must NOT recompute
        # from dcf_input_flat. Instead, just adjust per_share = equity / current.
        #
        # EXCEPTION: if D&A was just backfilled (base_depreciation changed from 0),
        # we MUST recompute from flat input because the old equity_value was
        # computed without D&A — the segment output is equally wrong.
        da_just_backfilled = (mf.get("depreciation_amortization") and
                              dcf_input_flat.base_depreciation > 0 and
                              state["dcf_output"].per_share_value > 0 and
                              # Check if the original output was from D&A=0 era
                              hasattr(state.get("_da_backfilled"), "__bool__") is False)
        # Simple heuristic: if we printed the backfill message, force flat recompute
        is_segment_dcf = (isinstance(state["dcf_output"], ConsolidatedDCFOutput)
                          and not state.get("_da_backfilled", False))

        if is_segment_dcf:
            old_ps = state["dcf_output"].per_share_value
            old_equity = state["dcf_output"].equity_value
            new_ps = old_equity / dcf_input_flat.shares_outstanding if dcf_input_flat.shares_outstanding > 0 else old_ps
            new_output = state["dcf_output"]  # Keep original output
            print(f"  [recompute] Segment DCF: per_share adjusted for current shares "
                  f"(${old_ps:.2f} → ${new_ps:.2f}, equity ${old_equity/1e9:.0f}B / "
                  f"{dcf_input_flat.shares_outstanding/1e9:.2f}B shares)")
        else:
            new_output = dcf.compute_dcf(dcf_input_flat)
            old_ps = state["dcf_output"].per_share_value
            new_ps = new_output.per_share_value

        if abs(new_ps - old_ps) / max(abs(old_ps), 1) > 0.01:
            print(f"  [recompute] DCF per_share: ${old_ps:.2f} → ${new_ps:.2f} "
                  f"({(new_ps/old_ps - 1)*100:+.1f}%)")
            if not is_segment_dcf:
                state["dcf_output"] = new_output
                # Update projections for HTML table
                raw_projs = new_output.projections
                state["dcf_projections"] = [
                    {"year": p.year, "revenue": p.revenue,
                     "operating_income": p.operating_income,
                     "nopat": p.nopat, "depreciation": p.depreciation,
                     "capex": p.capex, "sbc": p.sbc,
                     "change_in_nwc": p.change_in_nwc,
                     "fcff": p.fcff, "pv_fcff": p.pv_fcff,
                     "discount_factor": p.discount_factor}
                    for p in raw_projs
                ]
            # Update scenarios bridge
            scenarios = state["scenarios"]
            if isinstance(scenarios, dict):
                scenarios["dcf_bridge"] = {
                    "pv_fcff_sum": new_output.pv_fcff_sum,
                    "pv_terminal_value": new_output.pv_terminal_value,
                    "enterprise_value": new_output.enterprise_value,
                    "net_debt": dcf_input_flat.net_debt,
                    "equity_value": new_output.equity_value,
                    "future_shares": new_output.future_shares,
                    # new_ps, not new_output.per_share_value: on the segment
                    # path new_output is the ORIGINAL cached output whose
                    # per-share predates the share-count adjustment — bridge
                    # and base_value must agree.
                    "per_share_value": new_ps,
                }
                # Update scenario values — handle both nested and flat formats
                ratio = 1.0  # guard: referenced below even when base_value is 0/None
                if "base" in scenarios:
                    scenarios["base"]["per_share_value"] = new_ps
                if "base_value" in scenarios:
                    old_base = scenarios["base_value"]
                    scenarios["base_value"] = new_ps
                    # Scale bear/bull proportionally (same ratio as before)
                    if old_base and old_base > 0:
                        ratio = new_ps / old_base
                        for variant in ("bear", "bull"):
                            vk = f"{variant}_value"
                            if vk in scenarios and scenarios[vk]:
                                scenarios[vk] = scenarios[vk] * ratio

                # Recompute probability-weighted value
                scenarios["_old_pw"] = scenarios.get("probability_weighted_value")
                probs = state.get("scenario_probabilities") or {}
                bear_v = scenarios.get("bear_value") or (scenarios.get("bear", {}) or {}).get("per_share_value", 0)
                base_v = new_ps
                bull_v = scenarios.get("bull_value") or (scenarios.get("bull", {}) or {}).get("per_share_value", 0)
                if bear_v or bull_v:
                    pw = (probs.get("bear", 0.3) * bear_v
                          + probs.get("base", 0.45) * base_v
                          + probs.get("bull", 0.25) * bull_v)
                    scenarios["probability_weighted_value"] = pw
                    print(f"  [recompute] Scenarios: bear=${bear_v:.0f}, "
                          f"base=${base_v:.0f}, "
                          f"bull=${bull_v:.0f}, "
                          f"PW=${pw:.0f}")

                state["scenarios"] = scenarios

                # Record stale→new value map for narrative text fixup
                stale_map = {old_ps: new_ps}  # base case
                # old bear/bull: reverse the ratio to get pre-scale values
                if "base_value" in scenarios and ratio != 1:
                    stale_map[bear_v / ratio] = bear_v  # old_bear → new_bear
                    stale_map[bull_v / ratio] = bull_v  # old_bull → new_bull
                    old_pw = scenarios.get("_old_pw")
                    if old_pw:
                        stale_map[old_pw] = pw
                state["_stale_value_map"] = stale_map

            # Recompute sensitivity rankings + WACC×TGR table (only for flat
            # DCF — segment DCF's flat input has different growth assumptions).
            # HANDOFF 缺参修复: the old inline call passed no ranges to
            # two_way_table (TypeError swallowed by `except: pass`), so the
            # stale cached table shipped with a freshly recomputed dcf_output.
            if not is_segment_dcf:
                new_rankings, new_table = _recompute_sensitivity_flat(dcf_input_flat)
                if new_rankings:
                    state["sensitivity_rankings"] = new_rankings
                else:
                    print("  [recompute] ⚠ sensitivity rankings empty — keeping cached rankings")
                if new_table is not None:
                    state["sensitivity_table"] = new_table
                else:
                    print("  [recompute] ⚠ keeping cached sensitivity table "
                          "(2-way recompute failed)")
            if is_segment_dcf:
                # Scale sensitivity table/rankings proportionally (None cells
                # = infeasible WACC/g combos must be preserved, not multiplied)
                share_ratio = new_ps / old_ps if old_ps else 1.0
                state["sensitivity_rankings"], state["sensitivity_table"] = (
                    _scale_sensitivity_for_segment(
                        state.get("sensitivity_rankings"),
                        state.get("sensitivity_table"),
                        share_ratio,
                    )
                )
                # Keep dcf_output coherent with the share-adjusted per-share:
                # dcf_integrity_gate compares dcf_output.per_share_value with
                # the (now rescaled) matrix base cell — leaving the stale
                # per-share here would turn the newly-fed gate into a false
                # block. Frozen dataclass → dataclasses.replace.
                try:
                    import dataclasses as _dc
                    state["dcf_output"] = _dc.replace(
                        state["dcf_output"], per_share_value=new_ps,
                    )
                except Exception as _rep_err:
                    print(f"  [recompute] ⚠ segment dcf_output per_share update failed: {_rep_err}")
            what = "projections, bridge, sensitivity" if not is_segment_dcf else "scenarios, bridge, sensitivity (segment DCF preserved)"
            print(f"  [recompute] Updated: {what}")
        else:
            print(f"  [recompute] DCF per_share unchanged: ${new_ps:.2f}")
    except Exception as _dcf_err:
        print(f"  [recompute] DCF recompute failed: {_dcf_err}")

    # ── Optional Step 0: Re-run critics with fresh code ──
    critic_results_to_use = state["critic_results"]
    if args.rerun_critics:
        t_cr = time.time()
        from aegis.core.critics.logic_critic.critic import LogicCritic
        from aegis.core.critics.accounting_critic.critic import AccountingCritic
        from aegis.core.critics.evidence_critic.critic import EvidenceCritic
        from aegis.core.critics.sector_critic.critic import SectorCritic
        from aegis.core.critics.cognitive_bias_critic.critic import CognitiveBiasCritic
        from aegis.core.critics.macro_consistency_critic.critic import MacroConsistencyCritic
        from aegis.core.critics.market_critic.critic import MarketCritic
        from aegis.core.critics.numeric_consistency_critic.critic import NumericConsistencyCritic

        critic_results_to_use = []
        cctx = dict(state.get("critic_context", {}))
        # Backfill sbc_treatment from dcf_input_flat if missing (old caches
        # predate this being passed into critic_context).
        if "sbc_treatment" not in cctx:
            cctx["sbc_treatment"] = getattr(
                state.get("dcf_input_flat"), "sbc_treatment", "dilution_only",
            )
        for CriticCls in [LogicCritic, AccountingCritic, EvidenceCritic, SectorCritic,
                          CognitiveBiasCritic, MacroConsistencyCritic, MarketCritic,
                          NumericConsistencyCritic]:
            critic_results_to_use.append(
                CriticCls().review(state["all_judgments"], context=cctx)
            )
        blocks = sum(sum(1 for i in cr.issues if i.severity == "block")
                     for cr in critic_results_to_use)
        warns = sum(sum(1 for i in cr.issues if i.severity == "warn")
                    for cr in critic_results_to_use)
        print(f"  [{time.time()-t_cr:.2f}s] Re-ran {len(critic_results_to_use)} critics: "
              f"{blocks} blocks, {warns} warns (was "
              f"{sum(sum(1 for i in cr.issues if i.severity == 'block') for cr in state['critic_results'])} "
              f"blocks, "
              f"{sum(sum(1 for i in cr.issues if i.severity == 'warn') for cr in state['critic_results'])} warns)")

    # ── Step 1: Re-run Publish Gate with fresh code ──
    # 缺参修复（同类问题）: the gate used to receive only run_manifest_id,
    # so every integrity gate skipped for "missing inputs" — replay never
    # actually exercised dcf_integrity/terminal_value/valuation_sanity/
    # capex_attribution. Feed the full orchestrator Step-12 context.
    t1 = time.time()
    from aegis.core.publish_gate import PublishGate
    gate = PublishGate()
    gate_result = gate.evaluate(
        state["all_judgments"],
        critic_results_to_use,
        context=_build_gate_context(state),
    )
    print(f"  [{time.time()-t1:.2f}s] PublishGate: "
          f"{'✅ PASSED' if gate_result.publishable else '❌ BLOCKED'}")
    if not gate_result.publishable:
        print(f"    Blocked by: {gate_result.blocked_by}")
    # Print gate-level details
    passed = sum(1 for c in gate_result.checks if c.passed)
    total = len(gate_result.checks)
    print(f"    Gates passed: {passed}/{total}")
    # AUDIT 2026-07-12 (B4) parity: gates that skipped for missing inputs
    # cap confidence at medium in the decision engine — same as a fresh run.
    gate_skipped_names = [
        c.gate_name for c in gate_result.checks
        if c.passed and c.severity == "warn" and "skipped" in c.message
    ]
    if gate_skipped_names:
        print(f"    Gate skips (missing inputs): {gate_skipped_names}")
    if args.verbose or not gate_result.publishable:
        for c in gate_result.checks:
            status = "✓" if c.passed else "✗"
            sev = f"[{c.severity}]" if not c.passed else ""
            print(f"      {status} {c.gate_name} {sev}: {c.message[:100]}")

    # Critic statistics
    total_blocks = sum(sum(1 for i in cr.issues if i.severity == "block")
                       for cr in critic_results_to_use)
    total_warns = sum(sum(1 for i in cr.issues if i.severity == "warn")
                      for cr in critic_results_to_use)
    print(f"    Critics: {total_blocks} blocks, {total_warns} warns")

    # ── Step 2: Decision Engine ──
    t2 = time.time()
    from aegis.core.decision_engine import DecisionEngine
    from aegis.data_contracts.edge_assessment_schema import EdgeAssessment

    try:
        edge = EdgeAssessment.model_validate(state["edge_assessment_dict"])
    except Exception:
        sanitized = {k: str(v) if v is not None else v
                     for k, v in state["edge_assessment_dict"].items()}
        edge = EdgeAssessment.model_validate(sanitized)

    de = DecisionEngine()
    dcf_output = state["dcf_output"]
    dcf_input_flat = state["dcf_input_flat"]
    decision = de.decide(
        state["entity_id"], state["run_id"],
        state["all_judgments"], critic_results_to_use,
        gate_result.publishable,
        context={
            "edge_assessment": edge,
            "scenarios": state["scenarios"],
            "dcf_projections_base": state["dcf_projections"],
            "dcf_assumptions": {
                "revenue_growth_path": list(dcf_input_flat.revenue_growth_path),
                "operating_margin_path": list(dcf_input_flat.operating_margin_path),
                "wacc": dcf_input_flat.wacc,
                "terminal_growth_rate": dcf_input_flat.terminal_growth_rate,
                "segment_dcf": state["segment_projections_data"] is not None,
            },
            "tv_pct": dcf_output.pv_terminal_value / dcf_output.enterprise_value
                if dcf_output.enterprise_value else 0,
            "sensitivity_rankings": state["sensitivity_rankings"],
            "sensitivity_table": state["sensitivity_table"],
            "open_questions": state["open_questions"],
            # 缺参修复（同类问题）: mirror orchestrator Step 13 context so the
            # replayed decision matches a fresh run.
            "macro_dependency": f"US {getattr(state['config'], 'cycle_phase', 'late_expansion')}",
            "sector_cycle_position": "Auto-detected from sector pack",
            # AUDIT B4: without this key the confidence cap for skipped gates
            # never applied in replay (ctx.get defaulted to []). With the full
            # gate context above, a complete cache yields no skips → no cap;
            # a genuinely incomplete cache now caps at medium like a fresh run.
            "gate_skipped_names": gate_skipped_names,
        },
        synthesized_thesis=state["synthesized_thesis"],
    )
    print(f"  [{time.time()-t2:.2f}s] Decision: {decision.publishing_status}, "
          f"confidence={decision.confidence_bucket}")

    # ── Step 3: Portfolio Signal ──
    t3 = time.time()
    from aegis.core.portfolio.portfolio_integration import PortfolioIntegration
    pi = PortfolioIntegration()
    probs = state["scenario_probabilities"]
    signal = pi.generate_signal(
        decision,
        scenario_weights={"bear": probs["bear"], "base": probs["base"], "bull": probs["bull"]},
    )
    print(f"  [{time.time()-t3:.2f}s] Signal: {signal.direction}, "
          f"conviction={signal.conviction}, tier={signal.sizing_tier}")

    # ── Step 4: Report Editor (optional, expensive) ──
    edited_report = None
    if args.editor and state["synthesized_thesis"] is not None:
        t4 = time.time()
        try:
            from aegis.core.chief_analyst import ReportEditor
            editor = ReportEditor()
            # Editor 重跑固定走 DeepSeek（当前默认后端）。需要 DEEPSEEK_API_KEY，
            # 缺失时在此给出明确提示（DeepSeekClient 的报错只说缺 key）。
            if not (os.environ.get("DEEPSEEK_API_KEY") or getattr(state["config"], "deepseek_api_key", None)):
                raise RuntimeError(
                    "--editor 需要 DEEPSEEK_API_KEY（Report Editor 重跑走 DeepSeek 后端）。"
                    "请先 export DEEPSEEK_API_KEY=... 再重试。"
                )
            from aegis.core.llm.deepseek_client import DeepSeekClient
            editor._llm = DeepSeekClient(
                model=getattr(state["config"], "deepseek_model", None) or "deepseek-v4-pro",
                api_key=getattr(state["config"], "deepseek_api_key", None),
            )
            edited_report = editor.edit(
                entity_name=state["entity_name"],
                synthesized_thesis=state["synthesized_thesis"],
                directive=state["research_directive"],
                computed_metrics=state["computed_metrics"],
                market_data=state["market_data"],
                scenarios=state["scenarios"],
                meta_facts=state["meta_facts"],
                segment_detail=state["segment_detail"],
            )
            print(f"  [{time.time()-t4:.1f}s] Report Editor: "
                  f"headline='{edited_report.headline[:60]}...'")
        except Exception as e:
            print(f"  ⚠ Report Editor failed: {e}")
    else:
        print("  [skipped] Report Editor (use --editor to run)")

    # ── Step 5: HTML Report ──
    t5 = time.time()
    from aegis.core.reports.html_report import generate_html_report
    html = generate_html_report(
        decision=decision,
        scenarios=state["scenarios"],
        computed_metrics=state["computed_metrics"],
        market_data=state["market_data"],
        agent_judgments=state["all_judgments"],
        critic_results=critic_results_to_use,
        meta_facts=state["meta_facts"],
        dcf_projections=state["dcf_projections"],
        sensitivity_table=state["sensitivity_table"],
        sensitivity_rankings=state["sensitivity_rankings"],
        entity_name=state["entity_name"],
        segment_detail=state["segment_detail"],
        segment_projections=state["segment_projections_data"],
        consensus_estimates=state["consensus_estimates"],
        earnings_history=state["earnings_history"],
        peer_fundamentals=state["peer_fundamentals"],
        price_target_consensus=state["price_target_consensus"],
        edited_report=edited_report,
        research_directive=state["research_directive"],
        synthesized_thesis=state["synthesized_thesis"],
        earnings_call_insights=state["earnings_call_insights"],
        historical_valuation=state["historical_valuation"],
        catalyst_timeline=state["catalyst_timeline"],
        insider_summary=state["insider_summary"],
        news_sentiment_insights=state["news_sentiment_insights"],
        # v2 template-renderer extras — legacy renderer ignores unknown kwargs
        period=state["config"].period,
        dcf_output=state.get("dcf_output"),
        # Pick the model name from the most likely active backend so HTML
        # metadata reflects the current run, not whatever was cached.
        # Priority: DeepSeek key set → deepseek_model; else llm_model.
        model_name=(
            getattr(state["config"], "deepseek_model", None)
            if (os.environ.get("DEEPSEEK_API_KEY") or getattr(state["config"], "deepseek_api_key", None))
            else getattr(state["config"], "llm_model", None)
        ),
        macro_snapshot=state.get("macro_snapshot"),  # None for old caches
    )
    # ── Stale narrative fixup ──
    # When DCF was recomputed, agent narratives still reference old scenario
    # values. Replace dollar amounts that match old values with new ones.
    stale_map = state.get("_stale_value_map", {})
    if stale_map:
        import re as _re
        replacements = 0
        for old_val, new_val in stale_map.items():
            # Match $XXX.XX or $XXX patterns (with optional decimals)
            old_str = f"${old_val:,.2f}" if old_val != int(old_val) else f"${int(old_val):,}"
            new_str = f"${new_val:,.2f}" if new_val != int(new_val) else f"${int(new_val):,}"
            # Also try without comma
            for o, n in [(old_str, new_str),
                         (old_str.replace(",", ""), new_str.replace(",", "")),
                         (f"${old_val:.0f}", f"${new_val:.0f}"),
                         (f"${old_val:.2f}", f"${new_val:.2f}")]:
                if o in html:
                    count = html.count(o)
                    html = html.replace(o, n)
                    replacements += count
        if replacements:
            print(f"  [fixup] Replaced {replacements} stale narrative values")

    # Derive filename from cached config period, not hardcoded FY2024
    period_str = state["config"].period.lower()
    out_file = Path("demos") / f"{args.ticker.lower()}_{period_str}_auto_report.html"
    out_file.write_text(html, encoding="utf-8")
    print(f"  [{time.time()-t5:.2f}s] HTML report: {out_file} ({len(html)//1024}KB)")

    # ── Summary ──
    print()
    print(f"{'='*70}")
    print(f"  REPLAY RESULT")
    print(f"{'='*70}")
    print(f"  Total time:       {time.time()-t0:.2f}s")
    print(f"  Publishing:       {decision.publishing_status}")
    print(f"  Confidence:       {decision.confidence_bucket}")
    print(f"  Bias check:       {decision.bias_check_status}")
    print(f"  Signal:           {signal.direction} ({signal.conviction})")
    print(f"  Sizing:           {signal.sizing_tier}")
    print(f"  Kill criteria:    {len(decision.kill_criteria)}")
    print(f"  Monitorables:     {len(decision.monitorables)}")
    print(f"  Report:           file://{out_file.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
