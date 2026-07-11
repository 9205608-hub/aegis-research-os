"""90 天回看复盘测试 — Aegis 2.0 Phase 3 任务 B3.

覆盖：review_date 未到 → 不生成；已到 → 生成 json 且 PostMortem 校验通过；
已存在复盘文件 → 幂等跳过；quote_fn 返回 None → 跳过该票不崩；建仓价 <=0 →
跳过；total_return 计算正确；what_was_right/wrong 非空；方向兑现启发正确。

所有落盘用 tmp_path 重定向（thesis 链 + POSTMORTEM_DIR 都 monkeypatch），
绝不触真实 .cache/，绝不连网络（quote_fn / price_lookup 一律注入假函数）。
"""

from __future__ import annotations

import json

import pytest

from aegis.core.monitor import postmortem as pm
from aegis.data_contracts.common import ConfidenceBucket, EdgeType
from aegis.data_contracts.postmortem_schema import PostMortem


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """把 thesis 链目录与复盘目录都重定向到 tmp_path。"""
    thesis_dir = tmp_path / "thesis"
    pm_dir = tmp_path / "postmortems"
    thesis_dir.mkdir()
    monkeypatch.setattr(pm, "POSTMORTEM_DIR", pm_dir)
    return thesis_dir, pm_dir


def _thesis_payload(
    *,
    entity_id: str = "301358",
    version: int = 1,
    base_case_value: float | None = 30.0,
    publishing_status: str = "published",
    confidence_bucket: str = "high",
    primary_edge_type: str = "analytical",
    review_date: str = "2026-04-01",
    run_id: str = "run_20260101_120000_abc",
) -> dict:
    """一份最小可用的 thesis payload（ThesisContract.model_dump 的关键子集）。"""
    return {
        "thesis_id": f"thesis_{entity_id}",
        "thesis_version": version,
        "entity_id": entity_id,
        "run_id": run_id,
        "core_thesis": "核心论点占位",
        "my_variant": "差异化观点占位",
        "counter_thesis": "反方论点占位",
        "market_implied_story": "市场隐含预期占位",
        "base_case_value": base_case_value,
        "bear_case_value": None,
        "bull_case_value": None,
        "publishing_status": publishing_status,
        "confidence_bucket": confidence_bucket,
        "sector_cycle_position": "定价体制占位",
        "edge_classification": {"primary_edge_type": primary_edge_type},
        "must_monitor": [],
        "review_date": review_date,
    }


def _write_chain(thesis_dir, entity_id: str, records: list[dict]) -> None:
    """把一串 record 写成 {entity}.jsonl 链。"""
    path = thesis_dir / f"{entity_id}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _record(
    *,
    entity_id: str = "301358",
    version: int = 1,
    created_at: str = "2026-01-01T12:00:00",
    run_id: str = "run_20260101_120000_abc",
    **thesis_kw,
) -> dict:
    return {
        "version": version,
        "created_at": created_at,
        "run_id": run_id,
        "parent_version": None,
        "thesis": _thesis_payload(
            entity_id=entity_id, version=version, run_id=run_id, **thesis_kw
        ),
    }


# ---------------------------------------------------------------------------
# due_records
# ---------------------------------------------------------------------------

def test_due_records_empty_dir(dirs):
    thesis_dir, _ = dirs
    assert pm.due_records(thesis_dir=thesis_dir, today="2026-07-11") == []


def test_due_records_not_due(dirs):
    thesis_dir, _ = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-08-01")])
    # today < review_date → 未到期
    assert pm.due_records(thesis_dir=thesis_dir, today="2026-07-11") == []


def test_due_records_due(dirs):
    thesis_dir, _ = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-04-01")])
    due = pm.due_records(thesis_dir=thesis_dir, today="2026-07-11")
    assert len(due) == 1
    assert due[0]["thesis"]["entity_id"] == "301358"


def test_due_records_review_date_equal_today_is_due(dirs):
    thesis_dir, _ = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-07-11")])
    due = pm.due_records(thesis_dir=thesis_dir, today="2026-07-11")
    assert len(due) == 1


def test_due_records_skips_when_postmortem_exists(dirs):
    thesis_dir, pm_dir = dirs
    _write_chain(thesis_dir, "301358", [_record(version=1, review_date="2026-04-01")])
    pm_dir.mkdir(parents=True, exist_ok=True)
    (pm_dir / "301358_v1.json").write_text("{}", encoding="utf-8")
    assert pm.due_records(thesis_dir=thesis_dir, today="2026-07-11") == []


