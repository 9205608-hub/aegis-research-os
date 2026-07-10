"""PIT (point-in-time) fact store — Aegis 2.0 Phase 1 信息架构核心.

DESIGN_2.0 理念 B：单时点年报快照 → 时点数据库。本模块把两个月前设计好、
从未接线的 ``AtomicAccountingFact`` 死合同（accepted_at / effective_at /
restatement_flag / fact_version）翻译成 sqlite3 表结构，字段语义对照：

    AtomicAccountingFact.accepted_at      → facts.as_of        （系统摄取时刻，诚实 knowledge time）
    AtomicAccountingFact.effective_at     → facts.announce_date（披露日，economic knowledge time）
    AtomicAccountingFact.restatement_flag → facts.restatement_of（指向被重述的上一版本 id）
    AtomicAccountingFact.fact_version     → facts.fact_version （同键重录自动递增）

设计红线（DESIGN_2.0 六、必须遵守）：
- 红线 #3：双时间戳。``as_of`` 只保证 forward-looking 正确；历史回填必须
  显式 ``backfilled=True``。as-of 查询按 knowledge-time 语义过滤——
  晚摄取的事实在早 as_of 查询下不可见。
- 红线 #8：concept 列绑定 MetricRegistry 词表，禁自由字符串；
  ``register_concept()`` 是唯一显式逃生口。
- 红线 #10：sqlite3 标准库，零新依赖。单连接 + threading.Lock
  （单机低并发，不做连接池）。

业绩预告是区间型事实：``value_low`` / ``value_high`` 承载区间，
快报打 ``unaudited=True``，正式报告到达时以同 period 新记录替换
（``latest_value(prefer_audited=True)`` 自动偏好审计值）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from aegis.core.truth.registry.metric_registry import MetricRegistry
from aegis.core.truth.registry.seed_metrics import create_seeded_registry

__all__ = [
    "DEFAULT_DB_PATH",
    "FISCAL_PERIODS",
    "PITFact",
    "PITStore",
    "PITStoreError",
    "UnknownConceptError",
]

#: 默认库路径（.cache/ 已在 .gitignore）
DEFAULT_DB_PATH = Path(".cache") / "pit.db"

#: A 股披露节点：季报是年初累计值，故只有 Q1/H1/Q3/FY 四个报告期形态
FISCAL_PERIODS = frozenset({"FY", "Q1", "H1", "Q3"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id      TEXT    NOT NULL,
    concept        TEXT    NOT NULL,
    value          REAL,
    value_low      REAL,
    value_high     REAL,
    period         TEXT    NOT NULL,
    fiscal_period  TEXT,
    as_of          TEXT    NOT NULL,
    announce_date  TEXT,
    source         TEXT    NOT NULL,
    unaudited      INTEGER NOT NULL DEFAULT 0,
    backfilled     INTEGER NOT NULL DEFAULT 0,
    restatement_of INTEGER,
    fact_version   INTEGER NOT NULL DEFAULT 1,
    meta           TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_entity_concept_period
    ON facts (entity_id, concept, period);
"""

_COLUMNS = (
    "id", "entity_id", "concept", "value", "value_low", "value_high",
    "period", "fiscal_period", "as_of", "announce_date", "source",
    "unaudited", "backfilled", "restatement_of", "fact_version", "meta",
)


class PITStoreError(Exception):
    """PIT store 操作失败的基类异常。"""


class UnknownConceptError(PITStoreError):
    """concept 不在 MetricRegistry 词表内且未经 register_concept 注册。"""


@dataclass(frozen=True)
class PITFact:
    """一条时点事实（facts 表一行的类型化视图）。

    所有字段均可 JSON 序列化（见 :meth:`to_dict`）。
    """

    id: int
    entity_id: str
    concept: str
    value: float | None
    value_low: float | None
    value_high: float | None
    period: str
    fiscal_period: str | None
    as_of: str
    announce_date: str | None
    source: str
    unaudited: bool
    backfilled: bool
    restatement_of: int | None
    fact_version: int
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """返回全部字段的 JSON 可序列化字典。"""
        return asdict(self)


