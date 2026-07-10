"""Stage checkpoint + --update 增量复用 — Aegis 2.0 Phase 2 任务 C1/C2/C5。

覆盖四条验收线：

1. digest 稳定性：同输入两次同 digest；实时价格/时间戳/行情市值类输入
   变动 **不** 改 agents digest；基本面（含事件块）变动 **必** 改；
2. checkpoint 落盘/加载往返（含防 RLock 逐键净化 + digest 门）；
3. --update 复用路径零 LLM 调用（counting mock 断言 0 次）；
4. 复用标注中文渲染（「智能体分析引用自 …（基本面输入未变）」+
   「观点版本 v{N}」注入成品 HTML）。

离线挽具复用 tests/unit/test_auto_research.py 的 mock EDGAR 数据与假连接器
（不发任何网络请求）。
"""

from __future__ import annotations

import pickle
import threading
from unittest.mock import MagicMock, patch

import pytest

from aegis.core.orchestrator.auto_research import (
    AGENTS_DIGEST_EXCLUDE_KEYS,
    AutoResearchOrchestrator,
    ResearchConfig,
    agents_digest_payload,
    compute_agents_digest,
    compute_stage_digest,
    dump_stage_checkpoint,
    load_stage_checkpoint,
    stage_checkpoint_dir,
)
from tests.unit.test_auto_research import (
    MOCK_XBRL_FACTS,
    _FakeCatalystCalendar,
    _FakeForm4Connector,
    _FakeMarketDataConnector,
    _make_mock_packet,
)

# ─────────────────────────────────────────────────────────────────────
# 1. digest 稳定性
# ─────────────────────────────────────────────────────────────────────

_BASE_MF = {
    "revenue": 164_710_000_000.0,
    "net_income": 62_360_000_000.0,
    "operating_cash_flow": 91_145_000_000.0,
    "__historical_revenue": {2022: 116e9, 2023: 134e9, 2024: 164e9},
    "__recent_events": {
        "as_of": "2026-07-10",
        "announcements": [
            {"title": "关于全资子公司签订重大订单的公告",
             "announce_date": "2026-07-01"},
        ],
        "forecasts": [],
    },
}


class TestAgentsDigest:

    def test_same_input_same_digest(self):
        d1 = compute_agents_digest(dict(_BASE_MF), {"id": "sp_x"}, "FY2025")
        d2 = compute_agents_digest(dict(_BASE_MF), {"id": "sp_x"}, "FY2025")
        assert d1 == d2

    def test_price_and_market_noise_do_not_change_digest(self):
        """实时价格 / 行情市值 / 时间戳 / 价格衍生键抖动 → digest 不变。"""
        d0 = compute_agents_digest(dict(_BASE_MF), {"id": "sp_x"}, "FY2025")
        noisy = dict(_BASE_MF)
        # 价格衍生的顶层排除键（Step 7d / 4c 写入 meta_facts 的那批）
        noisy["__relative_valuation"] = {"target_pe_ttm": 42.0}
        noisy["__expectations_frontier"] = {"market_price": 189.0}
        noisy["__pricing_regime"] = {"dominant": "narrative"}
        noisy["__implied_growth_unreliable"] = True
        noisy["__data_freshness"] = {"days": 37}
        # 任意层级的易变键名（价格/市值/时间戳/摄取时点）
        noisy["current_price"] = 585.0
        noisy["market_cap"] = 1.51e12
        noisy["quote_timestamp"] = "2026-07-10T14:03:00"
        # 事件块里的摄取日期漂移（as_of 属时间戳，逐日变化不应作废分析）
        noisy["__recent_events"] = {
            **_BASE_MF["__recent_events"], "as_of": "2026-07-11",
        }
        assert compute_agents_digest(noisy, {"id": "sp_x"}, "FY2025") == d0

    def test_fundamental_change_changes_digest(self):
        d0 = compute_agents_digest(dict(_BASE_MF), {"id": "sp_x"}, "FY2025")
        changed = dict(_BASE_MF)
        changed["revenue"] = 180_000_000_000.0
        assert compute_agents_digest(changed, {"id": "sp_x"}, "FY2025") != d0

    def test_event_block_change_changes_digest(self):
        """事件块是 agents 输入的一部分：新公告落地必须作废复用。"""
        d0 = compute_agents_digest(dict(_BASE_MF), {"id": "sp_x"}, "FY2025")
        changed = dict(_BASE_MF)
        changed["__recent_events"] = {
            **_BASE_MF["__recent_events"],
            "announcements": _BASE_MF["__recent_events"]["announcements"] + [
                {"title": "关于计提资产减值准备的公告",
                 "announce_date": "2026-07-09"},
            ],
        }
        assert compute_agents_digest(changed, {"id": "sp_x"}, "FY2025") != d0

    def test_period_and_sector_pack_in_digest(self):
        d0 = compute_agents_digest(dict(_BASE_MF), {"id": "sp_x"}, "FY2025")
        assert compute_agents_digest(dict(_BASE_MF), {"id": "sp_y"}, "FY2025") != d0
        assert compute_agents_digest(dict(_BASE_MF), {"id": "sp_x"}, "FY2026") != d0

    def test_payload_strips_excluded_and_volatile_keys(self):
        mf = dict(_BASE_MF)
        for k in AGENTS_DIGEST_EXCLUDE_KEYS:
            mf[k] = "x"
        mf["nested"] = {"fetched_at": "now", "keep_me": 1}
        payload = agents_digest_payload(mf, {"id": "sp_x"}, "FY2025")
        assert not (set(payload["meta_facts"]) & AGENTS_DIGEST_EXCLUDE_KEYS)
        assert payload["meta_facts"]["nested"] == {"keep_me": 1}
        # 事件日期（announce_date）是基本面，不许被易变键模式误伤
        ann = payload["meta_facts"]["__recent_events"]["announcements"][0]
        assert ann["announce_date"] == "2026-07-01"

    def test_digest_never_raises_on_weird_payload(self):
        # dataclass/对象、None、不可 JSON 化对象——default=str 兜底
        assert compute_stage_digest({"obj": object(), "none": None})
        assert compute_agents_digest(None, None, None)


