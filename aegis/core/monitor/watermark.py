"""事件水位线库 — Aegis 2.0 Phase 3 任务 A2.

DESIGN_2.0 §五.3 + Phase 3 范围2：论点发布后进入事件循环，每次扫描从
:func:`~aegis.core.acquisition.connectors.em_events_connector.fetch_recent_events`
拿近 90 天的公告 / 业绩预告切片。本模块给每只票记一份**水位线**——已见的
公告 / 预告 key 集合 + 锚定价——扫描时用 fresh 事件的 key 减去已见 key 得
「新增」，把新 key 合并回写即幂等（再扫无新增）。em 返回近 90 天，故错过
几天也能补扫不漏。

key 设计（稳定、可 JSON 往返）：

- 公告 key = ``"{date}|{title}"``——同一天同标题视为同一条公告；
- 预告 key = ``"{report_period}|{forecast_type}|{notice_date}"``——同报告期、
  同类型、同披露日视为同一条预告（预告改版换披露日 → 视作新事件，应复研）。

设计红线 10：不建状态机、不引入 async / 新第三方依赖，整库存一个 JSON
文件（与 thesis JSONL 风格一致）。容错：缺文件 / 坏 JSON → 空库，永不
raise 到调用方。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_SEEN_KEYS",
    "TickerWatermark",
    "WatermarkStore",
    "announcement_key",
    "forecast_key",
    "diff_new",
    "merge_seen",
]

#: 每票每类保留的已见 key 上限（截断时保最近的，防止台账无限膨胀）。
MAX_SEEN_KEYS = 300


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class TickerWatermark:
    """单只票的事件水位线（一份落进整库 JSON 的记录）。"""

    ticker: str
    seen_announcement_keys: list[str] = field(default_factory=list)
    seen_forecast_keys: list[str] = field(default_factory=list)
    last_scan_at: str | None = None                 # 上次扫描时间（iso 字符串）
    anchor_price: float | None = None               # 论点建立 / 上次复研时锚定价
    anchor_thesis_version: int | None = None        # 锚定价对应的论点版本
    last_seen_announcement_date: str | None = None  # 已见最新公告日 YYYY-MM-DD


# ---------------------------------------------------------------------------
# 双形态取值（接受 dataclass 对象与 dict —— JSON 往返后即为 dict）
# ---------------------------------------------------------------------------

def _field(obj: Any, key: str, default: Any = "") -> Any:
    """dict 与 dataclass/对象双形态取值；None 归一为 ``default``。"""
    if isinstance(obj, dict):
        v = obj.get(key, default)
    else:
        v = getattr(obj, key, default)
    return v if v is not None else default


def announcement_key(a: Any) -> str:
    """Announcement 或其 dict → 稳定 key ``"{date}|{title}"``。"""
    return f"{_field(a, 'date')}|{_field(a, 'title')}"


def forecast_key(f: Any) -> str:
    """EarningsForecast 或其 dict → key
    ``"{report_period}|{forecast_type}|{notice_date}"``。"""
    return (
        f"{_field(f, 'report_period')}|"
        f"{_field(f, 'forecast_type')}|"
        f"{_field(f, 'notice_date')}"
    )


# ---------------------------------------------------------------------------
# diff / merge（纯逻辑，永不 raise）
# ---------------------------------------------------------------------------

def diff_new(
    wm: TickerWatermark,
    announcements: list[Any] | None,
    forecasts: list[Any] | None,
) -> tuple[list[Any], list[Any]]:
    """fresh 事件里 key 不在水位线已见集合内的那些（= 新增）。

    只做过滤、保持传入顺序、不改水位线；返回 ``(新增公告, 新增预告)``。
    """
    seen_a = set(wm.seen_announcement_keys)
    seen_f = set(wm.seen_forecast_keys)
    new_a = [a for a in (announcements or [])
             if announcement_key(a) not in seen_a]
    new_f = [f for f in (forecasts or [])
             if forecast_key(f) not in seen_f]
    return new_a, new_f


def _merge_keys(existing: list[str], fresh_keys: list[str]) -> list[str]:
    """把新 key 追加进 seen（去重、保序），截断到 :data:`MAX_SEEN_KEYS` 保最近。

    新 key 追加在末尾（= 最近），截断时保留末尾 ``MAX_SEEN_KEYS`` 个。
    """
    merged = list(existing)
    seen = set(merged)
    for k in fresh_keys:
        if k not in seen:
            merged.append(k)
            seen.add(k)
    if len(merged) > MAX_SEEN_KEYS:
        merged = merged[-MAX_SEEN_KEYS:]
    return merged


def merge_seen(
    wm: TickerWatermark,
    announcements: list[Any] | None,
    forecasts: list[Any] | None,
) -> TickerWatermark:
    """把 fresh 事件的 key 并入 seen 集合，更新 last_seen_announcement_date。

    返回**新的** :class:`TickerWatermark`（不原地改传入对象）；其余字段
    （last_scan_at / anchor_price / anchor_thesis_version）原样透传，由扫描器
    另行更新。合并后再 :func:`diff_new` 同一批事件即为空 → 幂等。
    """
    anns = announcements or []
    fcs = forecasts or []
    merged_a = _merge_keys(wm.seen_announcement_keys,
                           [announcement_key(a) for a in anns])
    merged_f = _merge_keys(wm.seen_forecast_keys,
                           [forecast_key(f) for f in fcs])

    # 已见最新公告日：ISO 日期串按字典序即时序，取 max。
    dates = [str(_field(a, "date") or "").strip() for a in anns]
    dates = [d for d in dates if d]
    last_seen = wm.last_seen_announcement_date
    if dates:
        newest = max(dates)
        if last_seen is None or newest > last_seen:
            last_seen = newest

    return TickerWatermark(
        ticker=wm.ticker,
        seen_announcement_keys=merged_a,
        seen_forecast_keys=merged_f,
        last_scan_at=wm.last_scan_at,
        anchor_price=wm.anchor_price,
        anchor_thesis_version=wm.anchor_thesis_version,
        last_seen_announcement_date=last_seen,
    )


# ---------------------------------------------------------------------------
# 整库读写（缺文件 / 坏 JSON → 空库，永不 raise）
# ---------------------------------------------------------------------------

def _copy(wm: TickerWatermark) -> TickerWatermark:
    """深拷一份水位线（含 list 字段），隔离内存库与调用方的别名。"""
    return TickerWatermark(
        ticker=wm.ticker,
        seen_announcement_keys=list(wm.seen_announcement_keys),
        seen_forecast_keys=list(wm.seen_forecast_keys),
        last_scan_at=wm.last_scan_at,
        anchor_price=wm.anchor_price,
        anchor_thesis_version=wm.anchor_thesis_version,
        last_seen_announcement_date=wm.last_seen_announcement_date,
    )


def _watermark_from_dict(ticker: str, d: Any) -> TickerWatermark:
    """整库 JSON 的一条 value → TickerWatermark（字段缺失 / 类型异常容错）。"""
    if not isinstance(d, dict):
        return TickerWatermark(ticker=ticker)

    def _str_list(v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x) for x in v if isinstance(x, (str, int, float))]

    def _opt_str(v: Any) -> str | None:
        return str(v) if isinstance(v, str) and v else None

    def _opt_float(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) else None

    def _opt_int(v: Any) -> int | None:
        return int(v) if isinstance(v, int) else None

    return TickerWatermark(
        ticker=ticker,
        seen_announcement_keys=_str_list(d.get("seen_announcement_keys")),
        seen_forecast_keys=_str_list(d.get("seen_forecast_keys")),
        last_scan_at=_opt_str(d.get("last_scan_at")),
        anchor_price=_opt_float(d.get("anchor_price")),
        anchor_thesis_version=_opt_int(d.get("anchor_thesis_version")),
        last_seen_announcement_date=_opt_str(d.get("last_seen_announcement_date")),
    )


def _watermark_to_dict(wm: TickerWatermark) -> dict[str, Any]:
    """TickerWatermark → 整库 JSON 的一条 value（ticker 作为库的键，值里不重复）。"""
    return {
        "seen_announcement_keys": list(wm.seen_announcement_keys),
        "seen_forecast_keys": list(wm.seen_forecast_keys),
        "last_scan_at": wm.last_scan_at,
        "anchor_price": wm.anchor_price,
        "anchor_thesis_version": wm.anchor_thesis_version,
        "last_seen_announcement_date": wm.last_seen_announcement_date,
    }


class WatermarkStore:
    """整库水位线库（``{ticker: TickerWatermark}``），一次 load 到内存。

    :meth:`get` 缺省返回空 :class:`TickerWatermark`（不写盘）；:meth:`put`
    更新内存并立刻 :meth:`save` 落盘。构造时读一次整库，缺文件 / 坏 JSON
    一律空库起步，永不 raise。
    """

    #: 整库默认落盘路径。
    DEFAULT_PATH = Path(".cache/monitor/watermarks.json")

    def __init__(self, path: Path | str | None = None) -> None:
        """打开（或新建）水位线库并读回整库到内存。

        Args:
            path: 整库 JSON 路径，缺省 :attr:`DEFAULT_PATH`。
        """
        self.path = Path(path) if path is not None else self.DEFAULT_PATH
        self._data: dict[str, TickerWatermark] = {}
        self._load()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """读回整库；缺文件 / 坏 JSON / 结构异常 → 空库（不崩）。"""
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning("watermark: 库 %s 损坏，空库起步: %s", self.path, e)
            return
        if not isinstance(raw, dict):
            logger.warning("watermark: 库 %s 结构异常，空库起步", self.path)
            return
        for ticker, d in raw.items():
            key = str(ticker)
            self._data[key] = _watermark_from_dict(key, d)

    def save(self) -> None:
        """整库 JSON 落盘（mkdir 父目录）；落盘失败仅告警，不 raise。"""
        payload = {
            ticker: _watermark_to_dict(wm)
            for ticker, wm in sorted(self._data.items())
        }
        # 审查发现 #6：原子落盘（临时文件 + os.replace），杜绝并发扫描读到
        # 半截 JSON 被当空库丢更新。os.replace 在同一文件系统上是原子替换。
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(self.path))
        except OSError as e:
            logger.warning("watermark: 库 %s 落盘失败: %s", self.path, e)

    # ------------------------------------------------------------------
    # 查询 / 更新
    # ------------------------------------------------------------------

    def get(self, ticker: str) -> TickerWatermark:
        """取一只票的水位线；缺省返回空 :class:`TickerWatermark`（不写盘）。

        返回内存记录的拷贝，调用方原地改动不会污染内存库——须经 :meth:`put`
        才回写。
        """
        key = str(ticker)
        wm = self._data.get(key)
        if wm is None:
            return TickerWatermark(ticker=key)
        return _copy(wm)

    def put(self, wm: TickerWatermark) -> None:
        """写回一只票的水位线：更新内存 + 立刻整库落盘。"""
        self._data[str(wm.ticker)] = _copy(wm)
        self.save()
