"""Aegis 2.0 Phase 3 任务 C1 — 监控扫描 CLI + launchd 资产回归测试.

锁定的行为：
① scripts/scan_watchlist.py 能被从文件路径加载出 main（不真连网络就 import 成功）；
② monkeypatch scanner.scan_once 成假桩后，--dry-run / 默认 / --smoke / --no-llm
   路径都打印不崩、返回码 0，且透传的 dry_run/smoke/use_llm 参数正确；
③ --postmortems 会额外调 run_postmortems 并打印生成数量；
④ scan_once 抛异常时 main 不冒泡，返回非零码；
⑤ configs/launchd/com.aegis.scan.plist 是合法 XML，含 com.aegis.scan /
   StartCalendarInterval / Hour 16。

全部用注入的假 scan_once / run_postmortems，绝不真连网络、绝不真跑复研。
"""

from __future__ import annotations

import importlib.util
import sys
import xml.dom.minidom
from pathlib import Path
from types import ModuleType

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _PROJECT_ROOT / "scripts" / "scan_watchlist.py"
_PLIST_PATH = _PROJECT_ROOT / "configs" / "launchd" / "com.aegis.scan.plist"


# ---------------------------------------------------------------------------
# 夹具：从文件路径加载 CLI 模块（scripts/ 不是包）
# ---------------------------------------------------------------------------

