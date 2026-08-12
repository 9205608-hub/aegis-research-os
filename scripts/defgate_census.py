#!/usr/bin/env python3
"""definition_gate 对齐率普查工具。

遍历 .cache/*_replay_state.pkl，提取全部 judgment 的 used_metric_ids，
版本归一化（剥尾部 _v<N>，与 PublishGate._definition_gate 同口径）后
与 seed_core_metrics 注册集比对。

输出每 run 与聚合的：
- distinct-id 对齐率
- 出现次数加权对齐率
- 未注册 id 清单（含出现次数）

用法:
    python scripts/defgate_census.py
    python scripts/defgate_census.py --cache-dir /path/to/.cache
"""
from __future__ import annotations

import argparse
import pickle
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_VERSION_TAIL = re.compile(r"_v\d+$")


def _norm(metric_id: str) -> str:
    """与 gate._definition_gate 同口径：剥尾部 _v<N>。"""
    return _VERSION_TAIL.sub("", str(metric_id))


def _iter_judgments(state: object):
    """从 replay pickle 抽出 judgment 对象。

    主流程落盘的 all_judgments 是 list[JudgmentContract]；兼容 dict
    （agent_name → judgment）与裸 dict 形态，避免旧 cache 炸普查。
    """
    if not isinstance(state, dict):
        return
    raw = state.get("all_judgments")
    if raw is None:
        return
    if isinstance(raw, dict):
        items = raw.values()
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        return
    for item in items:
        if item is None:
            continue
        yield item


def _used_metric_ids(judgment) -> list[str]:
    ids = getattr(judgment, "used_metric_ids", None)
    if ids is None and isinstance(judgment, dict):
        ids = judgment.get("used_metric_ids")
    if not ids:
        return []
    return [str(m) for m in ids if m]


def _seeded_normalized_ids() -> set[str]:
    from aegis.core.truth.registry.metric_registry import MetricRegistry
    from aegis.core.truth.registry.seed_metrics import seed_core_metrics

    registry = MetricRegistry()
    seed_core_metrics(registry)
    return {_norm(d.definition_id) for d in registry.list_all()}


def census(cache_dir: Path) -> dict:
    registered = _seeded_normalized_ids()
    pkls = sorted(cache_dir.glob("*_replay_state.pkl"))

    per_run: list[dict] = []
    agg_raw = Counter()
    agg_norm = Counter()
    judgment_total = 0

    for pkl in pkls:
        ticker = pkl.name.removesuffix("_replay_state.pkl")
        try:
            with pkl.open("rb") as f:
                state = pickle.load(f)
        except Exception as exc:
            per_run.append({
                "ticker": ticker,
                "error": str(exc),
                "judgments": 0,
                "distinct": 0,
                "hits": 0,
                "occ_total": 0,
                "occ_hits": 0,
                "unregistered": {},
            })
            continue

        run_raw = Counter()
        n_judgments = 0
        for j in _iter_judgments(state):
            n_judgments += 1
            for mid in _used_metric_ids(j):
                run_raw[mid] += 1
                agg_raw[mid] += 1
                agg_norm[_norm(mid)] += 1

        judgment_total += n_judgments
        run_norms = {_norm(m) for m in run_raw}
        run_unreg = sorted(n for n in run_norms if n not in registered)
        occ_total = sum(run_raw.values())
        occ_hits = sum(c for m, c in run_raw.items() if _norm(m) in registered)

        per_run.append({
            "ticker": ticker,
            "judgments": n_judgments,
            "distinct": len(run_norms),
            "hits": len(run_norms) - len(run_unreg),
            "occ_total": occ_total,
            "occ_hits": occ_hits,
            "unregistered": {
                n: sum(c for m, c in run_raw.items() if _norm(m) == n)
                for n in run_unreg
            },
        })

    all_norms = set(agg_norm)
    unreg_norms = sorted(n for n in all_norms if n not in registered)
    distinct_total = len(all_norms)
    distinct_hits = distinct_total - len(unreg_norms)
    occ_total = sum(agg_norm.values())
    occ_hits = sum(c for n, c in agg_norm.items() if n in registered)

    return {
        "cache_dir": str(cache_dir),
        "n_runs": len(pkls),
        "n_judgments": judgment_total,
        "registered_n": len(registered),
        "distinct_total": distinct_total,
        "distinct_hits": distinct_hits,
        "distinct_rate": (distinct_hits / distinct_total) if distinct_total else 1.0,
        "occ_total": occ_total,
        "occ_hits": occ_hits,
        "occ_rate": (occ_hits / occ_total) if occ_total else 1.0,
        "unregistered": {n: agg_norm[n] for n in unreg_norms},
        "per_run": per_run,
        "raw_counts": dict(agg_raw),
    }


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def format_report(result: dict) -> str:
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("  definition_gate census")
    lines.append("=" * 70)
    lines.append(f"  cache_dir:          {result['cache_dir']}")
    lines.append(f"  runs:               {result['n_runs']}")
    lines.append(f"  judgments:          {result['n_judgments']}")
    lines.append(f"  seed registered:    {result['registered_n']} (version-normalized)")
    lines.append(
        f"  distinct-id:        {result['distinct_hits']}/{result['distinct_total']}"
        f"  ({_pct(result['distinct_rate'])})"
    )
    lines.append(
        f"  occurrence-weighted:{result['occ_hits']}/{result['occ_total']}"
        f"  ({_pct(result['occ_rate'])})"
    )
    unreg = result["unregistered"]
    if unreg:
        lines.append("  unregistered ids:")
        for mid, cnt in sorted(unreg.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {mid:40s}  {cnt}")
    else:
        lines.append("  unregistered ids:   (none)")
    lines.append("")
    lines.append("  per-run:")
    lines.append(
        f"    {'ticker':12s}  {'j':>3s}  {'hit/dist':>8s}  "
        f"{'occ':>10s}  unregistered"
    )
    for run in result["per_run"]:
        if run.get("error"):
            lines.append(f"    {run['ticker']:12s}  ERROR {run['error']}")
            continue
        ur = ",".join(
            f"{k}×{v}" for k, v in sorted(run["unregistered"].items())
        ) or "-"
        lines.append(
            f"    {run['ticker']:12s}  {run['judgments']:3d}  "
            f"{run['hits']:2d}/{run['distinct']:<5d}  "
            f"{run['occ_hits']:4d}/{run['occ_total']:<4d}  {ur}"
        )
    lines.append("=" * 70)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Census used_metric_ids vs seed_core_metrics (version-normalized).",
    )
    parser.add_argument(
        "--cache-dir",
        default=".cache",
        help="Directory containing *_replay_state.pkl (default: .cache)",
    )
    args = parser.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_dir():
        print(f"❌ cache dir not found: {cache_dir}", file=sys.stderr)
        return 1

    result = census(cache_dir)
    print(format_report(result))
    # 非零仅表示对齐未满 100%——普查工具本身成功。便于脚本化闸门。
    if result["distinct_rate"] < 1.0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
