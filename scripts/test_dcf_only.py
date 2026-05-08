"""Fast DCF-only test using cached facts and metrics.

Lets us iterate on DCF growth/margin assumptions in <1 second, without
re-running agents or LLM calls.

Usage:
    python scripts/test_dcf_only.py NVDA
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    cache_file = Path(".cache") / f"{ticker.lower()}_replay_state.pkl"
    if not cache_file.exists():
        print(f"❌ No cache at {cache_file}")
        return 1

    with cache_file.open("rb") as f:
        state = pickle.load(f)

    facts = state["meta_facts"]
    metrics = state["computed_metrics"]
    market_data = state["market_data"]
    config = state["config"]
    consensus = state["consensus_estimates"]

    # Convert consensus list to dict format expected by _build_dcf_input
    consensus_dict = {}
    for est in consensus:
        metric = est.metric
        period = est.period
        key = f"{metric}_{period}"
        consensus_dict[key] = {"mean": est.consensus_mean}

    # Create orchestrator (lets us use its internal _load_sector_pack helper)
    from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
    orch_tmp = AutoResearchOrchestrator()
    try:
        sector_pack = orch_tmp._load_sector_pack(config.sector_pack_id, ticker) or {}
    except Exception as e:
        print(f"  ⚠ sector pack load failed: {e}, using empty dict")
        sector_pack = {}

    from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
    from aegis.core.truth.scenario_engine.dcf_engine import DCFEngine

    orch = AutoResearchOrchestrator()
    engine = DCFEngine()

    # Try segment DCF first (matches production flow when segments exist)
    segment_detail = state.get("segment_detail", {})
    product_segs = segment_detail.get("product", {}) if segment_detail else {}
    material_segs = {k: v for k, v in product_segs.items()
                     if v.get("revenue", 0) > facts.get("revenue", 1) * 0.05}

    # Apply hierarchy dedup (matches orchestrator logic)
    company_rev = facts.get("revenue", 0)
    if company_rev > 0 and material_segs:
        total_seg_rev = sum(v.get("revenue", 0) for v in material_segs.values())
        if total_seg_rev > company_rev * 1.10:
            seg_list = list(material_segs.items())
            n = len(seg_list)
            if n <= 12:
                best_subset = None
                best_diff = float("inf")
                for mask in range(1, 1 << n):
                    subset_rev = sum(
                        seg_list[i][1].get("revenue", 0)
                        for i in range(n) if mask & (1 << i)
                    )
                    if 0.85 * company_rev <= subset_rev <= 1.10 * company_rev:
                        diff = abs(subset_rev - company_rev)
                        if diff < best_diff:
                            best_diff = diff
                            best_subset = mask
                if best_subset is not None:
                    kept = {}
                    dropped = []
                    for i, (sid, sdata) in enumerate(seg_list):
                        if best_subset & (1 << i):
                            kept[sid] = sdata
                        else:
                            dropped.append(sid)
                    material_segs = kept
                    print(f"  Dedup: dropped {dropped}, kept {list(kept.keys())}")

    if len(material_segs) >= 2:
        print(f"  Using SEGMENT DCF ({len(material_segs)} material segments)")
        dcf_input, dcf_output, seg_proj = orch._build_segment_dcf(
            config, facts, metrics, market_data, sector_pack, material_segs,
            consensus_dict,
        )
        output = dcf_output
        # Print segment-level growth/margin paths
        print(f"\n  Segment-level assumptions:")
        for seg_id, seg_data in material_segs.items():
            print(f"    {seg_id}: revenue=${seg_data.get('revenue', 0)/1e9:.1f}B")
        # Print first segment's actual growth path so we can see consensus impact
        if seg_proj:
            first_id = list(seg_proj.keys())[0]
            projs = seg_proj[first_id]
            print(f"\n  {first_id} actual growth path (segment-level):")
            for yr, p in enumerate(projs, 1):
                if yr == 1:
                    continue
                prev = projs[yr-2]
                prev_rev = prev["revenue"] if isinstance(prev, dict) else getattr(prev, "revenue", 0)
                cur_rev = p["revenue"] if isinstance(p, dict) else getattr(p, "revenue", 0)
                if prev_rev > 0:
                    g = (cur_rev / prev_rev) - 1
                    print(f"    Year {yr}: {g*100:.1f}%")
    else:
        print(f"  Using FLAT DCF (<2 material segments)")
        dcf_input = orch._build_dcf_input(
            config, facts, metrics, market_data, sector_pack, consensus_dict,
        )
        output = engine.compute_dcf(dcf_input)

    print(f"{'='*60}")
    print(f"  DCF TEST — {ticker}")
    print(f"{'='*60}")
    print(f"  Current price:      ${market_data.get('current_price', 0):.2f}")
    print(f"  Base revenue:       ${dcf_input.base_revenue/1e9:.1f}B")
    print(f"  Current OM:         {metrics.get('operating_margin', 0)*100:.1f}%")
    print(f"  WACC:               {dcf_input.wacc:.1%}")
    print(f"  Terminal growth:    {dcf_input.terminal_growth_rate:.1%}")
    print()
    print(f"  Growth path:")
    for i, g in enumerate(dcf_input.revenue_growth_path):
        print(f"    Year {i+1}: {g*100:.1f}%")
    print()
    print(f"  Margin path:")
    for i, m in enumerate(dcf_input.operating_margin_path):
        print(f"    Year {i+1}: {m*100:.1f}%")
    print()
    print(f"  ── DCF Result ──")
    print(f"  Enterprise value:   ${output.enterprise_value/1e9:.1f}B")
    print(f"  Equity value:       ${output.equity_value/1e9:.1f}B")
    print(f"  Per share value:    ${output.per_share_value:.2f}")
    print(f"  vs current price:   {((output.per_share_value / market_data['current_price']) - 1)*100:+.1f}%")
    print(f"  Terminal value PV:  ${output.pv_terminal_value/1e9:.1f}B "
          f"({output.pv_terminal_value/output.enterprise_value*100:.0f}% of EV)")
    print()

    # Show projected revenue trajectory
    print(f"  ── Projected Revenue ──")
    prev = dcf_input.base_revenue
    for i, g in enumerate(dcf_input.revenue_growth_path):
        prev = prev * (1 + g)
        print(f"    Year {i+1}: ${prev/1e9:.0f}B")
    print(f"  Final/Base ratio:   {prev/dcf_input.base_revenue:.1f}x")

    return 0


if __name__ == "__main__":
    sys.exit(main())