# ─────────────────────────────────────────────────────────────────────
# 2. checkpoint 落盘 / 加载往返
# ─────────────────────────────────────────────────────────────────────

class TestCheckpointRoundTrip:

    def test_dump_then_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AEGIS_STAGE_DIR", str(tmp_path))
        payload = {"all_judgments": ["j1", "j2"], "open_questions": []}
        out = dump_stage_checkpoint(
            "META", "agents", payload, digest="d" * 64, run_id="run_x",
        )
        assert out is not None and out.exists()
        assert out == stage_checkpoint_dir("META") / "agents.pkl"
        rec = load_stage_checkpoint("META", "agents", expected_digest="d" * 64)
        assert rec is not None
        assert rec["payload"] == payload
        assert rec["run_id"] == "run_x"
        assert rec["stage"] == "agents"
        assert rec["created_at"]

    def test_digest_mismatch_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AEGIS_STAGE_DIR", str(tmp_path))
        dump_stage_checkpoint("META", "agents", {"a": 1}, digest="old")
        assert load_stage_checkpoint("META", "agents", expected_digest="new") is None
        # 不带 expected_digest 则照常读出（追溯用途）
        assert load_stage_checkpoint("META", "agents") is not None

    def test_missing_and_corrupted_return_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AEGIS_STAGE_DIR", str(tmp_path))
        assert load_stage_checkpoint("META", "agents") is None
        p = stage_checkpoint_dir("META")
        p.mkdir(parents=True, exist_ok=True)
        (p / "agents.pkl").write_bytes(b"not a pickle")
        assert load_stage_checkpoint("META", "agents") is None

    def test_unpicklable_keys_dropped_not_fatal(self, tmp_path, monkeypatch):
        """防 RLock 净化（replay cache 的教训）：不可序列化键逐键置 None，
        runtime 句柄键（shared_llm_client）直接剔除，dump 永不 raise。"""
        monkeypatch.setenv("AEGIS_STAGE_DIR", str(tmp_path))
        payload = {
            "good": [1, 2, 3],
            "bad_lock": threading.Lock(),
            "shared_llm_client": MagicMock(),
        }
        out = dump_stage_checkpoint("META", "agents", payload, digest="d")
        assert out is not None
        rec = pickle.loads(out.read_bytes())
        assert rec["payload"]["good"] == [1, 2, 3]
        assert rec["payload"]["bad_lock"] is None
        assert "shared_llm_client" not in rec["payload"]

    def test_smoke_mode_isolated_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AEGIS_STAGE_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert stage_checkpoint_dir("META", smoke_mode=True).as_posix().startswith(
            ".cache/smoke/stages/")
        assert stage_checkpoint_dir("META").as_posix().startswith(".cache/stages/")


# ─────────────────────────────────────────────────────────────────────
# 3+4. --update 复用：零 LLM 调用 + 中文标注（离线全管线）
# ─────────────────────────────────────────────────────────────────────

def _offline_orchestrator() -> AutoResearchOrchestrator:
    return AutoResearchOrchestrator(
        market_data_connector_factory=_FakeMarketDataConnector,
        catalyst_calendar_factory=_FakeCatalystCalendar,
        form4_connector_factory=_FakeForm4Connector,
    )


