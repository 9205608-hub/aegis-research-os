"""AUDIT-D1/D2/D3/D4 regression tests: backend-tiered performance knobs.

D1: AGENT_MAX_PARALLEL=2 was a subprocess-CLI rate-limit guard applied to
    every backend — API backends (deepseek/grok/sdk) now get their own cap
    (default 4) so batch 1's four agents run in one wave.
D2: run_research.sh never wired the already-implemented speed switches;
    AEGIS_LLM_CACHE now defaults ON and FAST_AGENTS=1 passes --fast-agents.
D3: DeepSeek (and OpenAI-compatible wrappers like Grok) cold-start
    max_tokens was flat (32768) for every
    agent depth; llm_agent_base now passes a depth-tiered max_tokens_hint
    (light=8K / standard=16K / deep=32K) and the clients anchor their
    shrink/grow recovery ladders on it. No hint → old behavior exactly.
D4: batch/watchdog timeouts (4800s/1800s, subprocess scale) get API-tier
    values (1800s/900s) so a hung network call surfaces in minutes.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import aegis.core.config as cfg
from aegis.core.agents.base import AgentInput
from aegis.core.agents.llm_agent_base import (
    DEPTH_MAX_TOKENS_HINT,
    LLMAgentBase,
    _client_accepts_max_tokens_hint,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SH = REPO_ROOT / "run_research.sh"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _reload_config(monkeypatch, **env):
    """Reload aegis.core.config with the given env vars set.

    The module reads env at import time, so override tests must re-import.
    A finalizer reload restores the pristine module for later tests.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    module = importlib.reload(cfg)
    return module


@pytest.fixture(autouse=True)
def _restore_config():
    """Whatever a test did to env/reload, leave a clean config module."""
    yield
    importlib.reload(cfg)


# ---------------------------------------------------------------------------
# D1: max_parallel is backend-tiered
# ---------------------------------------------------------------------------

class TestAgentMaxParallel:
    def test_api_backends_default_4(self):
        for backend in ("deepseek", "grok", "sdk"):
            assert cfg.agent_max_parallel_for(backend) == 4, backend

    def test_subprocess_keeps_2(self):
        assert cfg.agent_max_parallel_for("subprocess") == 2

    def test_unknown_backend_uses_conservative_legacy_cap(self):
        assert cfg.agent_max_parallel_for("something-new") == 2

    def test_legacy_env_still_governs_api_backends(self, monkeypatch):
        # Pre-D1 semantics preserved: an operator who throttled the one
        # legacy knob keeps their throttle on every backend.
        c = _reload_config(monkeypatch, AEGIS_AGENT_MAX_PARALLEL="1")
        assert c.agent_max_parallel_for("deepseek") == 1
        assert c.agent_max_parallel_for("subprocess") == 1

    def test_api_env_wins_for_api_backends(self, monkeypatch):
        c = _reload_config(
            monkeypatch,
            AEGIS_AGENT_MAX_PARALLEL="1",
            AEGIS_AGENT_MAX_PARALLEL_API="6",
        )
        assert c.agent_max_parallel_for("deepseek") == 6
        # subprocess unaffected by the API env
        assert c.agent_max_parallel_for("subprocess") == 1


# ---------------------------------------------------------------------------
# D4: batch / watchdog timeouts are backend-tiered
# ---------------------------------------------------------------------------

class TestTimeoutTiers:
    def test_batch_timeout_defaults(self):
        assert cfg.agent_batch_timeout_for("deepseek") == 1800
        assert cfg.agent_batch_timeout_for("grok") == 1800
        assert cfg.agent_batch_timeout_for("sdk") == 1800
        assert cfg.agent_batch_timeout_for("subprocess") == 4800

    def test_watchdog_timeout_defaults(self):
        assert cfg.agent_watchdog_timeout_for("deepseek") == 900
        assert cfg.agent_watchdog_timeout_for("subprocess") == 1800

    def test_api_env_overrides(self, monkeypatch):
        c = _reload_config(
            monkeypatch,
            AEGIS_AGENT_BATCH_TIMEOUT_API_S="1200",
            AEGIS_AGENT_WATCHDOG_API_S="600",
        )
        assert c.agent_batch_timeout_for("deepseek") == 1200
        assert c.agent_watchdog_timeout_for("grok") == 600
        # subprocess tier untouched
        assert c.agent_batch_timeout_for("subprocess") == 4800

    def test_legacy_env_governs_api_when_api_env_absent(self, monkeypatch):
        c = _reload_config(monkeypatch, AEGIS_AGENT_BATCH_TIMEOUT_S="999")
        assert c.agent_batch_timeout_for("deepseek") == 999
        assert c.agent_batch_timeout_for("subprocess") == 999


# ---------------------------------------------------------------------------
# D1: orchestrator resolves backend kind without instantiating a client
# ---------------------------------------------------------------------------

