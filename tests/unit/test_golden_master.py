# -*- coding: utf-8 -*-
"""Golden-master 挽具（scripts/golden_master.py）回归测试。

三层保障（红线 #7：拆单体前挽具必须可信）：
1. 规范化规则单测——合成 dict 上验证占位符替换、缺路径容错；
2. diff 引擎单测——added / removed / changed 三类都能被查出；
3. 真 pkl 集成——同一 pkl 两次重建规范化后逐字节一致（挽具可信前提），
   且当前代码 vs 已落盘基线零 diff。pkl / 基线缺失时 skip（CI 无 .cache）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_GM_PATH = PROJECT_ROOT / "scripts" / "golden_master.py"

_spec = importlib.util.spec_from_file_location("golden_master", _GM_PATH)
gm = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("golden_master", gm)
_spec.loader.exec_module(gm)

SNAPSHOT_NAMES = list(gm.SNAPSHOTS)

# 每个快照的重建结果缓存：集成测试要跑两次重建，跨用例复用避免三跑
_rebuild_cache: dict[str, list[dict]] = {}


def _rebuild_twice(name: str) -> list[dict]:
    if name not in _rebuild_cache:
        _rebuild_cache[name] = [
            gm.rebuild_normalized(name),
            gm.rebuild_normalized(name),
        ]
    return _rebuild_cache[name]


def _require_pkl(name: str) -> Path:
    pkl = PROJECT_ROOT / gm.SNAPSHOTS[name]
    if not pkl.exists():
        pytest.skip(f"replay pkl 缺失（CI 环境无 .cache）: {pkl}")
    return pkl


# ─────────────────────────────────────────────────────────────────
# 1. 规范化规则
# ─────────────────────────────────────────────────────────────────

def _fake_golden() -> dict:
    return {
        "summary": {"publishing_status": "published"},
        "report": {
            "company": "测试公司",
            "reportDate": "2026-07-10 08:00 UTC",
            "runId": "run_abc123",
            "pipelineDuration": "23m 41s",
            "staleBanner": "最新可得财报距今约 16 个月",
            "price": {"last": 13.76, "asOf": "2026-07-10 08:00 UTC"},
            "dataAsOf": {"latestPeriod": "2026-03-31", "days": 42,
                         "line": "数据截至 2026-03-31（时效 42 天）"},
            "rating": {"word": "持有"},
        },
    }


class TestNormalizeRules:
    def test_volatile_fields_replaced_with_placeholder(self):
        out = gm.normalize_golden(_fake_golden())
        r = out["report"]
        assert r["reportDate"] == gm.NORMALIZED_PLACEHOLDER
        assert r["runId"] == gm.NORMALIZED_PLACEHOLDER
        assert r["pipelineDuration"] == gm.NORMALIZED_PLACEHOLDER
        assert r["staleBanner"] == gm.NORMALIZED_PLACEHOLDER
        assert r["price"]["asOf"] == gm.NORMALIZED_PLACEHOLDER
        assert r["dataAsOf"]["days"] == gm.NORMALIZED_PLACEHOLDER
        assert r["dataAsOf"]["line"] == gm.NORMALIZED_PLACEHOLDER

    def test_stable_fields_untouched(self):
        out = gm.normalize_golden(_fake_golden())
        r = out["report"]
        assert r["company"] == "测试公司"
        assert r["price"]["last"] == 13.76
        assert r["dataAsOf"]["latestPeriod"] == "2026-03-31"
        assert r["rating"]["word"] == "持有"
        assert out["summary"]["publishing_status"] == "published"

    def test_none_values_stay_none(self):
        # staleBanner=None（无时效警告）是确定性的存在信号，不该被占位
        g = _fake_golden()
        g["report"]["staleBanner"] = None
        out = gm.normalize_golden(g)
        assert out["report"]["staleBanner"] is None

    def test_missing_paths_tolerated(self):
        # 无 dataAsOf / price 的最小 report（老 pkl 路径）不许炸
        g = {"summary": {}, "report": {"company": "X"}}
        out = gm.normalize_golden(g)
        assert out["report"]["company"] == "X"

    def test_does_not_mutate_input(self):
        g = _fake_golden()
        gm.normalize_golden(g)
        assert g["report"]["reportDate"] == "2026-07-10 08:00 UTC"

    def test_two_normalizations_byte_identical(self):
        # 规范化本身必须是幂等且确定性的
        a = gm.canonical_json(gm.normalize_golden(_fake_golden()))
        b = gm.canonical_json(gm.normalize_golden(_fake_golden()))
        assert a == b

    def test_rules_cover_known_volatile_keys(self):
        # 防止未来有人误删规则条目——这些是 build_report_dict 里
        # datetime.now()/date.today() 的直接产物
        paths = {p for p, _ in gm.NORMALIZE_RULES}
        for must in ("reportDate", "price.asOf", "runId",
                     "pipelineDuration", "dataAsOf.days", "dataAsOf.line",
                     "staleBanner"):
            assert must in paths, f"NORMALIZE_RULES 缺少 {must}"


class TestJsonify:
    def test_non_finite_floats_nulled(self):
        out = gm._jsonify({"a": float("nan"), "b": float("inf"), "c": 1.5})
        assert out["a"] is None and out["b"] is None and out["c"] == 1.5

    def test_non_str_keys_and_objects_stringified(self):
        class Obj:
            def __str__(self):
                return "obj!"
        out = gm._jsonify({1: Obj(), "t": (1, 2)})
        assert out["1"] == "obj!"
        assert out["t"] == [1, 2]


# ─────────────────────────────────────────────────────────────────
# 2. diff 引擎
# ─────────────────────────────────────────────────────────────────

class TestDiffStructures:
    def test_identical_yields_empty(self):
        d = {"a": 1, "b": {"c": [1, 2, {"d": "x"}]}}
        assert gm.diff_structures(d, json.loads(json.dumps(d))) == []

    def test_changed_scalar(self):
        diffs = gm.diff_structures({"a": {"b": 1}}, {"a": {"b": 2}})
        assert len(diffs) == 1
        assert diffs[0]["kind"] == "changed"
        assert diffs[0]["path"] == "$.a.b"

    def test_added_and_removed_keys(self):
        diffs = gm.diff_structures({"a": 1, "gone": 2}, {"a": 1, "new": 3})
        kinds = {d["path"]: d["kind"] for d in diffs}
        assert kinds == {"$.gone": "removed", "$.new": "added"}

    def test_list_length_change(self):
        diffs = gm.diff_structures({"xs": [1, 2, 3]}, {"xs": [1, 2]})
        assert any(d["path"] == "$.xs(len)" and d["kind"] == "changed"
                   for d in diffs)

    def test_nested_list_element_change(self):
        diffs = gm.diff_structures({"xs": [{"v": 1}]}, {"xs": [{"v": 9}]})
        assert diffs[0]["path"] == "$.xs[0].v"

    def test_type_change_detected(self):
        diffs = gm.diff_structures({"a": "1"}, {"a": 1})
        assert diffs and diffs[0]["kind"] == "changed"


# ─────────────────────────────────────────────────────────────────
# 3. 真 pkl 集成（pkl / 基线缺失时 skip）
# ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", SNAPSHOT_NAMES)
def test_rebuild_is_deterministic(name):
    """同一 pkl 两次重建、规范化后必须逐字节一致——挽具可信的前提。"""
    _require_pkl(name)
    first, second = _rebuild_twice(name)
    assert gm.canonical_json(first) == gm.canonical_json(second), (
        f"{name}: 两次重建结果不一致，存在漏网的非确定字段:\n"
        + "\n".join(str(d) for d in gm.diff_structures(first, second)[:10])
    )


@pytest.mark.parametrize("name", SNAPSHOT_NAMES)
def test_check_zero_diff_against_baseline(name):
    """当前代码重建的规范化输出必须与已落盘基线零 diff（红线 #7 闸门）。"""
    _require_pkl(name)
    baseline_file = gm.GOLDEN_DIR / f"{name}.json"
    if not baseline_file.exists():
        pytest.skip(f"基线缺失（先跑 golden_master.py record）: {baseline_file}")
    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    current = _rebuild_twice(name)[0]
    diffs = gm.diff_structures(baseline, current)
    assert diffs == [], (
        f"{name}: 与基线出现 {len(diffs)} 处差异（前 10 处）:\n"
        + "\n".join(
            f"  {d['kind']:8s} {d['path']}: {d['base']} -> {d['cur']}"
            for d in diffs[:10]
        )
    )


@pytest.mark.parametrize("name", SNAPSHOT_NAMES)
def test_baseline_file_is_canonical(name):
    """基线文件本身必须是 canonical_json 格式（键排序 + 尾换行），
    保证 record 重写时 git diff 干净。"""
    baseline_file = gm.GOLDEN_DIR / f"{name}.json"
    if not baseline_file.exists():
        pytest.skip(f"基线缺失: {baseline_file}")
    text = baseline_file.read_text(encoding="utf-8")
    assert text == gm.canonical_json(json.loads(text))
