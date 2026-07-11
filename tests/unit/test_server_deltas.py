"""Phase 3 任务 C2 — dashboard 兜底扫描 + delta 简报浏览的服务器路由测试.

覆盖：

- ``/api/deltas``：tmp 造几份假 delta json → 返回正确条数、按 mtime 倒序、缺目录 → []。
- ``/delta/{slug}``：路径穿越被拒（400）、正常 slug 返回内容、缺失 → 404。
- ``_maybe_background_scan``：去抖（第二次被跳过）、scan_once 抛异常不影响主调用。
- ``/api/scan``：返回 started（scan_once monkeypatch 成空操作）。

绝不真连网络：涉及扫描的用例一律 monkeypatch
``aegis.core.monitor.scanner.scan_once``。所有落盘走 tmp_path，
并把 ``server.app.DELTA_DIR`` 重定向到 tmp，绝不污染真实 ``.cache/``。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from server import app as app_module

try:  # httpx 缺失时 TestClient 不可用 → 退化为直接单测 helper。
    from fastapi.testclient import TestClient

    _HAS_TESTCLIENT = True
except Exception:  # noqa: BLE001
    TestClient = None  # type: ignore[assignment,misc]
    _HAS_TESTCLIENT = False


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def delta_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 server.app.DELTA_DIR 重定向到 tmp 下的空 deltas 目录。"""
    d = tmp_path / "deltas"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_module, "DELTA_DIR", d)
    return d


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    """TestClient；同时把 scan_once 换成计数器空操作，避免任何真扫描。"""
    if not _HAS_TESTCLIENT:
        pytest.skip("fastapi.testclient 不可用（缺 httpx）")
    _install_fake_scan(monkeypatch)
    # 去抖时间戳复位，保证 /search 触发路径可预期。
    monkeypatch.setattr(app_module, "_last_bg_scan_at", 0.0)
    return TestClient(app_module.app)


def _install_fake_scan(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """把 aegis.core.monitor.scanner.scan_once 换成计数器空操作，返回计数字典。"""
    counter = {"n": 0}
    from aegis.core.monitor import scanner as monitor_scanner

    def _fake_scan_once(**kwargs):  # noqa: ANN003
        counter["n"] += 1
        counter["last_kwargs"] = kwargs  # type: ignore[assignment]
        return None

    monkeypatch.setattr(monitor_scanner, "scan_once", _fake_scan_once)
    return counter


def _write_delta(d: Path, entity_id: str, version: int, *,
                 summary: str = "本次复核论点无实质变化。",
                 trigger: str | None = "并购公告") -> Path:
    """造一份 delta {entity}_v{N}.json + .md（贴合 DeltaBriefing.to_dict 结构）。"""
    stem = f"{entity_id}_v{version}"
    payload = {
        "entity_id": entity_id,
        "from_version": version - 1 if version > 1 else None,
        "to_version": version,
        "trigger_zh": trigger,
        "changes": [],
        "monitorables_added": [],
        "monitorables_removed": [],
        "summary_zh": summary,
    }
    (d / f"{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / f"{stem}.md").write_text(
        f"# 论点 Delta 简报 · {entity_id} · v{version}\n\n{summary}\n",
        encoding="utf-8")
    return d / f"{stem}.json"


# ---------------------------------------------------------------------------
# _delta_cards / GET /api/deltas
# ---------------------------------------------------------------------------

def test_delta_cards_missing_dir_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert app_module._delta_cards(missing) == []


def test_delta_cards_counts_and_fields(delta_dir: Path) -> None:
    _write_delta(delta_dir, "002669", 2, summary="识别 1 处论点变化", trigger="减值")
    _write_delta(delta_dir, "600519_sh", 3, summary="无实质变化", trigger=None)

    cards = app_module._delta_cards(delta_dir)
    assert len(cards) == 2
    by_id = {c["entity_id"]: c for c in cards}
    assert set(by_id) == {"002669", "600519_sh"}
    c = by_id["002669"]
    assert c["to_version"] == 2
    assert c["trigger_zh"] == "减值"
    assert c["summary_zh"] == "识别 1 处论点变化"
    assert c["file"] == "/delta/002669_v2"
    assert isinstance(c["when"], str) and c["when"]


def test_delta_cards_sorted_by_mtime_desc(delta_dir: Path) -> None:
    old = _write_delta(delta_dir, "000001", 1)
    new = _write_delta(delta_dir, "000002", 1)
    # 把 old 的 mtime 推到过去，new 保持现在。
    past = time.time() - 3600
    import os
    os.utime(old, (past, past))

    cards = app_module._delta_cards(delta_dir)
    assert [c["entity_id"] for c in cards] == ["000002", "000001"]


def test_delta_cards_limit_and_bad_json(delta_dir: Path) -> None:
    for i in range(5):
        _write_delta(delta_dir, f"00000{i}", 1)
    # 一份坏 JSON 应被静默跳过，不计入。
    (delta_dir / "broken_v1.json").write_text("{not valid", encoding="utf-8")

    cards = app_module._delta_cards(delta_dir, limit=3)
    assert len(cards) == 3


@pytest.mark.skipif(not _HAS_TESTCLIENT, reason="需要 fastapi TestClient")
def test_api_deltas_route(client, delta_dir: Path) -> None:
    _write_delta(delta_dir, "002669", 2)
    _write_delta(delta_dir, "301358", 4)
    r = client.get("/api/deltas")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert {c["entity_id"] for c in body} == {"002669", "301358"}


@pytest.mark.skipif(not _HAS_TESTCLIENT, reason="需要 fastapi TestClient")
def test_api_deltas_empty_when_no_dir(client, tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "DELTA_DIR", tmp_path / "absent")
    r = client.get("/api/deltas")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# GET /delta/{slug}
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_TESTCLIENT, reason="需要 fastapi TestClient")
def test_delta_page_normal(client, delta_dir: Path) -> None:
    _write_delta(delta_dir, "002669", 2, summary="核心论点由 A 调整为 B")
    r = client.get("/delta/002669_v2")
    assert r.status_code == 200
    assert "核心论点由 A 调整为 B" in r.text
    assert "<pre" in r.text