def _config(**overrides) -> ResearchConfig:
    base = dict(
        ticker="META",
        period="FY2024",
        generate_html=False,
        enable_openbb=False,
        enable_news_sentiment=False,
    )
    base.update(overrides)
    return ResearchConfig(**base)


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGIS_STAGE_DIR", str(tmp_path / "stages"))
    monkeypatch.setenv("AEGIS_THESIS_DIR", str(tmp_path / "thesis"))
    monkeypatch.setenv("AEGIS_SKIP_SPARKLINE", "1")  # 渲染层离线开关
    return tmp_path


class TestUpdateReusePath:

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_update_reuses_agents_with_zero_llm_calls(
        self, mock_fetch, isolated_dirs,
    ):
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS)

        # Run 1（全量，rule-based）：落 agents checkpoint + thesis v1
        r1 = _offline_orchestrator().run(_config())
        agents_pkl = (isolated_dirs / "stages" / "meta" / "agents.pkl")
        assert agents_pkl.exists(), "agents stage checkpoint 未落盘"
        for stage in ("data", "valuation"):
            assert (isolated_dirs / "stages" / "meta" / f"{stage}.pkl").exists()
        thesis_chain = isolated_dirs / "thesis" / "meta_platforms.jsonl"
        assert thesis_chain.exists(), "thesis 版本链未落盘"
        assert any("观点版本 v1" in line for line in r1.pipeline_log)

        # Run 2（--update + use_llm=True）：agents 输入未变 → 复用命中，
        # 全程零 LLM 调用（counting mock 断言 0 次）。
        llm_mock = MagicMock(name="llm_client_never_called")
        orch2 = _offline_orchestrator()
        with patch.object(
            AutoResearchOrchestrator, "_check_llm_backend_health",
            return_value=None,
        ), patch.object(
            AutoResearchOrchestrator, "_resolve_llm_client",
            return_value=llm_mock,
        ) as resolve_llm, patch.object(
            AutoResearchOrchestrator, "_resolve_fast_llm_client",
            return_value=llm_mock,
        ) as resolve_fast:
            r2 = orch2.run(_config(
                update_mode=True,
                use_llm=True,
                generate_html=True,
                output_dir=str(isolated_dirs / "reports"),
            ))

        # 零 LLM：连 client 都不许被解析，更不许被调用
        assert resolve_llm.call_count == 0
        assert resolve_fast.call_count == 0
        assert llm_mock.method_calls == []

        # 复用命中的日志（agents / critics / 合成器 / editor 四处）
        log_text = "\n".join(r2.pipeline_log)
        assert "增量复用" in log_text
        assert "基本面输入未变" in log_text
        assert "跳过 Report Editor" in log_text

        # 判断产物与 run 1 同源
        assert r2.decision.publishing_status == r1.decision.publishing_status
        assert r2.signal.direction == r1.signal.direction

        # 中文标注注入成品 HTML：分析时点声明 + 观点版本
        assert r2.html_path is not None
        html = open(r2.html_path, encoding="utf-8").read()
        assert "智能体分析引用自" in html
        assert "（基本面输入未变）" in html
        assert "观点版本 v2" in html  # run 2 追加了版本链第二版

        # thesis 版本链 append-only：两行、版本递增、父指针成链
        lines = [ln for ln in thesis_chain.read_text(encoding="utf-8").splitlines() if ln]
        assert len(lines) == 2
        import json
        v1, v2 = (json.loads(ln) for ln in lines)
        assert (v1["version"], v2["version"]) == (1, 2)
        assert v2["parent_version"] == 1

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_update_falls_back_to_rerun_on_fundamental_change(
        self, mock_fetch, isolated_dirs,
    ):
        """基本面变了（新财报数字）→ digest 不一致 → agents 正常重跑。"""
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS)
        _offline_orchestrator().run(_config())

        changed_facts = dict(MOCK_XBRL_FACTS)
        changed_facts["us-gaap:Revenues"] = 190_000_000_000
        changed_facts["us-gaap:GrossProfit"] = 150_000_000_000
        mock_fetch.return_value = _make_mock_packet(changed_facts)
        r2 = _offline_orchestrator().run(_config(update_mode=True))

        log_text = "\n".join(r2.pipeline_log)
        assert "digest 不一致" in log_text
        assert "个 agent 判断引用自" not in log_text  # 未复用 agents
        assert "Ran 7 agents" in log_text  # 正常重跑（rule-based）

    @patch("aegis.core.acquisition.connectors.edgar_connector.SECEDGARConnector.fetch")
    def test_update_without_prior_checkpoint_runs_full(
        self, mock_fetch, isolated_dirs,
    ):
        """没有历史 checkpoint 的 --update = 普通全量 run（不 raise）。"""
        mock_fetch.return_value = _make_mock_packet(MOCK_XBRL_FACTS)
        r = _offline_orchestrator().run(_config(update_mode=True))
        assert "Ran 7 agents" in "\n".join(r.pipeline_log)
        assert (isolated_dirs / "stages" / "meta" / "agents.pkl").exists()