def test_due_records_uses_latest_version(dirs):
    thesis_dir, _ = dirs
    _write_chain(
        thesis_dir,
        "301358",
        [
            _record(version=1, review_date="2026-04-01"),
            _record(version=2, review_date="2026-04-15"),
        ],
    )
    due = pm.due_records(thesis_dir=thesis_dir, today="2026-07-11")
    assert len(due) == 1
    assert due[0]["version"] == 2


def test_due_records_bad_chain_skipped(dirs):
    thesis_dir, _ = dirs
    # 坏 JSONL 行 + 一条无 review_date 的 thesis
    (thesis_dir / "bad.jsonl").write_text("{not json}\n", encoding="utf-8")
    rec = _record(entity_id="002669")
    rec["thesis"].pop("review_date")
    _write_chain(thesis_dir, "002669", [rec])
    # 不崩，返回空
    assert pm.due_records(thesis_dir=thesis_dir, today="2026-07-11") == []


# ---------------------------------------------------------------------------
# build_postmortem
# ---------------------------------------------------------------------------

def test_build_total_return_correct():
    rec = _record()
    out = pm.build_postmortem(
        rec, price_at_thesis=20.0, price_at_review=25.0, today="2026-07-11"
    )
    assert out.total_return == pytest.approx(0.25)
    assert out.price_at_thesis == 20.0
    assert out.price_at_review == 25.0


def test_build_fields_filled_and_valid():
    rec = _record()
    out = pm.build_postmortem(
        rec, price_at_thesis=20.0, price_at_review=25.0, today="2026-07-11"
    )
    assert isinstance(out, PostMortem)
    assert out.postmortem_id == "postmortem_301358_v1"
    assert out.thesis_id == "thesis_301358"
    assert out.thesis_version == 1
    assert out.original_run_id == "run_20260101_120000_abc"
    assert out.review_date.isoformat() == "2026-07-11"
    assert out.original_thesis_date.isoformat() == "2026-01-01"
    assert out.edge_type == EdgeType.ANALYTICAL
    assert out.original_confidence_bucket == ConfidenceBucket.HIGH
    # what_was_right / wrong 非空
    assert len(out.what_was_right) >= 1
    assert len(out.what_was_wrong) >= 1
    assert all(isinstance(s, str) and s for s in out.what_was_right)
    assert all(isinstance(s, str) and s for s in out.what_was_wrong)


def test_build_variant_realized_up():
    # base_case 30 > 建仓价 20 → 隐含上行；回看 25 → 收益 +25% → 兑现
    rec = _record(base_case_value=30.0)
    out = pm.build_postmortem(rec, price_at_thesis=20.0, price_at_review=25.0)
    assert out.variant_realized is True
    assert out.edge_realized is True
    assert out.thesis_survived is True


def test_build_variant_not_realized_contradicted():
    # 隐含上行，但回看下跌 → 未兑现且被证伪
    rec = _record(base_case_value=30.0)
    out = pm.build_postmortem(rec, price_at_thesis=20.0, price_at_review=15.0)
    assert out.variant_realized is False
    assert out.thesis_survived is False
    assert any("未兑现" in s for s in out.what_was_wrong)


def test_build_unknown_direction_conservative():
    # 无 base_case_value → 方向未知 → 保守：存续 True、兑现 False
    rec = _record(base_case_value=None)
    out = pm.build_postmortem(rec, price_at_thesis=20.0, price_at_review=25.0)
    assert out.thesis_survived is True
    assert out.variant_realized is False
    assert any("无法判定" in s for s in out.what_was_wrong)
    # 缺锚应给出系统改进建议
    assert any("anchor_price" in s for s in out.lessons_for_system)


def test_build_blocked_status_did_not_survive():
    rec = _record(publishing_status="blocked", base_case_value=30.0)
    out = pm.build_postmortem(rec, price_at_thesis=20.0, price_at_review=25.0)
    assert out.thesis_survived is False
    assert any("blocked" in s for s in out.what_was_wrong)


def test_build_bad_confidence_falls_back_medium():
    rec = _record(confidence_bucket="garbage")
    out = pm.build_postmortem(rec, price_at_thesis=20.0, price_at_review=25.0)
    assert out.original_confidence_bucket == ConfidenceBucket.MEDIUM


def test_build_bad_edge_type_falls_back_analytical():
    rec = _record(primary_edge_type="nonsense")
    out = pm.build_postmortem(rec, price_at_thesis=20.0, price_at_review=25.0)
    assert out.edge_type == EdgeType.ANALYTICAL


def test_build_raises_on_nonpositive_price():
    rec = _record()
    with pytest.raises(ValueError):
        pm.build_postmortem(rec, price_at_thesis=0.0, price_at_review=25.0)
    with pytest.raises(ValueError):
        pm.build_postmortem(rec, price_at_thesis=20.0, price_at_review=-5.0)


