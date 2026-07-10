"""Aegis 2.0 Phase 0 — 预期前沿求解器回归测试.

DESIGN_2.0.md 五.Phase 0 第 2 项 / 三.A。锁定的行为：

① 康达(002669)参数集（真实量级：营收 52.4 亿、margin 2.9%、FCFF 为负、
   现价 = 6.4× DCF base、3.03 亿股）三档利润率情景，每档每列必须给出
   解或结构化 no_solution 诊断 —— 不许抛异常、不许 n/a 空洞输出。
② 合成稳态公司（test_dcf_da_consistency.py 的 BENCHMARK 基准集）自洽性：
   forward 5% 增速的价格反解回 ≈5%（±0.5pp）。
③ 非单调场景多解全部返回并带 multiple_solutions 警告（BUG-Y13 根治：
   一维 bisection 在此静默失效，网格变号检测找全过零点）。
④ WACC±1% 单调性：WACC 越高、同价格隐含增速越高（设计红线 2 的
   条件化三列输出）。
⑤ 隐含增速对应 horizon 年累计营收 scale >30× 时打 extreme_expectation
   标志 —— 只标注不 cap（还原市场原始预期）。
⑥ 全网格（3 档 × 3 WACC × ~200 点 ≈ 1800 次 compute_dcf）总时长 <5s。
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from aegis.core.truth.scenario_engine.dcf_engine import DCFEngine, DCFInput
from aegis.core.truth.scenario_engine.expectations_frontier import (
    DIAG_VALUE_ABOVE_PRICE,
    DIAG_VALUE_BELOW_PRICE,
    EXTREME_REVENUE_SCALE,
    STATUS_MULTIPLE_SOLUTIONS,
    STATUS_NO_SOLUTION,
    STATUS_NO_VALID_GRID_POINTS,
    STATUS_SOLVED,
    ExpectationsFrontier,
    solve_expectations_frontier,
)

engine = DCFEngine()

# ── 康达新材(002669)量级参数集（DESIGN_2.0.md 诊断案例）──
# 营收 52.4 亿 / operating margin 2.9% / 高 NWC 占用 → Y1 FCFF 为负 /
# 3.03 亿股。base per_share ≈ ¥0.87（本模型口径），现价取 6.4× base，
# 复现"市价远高于 DCF 锚"的康达式定价结构。
KANGDA = dict(
    base_revenue=52.4e8,
    revenue_growth_path=[0.05] * 10,
    operating_margin_path=[0.029] * 10,
    capex_to_revenue_path=[0.08] * 10,
    effective_tax_rate=0.25,
    nwc_to_revenue_delta=0.10,
    terminal_growth_rate=0.025,
    wacc=0.095,
    sbc_to_revenue=0.0,
    dilution_rate_annual=0.0,
    shares_outstanding=3.03e8,  # 3.03 亿股
    net_debt=3.0e8,
    horizon_years=10,
    base_depreciation=1.6e8,
    capex_useful_life_years=7.0,
    currency="CNY",
)

# 三档终年利润率情景：维持现状 2.9% / 行业中位 8% / 中值 5.5%
KANGDA_SCENARIOS = [
    ("维持当前利润率", 0.029),
    ("行业中位", 0.08),
    ("中性情景", 0.055),
]

# ── 合成稳态基准集（与 test_dcf_da_consistency.py BENCHMARK 一致）──
BENCHMARK = dict(
    base_revenue=100e9,
    revenue_growth_path=[0.05] * 10,
    operating_margin_path=[0.20] * 10,
    capex_to_revenue_path=[0.10] * 10,
    effective_tax_rate=0.25,
    nwc_to_revenue_delta=0.01,
    terminal_growth_rate=0.025,
    wacc=0.095,
    sbc_to_revenue=0.0,
    dilution_rate_annual=0.0,
    shares_outstanding=1_000_000_000,
    net_debt=0.0,
    horizon_years=10,
    base_depreciation=10e9,
    capex_useful_life_years=5.0,
)


@pytest.fixture(scope="module")
def kangda_input() -> DCFInput:
    return DCFInput(**KANGDA)


@pytest.fixture(scope="module")
def kangda_price(kangda_input) -> float:
    """现价 = 6.4 × DCF base（康达式价差结构，按构造成立）。"""
    base = engine.compute_dcf(kangda_input)
    assert base.per_share_value > 0, "fixture 需要正的 DCF base 锚"
    # 前提复核：Y1 FCFF 必须为负（康达 CFO −12 亿的真实失血结构）
    assert base.projections[0].fcff < 0
    return 6.4 * base.per_share_value


@pytest.fixture(scope="module")
def kangda_frontier(kangda_input, kangda_price) -> ExpectationsFrontier:
    return solve_expectations_frontier(
        kangda_input, kangda_price, KANGDA_SCENARIOS
    )


# ═══════════════════════════════════════════════════════════════════════
# ① 康达参数集：每档每列必须是"解"或"结构化 no_solution"，绝不抛异常
# ═══════════════════════════════════════════════════════════════════════

class TestKangdaParameterSet:
    def test_three_scenarios_three_wacc_columns(self, kangda_frontier):
        assert len(kangda_frontier.scenarios) == 3
        for sc in kangda_frontier.scenarios:
            assert len(sc.wacc_columns) == 3

    def test_every_cell_solved_or_structured_no_solution(self, kangda_frontier):
        """负 FCF + 6.4× 价差：不许抛异常、不许 n/a —— 每个单元要么给出
        解，要么给出带双语诊断的结构化 no_solution。"""
        for sc in kangda_frontier.scenarios:
            for col in sc.wacc_columns:
                assert col.status in (
                    STATUS_SOLVED,
                    STATUS_MULTIPLE_SOLUTIONS,
                    STATUS_NO_SOLUTION,
                ), f"{sc.label} wacc={col.wacc}: {col.status}"
                if col.status == STATUS_NO_SOLUTION:
                    # 结构化诊断：code + zh + en + 边界价值区间全齐
                    assert col.diagnostic_code in (
                        DIAG_VALUE_ABOVE_PRICE, DIAG_VALUE_BELOW_PRICE,
                    )
                    assert col.diagnostic_zh.strip()
                    assert col.diagnostic_en.strip()
                    assert col.grid_price_min is not None
                    assert col.grid_price_max is not None
                else:
                    assert col.solutions, f"{sc.label}: solved 却无解列表"

    def test_maintain_margin_is_structured_no_solution(self, kangda_frontier):
        """维持 2.9% 档撑不起 6.4× 价差 —— 这本身是有力结论：诊断必须
        明确说"即使 +80% 增速也撑不起现价"（value_below_price）。"""
        maintain = kangda_frontier.scenarios[0]
        assert maintain.label == "维持当前利润率"
        for col in maintain.wacc_columns:
            assert col.status == STATUS_NO_SOLUTION
            assert col.diagnostic_code == DIAG_VALUE_BELOW_PRICE
            # 中文化铁律：A 股诊断必须有简体中文文本
            assert "撑不起现价" in col.diagnostic_zh
            assert "¥" in col.diagnostic_zh  # CNY 货币符号

    def test_industry_median_margin_has_solution(self, kangda_frontier):
        """行业中位 8% 档在全部三列 WACC 下都应有隐含增速解 ——
        条件化小表的核心行（"若达行业中位 8% → 需 YY% 增速"）。"""
        median = kangda_frontier.scenarios[1]
        assert median.label == "行业中位"
        for col in median.wacc_columns:
            assert col.status in (STATUS_SOLVED, STATUS_MULTIPLE_SOLUTIONS)
            assert len(col.solutions) >= 1
            for s in col.solutions:
                assert -0.20 <= s.implied_growth <= 0.80

    def test_json_serializable_tree(self, kangda_frontier):
        """输出必须可 JSON 序列化（规格 5：供 prompt 与渲染直接消费）。"""
        d = kangda_frontier.to_dict()
        text = json.dumps(d, ensure_ascii=False)
        rt = json.loads(text)
        assert rt["market_price"] == pytest.approx(kangda_frontier.market_price)
        assert len(rt["scenarios"]) == 3
        col0 = rt["scenarios"][0]["wacc_columns"][0]
        # 字段名清晰、类型为纯 primitives
        for key in ("wacc", "wacc_delta", "status", "solutions",
                    "multiple_solutions", "diagnostic_code",
                    "diagnostic_zh", "diagnostic_en",
                    "grid_price_min", "grid_price_max", "valid_grid_points"):
            assert key in col0

    def test_margin_path_converges_linearly_to_target(self, kangda_frontier):
        """每档 margin path 从当前值线性收敛，终年恰等于 target。"""
        for sc in kangda_frontier.scenarios:
            assert sc.starting_margin == pytest.approx(0.029)
            assert len(sc.margin_path) == 10
            assert sc.margin_path[-1] == pytest.approx(sc.target_margin)
            # 线性：一阶差分恒定
            diffs = [
                sc.margin_path[i + 1] - sc.margin_path[i]
                for i in range(len(sc.margin_path) - 1)
            ]
            for d in diffs:
                assert d == pytest.approx(diffs[0], abs=1e-12)


# ═══════════════════════════════════════════════════════════════════════
# ② 合成稳态公司自洽性：forward 5% 的价格反解回 ≈5%
# ═══════════════════════════════════════════════════════════════════════

class TestSteadyStateSelfConsistency:
    def test_round_trip_recovers_true_growth(self):
        """forward 5% 增速价格 → base WACC 列隐含增速 ≈5%（±0.5pp）。

        同模型原则（AUDIT-A2）：base_depreciation / terminal_growth / tax
        等全部继承自同一个 DCFInput —— 口径分裂会让这里回解出 ~2× 的假值。
        """
        inp = DCFInput(**BENCHMARK)
        fwd = engine.compute_dcf(inp)
        fr = solve_expectations_frontier(
            inp, fwd.per_share_value, [("维持利润率", 0.20)]
        )
        base_col = fr.scenarios[0].wacc_columns[1]  # 升序第 2 列 = base WACC
        assert base_col.wacc == pytest.approx(BENCHMARK["wacc"])
        assert base_col.wacc_delta == pytest.approx(0.0)
        assert base_col.status == STATUS_SOLVED
        assert len(base_col.solutions) == 1
        assert base_col.solutions[0].implied_growth == pytest.approx(
            0.05, abs=0.005
        ), "稳态自洽性：反解增速须回到 forward 真值 5% ±0.5pp"

    def test_solution_price_check_closes(self):
        """把解出的 g 代回同一模型，per_share 必须贴回 market_price
        （算术闭合 —— 不轻信任何反解数字）。"""
        inp = DCFInput(**BENCHMARK)
        fwd = engine.compute_dcf(inp)
        price = fwd.per_share_value
        fr = solve_expectations_frontier(inp, price, [("维持利润率", 0.20)])
        n = inp.horizon_years
        sc = fr.scenarios[0]
        for col in fr.scenarios[0].wacc_columns:
            for s in col.solutions:
                candidate = replace(
                    inp,
                    revenue_growth_path=[s.implied_growth] * n,
                    operating_margin_path=list(sc.margin_path),
                    wacc=col.wacc,
                )
                back = engine.compute_dcf(candidate).per_share_value
                assert back == pytest.approx(price, rel=0.005), (
                    f"wacc={col.wacc}: 回代价格 {back:.2f} ≠ 目标 {price:.2f}"
                )


# ═══════════════════════════════════════════════════════════════════════
# ③ 非单调场景：多解全部返回 + multiple_solutions 警告（BUG-Y13 根治）
# ═══════════════════════════════════════════════════════════════════════

class TestNonMonotonicMultipleSolutions:
    def test_kangda_mid_margin_returns_all_roots(
        self, kangda_input, kangda_price, kangda_frontier
    ):
        """康达中值 5.5% 档的 price(g) 曲线非单调（增速越高前期
        capex/NWC 失血越大），同一现价存在低增速与极端增速两个解 ——
        一维 bisection 在此静默收敛到边界伪值（BUG-Y13），网格法必须
        把两个根都找出来并打 multiple_solutions 警告。"""
        mid = kangda_frontier.scenarios[2]
        assert mid.label == "中性情景"
        base_col = mid.wacc_columns[1]
        assert base_col.status == STATUS_MULTIPLE_SOLUTIONS
        assert base_col.multiple_solutions is True
        assert len(base_col.solutions) >= 2
        # 双语警告文本
        assert "非单调" in base_col.diagnostic_zh
        assert "non-monotonic" in base_col.diagnostic_en

        # 每个根都必须真实闭合：代回同一模型 → per_share ≈ 现价
        n = kangda_input.horizon_years
        for s in base_col.solutions:
            candidate = replace(
                kangda_input,
                revenue_growth_path=[s.implied_growth] * n,
                operating_margin_path=list(mid.margin_path),
                wacc=base_col.wacc,
            )
            back = engine.compute_dcf(candidate).per_share_value
            assert back == pytest.approx(kangda_price, abs=0.05), (
                f"根 g={s.implied_growth:.4f} 回代 {back:.4f} "
                f"≠ 现价 {kangda_price:.4f}"
            )

    def test_roots_returned_in_ascending_order(self, kangda_frontier):
        """网格从低到高扫描 → 多解按增速升序返回（渲染稳定性）。"""
        for sc in kangda_frontier.scenarios:
            for col in sc.wacc_columns:
                gs = [s.implied_growth for s in col.solutions]
                assert gs == sorted(gs)


# ═══════════════════════════════════════════════════════════════════════
# ④ WACC±1% 单调性：折现率越高，同一价格需要的隐含增速越高
# ═══════════════════════════════════════════════════════════════════════

class TestWACCMonotonicity:
    def test_benchmark_implied_growth_increases_with_wacc(self):
        inp = DCFInput(**BENCHMARK)
        fwd = engine.compute_dcf(inp)
        fr = solve_expectations_frontier(
            inp, fwd.per_share_value, [("维持利润率", 0.20)]
        )
        cols = fr.scenarios[0].wacc_columns
        # 三列按 WACC 升序：base−1%, base, base+1%
        assert [c.wacc_delta for c in cols] == pytest.approx(
            [-0.01, 0.0, 0.01]
        )
        assert all(c.status == STATUS_SOLVED for c in cols)
        g_low, g_base, g_high = (c.solutions[0].implied_growth for c in cols)
        assert g_low < g_base < g_high, (
            f"WACC 单调性破坏: {g_low:.4f} / {g_base:.4f} / {g_high:.4f}"
        )

    def test_kangda_industry_median_monotone_in_wacc(self, kangda_frontier):
        """真实量级（康达行业中位档）同样满足 WACC 单调性——取每列最小根
        （主分支）比较。"""
        median = kangda_frontier.scenarios[1]
        primary = [
            min(s.implied_growth for s in col.solutions)
            for col in median.wacc_columns
        ]
        assert primary[0] < primary[1] < primary[2]


# ═══════════════════════════════════════════════════════════════════════
# ⑤ extreme_expectation：累计营收 scale >30× 只标注、不 cap
# ═══════════════════════════════════════════════════════════════════════

class TestExtremeExpectationFlag:
    def test_flag_set_above_30x_and_growth_not_capped(self):
        """把现价钉在 forward g=50% 的价格上 → 隐含增速应解回 ≈50%
        （(1.5)^10 ≈ 57.7× > 30×）：标志置位，数值不截断。"""
        inp = DCFInput(**BENCHMARK)
        extreme_price = engine.compute_dcf(
            replace(inp, revenue_growth_path=[0.50] * 10)
        ).per_share_value
        fr = solve_expectations_frontier(
            inp, extreme_price, [("维持利润率", 0.20)]
        )
        base_col = fr.scenarios[0].wacc_columns[1]
        assert base_col.status == STATUS_SOLVED
        sol = base_col.solutions[0]
        # 不 cap：还原出接近 50% 的原始预期，而不是被截到 30× 对应值
        assert sol.implied_growth == pytest.approx(0.50, abs=0.005)
        assert sol.cumulative_revenue_scale == pytest.approx(
            1.5 ** 10, rel=0.05
        )
        assert sol.cumulative_revenue_scale > EXTREME_REVENUE_SCALE
        assert sol.extreme_expectation is True

    def test_flag_not_set_for_moderate_growth(self):
        inp = DCFInput(**BENCHMARK)
        fwd = engine.compute_dcf(inp)  # forward 5%: (1.05)^10 ≈ 1.63×
        fr = solve_expectations_frontier(
            inp, fwd.per_share_value, [("维持利润率", 0.20)]
        )
        sol = fr.scenarios[0].wacc_columns[1].solutions[0]
        assert sol.cumulative_revenue_scale < EXTREME_REVENUE_SCALE
        assert sol.extreme_expectation is False


# ═══════════════════════════════════════════════════════════════════════
# 无解/无效列的结构化诊断（规格 2 的另一半）+ 入参契约
# ═══════════════════════════════════════════════════════════════════════

class TestStructuredDiagnostics:
    def test_price_below_entire_frontier(self):
        """现价低于全前沿（即使 −20% 增速估值仍高于现价）→
        value_above_price_everywhere，结论结构化输出而非报错。"""
        inp = DCFInput(**BENCHMARK)
        fr = solve_expectations_frontier(inp, 0.01, [("维持利润率", 0.20)])
        col = fr.scenarios[0].wacc_columns[1]
        assert col.status == STATUS_NO_SOLUTION
        assert col.diagnostic_code == DIAG_VALUE_ABOVE_PRICE
        assert "仍高于现价" in col.diagnostic_zh
        assert "above the current price" in col.diagnostic_en
        assert col.grid_price_min is not None and col.grid_price_min > 0.01

    def test_wacc_below_terminal_growth_column_degrades_gracefully(self):
        """WACC−1% ≤ 永续增速 → 该列全部格点 ValueError → 列状态
        no_valid_grid_points（诊断齐全），其余列照常求解，不抛异常。"""
        inp = DCFInput(**{**BENCHMARK, "wacc": 0.03})  # −1% 列 = 2% < tg 2.5%
        fr = solve_expectations_frontier(inp, 100.0, [("维持利润率", 0.20)])
        cols = fr.scenarios[0].wacc_columns
        assert cols[0].status == STATUS_NO_VALID_GRID_POINTS
        assert cols[0].valid_grid_points == 0
        assert cols[0].diagnostic_zh.strip() and cols[0].diagnostic_en.strip()
        # base / +1% 列仍是正常结构化结果
        for col in cols[1:]:
            assert col.status in (
                STATUS_SOLVED, STATUS_MULTIPLE_SOLUTIONS, STATUS_NO_SOLUTION,
            )
            assert col.valid_grid_points > 0

    def test_invalid_market_price_rejected(self, kangda_input):
        with pytest.raises(ValueError, match="market_price"):
            solve_expectations_frontier(kangda_input, 0.0, KANGDA_SCENARIOS)

    def test_empty_scenarios_rejected(self, kangda_input):
        with pytest.raises(ValueError, match="margin_scenarios"):
            solve_expectations_frontier(kangda_input, 10.0, [])


# ═══════════════════════════════════════════════════════════════════════
# ⑥ 性能：3 档 × 3 WACC × ~200 点 ≈ 1800 次 compute_dcf，总时长 <5s
# ═══════════════════════════════════════════════════════════════════════

class TestPerformance:
    def test_full_grid_under_five_seconds(self, kangda_input, kangda_price):
        t0 = time.monotonic()
        fr = solve_expectations_frontier(
            kangda_input, kangda_price, KANGDA_SCENARIOS
        )
        elapsed = time.monotonic() - t0
        # 复核确实扫了全网格（每列 ≤201 个有效格点，非空）
        total_points = sum(
            col.valid_grid_points
            for sc in fr.scenarios
            for col in sc.wacc_columns
        )
        assert total_points > 1000, "网格未完整扫描"
        assert elapsed < 5.0, f"全网格耗时 {elapsed:.2f}s ≥ 5s 预算"
