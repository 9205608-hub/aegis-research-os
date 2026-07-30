"""L1 Wave 1（2026-07-31）：A 股分部收入摄取（东财主营构成 zygc）。

七轮 Grok 审计（GROK_REAUDIT_2026-07-12.md）每轮通杀的扣分："分部收入 /
分部毛利结构未闭合，论点把自承未知当已证实事实使用"——根因是 A 股路径的
segment_detail 一直是空 dict（cninfo_connector:185），agents 拿不到任何
分部数据，只能在 open_questions 里反复追问。

数据源：akshare ``stock_zygc_em``（东财 PC_HSF10 zygcfx 的包装），三轴
（按产品 / 按地区 / 按行业），含收入 / 成本 / 利润 / 毛利率 / 收入占比，
多报告期（年报 + 中报）。2026-07-31 实测 300750：177 行，含 2026-06-30
中报期与 2025-12-31 年报期。

产出三份消费物：
- ``detail``：segment_detail 契约（{axis: {name: {"revenue": ...}}}），
  BUG-46 去重 / Segment-DCF / Report Editor 既有管道直接点亮；
- ``lines_zh``：prompt 注入用中文行（agents + synthesizer），模式同
  aegis.core.truth.model_free_anchors；
- ``sanctioned_pcts``：分部占比 / 毛利率百分数进清洗白名单（设计红线 9
  同则——引用真数据的 % 不许被 strict 清洗误杀）。
"""

from __future__ import annotations

from typing import Any

from aegis.core.acquisition.connectors.akshare_connector import _no_proxy

# 东财轴名 → segment_detail 类目键（与 EDGAR 侧的类目命名习惯对齐）
_AXIS_MAP = {
    "按产品分类": "product",
    "按地区分类": "region",
    "按行业分类": "industry",
}
_AXIS_ZH = {"product": "分产品", "region": "分地区", "industry": "分行业"}

# lines_zh 每轴最多列出的分部数——透传要全，但清单无限长是审计眼里的噪声
# （R2-3 触发器"垃圾场"教训同则）。
_MAX_SEGMENTS_PER_AXIS = 8


def _em_symbol(stock_code: str) -> str:
    """6 位代码 → 东财 symbol（SH/SZ/BJ 前缀）。"""
    code = str(stock_code).strip()[:6]
    if code.startswith(("6", "9", "5")):
        return f"SH{code}"
    if code.startswith(("8", "4")):
        return f"BJ{code}"
    return f"SZ{code}"


def _yi(v: float) -> str:
    """人民币金额 → 亿元显示（A 股惯例）。"""
    return f"¥{v / 1e8:.1f}亿"


def fetch_segment_composition(stock_code: str) -> dict[str, Any] | None:
    """拉取并归一分部构成。网络失败 / 无数据返回 None，永不 raise。"""
    try:
        with _no_proxy():
            import akshare as ak
            df = ak.stock_zygc_em(symbol=_em_symbol(stock_code))
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        return _normalize(df)
    except Exception:
        return None


def _normalize(df: Any) -> dict[str, Any] | None:
    need = {"报告日期", "分类类型", "主营构成", "主营收入"}
    if not need.issubset(set(df.columns)):
        return None
    df = df.copy()
    df["报告日期"] = df["报告日期"].astype(str).str[:10]
    periods = sorted({p for p in df["报告日期"] if p and p != "nan"}, reverse=True)
    if not periods:
        return None
    annuals = [p for p in periods if p.endswith("12-31")]
    fiscal_period = annuals[0] if annuals else periods[0]
    latest_period = periods[0]

    def _axes_for(period: str) -> dict[str, dict[str, dict[str, float]]]:
        out: dict[str, dict[str, dict[str, float]]] = {}
        sub = df[df["报告日期"] == period]
        for axis_zh, axis_key in _AXIS_MAP.items():
            rows = sub[sub["分类类型"] == axis_zh]
            if rows.empty:
                continue
            segs: dict[str, dict[str, float]] = {}
            for _, r in rows.iterrows():
                name = str(r["主营构成"]).strip()
                if not name:
                    continue
                entry: dict[str, float] = {}
                for src, dst in (
                    ("主营收入", "revenue"), ("主营成本", "cost"),
                    ("主营利润", "profit"), ("毛利率", "gross_margin"),
                    ("收入比例", "revenue_share"),
                ):
                    v = r.get(src)
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    if fv == fv:  # 滤 NaN
                        entry[dst] = fv
                if entry.get("revenue"):
                    segs[name] = entry
            if segs:
                out[axis_key] = segs
        return out

    detail = _axes_for(fiscal_period)
    if not detail:
        return None

    # ── lines_zh：年报期全轴 + （若更近）最新中报期的产品轴 ──
    lines: list[str] = []
    pcts: list[float] = []

    def _fmt_axis(period: str, axis_key: str,
                  segs: dict[str, dict[str, float]]) -> None:
        ranked = sorted(segs.items(),
                        key=lambda kv: kv[1].get("revenue", 0.0), reverse=True)
        shown = ranked[:_MAX_SEGMENTS_PER_AXIS]
        parts = []
        for name, e in shown:
            bits = [_yi(e["revenue"])]
            share = e.get("revenue_share")
            if share is not None:
                bits.append(f"占{share * 100:.1f}%")
                pcts.append(round(share * 100, 1))
            gm = e.get("gross_margin")
            if gm is not None:
                bits.append(f"毛利率{gm * 100:.1f}%")
                pcts.append(round(gm * 100, 1))
            parts.append(f"{name} {('，'.join(bits))}")
        omitted = len(ranked) - len(shown)
        tail = f"；（另 {omitted} 项从略）" if omitted > 0 else ""
        lines.append(f"[{period} {_AXIS_ZH[axis_key]}] " + "；".join(parts) + tail)

    for axis_key in ("product", "region", "industry"):
        if axis_key in detail:
            _fmt_axis(fiscal_period, axis_key, detail[axis_key])
    if latest_period != fiscal_period:
        interim = _axes_for(latest_period)
        for axis_key in ("product", "region"):
            if axis_key in interim:
                _fmt_axis(latest_period, axis_key, interim[axis_key])

    return {
        "source": "eastmoney_zygc",
        "fiscal_period": fiscal_period,
        "latest_period": latest_period,
        "detail": detail,
        "lines_zh": lines,
        # 设计红线 9：真实数据派生的 % 注册进清洗白名单（去重保序）
        "sanctioned_pcts": list(dict.fromkeys(pcts)),
        "source_note": f"东财主营构成（zygc），年报期 {fiscal_period}，最新期 {latest_period}",
    }


def segment_sanctioned_pcts(seg: Any) -> list[float]:
    """从 __segment_composition 块提取白名单 %（缺省容错）。

    红线 8 说明：本模块自身不读写共享事实字典——盖章由 orchestrator
    （棘轮白名单内）完成，这里只接收已提取的块作显式参数。
    """
    if isinstance(seg, dict):
        vals = seg.get("sanctioned_pcts")
        if isinstance(vals, list):
            return [float(v) for v in vals if isinstance(v, (int, float))]
    return []