# ---------------------------------------------------------------------------
# run_postmortems
# ---------------------------------------------------------------------------

def test_run_generates_file_and_valid_postmortem(dirs):
    thesis_dir, pm_dir = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-04-01")])

    out = pm.run_postmortems(
        quote_fn=lambda t: 25.0,
        thesis_dir=thesis_dir,
        today="2026-07-11",
        price_lookup=lambda rec: 20.0,
    )
    assert len(out) == 1
    path = pm_dir / "301358_v1.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    # 落盘内容能被 PostMortem 重新校验
    reparsed = PostMortem.model_validate(data)
    assert reparsed.total_return == pytest.approx(0.25)
    assert reparsed.postmortem_id == "postmortem_301358_v1"


def test_run_idempotent(dirs):
    thesis_dir, pm_dir = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-04-01")])
    kw = dict(
        quote_fn=lambda t: 25.0,
        thesis_dir=thesis_dir,
        today="2026-07-11",
        price_lookup=lambda rec: 20.0,
    )
    first = pm.run_postmortems(**kw)
    assert len(first) == 1
    # 第二次：文件已存在 → due_records 过滤，无新增
    second = pm.run_postmortems(**kw)
    assert second == []


def test_run_skips_when_quote_none(dirs):
    thesis_dir, pm_dir = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-04-01")])
    out = pm.run_postmortems(
        quote_fn=lambda t: None,
        thesis_dir=thesis_dir,
        today="2026-07-11",
        price_lookup=lambda rec: 20.0,
    )
    assert out == []
    assert not (pm_dir / "301358_v1.json").exists()


def test_run_skips_when_quote_raises(dirs):
    thesis_dir, _ = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-04-01")])

    def boom(_t):
        raise RuntimeError("network down")

    out = pm.run_postmortems(
        quote_fn=boom,
        thesis_dir=thesis_dir,
        today="2026-07-11",
        price_lookup=lambda rec: 20.0,
    )
    assert out == []


def test_run_skips_when_anchor_missing(dirs):
    thesis_dir, pm_dir = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-04-01")])
    # 无 anchor_price、无 price_lookup → 跳过
    out = pm.run_postmortems(
        quote_fn=lambda t: 25.0,
        thesis_dir=thesis_dir,
        today="2026-07-11",
    )
    assert out == []
    assert not (pm_dir / "301358_v1.json").exists()


def test_run_skips_when_price_lookup_nonpositive(dirs):
    thesis_dir, _ = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-04-01")])
    out = pm.run_postmortems(
        quote_fn=lambda t: 25.0,
        thesis_dir=thesis_dir,
        today="2026-07-11",
        price_lookup=lambda rec: 0.0,
    )
    assert out == []


def test_run_uses_top_level_anchor_price(dirs):
    thesis_dir, pm_dir = dirs
    rec = _record(review_date="2026-04-01")
    rec["anchor_price"] = 20.0  # 顶层锚，无需 price_lookup
    _write_chain(thesis_dir, "301358", [rec])
    out = pm.run_postmortems(
        quote_fn=lambda t: 25.0,
        thesis_dir=thesis_dir,
        today="2026-07-11",
    )
    assert len(out) == 1
    assert out[0].price_at_thesis == 20.0


def test_run_quote_object_with_current_price(dirs):
    thesis_dir, pm_dir = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-04-01")])

    class FakeQuote:
        current_price = 25.0

    out = pm.run_postmortems(
        quote_fn=lambda t: FakeQuote(),
        thesis_dir=thesis_dir,
        today="2026-07-11",
        price_lookup=lambda rec: 20.0,
    )
    assert len(out) == 1
    assert out[0].price_at_review == 25.0


def test_run_not_due_generates_nothing(dirs):
    thesis_dir, pm_dir = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-12-01")])
    out = pm.run_postmortems(
        quote_fn=lambda t: 25.0,
        thesis_dir=thesis_dir,
        today="2026-07-11",
        price_lookup=lambda rec: 20.0,
    )
    assert out == []
    assert not (pm_dir / "301358_v1.json").exists()


def test_run_ticker_passed_to_quote_fn(dirs):
    thesis_dir, _ = dirs
    _write_chain(thesis_dir, "301358", [_record(review_date="2026-04-01")])
    seen = {}

    def qf(ticker):
        seen["ticker"] = ticker
        return 25.0

    pm.run_postmortems(
        quote_fn=qf,
        thesis_dir=thesis_dir,
        today="2026-07-11",
        price_lookup=lambda rec: 20.0,
    )
    assert seen["ticker"] == "301358"
