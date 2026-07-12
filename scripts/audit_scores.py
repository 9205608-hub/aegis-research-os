#!/usr/bin/env python3
"""R5-L3：Grok 审计分数提取 + 多次采样均值汇总。

四轮复审的测量学教训（GROK_REAUDIT_2026-07-12.md 平台期锁 #3）：审计员对
同一份材料的重复打分噪声约 ±0.5，大于修复轮之间的增量——KPI 逼近阈值时，
单次采样读不出信号。配合 grok_audit_stock.sh 的 AUDIT_RUNS=N 重复采样，
本脚本从审计 md 里提取第 7 节的 0-10 可信度分，按票聚合均值±极差，再给
整体均分（票均值的均值）。

用法:
    python scripts/audit_scores.py logs/grok_audits
    python scripts/audit_scores.py logs/round5 --json

文件约定（grok_audit_stock.sh 的产物）:
    {code}_audit.md          单次采样（或多次采样时 run1 的兼容副本）
    {code}_audit_run{i}.md   第 i 次独立采样
存在 run 文件的票自动忽略 _audit.md（它只是 run1 的副本，防止重复计数）。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

# 分数样式实测（logs/grok_audits/*_audit.md）："**3 / 10**"、"3/10"、
# "**3.5 / 10**"，均出现在第 7 节总评；全角斜杠留余量。取**最后一个**
# 匹配——总评是审计的最后一节，正文里若引用过别的 N/10（罕见）不受影响。
_SCORE_RE = re.compile(r"(\d+(?:[.．]\d+)?)\s*[/／]\s*10\b")

_RUN_FILE_RE = re.compile(r"^(?P<code>.+)_audit_run(?P<run>\d+)\.md$")
_PLAIN_FILE_RE = re.compile(r"^(?P<code>.+)_audit\.md$")


def extract_score(text: str) -> float | None:
    """从一份审计 markdown 提取 0-10 可信度分；提取不到返回 None。"""
    if not text:
        return None
    matches = _SCORE_RE.findall(text)
    for raw in reversed(matches):
        try:
            v = float(raw.replace("．", "."))
        except ValueError:
            continue
        if 0.0 <= v <= 10.0:
            return v
    return None


def collect_scores(audit_dir: Path) -> dict[str, list[float]]:
    """扫描目录 → {code: [各次采样分数]}（跳过 prompt/err/占位文件）。"""
    runs: dict[str, dict[int, float]] = {}
    plain: dict[str, float] = {}
    for p in sorted(audit_dir.glob("*_audit*.md")):
        m_run = _RUN_FILE_RE.match(p.name)
        m_plain = _PLAIN_FILE_RE.match(p.name) if not m_run else None
        if not m_run and not m_plain:
            continue
        score = extract_score(p.read_text(encoding="utf-8", errors="replace"))
        if score is None:
            print(f"  ⚠ 提取不到分数: {p.name}", file=sys.stderr)
            continue
        if m_run:
            code = m_run.group("code")
            runs.setdefault(code, {})[int(m_run.group("run"))] = score
        else:
            plain[m_plain.group("code")] = score
    out: dict[str, list[float]] = {}
    for code, by_run in runs.items():
        out[code] = [by_run[i] for i in sorted(by_run)]
    for code, score in plain.items():
        if code not in out:  # 有 run 文件时 _audit.md 只是 run1 副本
            out[code] = [score]
    return out


def summarize(scores: dict[str, list[float]]) -> dict:
    per_code = {}
    for code, vals in sorted(scores.items()):
        per_code[code] = {
            "n": len(vals),
            "mean": round(statistics.mean(vals), 2),
            "min": min(vals),
            "max": max(vals),
            "scores": vals,
        }
    means = [v["mean"] for v in per_code.values()]
    return {
        "tickers": per_code,
        "overall_mean": round(statistics.mean(means), 2) if means else None,
        "n_tickers": len(per_code),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("audit_dir", type=Path, help="审计产物目录，如 logs/grok_audits")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供脚本消费）")
    args = ap.parse_args()
    if not args.audit_dir.is_dir():
        print(f"目录不存在: {args.audit_dir}", file=sys.stderr)
        return 1
    summary = summarize(collect_scores(args.audit_dir))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if not summary["tickers"]:
        print("未找到可解析的审计文件。")
        return 1
    print(f"{'票':<10}{'n':>3}{'均值':>7}{'极差':>12}  各次采样")
    for code, row in summary["tickers"].items():
        spread = f"{row['min']:.1f}–{row['max']:.1f}"
        print(f"{code:<10}{row['n']:>3}{row['mean']:>7.2f}{spread:>12}  "
              f"{', '.join(f'{s:.1f}' for s in row['scores'])}")
    print(f"\n整体均分（{summary['n_tickers']} 票）: {summary['overall_mean']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
