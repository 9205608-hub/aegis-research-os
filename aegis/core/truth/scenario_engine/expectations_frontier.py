"""Expectations Frontier Solver — 反解市场价格隐含的增长预期.

Aegis 2.0 Phase 0 旗舰引擎（DESIGN_2.0.md 三.A / 五.Phase 0 第 2 项）。

1.x 框架问的是"公司值多少钱"，然后把与市价的差值当"错误定价"输出。
本引擎把问题反过来：

    "当前价格隐含了什么样的预期？该预期与可验证事实是否相容？
     证伪它需要看什么信号？"

方法 —— (增速 g × 利润率 m) 条件化反解：

* 一个价格反解不出"增速 + 利润率"两个未知数（不可辨识）。调用方固定
  2-3 档终年利润率情景（维持现状 / 行业中位 / 管理层目标——引擎不猜，
  由调用方传入），每档反解一个隐含 flat 增速。
* 每档利润率情景把 operating margin path 从当前值线性收敛到 target
  （horizon 年，终年恰为 target）。
* 每档对 WACC ∈ {base−1%, base, base+1%} 各解一遍 —— 设计红线 2：
  输出必须条件化并附 WACC±1% 三列，禁止单点"市场隐含增速 Z%"。
* 对每个 (margin, WACC) 单元：g 在 [-0.20, +0.80] 网格（步长 0.005）
  上扫 flat 增速路径，逐点跑与 forward 完全同一的 DCF 模型（DCFInput
  其余字段全部继承 —— AUDIT-A2 教训：正反向必须同一模型），对
  per_share(g) − market_price 做变号检测，相邻两点线性插值精化。

为什么用网格而不是 bisection：对亏损/高资本开支公司，price-vs-growth
曲线非单调（BUG-Y13 —— 增速越高，前期 capex/NWC 失血越大，终值追不
回来），一维 bisection 会静默收敛到边界伪值。穷举扫描找出**全部**过零
点并如实返回；没有过零点本身就是有力结论（"即使 +80% 增速该利润率档
也撑不起现价"），以结构化诊断输出而非报错。

规格 4：隐含增速对应 horizon 年累计营收 scale >30× 时只打
extreme_expectation 标志，**不 cap** —— 我们在还原市场的原始预期，
截断会失真（与 forward 路径的 BUG-Y23 cap 语义不同，勿混用）。

自然语言诊断按 zh/en 双语输出，渲染层按市场选用（中文化铁律）。
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from .dcf_engine import DCFEngine, DCFInput

# ── Grid spec (DESIGN_2.0.md Phase 0 item 2) ──
GROWTH_GRID_LOW = -0.20
GROWTH_GRID_HIGH = 0.80
GROWTH_GRID_STEP = 0.005
DEFAULT_WACC_DELTAS: tuple[float, ...] = (-0.01, 0.0, 0.01)
# 规格 4: (1+g)^horizon > 30× → extreme_expectation 标志（只标注，不 cap）
EXTREME_REVENUE_SCALE = 30.0

_CURRENCY_SYMBOLS = {"CNY": "¥", "USD": "$", "HKD": "HK$"}

# ── Column status values ──
STATUS_SOLVED = "solved"
STATUS_MULTIPLE_SOLUTIONS = "multiple_solutions"
STATUS_NO_SOLUTION = "no_solution"
STATUS_NO_VALID_GRID_POINTS = "no_valid_grid_points"

# ── Diagnostic codes (machine-readable; zh/en text derived from these) ──
DIAG_VALUE_ABOVE_PRICE = "value_above_price_everywhere"   # 现价低于全前沿
DIAG_VALUE_BELOW_PRICE = "value_below_price_everywhere"   # +80% 也撑不起现价
DIAG_NO_VALID_GRID_POINTS = "no_valid_grid_points"        # 整列格点无法计算
DIAG_NO_CROSSING = "no_crossing_detected"                 # 理论不可达的兜底


@dataclass(frozen=True)
class ImpliedGrowthSolution:
    """per_share(g) == market_price 的一个根（单个 margin×WACC 单元内）。"""

    implied_growth: float
    # (1 + g)^horizon —— 终年营收相对基年的倍数（"10 年累计营收 scale"）
    cumulative_revenue_scale: float
    # 规格 4：scale > 30× 时为 True。只标注，不 cap。
    extreme_expectation: bool


@dataclass(frozen=True)
class WACCColumn:
    """一个 WACC 档位下的求解结果（每档利润率情景有三列）。"""

    wacc: float
    wacc_delta: float  # 相对 base WACC 的偏移：-0.01 / 0.0 / +0.01
    # solved | multiple_solutions | no_solution | no_valid_grid_points
    status: str
    solutions: list[ImpliedGrowthSolution]
    multiple_solutions: bool
    diagnostic_code: str  # "" when solved cleanly
    diagnostic_zh: str
    diagnostic_en: str
    # 边界诊断：有效格点上 per_share 的最小/最大值（全列无效时为 None）
    grid_price_min: float | None
    grid_price_max: float | None
    valid_grid_points: int


@dataclass(frozen=True)
class MarginScenarioResult:
    """一档终年利润率情景的完整结果（含 WACC±1% 三列）。"""

    label: str
    target_margin: float
    starting_margin: float
    margin_path: list[float]  # 从 starting_margin 线性收敛到 target_margin
    wacc_columns: list[WACCColumn]  # 按 WACC 升序：base−1%, base, base+1%


@dataclass(frozen=True)
class ExpectationsFrontier:
    """预期前沿求解结果 —— 可 JSON 序列化的 dataclass 树。

    渲染示例（设计红线 2 的条件化小表）:
        "¥13.76 隐含：若利润率维持 2.9% → 需 XX% 增速；
         若达行业中位 8% → 需 YY% 增速"（每档附 WACC±1% 三列）
    """

    market_price: float
    currency: str
    base_wacc: float
    horizon_years: int
    growth_grid_low: float
    growth_grid_high: float
    growth_grid_step: float
    scenarios: list[MarginScenarioResult]

    def to_dict(self) -> dict:
        """纯 primitives 的嵌套 dict —— 直接喂 json.dumps / prompt / 渲染。"""
        return asdict(self)


def _build_margin_path(start: float, target: float, n: int) -> list[float]:
    """从当前利润率线性收敛到 target；终年（第 n 年）恰等于 target。"""
    return [start + (target - start) * ((i + 1) / n) for i in range(n)]


def _fmt_pct(x: float) -> str:
    return f"{x:+.1%}"


def _solve_column(
    engine: DCFEngine,
    base_input: DCFInput,
    margin_path: list[float],
    wacc: float,
    wacc_delta: float,
    market_price: float,
    grid: list[float],
    symbol: str,
) -> WACCColumn:
    """在一个 (margin scenario × WACC) 单元上做全网格扫描 + 变号检测。"""
    n = base_input.horizon_years

    # 逐点求值。compute_dcf 抛 ValueError（如 g 组合触发校验、wacc ≤ 永续
    # 增速）的格点跳过（规格 2）。warnings（Y1 FCFF sanity / WACC 范围）
    # 在 ~200 次扫描里只是噪音，整列静音一次。
    valid: list[tuple[float, float]] = []  # (g, per_share)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for g in grid:
            candidate = replace(
                base_input,
                revenue_growth_path=[g] * n,
                operating_margin_path=list(margin_path),
                wacc=wacc,
            )
            try:
                out = engine.compute_dcf(candidate)
            except ValueError:
                continue
            valid.append((g, out.per_share_value))

    lo_g, hi_g = grid[0], grid[-1]

    if not valid:
        zh = (
            f"该 WACC 档（{wacc:.1%}）下增速网格 "
            f"[{_fmt_pct(lo_g)}, {_fmt_pct(hi_g)}] 内所有格点均无法计算"
            f"（如 WACC 不高于永续增速），无法求解隐含增速。"
        )
        en = (
            f"At WACC {wacc:.1%}, no growth grid point in "
            f"[{_fmt_pct(lo_g)}, {_fmt_pct(hi_g)}] is computable "
            f"(e.g. WACC does not exceed terminal growth); "
            f"implied growth cannot be solved."
        )
        return WACCColumn(
            wacc=wacc,
            wacc_delta=wacc_delta,
            status=STATUS_NO_VALID_GRID_POINTS,
            solutions=[],
            multiple_solutions=False,
            diagnostic_code=DIAG_NO_VALID_GRID_POINTS,
            diagnostic_zh=zh,
            diagnostic_en=en,
            grid_price_min=None,
            grid_price_max=None,
            valid_grid_points=0,
        )

    # ── 变号检测 + 线性插值精化（相邻有效格点；无效格点已被跳过） ──
    diffs = [(g, price - market_price) for g, price in valid]
    roots: list[float] = []
    for i in range(len(diffs) - 1):
        g1, d1 = diffs[i]
        g2, d2 = diffs[i + 1]
        if d1 == 0.0:
            roots.append(g1)
        elif d1 * d2 < 0:
            roots.append(g1 + (g2 - g1) * (-d1) / (d2 - d1))
    if diffs[-1][1] == 0.0:
        roots.append(diffs[-1][0])

    prices = [p for _, p in valid]
    price_min, price_max = min(prices), max(prices)

    if roots:
        solutions = []
        for g in roots:
            scale = (1.0 + g) ** n
            solutions.append(ImpliedGrowthSolution(
                implied_growth=g,
                cumulative_revenue_scale=scale,
                # 规格 4：只标注，不 cap —— 还原市场原始预期
                extreme_expectation=scale > EXTREME_REVENUE_SCALE,
            ))
        multiple = len(solutions) > 1
        zh = en = ""
        if multiple:
            gs = "、".join(_fmt_pct(s.implied_growth) for s in solutions)
            gs_en = ", ".join(_fmt_pct(s.implied_growth) for s in solutions)
            zh = (
                f"价格-增速曲线在该利润率档下非单调，存在 {len(solutions)} 个"
                f"隐含增速解（{gs}）——同一现价可由多种增长预期支撑，"
                f"解读时须结合定价体制与验证点。"
            )
            en = (
                f"The price-vs-growth curve is non-monotonic under this margin "
                f"scenario: {len(solutions)} implied-growth solutions "
                f"({gs_en}). The same price is consistent with multiple "
                f"growth expectations; interpret alongside the pricing regime "
                f"and verification checklist."
            )
        return WACCColumn(
            wacc=wacc,
            wacc_delta=wacc_delta,
            status=STATUS_MULTIPLE_SOLUTIONS if multiple else STATUS_SOLVED,
            solutions=solutions,
            multiple_solutions=multiple,
            diagnostic_code="",
            diagnostic_zh=zh,
            diagnostic_en=en,
            grid_price_min=price_min,
            grid_price_max=price_max,
            valid_grid_points=len(valid),
        )

    # ── 无解：边界诊断本身是有力结论，结构化输出（规格 2） ──
    all_above = all(d > 0 for _, d in diffs)
    all_below = all(d < 0 for _, d in diffs)
    if all_above:
        code = DIAG_VALUE_ABOVE_PRICE
        zh = (
            f"即使增速低至 {_fmt_pct(lo_g)}，该利润率档 DCF 每股价值"
            f"（最低 {symbol}{price_min:.2f}）仍高于现价 "
            f"{symbol}{market_price:.2f} —— 市场定价低于该情景的全部前沿，"
            f"现价未隐含正增长预期。"
        )
        en = (
            f"Even at {_fmt_pct(lo_g)} growth, this margin scenario values "
            f"the shares at no less than {symbol}{price_min:.2f} — above the "
            f"current price {symbol}{market_price:.2f}. The market price sits "
            f"below the entire frontier for this scenario; no positive growth "
            f"expectation is embedded in the price."
        )
    elif all_below:
        code = DIAG_VALUE_BELOW_PRICE
        zh = (
            f"即使增速高达 {_fmt_pct(hi_g)}，该利润率档 DCF 每股价值"
            f"（最高 {symbol}{price_max:.2f}）也撑不起现价 "
            f"{symbol}{market_price:.2f} —— 现价隐含的预期超出该利润率档"
            f"在网格内可解释的范围（需要更高利润率或网格外的极端增速）。"
        )
        en = (
            f"Even at {_fmt_pct(hi_g)} growth, this margin scenario reaches "
            f"at most {symbol}{price_max:.2f} per share — below the current "
            f"price {symbol}{market_price:.2f}. The price embeds expectations "
            f"beyond what this margin scenario can explain within the grid "
            f"(a higher margin, or growth outside the grid, would be needed)."
        )
    else:
        # 理论上有效格点符号混合必有相邻变号；此分支为防御性兜底。
        code = DIAG_NO_CROSSING
        zh = (
            f"网格内未检测到 per_share(g) 与现价 {symbol}{market_price:.2f} "
            f"的过零点（有效格点 {len(valid)} 个，价值区间 "
            f"{symbol}{price_min:.2f} ~ {symbol}{price_max:.2f}）。"
        )
        en = (
            f"No crossing of per_share(g) with the current price "
            f"{symbol}{market_price:.2f} was detected on the grid "
            f"({len(valid)} valid points, value range "
            f"{symbol}{price_min:.2f} ~ {symbol}{price_max:.2f})."
        )
    return WACCColumn(
        wacc=wacc,
        wacc_delta=wacc_delta,
        status=STATUS_NO_SOLUTION,
        solutions=[],
        multiple_solutions=False,
        diagnostic_code=code,
        diagnostic_zh=zh,
        diagnostic_en=en,
        grid_price_min=price_min,
        grid_price_max=price_max,
        valid_grid_points=len(valid),
    )


def solve_expectations_frontier(
    base_input: DCFInput,
    market_price: float,
    margin_scenarios: Sequence[tuple[str, float]],
    *,
    starting_margin: float | None = None,
    wacc_deltas: Sequence[float] = DEFAULT_WACC_DELTAS,
    growth_low: float = GROWTH_GRID_LOW,
    growth_high: float = GROWTH_GRID_HIGH,
    growth_step: float = GROWTH_GRID_STEP,
) -> ExpectationsFrontier:
    """求解市场价格的条件化隐含增速前沿。

    Args:
        base_input: forward DCF 用的同一个 DCFInput。terminal_growth /
            base_depreciation / tax / SBC 等其余字段**全部严格继承**
            （AUDIT-A2 同模型原则）——只覆盖 revenue_growth_path（网格扫
            描）、operating_margin_path（情景收敛路径）、wacc（±1% 列）。
        market_price: 当前市价（每股，> 0）。
        margin_scenarios: [(label, target_margin), ...]，由调用方传入
            2-3 档终年利润率情景（引擎不猜）。
        starting_margin: margin path 的起点（"当前利润率"）。默认取
            base_input.operating_margin_path[0]。
        wacc_deltas: 相对 base WACC 的偏移列表，默认 (−1%, 0, +1%)。

    Returns:
        ExpectationsFrontier —— 可 JSON 序列化的 dataclass 树。每档
        margin 情景 × 每列 WACC 给出全部解（多解全返回并带
        multiple_solutions 警告）或结构化 no_solution 边界诊断，
        绝不因"无解"抛异常。

    Raises:
        ValueError: market_price ≤ 0 或 margin_scenarios 为空（调用方
            契约错误，与"无解"不同）。
    """
    if market_price <= 0:
        raise ValueError(f"market_price={market_price} must be positive.")
    if not margin_scenarios:
        raise ValueError("margin_scenarios must contain at least one scenario.")
    if growth_step <= 0 or growth_high <= growth_low:
        raise ValueError(
            f"Invalid growth grid: [{growth_low}, {growth_high}] "
            f"step {growth_step}."
        )

    engine = DCFEngine()
    n = base_input.horizon_years
    start_m = (
        starting_margin
        if starting_margin is not None
        else base_input.operating_margin_path[0]
    )
    symbol = _CURRENCY_SYMBOLS.get(base_input.currency, "")

    # 整数步数构网格，避免浮点步进漂移（~200 点，步长 0.005）
    n_steps = int(round((growth_high - growth_low) / growth_step))
    grid = [growth_low + i * growth_step for i in range(n_steps + 1)]

    scenario_results: list[MarginScenarioResult] = []
    for label, target_margin in margin_scenarios:
        margin_path = _build_margin_path(start_m, target_margin, n)
        columns = [
            _solve_column(
                engine=engine,
                base_input=base_input,
                margin_path=margin_path,
                wacc=base_input.wacc + delta,
                wacc_delta=delta,
                market_price=market_price,
                grid=grid,
                symbol=symbol,
            )
            for delta in sorted(wacc_deltas)
        ]
        scenario_results.append(MarginScenarioResult(
            label=label,
            target_margin=target_margin,
            starting_margin=start_m,
            margin_path=margin_path,
            wacc_columns=columns,
        ))

    return ExpectationsFrontier(
        market_price=market_price,
        currency=base_input.currency,
        base_wacc=base_input.wacc,
        horizon_years=n,
        growth_grid_low=growth_low,
        growth_grid_high=growth_high,
        growth_grid_step=growth_step,
        scenarios=scenario_results,
    )
