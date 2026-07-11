"""每日 LLM 预算熔断测试 — Aegis 2.0 Phase 3 任务 A4.

覆盖：初始 spent=0；charge 累计并能被新实例读回（跨"进程"）；can_afford
超限后 False；cap<=0 永远可负担；负成本当 0；坏台账不崩；不同 today 隔离。
所有落盘用 tmp_path 重定向，不触真实 .cache/，绝不连网络。
"""

from __future__ import annotations

import json

from aegis.core.monitor.budget import DailyBudget, SpendRecord


# ---------------------------------------------------------------------------
# 初始状态
# ---------------------------------------------------------------------------

def test_initial_spent_is_zero(tmp_path):
    b = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    assert b.spent_today() == 0.0
    assert b.remaining() == 10.0
    assert b.can_afford() is True
    # 未 charge 前不应写文件。
    assert not b.path.exists()


def test_ledger_filename_uses_dashless_date(tmp_path):
    b = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    assert b.path == tmp_path / "20260711.json"


# ---------------------------------------------------------------------------
# charge 累计 + 落盘 + 跨实例读回
# ---------------------------------------------------------------------------

def test_charge_accumulates_and_persists(tmp_path):
    b = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    b.charge("301358", 2.5)
    b.charge("002669", 1.5)
    assert b.spent_today() == 4.0
    assert b.remaining() == 6.0
    # 落盘台账内容正确。
    data = json.loads(b.path.read_text(encoding="utf-8"))
    assert data["date"] == "2026-07-11"
    assert data["spent_usd"] == 4.0
    assert len(data["runs"]) == 2
    assert data["runs"][0]["ticker"] == "301358"
    assert data["runs"][0]["cost_usd"] == 2.5
    assert "at" in data["runs"][0]


def test_new_instance_reads_back_accumulated_spend(tmp_path):
    """同日多次 new DailyBudget 能读回累计值（跨进程/跨次扫描累计）。"""
    b1 = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    b1.charge("301358", 3.0)

    b2 = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    assert b2.spent_today() == 3.0
    assert b2.remaining() == 7.0
    # 继续在新实例上累计。
    b2.charge("002669", 2.0)
    assert b2.spent_today() == 5.0

    b3 = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    assert b3.spent_today() == 5.0
    # runs 两笔都在。
    data = json.loads(b3.path.read_text(encoding="utf-8"))
    assert len(data["runs"]) == 2


# ---------------------------------------------------------------------------
# can_afford 熔断
# ---------------------------------------------------------------------------

def test_can_afford_false_after_over_limit(tmp_path):
    b = DailyBudget(5.0, dir=tmp_path, today="2026-07-11")
    assert b.can_afford(3.0) is True
    b.charge("301358", 5.0)  # 花光到上限
    assert b.spent_today() == 5.0
    # 已花 == 上限，不满足 "已花 < 上限"。
    assert b.can_afford() is False
    assert b.can_afford(0.0) is False
    assert b.remaining() == 0.0


def test_can_afford_respects_estimate(tmp_path):
    b = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    b.charge("301358", 8.0)
    assert b.spent_today() == 8.0
    # 已花 8 < 10 但 8+3 > 10 → 不能承担预估 3。
    assert b.can_afford(3.0) is False
    # 8+2 == 10 → 恰好可承担。
    assert b.can_afford(2.0) is True
    # 不带预估仍可（8 < 10）。
    assert b.can_afford() is True


def test_can_afford_negative_estimate_treated_as_zero(tmp_path):
    b = DailyBudget(5.0, dir=tmp_path, today="2026-07-11")
    b.charge("301358", 5.0)
    # 负 est 视为 0，但已花 == 上限，仍 False。
    assert b.can_afford(-100.0) is False
    b2 = DailyBudget(5.0, dir=tmp_path, today="2026-07-12")
    b2.charge("301358", 3.0)
    # 负 est 不应让超限判断"放水"：等价于 est=0，3 < 5 → True。
    assert b2.can_afford(-100.0) is True


