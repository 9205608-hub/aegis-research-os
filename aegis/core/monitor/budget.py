"""每日 LLM 预算熔断 — Aegis 2.0 Phase 3 任务 A4.

公告密集期（年报季）扫描会触发多次 ``--update`` 复研，每次可能烧 LLM。
本模块按**当日累计成本**熔断：超过每日上限就跳过后续 update，防止单日成本
失控（设计红线：单日成本必须有上限）。

口径与项目既有 :class:`~aegis.core.llm.config.CostTracker` 一致——成本单位
一律美元（USD）。当日成本落台账 JSON，charge 后立刻落盘，因此**跨进程 /
跨次扫描累计**：同一天多次 ``new DailyBudget`` 都能读回累计已花值。

台账文件（设计红线 10：存储用 JSON 文件，与 thesis JSONL 风格一致）::

    {dir}/{YYYYMMDD}.json = {"date": "YYYY-MM-DD", "spent_usd": 12.34,
                             "runs": [{"ticker","cost_usd","at"}, ...]}

容错：坏台账 / 缺文件一律当日从 0 起，永不 raise 到调用方。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["SpendRecord", "DailyBudget"]


@dataclass
class SpendRecord:
    """一笔 LLM 花费（落进当日台账的 ``runs``）。"""

    ticker: str
    cost_usd: float
    at: str  # iso 时间戳


class DailyBudget:
    """当日 LLM 成本熔断器（成本单位 USD）。

    构造时从台账读回当日累计已花值；:meth:`charge` 记一笔并立刻落盘。
    ``daily_cap_usd <= 0`` 视为不限额（:meth:`can_afford` 永远 True）。
    """

    #: 台账默认落盘目录。
    DEFAULT_DIR = Path(".cache/monitor/spend")

    def __init__(
        self,
        daily_cap_usd: float,
        *,
        dir: Path | str | None = None,
        today: str | None = None,
    ) -> None:
        """初始化熔断器并读回当日台账。

        Args:
            daily_cap_usd: 每日成本上限（USD）；``<= 0`` 视为不限额。
            dir: 台账目录，缺省 :attr:`DEFAULT_DIR`。
            today: 当日日期 ``YYYY-MM-DD``，缺省取 ``date.today().isoformat()``。
        """
        try:
            self.daily_cap_usd = float(daily_cap_usd)
        except (TypeError, ValueError):
            self.daily_cap_usd = 0.0
        self._dir = Path(dir) if dir is not None else self.DEFAULT_DIR
        self.today = (today or date.today().isoformat()).strip()

        self._spent_usd: float = 0.0
        self._runs: list[dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------
    # 台账读写（坏文件 / 缺文件一律从 0 起，永不 raise）
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """当日台账文件路径（文件名用无横线日期 ``YYYYMMDD.json``）。"""
        stamp = self.today.replace("-", "")
        return self._dir / f"{stamp}.json"

    def _load(self) -> None:
        """读回当日台账；缺文件 / 坏文件 → 当日从 0 起。"""
        path = self.path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning("daily budget: 台账 %s 损坏，当日从 0 起: %s", path, e)
            return
        if not isinstance(raw, dict):
            logger.warning("daily budget: 台账 %s 结构异常，当日从 0 起", path)
            return
        runs = raw.get("runs")
        self._runs = list(runs) if isinstance(runs, list) else []
        spent = raw.get("spent_usd")
        if isinstance(spent, (int, float)):
            self._spent_usd = float(spent)
        else:
            # 台账缺 spent_usd 字段时，由 runs 重算兜底。
            self._spent_usd = sum(
                float(r.get("cost_usd", 0.0))
                for r in self._runs
                if isinstance(r, dict) and isinstance(
                    r.get("cost_usd"), (int, float))
            )

    def _save(self) -> None:
        """落盘当日台账（幂等：charge 后立刻调用）。"""
        payload = {
            "date": self.today,
            "spent_usd": round(self._spent_usd, 6),
            "runs": self._runs,
        }
        # 审查发现 #6：原子落盘（临时文件 + os.replace），杜绝并发扫描读到
        # 半截 JSON 被当空台账 → 当日已花清零 → 预算熔断失效。
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(self.path))
        except OSError as e:
            logger.warning("daily budget: 台账 %s 落盘失败: %s", self.path, e)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def spent_today(self) -> float:
        """当日累计已花（USD）。"""
        return self._spent_usd

    def remaining(self) -> float:
        """当日剩余额度（USD），``max(0, cap - spent)``；不限额时返回 0.0。"""
        if self.daily_cap_usd <= 0:
            return 0.0
        return max(0.0, self.daily_cap_usd - self._spent_usd)

    def can_afford(self, est_usd: float = 0.0) -> bool:
        """当日预算是否还能承担一笔预估成本 ``est_usd``。

        规则：当日已花 ``< 上限`` **且** ``已花 + est <= 上限`` 时 True。
        ``cap <= 0`` 视为不限额，永远 True。负的 ``est`` 视为 0。
        """
        if self.daily_cap_usd <= 0:
            return True
        try:
            est = max(0.0, float(est_usd))
        except (TypeError, ValueError):
            est = 0.0
        return (self._spent_usd < self.daily_cap_usd
                and self._spent_usd + est <= self.daily_cap_usd)

    # ------------------------------------------------------------------
    # 计费
    # ------------------------------------------------------------------

    def charge(self, ticker: str, cost_usd: float) -> None:
        """记一笔花费：追加 ``runs`` + 累加 ``spent_usd`` + 立刻落盘。

        ``cost_usd < 0`` 视为 0（不倒扣）。坏输入不崩。
        """
        try:
            cost = float(cost_usd)
        except (TypeError, ValueError):
            cost = 0.0
        if cost < 0:
            cost = 0.0
        rec = SpendRecord(
            ticker=str(ticker or ""),
            cost_usd=cost,
            at=datetime.now().isoformat(),
        )
        self._runs.append(asdict(rec))
        self._spent_usd += cost
        self._save()