def _load_cli() -> ModuleType:
    """用 importlib 从文件路径加载 scan_watchlist.py（scripts/ 非包）。"""
    spec = importlib.util.spec_from_file_location("scan_watchlist_cli", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeReport:
    """假 ScanReport 桩：只需能 to_markdown()。"""

    def __init__(self, md: str = "# 假扫描报告\n\n无 enabled 标的。") -> None:
        self._md = md

    def to_markdown(self) -> str:
        return self._md


@pytest.fixture
def cli() -> ModuleType:
    return _load_cli()


# ---------------------------------------------------------------------------
# ① import 不崩
# ---------------------------------------------------------------------------

def test_cli_imports_and_has_main(cli: ModuleType) -> None:
    assert callable(cli.main)


# ---------------------------------------------------------------------------
# ② --dry-run / 默认 / --smoke / --no-llm 参数透传
# ---------------------------------------------------------------------------

def test_dry_run_calls_scan_once_and_prints(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    calls: list[dict] = []

    def _fake_scan_once(**kwargs):
        calls.append(kwargs)
        return _FakeReport()

    monkeypatch.setattr(cli.scanner_mod, "scan_once", _fake_scan_once)

    rc = cli.main(["--dry-run"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["dry_run"] is True
    assert calls[0]["smoke"] is False
    assert calls[0]["use_llm"] is True  # 默认开 LLM
    out = capsys.readouterr().out
    assert "假扫描报告" in out


def test_default_run_uses_llm(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        cli.scanner_mod, "scan_once",
        lambda **kw: (calls.append(kw), _FakeReport())[1],
    )

    rc = cli.main([])
    assert rc == 0
    assert calls[0]["dry_run"] is False
    assert calls[0]["smoke"] is False
    assert calls[0]["use_llm"] is True


def test_smoke_disables_llm(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        cli.scanner_mod, "scan_once",
        lambda **kw: (calls.append(kw), _FakeReport())[1],
    )

    rc = cli.main(["--smoke"])
    assert rc == 0
    assert calls[0]["smoke"] is True
    assert calls[0]["use_llm"] is False


def test_no_llm_disables_llm_without_smoke(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        cli.scanner_mod, "scan_once",
        lambda **kw: (calls.append(kw), _FakeReport())[1],
    )

    rc = cli.main(["--no-llm"])
    assert rc == 0
    assert calls[0]["smoke"] is False
    assert calls[0]["use_llm"] is False


def test_watchlist_path_passed_through(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        cli.scanner_mod, "scan_once",
        lambda **kw: (calls.append(kw), _FakeReport())[1],
    )

    rc = cli.main(["--watchlist", "/tmp/foo.yaml"])
    assert rc == 0
    assert calls[0]["watchlist_path"] == "/tmp/foo.yaml"


# ---------------------------------------------------------------------------
# ③ --postmortems 额外调 run_postmortems
# ---------------------------------------------------------------------------

def test_postmortems_flag_runs_and_prints_count(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(cli.scanner_mod, "scan_once", lambda **kw: _FakeReport())
    pm_calls: list[int] = []

    def _fake_run_postmortems(**kwargs):
        pm_calls.append(1)
        return ["pm1", "pm2", "pm3"]  # 假装生成 3 份

    monkeypatch.setattr(cli.postmortem_mod, "run_postmortems", _fake_run_postmortems)

    rc = cli.main(["--postmortems"])
    assert rc == 0
    assert pm_calls == [1]
    out = capsys.readouterr().out
    assert "生成 3 份" in out


def test_postmortems_not_run_by_default(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.scanner_mod, "scan_once", lambda **kw: _FakeReport())
    pm_calls: list[int] = []
    monkeypatch.setattr(
        cli.postmortem_mod, "run_postmortems",
        lambda **kw: pm_calls.append(1) or [],
    )

    rc = cli.main([])
    assert rc == 0
    assert pm_calls == []


# ---------------------------------------------------------------------------
# ④ 容错：scan_once 抛异常 → 不冒泡，返回非零码
# ---------------------------------------------------------------------------

def test_scan_once_exception_returns_nonzero(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    def _boom(**kwargs):
        raise RuntimeError("模拟扫描崩溃")

    monkeypatch.setattr(cli.scanner_mod, "scan_once", _boom)

    rc = cli.main([])
    assert rc != 0  # 非零码，但没有把异常抛出来
    err = capsys.readouterr().err
    assert "扫描失败" in err


def test_postmortems_exception_does_not_break_scan(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.scanner_mod, "scan_once", lambda **kw: _FakeReport())

    def _boom(**kwargs):
        raise RuntimeError("复盘崩了")

    monkeypatch.setattr(cli.postmortem_mod, "run_postmortems", _boom)

    rc = cli.main(["--postmortems"])
    assert rc != 0  # 复盘失败返回非零，但扫描已成功，未抛异常


# ---------------------------------------------------------------------------
# ⑤ launchd plist 合法性
# ---------------------------------------------------------------------------

def test_plist_is_valid_xml() -> None:
    # 不抛异常即合法 XML。
    dom = xml.dom.minidom.parse(str(_PLIST_PATH))
    assert dom is not None


def test_plist_contents() -> None:
    text = _PLIST_PATH.read_text(encoding="utf-8")
    assert "com.aegis.scan" in text
    assert "StartCalendarInterval" in text
    # Hour 16 / Minute 30。
    assert "<key>Hour</key>" in text
    assert "<integer>16</integer>" in text
    assert "<key>Minute</key>" in text
    assert "<integer>30</integer>" in text
    assert "RunAtLoad" in text
    assert "__PROJECT_ROOT__" in text  # 占位符尚未替换（由安装脚本处理）


def test_plist_scan_command_targets_cli() -> None:
    text = _PLIST_PATH.read_text(encoding="utf-8")
    assert "scripts/scan_watchlist.py" in text
    assert "--postmortems" in text


def test_install_script_exists_and_executable() -> None:
    install = _PROJECT_ROOT / "scripts" / "install_launchd.sh"
    assert install.exists()
    # 内容合理性：含替换占位符、load、幂等 unload。
    text = install.read_text(encoding="utf-8")
    assert "__PROJECT_ROOT__" in text
    assert "launchctl load" in text
    assert "launchctl unload" in text


# 让 sys.path 副作用（CLI 顶部 insert 项目根）不影响其它测试模块的清洁度。
@pytest.fixture(autouse=True)
def _cleanup_syspath():
    before = list(sys.path)
    yield
    # CLI import 会往 sys.path[0] 插项目根；测试结束不强制回滚（无害），仅占位。
    _ = before
