"""Spawn the pipeline as a subprocess and track run status.

Why subprocess and not in-process?
    - The core pipeline (`aegis/core/orchestrator/auto_research.py`) is
      synchronous Python that runs for ~25 minutes and makes network
      calls. Running it in the FastAPI event loop would block every other
      request. Importing it once and then offloading via `asyncio.to_thread`
      would work too, but spawning `./run_research.sh` keeps the web layer
      as a pure shell — the core codebase doesn't change at all.
    - Subprocess stdout streams into `logs/run_{id}.log` which the SSE
      endpoint (`/api/progress/{run_id}`) tails live.

Run state is persisted to `logs/runs/{run_id}.json`, so `--reload` or
crashes don't lose active-run tracking. On startup we reconnect to live
subprocesses via PID liveness checks.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any


@dataclass
class RunState:
    run_id: str
    ticker: str
    started_at: float
    status: str = "running"   # running | finished | failed
    log_path: str = ""
    report_path: str | None = None
    exit_code: int | None = None
    pid: int | None = None
    notified: bool = False    # macOS notification fired exactly once


class RunnerRegistry:
    """Tiny process-lifetime registry of pipeline runs.

    Not thread-safe in the strictest sense but FastAPI handles
    dispatch and our operations are cheap dict updates. Fine for
    single-user local dev.
    """

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.logs_dir = project_root / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.logs_dir / "runs"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, RunState] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        # Reconnect to any runs from prior server life (--reload / crash).
        # Subprocesses spawned with start_new_session=True outlive the server.
        self._restore_runs()

    # ── Disk persistence ─────────────────────────────────────────────

    def _state_file(self, run_id: str) -> Path:
        return self.state_dir / f"{run_id}.json"

    def _persist(self, state: RunState) -> None:
        """Write state to disk (best-effort, never raises)."""
        try:
            self._state_file(state.run_id).write_text(
                json.dumps(asdict(state), indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _restore_runs(self) -> None:
        """Reload persisted states and reconcile with live PIDs.

        For each persisted run:
        - terminal (finished/failed) → load as-is
        - running + PID alive → load as-is; poll() will observe the exit
        - running + PID dead → figure out terminal state by report file
          existence (don't fire notification — too late to be useful)
        """
        for state_file in sorted(self.state_dir.glob("*.json")):
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                state = RunState(**data)
            except Exception:
                continue
            if state.status == "running":
                if state.pid and _pid_alive(state.pid):
                    # Still running — keep in memory, poll() will work
                    # without a Popen handle (PID-liveness path).
                    pass
                else:
                    # Died without us watching. Retroactively set terminal
                    # state from on-disk evidence; skip notification.
                    self._finalize_from_disk(state, fire_notification=False)
            self._runs[state.run_id] = state

    def _finalize_from_disk(self, state: RunState, fire_notification: bool) -> None:
        """Set terminal status using demos/ + log evidence."""
        demos = self.project_root / "demos"
        matches = sorted(
            demos.glob(f"{state.ticker.lower()}_*_auto_report.html"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        recent = matches and matches[0].stat().st_mtime >= state.started_at
        if recent:
            state.status = "finished"
            state.exit_code = 0
            state.report_path = f"/report/{matches[0].stem}"
        else:
            state.status = "failed"
            state.exit_code = state.exit_code if state.exit_code is not None else 1
        if fire_notification and not state.notified:
            state.notified = True
            _notify_macos(state)
        self._persist(state)

    def start_run(self, ticker: str) -> RunState:
        """Spawn the pipeline for `ticker` and return its initial state."""
        run_id = _make_run_id()
        log_path = self.logs_dir / f"run_{run_id}.log"

        # Spawn via run_research.sh so any env-var setup there applies.
        # Merge stderr into stdout so the tailer catches everything.
        log_fh = log_path.open("w", encoding="utf-8", buffering=1)
        env = os.environ.copy()
        # Unbuffer Python in the child so log tailing sees each line promptly.
        env.setdefault("PYTHONUNBUFFERED", "1")

        script = str(self.project_root / "run_research.sh")
        cmd = ["bash", script, ticker]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,  # detach so server restart doesn't kill it
            )
        except OSError as e:
            state = RunState(
                run_id=run_id,
                ticker=ticker,
                started_at=time.time(),
                status="failed",
                log_path=str(log_path),
                exit_code=-1,
            )
            log_fh.write(f"[runner] spawn failed: {e}\n")
            log_fh.close()
            self._runs[run_id] = state
            self._persist(state)
            return state

        state = RunState(
            run_id=run_id,
            ticker=ticker,
            started_at=time.time(),
            status="running",
            log_path=str(log_path),
            pid=proc.pid,
        )
        self._runs[run_id] = state
        self._procs[run_id] = proc
        self._persist(state)
        return state

    def poll(self, run_id: str) -> RunState | None:
        """Refresh a run's state by polling the subprocess. Returns None if unknown.

        Two observation paths:
        - We have the Popen handle → `proc.poll()` gives the exit code directly.
        - We only have a PID (reconnected after --reload) → `os.kill(pid, 0)`
          tells us alive/dead; on dead we reconstruct terminal state from the
          demos/ file timestamp.
        """
        state = self._runs.get(run_id)
        if state is None:
            return None
        if state.status != "running":
            return state

        proc = self._procs.get(run_id)
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                return state
            state.exit_code = rc
            state.status = "finished" if rc == 0 else "failed"
            self._locate_report(state)
            if not state.notified:
                state.notified = True
                _notify_macos(state)
            self._persist(state)
            return state

        # No Popen handle — PID-liveness path (post-reconnect).
        if state.pid and _pid_alive(state.pid):
            return state
        # PID dead; finalize from disk + fire notification (we saw the
        # transition live even if we started watching mid-run).
        self._finalize_from_disk(state, fire_notification=True)
        return state

    def _locate_report(self, state: RunState) -> None:
        """Fill state.report_path by globbing demos/ for the newest match,
        but only if that file was produced by THIS run (mtime >= started_at).
        Otherwise a failed run would silently link to a stale report."""
        demos = self.project_root / "demos"
        matches = sorted(
            demos.glob(f"{state.ticker.lower()}_*_auto_report.html"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches and matches[0].stat().st_mtime >= state.started_at:
            state.report_path = f"/report/{matches[0].stem}"

    def as_dict(self, state: RunState) -> dict[str, Any]:
        return asdict(state)

    def list_active(self) -> list[RunState]:
        out: list[RunState] = []
        for rid in list(self._runs.keys()):
            s = self.poll(rid)
            if s and s.status == "running":
                out.append(s)
        return out


def _pid_alive(pid: int) -> bool:
    """Check if a PID is still running (kill with signal 0)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _make_run_id() -> str:
    """Human-ish run id: `20260418_151802_7f3a`."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:4]
    return f"{ts}_{suffix}"


def _notify_macos(state: RunState) -> None:
    """Fire a macOS banner when a run terminates. Fails silently off-Mac."""
    import sys
    if sys.platform != "darwin":
        return
    elapsed_min = (time.time() - state.started_at) / 60.0
    if state.status == "finished":
        title = f"Aegis · {state.ticker} 报告完成"
        body = f"耗时 {elapsed_min:.1f} 分 · 点击通知中心前往查看"
    else:
        title = f"Aegis · {state.ticker} 运行失败"
        body = f"退出码 {state.exit_code} · 查看日志 logs/run_{state.run_id}.log"
    # Escape double quotes for AppleScript.
    safe_title = title.replace('"', '\\"')
    safe_body = body.replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_title}" sound name "Submarine"'
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
