"""meta_facts 退役棘轮 — 设计红线 #8（Aegis 2.0 Phase 2 任务 C4）。

DESIGN_2.0 §三.B / 红线 8：**meta_facts 引用文件数只减不增**。pit 层是新
数据唯一事实源，meta_facts 终将降级为 legacy view；在那之前，先用 CI 棘轮
锁死「直接触碰 meta_facts 的生产文件集合」：

- 白名单外出现新文件 → FAIL（禁增——新代码必须走 pit 层 / 显式契约）；
- 白名单文件不再触碰 meta_facts → FAIL 并提示把它从白名单里删掉
  （可缩不可扩：清单只许变短，缩短本身就是退役进度条）。

统计口径（与任务规格一致）：grep aegis/ 下 .py 生产文件，匹配
``meta_facts[`` / ``meta_facts.get(`` / ``meta_facts.setdefault(`` 三种
直接触碰模式（含 ``xxx.meta_facts.get(...)`` 属性形态——同样是直接读写）。

白名单基线（2026-07-10，Phase 2 C4 上线时的真实清单，共 13 文件）。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AEGIS_DIR = PROJECT_ROOT / "aegis"

# 直接触碰 meta_facts 的模式（下标 / .get / .setdefault）。
_TOUCH_PAT = re.compile(r"meta_facts(\[|\.get\(|\.setdefault\()")

# ── 红线 8 棘轮白名单：只许删行，不许加行 ─────────────────────────────
# 想让新文件读 meta_facts？不行——新数据走 pit 层（aegis/pit/store.py）
# 或显式函数参数传递。想把某文件迁移干净？删掉这里对应的一行，棘轮收紧。
FROZEN_WHITELIST: frozenset[str] = frozenset({
    "aegis/core/_display.py",
    "aegis/core/acquisition/fact_bridge.py",
    "aegis/core/agents/llm_agent_base.py",
    "aegis/core/chief_analyst/report_editor.py",
    "aegis/core/chief_analyst/research_director.py",
    "aegis/core/chief_analyst/scenario_architect.py",
    "aegis/core/chief_analyst/thesis_synthesizer.py",
    "aegis/core/critics/llm_judge_critic/critic.py",
    "aegis/core/critics/logic_critic/critic.py",
    "aegis/core/critics/narrative_fact_critic/critic.py",
    "aegis/core/orchestrator/auto_research.py",
    "aegis/core/reports/html_report_v2.py",
    "aegis/core/truth/verification.py",
})


def _scan_touching_files() -> set[str]:
    """遍历 aegis/ 下全部 .py 生产文件，返回直接触碰 meta_facts 的相对路径集合。"""
    touching: set[str] = set()
    for path in sorted(AEGIS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _TOUCH_PAT.search(text):
            touching.add(path.relative_to(PROJECT_ROOT).as_posix())
    return touching


def test_no_new_files_touch_meta_facts():
    """禁增：白名单之外的任何生产文件都不得直接触碰 meta_facts。"""
    touching = _scan_touching_files()
    intruders = sorted(touching - FROZEN_WHITELIST)
    assert not intruders, (
        "红线 8 棘轮违规：以下文件新增了对 meta_facts 的直接引用（禁增）。\n"
        "新数据请走 pit 层（aegis/pit/store.py）或显式参数传递，"
        "不要扩大 meta_facts 的引用面：\n  - " + "\n  - ".join(intruders)
    )


def test_whitelist_shrinks_with_migration():
    """可缩不可扩：白名单文件若已迁移干净（不再触碰），必须同步从
    FROZEN_WHITELIST 删除，让棘轮真的收紧。"""
    touching = _scan_touching_files()
    stale = sorted(FROZEN_WHITELIST - touching)
    assert not stale, (
        "红线 8 棘轮提示：以下文件已不再直接触碰 meta_facts——请把它们从 "
        "tests/unit/test_meta_facts_ratchet.py 的 FROZEN_WHITELIST 中删除"
        "（白名单只许缩短）：\n  - " + "\n  - ".join(stale)
    )


def test_whitelist_matches_reality_exactly():
    """自洽守卫：上两条合起来等价于 touching == FROZEN_WHITELIST；
    单独再断言一次，棘轮当前状态在测试输出里一目了然。"""
    assert _scan_touching_files() == set(FROZEN_WHITELIST)