# ---------------------------------------------------------------------------
# 不限额
# ---------------------------------------------------------------------------

def test_zero_cap_is_unlimited(tmp_path):
    b = DailyBudget(0.0, dir=tmp_path, today="2026-07-11")
    assert b.can_afford() is True
    assert b.can_afford(1_000_000.0) is True
    b.charge("301358", 999.0)
    assert b.can_afford(1_000_000.0) is True
    assert b.spent_today() == 999.0
    # 不限额时 remaining 返回 0.0（无意义额度）。
    assert b.remaining() == 0.0


def test_negative_cap_is_unlimited(tmp_path):
    b = DailyBudget(-5.0, dir=tmp_path, today="2026-07-11")
    assert b.can_afford(1_000.0) is True


# ---------------------------------------------------------------------------
# 负成本 / 坏输入
# ---------------------------------------------------------------------------

def test_negative_cost_treated_as_zero(tmp_path):
    b = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    b.charge("301358", -3.0)
    assert b.spent_today() == 0.0
    b.charge("301358", 2.0)
    assert b.spent_today() == 2.0
    # 台账里负成本记为 0。
    data = json.loads(b.path.read_text(encoding="utf-8"))
    assert data["runs"][0]["cost_usd"] == 0.0


def test_charge_bad_cost_does_not_crash(tmp_path):
    b = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    b.charge("301358", "not-a-number")  # type: ignore[arg-type]
    assert b.spent_today() == 0.0
    b.charge(None, 1.0)  # type: ignore[arg-type]
    assert b.spent_today() == 1.0


# ---------------------------------------------------------------------------
# 坏台账容错
# ---------------------------------------------------------------------------

def test_corrupt_ledger_file_starts_from_zero(tmp_path):
    path = tmp_path / "20260711.json"
    path.write_text("{ this is not valid json ", encoding="utf-8")
    b = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    assert b.spent_today() == 0.0
    # charge 后覆写为合法台账。
    b.charge("301358", 1.0)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["spent_usd"] == 1.0


def test_non_dict_ledger_starts_from_zero(tmp_path):
    path = tmp_path / "20260711.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    b = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    assert b.spent_today() == 0.0


def test_ledger_missing_spent_recomputed_from_runs(tmp_path):
    path = tmp_path / "20260711.json"
    path.write_text(json.dumps({
        "date": "2026-07-11",
        "runs": [
            {"ticker": "301358", "cost_usd": 1.5, "at": "x"},
            {"ticker": "002669", "cost_usd": 2.0, "at": "y"},
        ],
    }), encoding="utf-8")
    b = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    assert b.spent_today() == 3.5


# ---------------------------------------------------------------------------
# 不同 today 隔离
# ---------------------------------------------------------------------------

def test_different_today_uses_separate_file(tmp_path):
    b1 = DailyBudget(10.0, dir=tmp_path, today="2026-07-11")
    b1.charge("301358", 4.0)
    b2 = DailyBudget(10.0, dir=tmp_path, today="2026-07-12")
    # 隔天从 0 起。
    assert b2.spent_today() == 0.0
    b2.charge("301358", 1.0)
    assert b2.spent_today() == 1.0
    # 两个文件互不干扰。
    assert (tmp_path / "20260711.json").exists()
    assert (tmp_path / "20260712.json").exists()
    d1 = json.loads((tmp_path / "20260711.json").read_text(encoding="utf-8"))
    assert d1["spent_usd"] == 4.0


def test_default_today_when_omitted(tmp_path):
    from datetime import date
    b = DailyBudget(10.0, dir=tmp_path)
    assert b.today == date.today().isoformat()


# ---------------------------------------------------------------------------
# SpendRecord dataclass
# ---------------------------------------------------------------------------

def test_spend_record_fields():
    r = SpendRecord(ticker="301358", cost_usd=1.25, at="2026-07-11T10:00:00")
    assert r.ticker == "301358"
    assert r.cost_usd == 1.25
    assert r.at == "2026-07-11T10:00:00"
