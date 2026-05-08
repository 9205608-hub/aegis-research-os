"""Test NarrativeFactCritic against all cached reports.

Usage: python scripts/test_narrative_critic.py
"""
import pickle
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.core.critics.narrative_fact_critic.critic import NarrativeFactCritic


def main():
    critic = NarrativeFactCritic()
    caches = sorted(glob.glob(".cache/*_replay_state.pkl"))

    if not caches:
        print("No cached reports found in .cache/")
        return

    total_issues = 0
    total_blocks = 0

    for cache_path in caches:
        ticker = cache_path.split("/")[-1].replace("_replay_state.pkl", "").upper()
        with open(cache_path, "rb") as f:
            state = pickle.load(f)

        meta_facts = state.get("meta_facts", {})
        computed_metrics = state.get("computed_metrics", {})
        market_data = state.get("market_data", {})
        judgments = state.get("all_judgments", []) or state.get("agent_judgments", [])

        if not judgments:
            print(f"\n{ticker}: no judgments in cache, skipping")
            continue

        context = {
            "meta_facts": meta_facts,
            "computed_metrics": computed_metrics,
            "market_data": market_data,
        }

        result = critic.review(judgments, context=context)

        n_issues = len(result.issues)
        n_blocks = sum(1 for i in result.issues if i.severity == "block")
        n_warns = sum(1 for i in result.issues if i.severity == "warn")
        total_issues += n_issues
        total_blocks += n_blocks

        status = "✅" if n_issues == 0 else ("🔴" if n_blocks > 0 else "🟡")
        print(f"\n{status} {ticker}: {n_issues} issues ({n_blocks} block, {n_warns} warn)")
        for issue in result.issues:
            sev = "BLOCK" if issue.severity == "block" else "WARN "
            print(f"  [{sev}] {issue.issue_code}: {issue.message}")
            if issue.recommended_action:
                print(f"         → {issue.recommended_action}")

    print(f"\n{'='*60}")
    print(f"TOTAL: {total_issues} issues across {len(caches)} reports "
          f"({total_blocks} blocks)")

    if total_blocks > 0:
        print("⚠ BLOCKS found — these would prevent publish!")


if __name__ == "__main__":
    main()