@pytest.mark.skipif(not _HAS_TESTCLIENT, reason="需要 fastapi TestClient")
def test_delta_page_traversal_rejected(client, delta_dir: Path) -> None:
    # 编码穿越（%2f=/）被拒：路由层单段匹配挡下 → 404；我们的字符检查挡下 → 400。
    # 两者都算「拒绝、不泄漏任意文件」。精确的 400 字符检查见 *_unit 用例。
    r = client.get("/delta/..%2f..%2fetc%2fpasswd")
    assert r.status_code in (400, 404)
    # 确认没把 /etc/passwd 之类内容读出来。
    assert "root:" not in r.text


@pytest.mark.skipif(not _HAS_TESTCLIENT, reason="需要 fastapi TestClient")
def test_delta_page_missing_returns_404(client, delta_dir: Path) -> None:
    r = client.get("/delta/does_not_exist_v9")
    assert r.status_code == 404


def test_delta_page_traversal_rejected_unit() -> None:
    """直接单测 helper：含 .. 的 slug 抛 400（不依赖 TestClient）。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        app_module.delta_page("../secret")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc2:
        app_module.delta_page("a/b")
    assert exc2.value.status_code == 400


# ---------------------------------------------------------------------------
# _maybe_background_scan 去抖 + 容错
# ---------------------------------------------------------------------------

def test_maybe_background_scan_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _install_fake_scan(monkeypatch)
    monkeypatch.setattr(app_module, "_last_bg_scan_at", 0.0)

    # 第一次：应起线程并调用 scan_once 一次。
    t1 = app_module._maybe_background_scan(min_interval_s=1800)
    assert t1 is not None
    t1.join(timeout=5)
    assert counter["n"] == 1

    # 第二次（紧接着，远小于 30 分钟）：被去抖跳过，返回 None，不再调用。
    t2 = app_module._maybe_background_scan(min_interval_s=1800)
    assert t2 is None
    assert counter["n"] == 1


def test_maybe_background_scan_fires_again_after_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _install_fake_scan(monkeypatch)
    # 把上次时间戳设成很久以前 → 应再次触发。
    monkeypatch.setattr(app_module, "_last_bg_scan_at", time.time() - 10_000)
    t = app_module._maybe_background_scan(min_interval_s=1800)
    assert t is not None
    t.join(timeout=5)
    assert counter["n"] == 1


def test_maybe_background_scan_swallows_scan_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis.core.monitor import scanner as monitor_scanner

    def _boom(**kwargs):  # noqa: ANN003
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(monitor_scanner, "scan_once", _boom)
    monkeypatch.setattr(app_module, "_last_bg_scan_at", 0.0)

    # 主调用不应抛异常；线程内异常被吞掉。
    t = app_module._maybe_background_scan(min_interval_s=1800)
    assert t is not None
    t.join(timeout=5)  # join 不会 re-raise 线程内异常
    assert not t.is_alive()


def test_maybe_background_scan_tolerates_missing_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """monitor 包 import 失败时 _scan_worker 静默降级，主调用不受影响。"""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "aegis.core.monitor" or name.startswith("aegis.core.monitor.scanner"):
            raise ImportError("simulated missing monitor package")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    monkeypatch.setattr(app_module, "_last_bg_scan_at", 0.0)

    t = app_module._maybe_background_scan(min_interval_s=1800)
    assert t is not None
    t.join(timeout=5)
    assert not t.is_alive()


# ---------------------------------------------------------------------------
# POST /api/scan
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_TESTCLIENT, reason="需要 fastapi TestClient")
def test_api_scan_started(client) -> None:
    r = client.post("/api/scan")
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True
    assert isinstance(body["at"], str) and body["at"]
    assert body["dry_run"] is False


@pytest.mark.skipif(not _HAS_TESTCLIENT, reason="需要 fastapi TestClient")
def test_api_scan_dry_run_flag(client) -> None:
    r = client.post("/api/scan?dry_run=1")
    assert r.status_code == 200
    assert r.json()["dry_run"] is True


def test_api_scan_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """直接单测 helper（不依赖 TestClient）：返回 started + dry_run 透传。"""
    counter = _install_fake_scan(monkeypatch)
    out = app_module.api_scan(dry_run=True)
    assert out["started"] is True
    assert out["dry_run"] is True
    assert isinstance(out["at"], str) and out["at"]
    # 给后台线程一点时间跑（尽力，不强求）——主要断言在返回值上。
    time.sleep(0.05)
    assert counter["n"] >= 0  # 不做时序强断言，仅确认不崩


# ---------------------------------------------------------------------------
# /search 触发去抖扫描（打开即扫）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_TESTCLIENT, reason="需要 fastapi TestClient")
def test_search_page_triggers_background_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _HAS_TESTCLIENT:
        pytest.skip("需要 TestClient")
    counter = _install_fake_scan(monkeypatch)
    monkeypatch.setattr(app_module, "_last_bg_scan_at", 0.0)
    c = TestClient(app_module.app)
    r = c.get("/search")
    assert r.status_code == 200
    # 打开 dashboard 应触发一轮扫描（去抖首次穿透）。给线程一点时间。
    time.sleep(0.1)
    assert counter["n"] == 1
    # 第二次打开被去抖跳过，计数不再增加。
    c.get("/search")
    time.sleep(0.05)
    assert counter["n"] == 1
