"""监控扫描 CLI 入口 — Aegis 2.0 Phase 3 任务 C1.

事件循环的「手动/定时触发点」：把 :func:`aegis.core.monitor.scanner.scan_once`
（一轮扫描）与 :func:`aegis.core.monitor.postmortem.run_postmortems`（到期复盘）
包成一个命令行入口，供人工敲一次或 launchd 每日盘后自动跑一次。

用法::

    python scripts/scan_watchlist.py                 # 真实一轮，打印中文扫描报告
    python scripts/scan_watchlist.py --dry-run       # 只判触发，不跑复研（零副作用）
    python scripts/scan_watchlist.py --smoke          # rule-based 复研，无 LLM 成本
    python scripts/scan_watchlist.py --postmortems   # 额外跑一遍到期 90 天回看复盘
    python scripts/scan_watchlist.py --no-llm         # 复研关掉 LLM
    python scripts/scan_watchlist.py --watchlist path/to/watchlist.yaml

设计红线 10：不引入 async / 状态机 / 新第三方依赖，纯标准库 argparse。
容错：:func:`main` 永不把未捕获异常抛给外层——一律打印错误 + 返回非零码，
保证 launchd 不会因为一次异常而反复重试污染日志。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 确保项目根在 import 路径上（照 demos/auto_research_demo.py 的写法）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.core.monitor import postmortem as postmortem_mod  # noqa: E402
from aegis.core.monitor import scanner as scanner_mod  # noqa: E402

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scan_watchlist",
        description="Aegis 监控扫描：对票池跑一轮事件/触发扫描，可选跑到期复盘。",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只判触发，不跑复研（零副作用：不改水位线、不计费、不落 delta）。",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="冒烟模式：rule-based 复研，不调 LLM（无成本），落盘重定向到 smoke 目录。",
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="复研关掉 LLM（rule-based），与 --smoke 的区别是不改落盘目录。",
    )
    parser.add_argument(
        "--postmortems", action="store_true",
        help="扫描后额外跑一遍到期 90 天回看复盘（run_postmortems）。",
    )
    parser.add_argument(
        "--watchlist", default=None, metavar="PATH",
        help="票池 YAML 路径（缺省用内置默认票池 configs/watchlist.yaml）。",
    )
    return parser


def _run_scan(args: argparse.Namespace) -> int:
    """跑一轮扫描并打印中文报告；成功返回 0。"""
    # --smoke / --no-llm 都关 LLM；--smoke 额外走 smoke 落盘目录。
    use_llm = not (args.smoke or args.no_llm)
    kwargs: dict = dict(
        watchlist_path=args.watchlist,
        dry_run=args.dry_run,
        smoke=args.smoke,
        use_llm=use_llm,
    )
    if args.smoke:
        # 审查发现 #7：冒烟扫描把水位线 / 预算台账 / 扫描报告 / delta / 论点链
        # 全部落 smoke 沙箱，对生产零副作用（spend/scans 挂在 watermark_path.parent
        # 下，重定向 watermark_path 即一并隔离；thesis_dir 与冒烟复研 orchestrator
        # 的 .cache/smoke/thesis 写入口对齐，读写同源）。
        kwargs["watermark_path"] = ".cache/monitor/smoke/watermarks.json"
        kwargs["delta_dir"] = ".cache/monitor/smoke/deltas"
        kwargs["thesis_dir"] = ".cache/smoke/thesis"
    report = scanner_mod.scan_once(**kwargs)
    print(report.to_markdown())
    return 0


def _run_postmortems(smoke: bool = False) -> int:
    """跑一遍到期复盘并打印生成数量；成功返回 0。

    审查复核附注：``--smoke`` 时把复盘也关进 smoke 沙箱（只回看 smoke 论点链、
    复盘落 smoke 目录），使冒烟运行对生产 ``.cache/thesis`` / ``.cache/postmortems``
    真正零副作用（否则 ``--smoke --postmortems`` 会对生产论点跑真复盘并落盘）。
    """
    kwargs: dict = {}
    if smoke:
        kwargs["thesis_dir"] = ".cache/smoke/thesis"
        # run_postmortems 落盘目录取模块级 POSTMORTEM_DIR；一次性 CLI 进程内
        # 重定向到 smoke 沙箱即可（不影响其他进程）。
        postmortem_mod.POSTMORTEM_DIR = Path(".cache/monitor/smoke/postmortems")
    generated = postmortem_mod.run_postmortems(**kwargs)
    print("")
    print(f"## 到期复盘\n\n本轮生成 {len(generated)} 份 90 天回看复盘。")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。永不抛未捕获异常：出错打印 + 返回非零码。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        rc = _run_scan(args)
    except Exception as e:  # noqa: BLE001 — 入口兜底，绝不把异常抛给 launchd
        logger.exception("scan_watchlist: 扫描失败")
        print(f"[错误] 扫描失败：{e}", file=sys.stderr)
        return 1

    if args.postmortems:
        try:
            _run_postmortems(args.smoke)
        except Exception as e:  # noqa: BLE001 — 复盘失败不影响扫描已成功的返回
            logger.exception("scan_watchlist: 到期复盘失败")
            print(f"[错误] 到期复盘失败：{e}", file=sys.stderr)
            return 2

    return rc


if __name__ == "__main__":
    sys.exit(main())