class TestResolvedBackendKind:
    @pytest.fixture()
    def orch(self):
        from aegis.core.orchestrator.auto_research import AutoResearchOrchestrator
        return object.__new__(AutoResearchOrchestrator)

    @staticmethod
    def _cfg(backend="auto", deepseek_key="", grok_key=""):
        return SimpleNamespace(
            llm_backend=backend,
            deepseek_api_key=deepseek_key,
            grok_api_key=grok_key,
        )

    def test_explicit_backend_passthrough(self, orch):
        assert orch._resolved_backend_kind(self._cfg("subprocess")) == "subprocess"
        assert orch._resolved_backend_kind(self._cfg("deepseek")) == "deepseek"

    def test_auto_prefers_deepseek_key(self, orch, monkeypatch):
        for var in ("DEEPSEEK_API_KEY", "GROK_API_KEY", "XAI_API_KEY",
                    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        assert orch._resolved_backend_kind(
            self._cfg("auto", deepseek_key="sk-x")) == "deepseek"

    def test_auto_prefers_grok_when_no_deepseek(self, orch, monkeypatch):
        for var in ("DEEPSEEK_API_KEY", "GROK_API_KEY", "XAI_API_KEY",
                    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        assert orch._resolved_backend_kind(
            self._cfg("auto", grok_key="xai-x")) == "grok"

    def test_auto_falls_to_subprocess_without_keys(self, orch, monkeypatch):
        for var in ("DEEPSEEK_API_KEY", "GROK_API_KEY", "XAI_API_KEY",
                    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        assert orch._resolved_backend_kind(self._cfg("auto")) == "subprocess"

    def test_api_backend_yields_cap_4_subprocess_cap_2(self, orch):
        # End-to-end D1 assertion: resolved kind → tiered cap.
        kind_api = orch._resolved_backend_kind(self._cfg("deepseek"))
        kind_sub = orch._resolved_backend_kind(self._cfg("subprocess"))
        assert cfg.agent_max_parallel_for(kind_api) == 4
        assert cfg.agent_max_parallel_for(kind_sub) == 2


# ---------------------------------------------------------------------------
# D2: run_research.sh wiring (syntax + grep-level assertions)
# ---------------------------------------------------------------------------

class TestRunResearchShWiring:
    def test_script_syntax_ok(self):
        proc = subprocess.run(
            ["bash", "-n", str(RUN_SH)], capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr

    def test_llm_cache_defaults_on_and_overridable(self):
        src = RUN_SH.read_text(encoding="utf-8")
        # Default ON with the standard "caller env wins" idiom.
        assert 'export AEGIS_LLM_CACHE="${AEGIS_LLM_CACHE:-1}"' in src

    def test_fast_agents_switch_wired(self):
        src = RUN_SH.read_text(encoding="utf-8")
        # Env switch exists, is documented, defaults OFF, passes the flag.
        assert 'FAST_AGENTS' in src
        assert '"${FAST_AGENTS:-0}" = "1"' in src
        assert 'FAST_AGENTS_ARG="--fast-agents"' in src
        # …and the arg actually reaches the LLM invocation line.
        llm_lines = [l for l in src.splitlines() if "--llm" in l and "python" not in l]
        assert any("$FAST_AGENTS_ARG" in l for l in llm_lines), llm_lines

    def test_fast_pipeline_stays_opt_in(self):
        # --fast (DEEP→standard) must NOT be wired as a default: it trades
        # away narrative_supplement content.
        src = RUN_SH.read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "--fast " not in stripped and not stripped.endswith("--fast"), line


# ---------------------------------------------------------------------------
# D3: depth → max_tokens_hint mapping and client honoring
# ---------------------------------------------------------------------------

def _fake_openai_response(payload: dict):
    msg = SimpleNamespace(
        tool_calls=[SimpleNamespace(
            function=SimpleNamespace(name="output", arguments=json.dumps(payload)),
        )],
        content=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class TestDepthMapping:
    def test_mapping_values(self):
        assert DEPTH_MAX_TOKENS_HINT == {
            "light": 8192, "standard": 16384, "deep": 32768,
        }


class TestDeepSeekHint:
    def _client(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        from aegis.core.llm.deepseek_client import DeepSeekClient
        client = DeepSeekClient(model="deepseek-v4-pro")
        fake = _FakeCompletions(_fake_openai_response({"ok": 1}))
        client._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
        return client, fake

    def test_hint_sets_cold_start_budget(self, monkeypatch):
        client, fake = self._client(monkeypatch)
        client.call_structured("sys", "user", {"type": "object"},
                               max_tokens_hint=8192)
        assert fake.calls[0]["max_tokens"] == 8192

    def test_no_hint_keeps_historical_32768(self, monkeypatch):
        client, fake = self._client(monkeypatch)
        client.call_structured("sys", "user", {"type": "object"})
        assert fake.calls[0]["max_tokens"] == 32768


class TestGrokHint:
    def _client(self, monkeypatch):
        monkeypatch.setenv("GROK_API_KEY", "xai-test")
        from aegis.core.llm.grok_client import GrokClient
        client = GrokClient()
        fake = _FakeCompletions(_fake_openai_response({"ok": 1}))
        client._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
        return client, fake

    def test_hint_sets_cold_start_budget(self, monkeypatch):
        client, fake = self._client(monkeypatch)
        client.call_structured("sys", "user", {"type": "object"},
                               max_tokens_hint=8192)
        assert fake.calls[0]["max_tokens"] == 8192

    def test_no_hint_keeps_inherited_32768(self, monkeypatch):
        # GrokClient inherits DeepSeekClient's historical 32768 cold start.
        client, fake = self._client(monkeypatch)
        client.call_structured("sys", "user", {"type": "object"})
        assert fake.calls[0]["max_tokens"] == 32768


# ---------------------------------------------------------------------------
# D3: llm_agent_base passes the hint (and only to capable clients)
# ---------------------------------------------------------------------------

class _StubLLM:
    """Records call_structured kwargs; **kwargs → hint-capable."""

    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def call_structured(self, **kwargs):
        self.calls.append(kwargs)
        return self.raw


class _ClosedSigLLM:
    """No **kwargs, no max_tokens_hint — mirrors LLMClient's closed
    signature; must NOT be sent the hint."""

    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def call_structured(self, system_prompt, user_message, tool_schema,
                        tool_name="output", role="specialist_agent",
                        max_retries=3):
        self.calls.append({"system_prompt": system_prompt})
        return self.raw


class _HintAgent(LLMAgentBase):
    AGENT_NAME = "perf_test_agent"
    AGENT_VERSION = "0.0.1"
    SYSTEM_PROMPT = "You are a test agent."

    def __init__(self, llm):  # bypass LLMAgentBase.__init__ (no env config)
        self._llm = llm


def _minimal_raw():
    """< 4 observations so the 8/0 auto-rescue never fires."""
    return {
        "observations": [
            {"text": "营收同比增长 30%", "source_ids": ["m_revenue"]},
            {"text": "毛利率维持 45%", "source_ids": ["m_gross_margin"]},
        ],
        "inferences": [
            {"text": "增长动能可持续", "based_on_observation_indices": [0], "confidence": "high"},
            {"text": "盈利质量稳定", "based_on_observation_indices": [1], "confidence": "medium"},
        ],
        "counterarguments": [
            {"text": "行业景气度可能见顶", "strength": "moderate", "evidence_ids": []},
        ],
        "disconfirming_triggers": [{"text": "增速跌破 10%"}],
        "cognitive_bias_self_check": {
            "anchoring_risk": "low",
            "confirmation_bias_risk": "medium",
            "recency_bias_risk": "medium",
            "narrative_fallacy_risk": "low",
            "mitigation_steps_taken": ["复核了反面证据"],
        },
        "self_reported_uncertainties": ["宏观需求不确定"],
    }


class TestAgentHintPassthrough:
    def test_default_depth_is_standard_16384(self):
        agent = _HintAgent(_StubLLM(_minimal_raw()))
        agent.run(AgentInput(entity_id="e", run_id="r", question_id="q"))
        assert agent._llm.calls[0]["max_tokens_hint"] == 16384

    def test_deep_directive_gets_32768(self):
        agent = _HintAgent(_StubLLM(_minimal_raw()))
        inp = AgentInput(
            entity_id="e", run_id="r", question_id="q",
            macro_context={"research_directive": {"_depth": "deep"}},
        )
        agent.run(inp)
        assert agent._llm.calls[0]["max_tokens_hint"] == 32768

    def test_light_directive_gets_8192(self):
        agent = _HintAgent(_StubLLM(_minimal_raw()))
        inp = AgentInput(
            entity_id="e", run_id="r", question_id="q",
            macro_context={"research_directive": {"_depth": "light"}},
        )
        agent.run(inp)
        assert agent._llm.calls[0]["max_tokens_hint"] == 8192

    def test_closed_signature_client_not_hinted_and_not_crashed(self):
        # Passing the hint to LLMClient's closed signature would TypeError
        # → mock fallback. The capability gate must skip it instead.
        agent = _HintAgent(_ClosedSigLLM(_minimal_raw()))
        out = agent.run(AgentInput(entity_id="e", run_id="r", question_id="q"))
        assert not out.is_llm_fallback
        assert len(agent._llm.calls) == 1


class TestHintCapabilityCheck:
    def test_kwargs_client_accepted(self):
        assert _client_accepts_max_tokens_hint(_StubLLM({}))

    def test_closed_signature_rejected(self):
        assert not _client_accepts_max_tokens_hint(_ClosedSigLLM({}))

    def test_real_llm_client_rejected(self):
        # LLMClient (LIVE mode) is the one closed-signature client in tree.
        from aegis.core.llm.client import LLMClient
        assert not _client_accepts_max_tokens_hint(
            SimpleNamespace(call_structured=LLMClient.call_structured),
        )

    def test_deepseek_and_grok_accepted(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("GROK_API_KEY", "xai-test")
        from aegis.core.llm.deepseek_client import DeepSeekClient
        from aegis.core.llm.grok_client import GrokClient
        assert _client_accepts_max_tokens_hint(DeepSeekClient())
        assert _client_accepts_max_tokens_hint(GrokClient())