def _norm_ts(value: str | datetime | None) -> str:
    """归一化时间戳到 UTC ISO 字符串（同一格式 → 字符串比较即时间比较）。

    naive datetime / 无时区字符串一律视为 UTC；None 取当前时刻。
    """
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise PITStoreError(f"无法解析时间戳: {value!r}") from exc
    if not isinstance(value, datetime):
        raise PITStoreError(f"时间戳类型不支持: {type(value).__name__}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"


def _norm_date(value: str | date | datetime | None, *, label: str) -> str | None:
    """归一化日期到 YYYY-MM-DD 字符串；None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError as exc:
            raise PITStoreError(f"{label} 不是合法日期 (YYYY-MM-DD): {value!r}") from exc
    raise PITStoreError(f"{label} 类型不支持: {type(value).__name__}")


class PITStore:
    """sqlite3 时点事实库。

    Parameters
    ----------
    db_path:
        库文件路径；默认 ``.cache/pit.db``（gitignored）。测试注入 tmp_path。
    registry:
        concept 词表来源；默认 ``create_seeded_registry()``。词表 =
        所有 metric_name ∪ 所有 allowed_inputs。词表外 concept 一律
        raise :class:`UnknownConceptError`，除非先 :meth:`register_concept`。
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        registry: MetricRegistry | None = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

        if registry is None:
            registry = create_seeded_registry()
        self._allowed_concepts: set[str] = set()
        for defn in registry.list_all():
            self._allowed_concepts.add(defn.metric_name)
            self._allowed_concepts.update(defn.allowed_inputs)
        #: 经 register_concept 显式注册的扩展词（进程内有效，摄取端每次启动重注册）
        self._extra_concepts: set[str] = set()

    # ------------------------------------------------------------------
    # concept 词表治理（红线 #8）
    # ------------------------------------------------------------------

    def register_concept(self, concept: str) -> None:
        """显式逃生口：把一个 registry 外的 concept 加入本实例词表。

        仅进程内有效（不落库）——摄取端必须在代码里显式声明它引入了
        什么新概念，这正是"禁自由字符串"要的可审计性。
        """
        if not concept or not isinstance(concept, str):
            raise PITStoreError("concept 必须是非空字符串")
        self._extra_concepts.add(concept)

    def is_known_concept(self, concept: str) -> bool:
        return concept in self._allowed_concepts or concept in self._extra_concepts

    def _check_concept(self, concept: str) -> None:
        if not self.is_known_concept(concept):
            raise UnknownConceptError(
                f"concept {concept!r} 不在 MetricRegistry 词表内。"
                f"如确需新概念，请先调用 register_concept({concept!r})。"
            )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def record_fact(
        self,
        *,
        entity_id: str,
        concept: str,
        period: str | date,
        source: str,
        value: float | None = None,
        value_low: float | None = None,
        value_high: float | None = None,
        fiscal_period: str | None = None,
        as_of: str | datetime | None = None,
        announce_date: str | date | None = None,
        unaudited: bool = False,
        backfilled: bool = False,
        restatement_of: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """写入一条事实，返回其 id。

        版本语义：同 (entity_id, concept, period, source) 键再录**不同值**
        = 自动生成新 fact_version 并把 restatement_of 指向上一版本 id，
        历史版本永不删除；重录**相同值**幂等返回既有 id（不产生新行、
        不篡改原 as_of）。
        """
        if not entity_id:
            raise PITStoreError("entity_id 不能为空")
        if not source:
            raise PITStoreError("source 不能为空")
        self._check_concept(concept)
        if value is None and value_low is None and value_high is None:
            raise PITStoreError("value 与 value_low/value_high 不能同时为空")
        if fiscal_period is not None and fiscal_period not in FISCAL_PERIODS:
            raise PITStoreError(
                f"fiscal_period {fiscal_period!r} 非法，A 股披露节点只有 {sorted(FISCAL_PERIODS)}"
            )

        period_s = _norm_date(period, label="period")
        as_of_s = _norm_ts(as_of)
        announce_s = _norm_date(announce_date, label="announce_date")
        meta_s = json.dumps(meta or {}, ensure_ascii=False, sort_keys=True)

        with self._lock:
            cur = self._conn.execute(
                "SELECT id, value, value_low, value_high, fact_version FROM facts "
                "WHERE entity_id=? AND concept=? AND period=? AND source=? "
                "ORDER BY fact_version DESC, id DESC LIMIT 1",
                (entity_id, concept, period_s, source),
            )
            prev = cur.fetchone()
            if prev is not None:
                same_value = (
                    prev["value"] == value
                    and prev["value_low"] == value_low
                    and prev["value_high"] == value_high
                )
                if same_value:
                    return int(prev["id"])  # 幂等：不产生新版本
                fact_version = int(prev["fact_version"]) + 1
                if restatement_of is None:
                    restatement_of = int(prev["id"])
            else:
                fact_version = 1

            cur = self._conn.execute(
                "INSERT INTO facts (entity_id, concept, value, value_low, value_high, "
                "period, fiscal_period, as_of, announce_date, source, unaudited, "
                "backfilled, restatement_of, fact_version, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id, concept, value, value_low, value_high,
                    period_s, fiscal_period, as_of_s, announce_s, source,
                    int(bool(unaudited)), int(bool(backfilled)),
                    restatement_of, fact_version, meta_s,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> PITFact:
        raw_meta = row["meta"]
        try:
            meta = json.loads(raw_meta) if raw_meta else {}
        except (TypeError, ValueError):
            meta = {}
        return PITFact(
            id=int(row["id"]),
            entity_id=row["entity_id"],
            concept=row["concept"],
            value=row["value"],
            value_low=row["value_low"],
            value_high=row["value_high"],
            period=row["period"],
            fiscal_period=row["fiscal_period"],
            as_of=row["as_of"],
            announce_date=row["announce_date"],
            source=row["source"],
            unaudited=bool(row["unaudited"]),
            backfilled=bool(row["backfilled"]),
            restatement_of=row["restatement_of"],
            fact_version=int(row["fact_version"]),
            meta=meta if isinstance(meta, dict) else {},
        )

    def get_facts(
        self,
        entity_id: str,
        concept: str | None = None,
        *,
        as_of: str | datetime | None = None,
    ) -> list[PITFact]:
        """按 knowledge-time 语义取事实（红线 #3 核心）。

        ``as_of`` 给定时，只返回 **摄取时刻 ≤ as_of** 的记录——
        晚于 as_of 摄取的事实（哪怕它描述更早的报告期）一律不可见。
        ``as_of=None`` = 返回全部（当前全知视角）。
        """
        sql = "SELECT * FROM facts WHERE entity_id=?"
        params: list[Any] = [entity_id]
        if concept is not None:
            sql += " AND concept=?"
            params.append(concept)
        if as_of is not None:
            sql += " AND as_of<=?"
            params.append(_norm_ts(as_of))
        sql += " ORDER BY period ASC, concept ASC, fact_version ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def latest_value(
        self,
        entity_id: str,
        concept: str,
        *,
        as_of: str | datetime | None = None,
        prefer_audited: bool = True,
    ) -> PITFact | None:
        """返回（as_of 视角下）最新报告期的最新版本事实。

        选取规则：
        1. 先过滤 knowledge-time（as_of 语义同 :meth:`get_facts`）；
        2. 取 period 最大的报告期；
        3. ``prefer_audited=True`` 且该期存在审计值（unaudited=0）时，
           只在审计值中选——快报被同期正式报告自然取代；
        4. 余下候选按 (as_of, fact_version, id) 取最新——重述链上
           永远返回最新版本。
        """
        candidates = self.get_facts(entity_id, concept, as_of=as_of)
        if not candidates:
            return None
        latest_period = max(f.period for f in candidates)
        pool = [f for f in candidates if f.period == latest_period]
        if prefer_audited:
            audited = [f for f in pool if not f.unaudited]
            if audited:
                pool = audited
        return max(pool, key=lambda f: (f.as_of, f.fact_version, f.id))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "PITStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
