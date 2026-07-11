"""FastAPI app — local web entry for Aegis Research OS.

Routes:

    GET  /                       → redirect to /search
    GET  /search                 → serve web/search.html (with API-driven data)
    GET  /progress               → serve web/progress.html
    GET  /report/{slug}          → serve a completed report from demos/
    GET  /web/*                  → static assets (report.jsx, future CSS, etc.)

    GET  /api/universe           → ticker universe (list of {tck, ex, name, sector})
    GET  /api/recent             → recent reports scanned from demos/
    GET  /api/runs               → active runs
    POST /api/run                → {ticker: "NVDA"} → spawn pipeline, returns run state
    GET  /api/runs/{id}          → poll run state
    GET  /api/progress/{id}      → SSE stream tailing logs/run_{id}.log

    POST /api/scan               → 手动触发一轮 Phase 3 监控扫描（后台线程）
    GET  /api/deltas             → 最近的论点 delta 简报卡片列表
    GET  /delta/{slug}           → 单份 delta 简报原文（Markdown 包 <pre>）

Phase 3 事件循环兜底：打开 dashboard（GET /search）即去抖触发一轮后台扫描
（:func:`_maybe_background_scan`），扫出的 delta 简报通过 /api/deltas 可见。
所有监控相关逻辑对「monitor 包不可用」容错降级——扫描器缺失时页面照常服务。

Designed for single-user local dev (`uvicorn server.app:app --reload`).
Not hardened for untrusted input.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .runner import RunnerRegistry
from .scanner import read_report_html, scan_demos, _relative_time
from .universe import get_universe

# ─────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = PROJECT_ROOT / "web"
DEMOS_DIR = PROJECT_ROOT / "demos"
#: Phase 3 delta 简报落盘目录（扫描器写 {entity_id}_v{N}.md/.json）。
#: 测试可 monkeypatch 本常量重定向到 tmp 目录。
DELTA_DIR = PROJECT_ROOT / ".cache" / "deltas"

app = FastAPI(title="Aegis Research OS", version="0.2.0")

_runner = RunnerRegistry(PROJECT_ROOT)

# ── Phase 3 兜底扫描的去抖状态（模块级；线程安全）─────────────────
_bg_scan_lock = threading.Lock()
_last_bg_scan_at: float = 0.0   # 上次触发后台扫描的 wall-clock 时间戳（time.time()）


# ─────────────────────────────────────────────────────────────────
# Phase 3 兜底扫描（后台线程 + 去抖）— 全部对 monitor 包缺失容错降级
# ─────────────────────────────────────────────────────────────────

def _scan_worker(**kwargs: Any) -> None:
    """在后台线程里同步跑一轮 :func:`scan_once`；吞掉一切异常（兜底路径）。

    延迟 import scanner，既容忍 monitor 包不可用，又让测试可 monkeypatch
    ``aegis.core.monitor.scanner.scan_once`` 生效。永不把异常抛出线程。
    """
    try:
        from aegis.core.monitor import scanner as monitor_scanner
    except Exception as e:  # noqa: BLE001 — monitor 包不可用则静默降级
        logger.warning("后台扫描不可用：monitor 包 import 失败：%s", e)
        return
    try:
        monitor_scanner.scan_once(**kwargs)
    except Exception as e:  # noqa: BLE001 — 扫描内部异常不影响 web 层
        logger.warning("后台 scan_once 失败：%s", e)


def _run_scan_background(**kwargs: Any) -> threading.Thread | None:
    """起一个 daemon 线程跑一轮扫描（fire-and-forget）。返回线程句柄或 None。

    ``scan_once`` 是同步函数，在独立线程里跑，绝不 async 化、不阻塞调用方。
    起线程失败（极少见）也不 raise，仅告警返回 None。
    """
    try:
        t = threading.Thread(
            target=_scan_worker,
            kwargs=kwargs,
            name="aegis-monitor-scan",
            daemon=True,
        )
        t.start()
        return t
    except Exception as e:  # noqa: BLE001 — 起线程失败也不打断请求
        logger.warning("起后台扫描线程失败：%s", e)
        return None


def _maybe_background_scan(min_interval_s: float = 1800.0) -> threading.Thread | None:
    """去抖后台扫描：距上次 < ``min_interval_s`` 则跳过，返回 None。

    否则记录本次时间戳并起一个后台扫描线程，返回其句柄。用模块级时间戳
    ``_last_bg_scan_at`` 去抖，加锁避免并发请求同时穿透。默认 30 分钟一轮。
    """
    global _last_bg_scan_at
    now = time.time()
    with _bg_scan_lock:
        if now - _last_bg_scan_at < min_interval_s:
            return None
        _last_bg_scan_at = now
    return _run_scan_background()


# ─────────────────────────────────────────────────────────────────
# Static / page routes
# ─────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/search")


@app.get("/search", response_class=HTMLResponse, include_in_schema=False)
def search_page() -> FileResponse:
    # dashboard 打开即去抖触发一轮兜底扫描（fire-and-forget，不阻塞响应）。
    try:
        _maybe_background_scan()
    except Exception as e:  # noqa: BLE001 — 兜底扫描任何异常都不该拖垮页面
        logger.debug("search 页触发后台扫描失败（忽略）：%s", e)
    return FileResponse(WEB_DIR / "search.html", media_type="text/html")


@app.get("/progress", response_class=HTMLResponse, include_in_schema=False)
def progress_page() -> FileResponse:
    return FileResponse(WEB_DIR / "progress.html", media_type="text/html")


@app.get("/report/{slug}", response_class=HTMLResponse, include_in_schema=False)
def report_page(slug: str) -> HTMLResponse:
    """Serve a rendered report by filename slug (no extension).

    Slug format: `{ticker}_{period}_auto_report` e.g. `301358_fy2024_auto_report`.
    """
    # Reject traversal attempts before touching the filesystem.
    if "/" in slug or "\\" in slug or ".." in slug:
        raise HTTPException(status_code=400, detail="invalid slug")
    html = read_report_html(DEMOS_DIR, slug)
    if html is None:
        raise HTTPException(status_code=404, detail="report not found")
    return HTMLResponse(content=html)


# Mount the raw web/ for developer access (e.g. open report.jsx directly
# during debugging). Report pages themselves are self-contained — no
# client-side load from /web/report.jsx needed.
app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")


# ─────────────────────────────────────────────────────────────────
# API routes
# ─────────────────────────────────────────────────────────────────

@app.get("/api/universe")
def api_universe() -> list[dict[str, Any]]:
    return get_universe(PROJECT_ROOT)


@app.get("/api/recent")
def api_recent(limit: int = 12) -> list[dict[str, Any]]:
    limit = max(1, min(50, limit))
    return scan_demos(DEMOS_DIR, limit=limit)


class RunRequest(BaseModel):
    ticker: str


@app.post("/api/run")
def api_run(req: RunRequest) -> dict[str, Any]:
    ticker = req.ticker.strip().upper()
    if not ticker or len(ticker) > 12 or not all(c.isalnum() or c in "._-" for c in ticker):
        raise HTTPException(status_code=400, detail="invalid ticker")
    state = _runner.start_run(ticker)
    return _runner.as_dict(state)


@app.get("/api/runs")
def api_runs() -> list[dict[str, Any]]:
    return [_runner.as_dict(s) for s in _runner.list_active()]


@app.get("/api/runs/{run_id}")
def api_run_state(run_id: str) -> dict[str, Any]:
    state = _runner.poll(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _runner.as_dict(state)


@app.get("/api/progress/{run_id}")
async def api_progress(run_id: str, request: Request) -> StreamingResponse:
    """Server-sent events stream of the run's live log + terminal state.

    Event payloads are JSON with a `type` discriminator:
      - {"type": "log",   "line": "...", "seq": N}
      - {"type": "state", "status": "running|finished|failed", "report": "/report/..." | null}
      - {"type": "hb"}   # heartbeat every ~10s to keep the connection alive

    Client should subscribe with `new EventSource("/api/progress/{id}")`
    and close after receiving a terminal "state" message.
    """
    state = _runner.poll(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")

    log_path = Path(state.log_path)

    async def gen() -> AsyncGenerator[bytes, None]:
        # Initial state snapshot so the client can render immediately.
        yield _sse({"type": "state", **_state_payload(state)})

        seq = 0
        pos = 0
        last_hb = asyncio.get_event_loop().time()

        while True:
            if await request.is_disconnected():
                return

            # Tail new bytes from the log file if it exists.
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
            except FileNotFoundError:
                chunk = ""

            if chunk:
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    seq += 1
                    yield _sse({"type": "log", "line": line, "seq": seq})

            # Re-poll for terminal state.
            s = _runner.poll(run_id)
            if s and s.status != "running":
                # Drain any remaining bytes the subprocess flushed before exit.
                try:
                    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        tail = fh.read()
                except FileNotFoundError:
                    tail = ""
                for line in tail.splitlines():
                    if not line.strip():
                        continue
                    seq += 1
                    yield _sse({"type": "log", "line": line, "seq": seq})
                yield _sse({"type": "state", **_state_payload(s)})
                return

            now = asyncio.get_event_loop().time()
            if now - last_hb > 10:
                last_hb = now
                yield _sse({"type": "hb"})

            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # in case a proxy is in front
        },
    )


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _state_payload(state: Any) -> dict[str, Any]:
    return {
        "status": state.status,
        "report": state.report_path,
        "exit_code": state.exit_code,
        "ticker": state.ticker,
        "run_id": state.run_id,
    }


# ─────────────────────────────────────────────────────────────────
# Phase 3 监控 API：手动扫描 + delta 简报浏览
# ─────────────────────────────────────────────────────────────────

def _delta_cards(delta_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    """扫 ``delta_dir/*.json``，按 mtime 倒序返回卡片列表。缺目录 → []。

    每张卡片：entity_id / to_version / trigger_zh / summary_zh / when(相对时间)
    / file(``/delta/{stem}`` 指向同名 .md 原文)。坏 JSON / 读盘失败逐条跳过。
    """
    if not delta_dir.exists():
        return []
    now = time.time()
    rows: list[tuple[float, dict[str, Any]]] = []
    for f in delta_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("跳过坏 delta json %s：%s", f.name, e)
            continue
        if not isinstance(data, dict):
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        rows.append((mtime, {
            "entity_id": data.get("entity_id") or f.stem,
            "to_version": data.get("to_version"),
            "trigger_zh": data.get("trigger_zh"),
            "summary_zh": data.get("summary_zh") or "",
            "when": _relative_time(mtime, now),
            "file": f"/delta/{f.stem}",
        }))
    rows.sort(key=lambda t: t[0], reverse=True)
    return [card for _, card in rows[:limit]]


@app.post("/api/scan")
def api_scan(dry_run: bool = False) -> dict[str, Any]:
    """手动触发一轮 Phase 3 监控扫描（后台线程，fire-and-forget）。

    ``?dry_run=1`` 只报「会触发」，零副作用（透传给 scan_once）。返回受理时刻。
    """
    at = datetime.now().isoformat()
    thread = _run_scan_background(dry_run=dry_run)
    return {"started": thread is not None, "at": at, "dry_run": dry_run}


@app.get("/api/deltas")
def api_deltas(limit: int = 20) -> list[dict[str, Any]]:
    """最近的论点 delta 简报卡片，按 mtime 倒序。monitor 缺失 / 空目录 → []。"""
    limit = max(1, min(100, limit))
    try:
        return _delta_cards(DELTA_DIR, limit=limit)
    except Exception as e:  # noqa: BLE001 — 任何异常都降级为空列表
        logger.warning("api_deltas 失败：%s", e)
        return []


@app.get("/delta/{slug}", response_class=HTMLResponse, include_in_schema=False)
def delta_page(slug: str) -> HTMLResponse:
    """单份 delta 简报原文（Markdown 包一层 ``<pre>`` 返回）。

    slug 为 ``{entity_id}_v{N}``（不含扩展名）。防路径穿越：拒绝含
    ``/`` ``\\`` ``..`` 的 slug，并校验解析后路径仍在 :data:`DELTA_DIR` 内。
    """
    if "/" in slug or "\\" in slug or ".." in slug:
        raise HTTPException(status_code=400, detail="invalid slug")
    resolved = (DELTA_DIR / f"{slug}.md").resolve()
    if not str(resolved).startswith(str(DELTA_DIR.resolve())):
        raise HTTPException(status_code=400, detail="invalid slug")
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise HTTPException(status_code=404, detail="delta not found")
    body = (
        "<pre style=\"white-space:pre-wrap;word-break:break-word;"
        "font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;"
        "padding:24px;max-width:820px;margin:0 auto;\">"
        + html.escape(text)
        + "</pre>"
    )
    return HTMLResponse(content=body)
