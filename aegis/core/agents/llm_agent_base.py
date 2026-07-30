"""LLM-powered Agent Base — Section 19 + 28.2.

Inherits from AgentBase. Replaces rule-based abstract methods with
LLM calls via tool_use structured output.

Pipeline:
  1. Build prompt from AgentInput + prompt bundle
  2. Call LLM (mock or live) with JudgmentContract tool schema
  3. Parse LLM output into Observation/Inference/Counterargument
  4. Run AgentBase._validate_constraints (unchanged)
  5. Return AgentOutput
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from aegis.core.agents.base import AgentBase, AgentInput, AgentOutput
from aegis.core.llm.config import LLMConfig, LLMMode
from aegis.core.llm.client import LLMClient
from aegis.core.llm.mock_client import MockLLMClient
from aegis.data_contracts.judgment_schema import (
    CognitiveBiasSelfCheck,
    Counterargument,
    DisconfirmingTrigger,
    FollowUpQuestion,
    Inference,
    JudgmentContract,
    Observation,
)


# Tool schema for JudgmentContract output
JUDGMENT_TOOL_SCHEMA = {
    "type": "object",
    "required": ["observations", "inferences", "counterarguments",
                 "disconfirming_triggers", "cognitive_bias_self_check"],
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "source_ids"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "source_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
            },
            "minItems": 1,
        },
        "inferences": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "based_on_observation_indices", "confidence"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "based_on_observation_indices": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
            },
            # TODO-X2: DeepSeek V4 (auto tool_choice) intermittently returns
            # observations[] populated but inferences[] empty. minItems=2 plus
            # explicit prompt directive (see _build_judgment_directive) reduces
            # but cannot eliminate this — we still gate-check + retry in the
            # orchestrator's first-pass quality gate.
            "minItems": 2,
        },
        "counterarguments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "strength"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "strength": {"type": "string", "enum": ["weak", "moderate", "strong"]},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}, "default": []},
                },
            },
            "minItems": 1,
        },
        "disconfirming_triggers": {
            "type": "array",
            "description": (
                "FALSIFICATION direction ONLY: observable conditions that, if "
                "they occur, WEAKEN or REFUTE your judgment (证伪方向——出现即"
                "削弱/推翻你的结论). NEVER list confirmation signals here (events "
                "that would prove the thesis right belong in inferences). Each "
                "trigger should name ONE observable with a quantified threshold "
                "where possible (e.g. '单季营收增速低于80%', not '增长放缓')."
            ),
            "items": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "monitorable": {"type": "boolean", "default": True},
                    "check_frequency": {"type": "string", "default": "quarterly"},
                },
            },
        },
        "self_reported_uncertainties": {
            "type": "array",
            "items": {"type": "string"},
        },
        "cognitive_bias_self_check": {
            "type": "object",
            "required": ["anchoring_risk", "confirmation_bias_risk",
                         "recency_bias_risk", "narrative_fallacy_risk"],
            "properties": {
                "anchoring_risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "confirmation_bias_risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "recency_bias_risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "narrative_fallacy_risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "mitigation_steps_taken": {"type": "array", "items": {"type": "string"}},
            },
        },
        "follow_up_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question", "data_type", "data_key", "priority"],
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "A specific question you need answered to strengthen your analysis. e.g. 'What is the gross margin breakdown by product segment?'",
                    },
                    "data_type": {
                        "type": "string",
                        "enum": ["metric", "segment", "time_series", "fact"],
                        "description": "What kind of data would answer this: 'metric' (a computed ratio), 'segment' (segment-level breakdown), 'time_series' (historical trend), 'fact' (a specific financial fact).",
                    },
                    "data_key": {
                        "type": "string",
                        "description": "The specific data key to look up. Use snake_case. e.g. 'gross_margin_by_segment', 'capex_to_revenue', 'revenue_by_geography'.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "'high' = this materially affects my judgment and I want to re-analyze if the data exists. 'medium' = useful but won't change my conclusion. 'low' = nice to have.",
                    },
                },
            },
            "maxItems": 3,
            "description": "Up to 3 questions you need answered to refine your judgment. Only ask if the data would MATERIALLY change your analysis. The system will check if answers exist in available data and may re-run you with supplemental data.",
        },
    },
}

# Deep mode schema — includes narrative_supplement for free-form analysis
JUDGMENT_TOOL_SCHEMA_DEEP = {
    **JUDGMENT_TOOL_SCHEMA,
    "properties": {
        **JUDGMENT_TOOL_SCHEMA["properties"],
        "narrative_supplement": {
            "type": "string",
            "description": (
                "DEEP MODE ONLY: A free-form analytical narrative (300-800 words) that goes BEYOND "
                "the structured observations/inferences. This is where you can:\n"
                "- Explore a nuance that doesn't fit neatly into an observation\n"
                "- Develop a chain of reasoning that connects multiple data points\n"
                "- Describe a pattern you see that's hard to capture in bullet points\n"
                "- Flag something that 'doesn't feel right' even if you can't pin it to a metric\n"
                "- Compare to analogous situations you know about\n"
                "Write like a senior analyst's internal memo — honest, specific, opinionated."
            ),
        },
    },
}


# TODO-6 (2026-05-05): two-step prompt schemas. The single-call path can
# burn 8-15 min on V4-pro because the model has to think AND emit a long
# multi-array JSON in one shot. Splitting into:
#   STEP 1 — OBSERVATIONS_ONLY_SCHEMA: only observations[]. Smaller output,
#            less thinking budget, completes faster.
#   STEP 2 — INFERENCES_FROM_OBSERVATIONS_SCHEMA: inferences[],
#            counterarguments[], disconfirming_triggers[],
#            cognitive_bias_self_check, self_reported_uncertainties,
#            follow_up_questions[], (optional narrative_supplement) — but
#            takes the step-1 observations as input context, so the model
#            doesn't re-derive them and can focus its reasoning budget on
#            the analytical leap.
# The two outputs are stitched into one judgment dict matching the original
# JUDGMENT_TOOL_SCHEMA before the existing parsing logic runs.
OBSERVATIONS_ONLY_SCHEMA = {
    "type": "object",
    "required": ["observations"],
    "properties": {
        "observations": JUDGMENT_TOOL_SCHEMA["properties"]["observations"],
    },
}

INFERENCES_FROM_OBSERVATIONS_SCHEMA = {
    "type": "object",
    "required": ["inferences", "counterarguments",
                 "disconfirming_triggers", "cognitive_bias_self_check"],
    "properties": {
        k: v for k, v in JUDGMENT_TOOL_SCHEMA["properties"].items()
        if k != "observations"
    },
}

INFERENCES_FROM_OBSERVATIONS_SCHEMA_DEEP = {
    **INFERENCES_FROM_OBSERVATIONS_SCHEMA,
    "properties": {
        **INFERENCES_FROM_OBSERVATIONS_SCHEMA["properties"],
        "narrative_supplement": JUDGMENT_TOOL_SCHEMA_DEEP["properties"]["narrative_supplement"],
    },
}


# AUDIT-D3: starting output-token budget by Director depth tier, passed to
# the LLM client as `max_tokens_hint`. DeepSeek/Grok honour it (their
# shrink/grow recovery ladders re-anchor on it); other clients ignore it via
# **kwargs. Bigger budgets make reasoning models think longer (BUG-A20), so
# light/standard calls should not pay for deep-tier headroom.
DEPTH_MAX_TOKENS_HINT: dict[str, int] = {
    "light": 8192,
    "standard": 16384,
    "deep": 32768,
}


def _client_accepts_max_tokens_hint(client: Any) -> bool:
    """AUDIT-D3: only pass max_tokens_hint to clients whose call_structured
    can absorb it (explicit param or **kwargs). LLMClient (LIVE mode) has a
    closed signature — passing the hint there would TypeError and dump an
    otherwise-healthy call into the mock-fallback path."""
    import inspect
    try:
        sig = inspect.signature(client.call_structured)
    except (TypeError, ValueError):
        return False
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD or p.name == "max_tokens_hint"
        for p in sig.parameters.values()
    )


def _strip_sensitive(text: str) -> str:
    """Remove or soften phrases that commonly trip LLM API content filters
    (backend-agnostic defense; originally added for a CN-hosted backend).
    Keeps financial substance, removes geopolitical/regulatory framings.

    AUDIT-B6 (2026-07-09): the original 10 rules were EN-only with ``\\b``
    boundaries. Python ``re`` counts CJK as ``\\w``, so '与Huawei' / '受Taiwan'
    have no word boundary and never matched — the A-share content-filter
    retry path was a no-op (3 tested CJK sentences all came back UNCHANGED).
    Fix: English terms use ``(?<![A-Za-z])…(?![A-Za-z])`` letter-boundary
    lookarounds (CJK-adjacency safe); Chinese pairs match as plain substrings
    (no ``\\b`` at all). Promoted from a ``run()`` closure to module level
    for direct unit-testability (AUDIT-E1).
    """
    import re
    replacements = [
        # English terms — letter-boundary lookarounds instead of \b so a
        # brand name embedded in a Chinese sentence still matches.
        (r"(?<![A-Za-z])[Ee]xport control[s]?(?![A-Za-z])", "trade restrictions"),
        (r"(?<![A-Za-z])[Ss]anction[s]?(?![A-Za-z])", "trade restrictions"),
        (r"(?<![A-Za-z])[Hh]uawei(?![A-Za-z])", "a regional competitor"),
        (r"(?<![A-Za-z])[Tt]aiwan(?![A-Za-z])(?!\s*Semiconductor)", "the region"),
        (r"(?<![A-Za-z])CCP(?![A-Za-z])", "the government"),
        (r"(?<![A-Za-z])Chinese Communist Party(?![A-Za-z])", "the government"),
        (r"(?<![A-Za-z])Chinese military(?![A-Za-z])", "state entities"),
        (r"(?<![A-Za-z])[Mm]ilitary use(?![A-Za-z])", "restricted use"),
        (r"(?<![A-Za-z])BIS(?![A-Za-z])", "regulators"),
        (r"(?<![A-Za-z])Entity List(?![A-Za-z])", "restricted-party list"),
        # 中文敏感词 — 直接子串匹配（\b 对 CJK 无效，不可用）。仅影响
        # content-filter 重试时发给 LLM 的 prompt，不进报告正文。
        (r"出口管制", "贸易限制"),
        (r"制裁", "贸易限制"),
        (r"华为", "某区域竞争对手"),
        (r"台湾(?!积体)", "该地区"),  # 负向前瞻避开"台湾积体电路"(台积电全称)
        (r"实体清单", "受限名单"),
        (r"军用|军工|军方", "受限用途"),
    ]
    for pat, repl in replacements:
        text = re.sub(pat, repl, text)
    return text


def _is_content_filter_error(e: Exception) -> bool:
    """Classify an LLM exception as a content-filter rejection.

    AUDIT-B7 (2026-07-09): the old check (``"400" in msg[:30]``) marked ANY
    400 — schema-invalid, oversized request body — as content_filter, which
    burned a pointless strip-retry and mislabeled ``_failure_reason`` as
    'content_filter+…' (metadata lying to the operator, bug type #6). Now:
    the typed DeepSeekContentFilterError (also raised by GrokClient, which
    inherits from DeepSeekClient) and semantic keywords are authoritative;
    a bare 400 only counts when the body actually mentions content/risk/filter.
    """
    try:
        from aegis.core.llm.deepseek_client import DeepSeekContentFilterError
        if isinstance(e, DeepSeekContentFilterError):
            return True
    except ImportError:
        pass
    msg = str(e).lower()
    if "content_filter" in msg or "high risk" in msg or "contentpolicy" in msg or "content policy" in msg:
        return True
    if "400" in msg[:30]:
        return any(k in msg for k in ("content", "risk", "filter"))
    return False


class LLMAgentBase(AgentBase):
    """Base class for LLM-powered agents.

    Subclasses set AGENT_NAME, AGENT_VERSION, SYSTEM_PROMPT.
    The LLM does the analytical work; framework enforces constraints.
    """

    SYSTEM_PROMPT: str = ""  # Override per agent

    def __init__(self, llm_config: LLMConfig | None = None) -> None:
        self._config = llm_config or LLMConfig.from_env()
        if self._config.mode == LLMMode.LIVE:
            self._llm = LLMClient(self._config)
        elif self._config.mode == LLMMode.SUBPROCESS:
            from aegis.core.llm.subprocess_client import SubprocessLLMClient
            self._llm = SubprocessLLMClient(model="sonnet")
        else:
            self._llm = MockLLMClient()

    def run(self, agent_input: AgentInput) -> AgentOutput:
        """Override run to use LLM pipeline with fallback."""
        # Build user message from input
        user_message = self._build_user_message(agent_input)

        # Determine if this is a deep-mode run (uses richer schema with narrative_supplement)
        rd = agent_input.macro_context.get("research_directive") if agent_input.macro_context else None
        is_deep = rd and rd.get("_depth") == "deep"
        schema = JUDGMENT_TOOL_SCHEMA_DEEP if is_deep else JUDGMENT_TOOL_SCHEMA

        # AUDIT-D3: depth → starting output-token budget for every structured
        # call this run makes (initial, strip-retry, split steps, rescue).
        # Kwargs-gated so clients with a closed call_structured signature
        # (LLMClient) never see the extra keyword.
        _depth = rd.get("_depth", "standard") if rd else "standard"
        _hint_kwargs: dict[str, int] = {}
        if _client_accepts_max_tokens_hint(self._llm):
            _hint_kwargs = {
                "max_tokens_hint": DEPTH_MAX_TOKENS_HINT.get(
                    _depth, DEPTH_MAX_TOKENS_HINT["standard"],
                ),
            }

        # Call LLM with tiered fallback on failure:
        # 1. primary LLM (DeepSeek by default; Grok as alternate backend)
        # 2. if content_filter: strip geopolitically-sensitive phrases, retry primary
        # 3. last resort: mock fallback (produces thin output, flag as degraded)
        # AUDIT-B6/B7 (2026-07-09): _strip_sensitive / _is_content_filter_error
        # promoted to module level (CJK-aware stripping + tightened 400
        # classification); see definitions above the class.
        import sys
        raw = None
        system_prompt = self._build_system_prompt(agent_input)
        # TODO-2 (2026-04-24): include reason/attempts/elapsed in fallback log
        # so diagnosing why an agent went mock doesn't require digging through
        # process-level logs. subprocess_client.RuntimeError already embeds
        # `kind=... attempts=N/3 elapsed=Xs` so we just propagate the str(e).
        _failure_reason: str | None = None
        # Refactor 5 (2026-05-04): track LLM-fallback state outside the
        # try/except so the success path keeps the default (False) and
        # only the mock-fallback branch flips it to True.
        _llm_fallback_active = False
        _llm_fallback_reason = ""
        # TODO-2 (2026-05-05): track attempts + wall-clock for failure logs.
        # Previously the fallback line said only `(reason: …)`; operators
        # could not tell whether we hit content-filter once and gave up vs
        # retried twice on JSON-parse and burned 90s. With these we get
        # `(reason: …; attempts=2/2; elapsed=42s)` which is enough signal
        # to triage without enabling client-side request logs.
        import time as _time_mod
        _llm_t0 = _time_mod.monotonic()
        _llm_attempts = 0
        # TODO-6: 2-step path when macro_context["split_prompts"] is set.
        # Step 1 emits only observations[]; step 2 receives those observations
        # as context and emits everything else. Resulting `raw` matches the
        # single-call schema so all downstream parsing is unchanged.
        _split_prompts = bool(
            agent_input.macro_context
            and agent_input.macro_context.get("split_prompts")
        )
        try:
            if _split_prompts:
                _llm_attempts = 1
                raw = self._call_split(
                    system_prompt, user_message, is_deep, _hint_kwargs,
                )
            else:
                _llm_attempts = 1
                raw = self._llm.call_structured(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    tool_schema=schema,
                    tool_name="judgment",
                    role=self.AGENT_NAME,
                    **_hint_kwargs,
                )
        except Exception as e:
            if _is_content_filter_error(e):
                # Retry with stripped prompt
                print(
                    f"  ⚠ {self.AGENT_NAME} content_filter hit, retrying with stripped prompt",
                    file=sys.stderr,
                )
                try:
                    _llm_attempts = 2
                    raw = self._llm.call_structured(
                        system_prompt=_strip_sensitive(system_prompt),
                        user_message=_strip_sensitive(user_message),
                        tool_schema=schema,
                        tool_name="judgment",
                        role=self.AGENT_NAME,
                        **_hint_kwargs,
                    )
                except Exception as e2:
                    _failure_reason = f"content_filter+strip_retry_failed: {e2!s}"
                    raw = None
            else:
                _failure_reason = str(e)
                raw = None

            if raw is None:
                # TODO-5 (2026-04-24): in --strict-llm mode, abort instead
                # of silently producing mock-templated output that pollutes
                # the report. Re-raise the original failure so the pipeline
                # exits non-zero with a clear cause.
                _strict = bool(
                    agent_input.macro_context
                    and agent_input.macro_context.get("strict_llm")
                )
                if _strict:
                    raise RuntimeError(
                        f"{self.AGENT_NAME}: LLM exhausted in --strict-llm mode "
                        f"(reason: {(_failure_reason or 'unknown')[:240]})"
                    )
                # BUG-A16 (2026-05-04): mirror to stdout so live pipeline logs
                # surface the fallback. stderr-only made silent mock fallbacks
                # easy to miss — orchestrator's "first pass too thin (3/1)"
                # pipeline log line was misleading because the 3/1 was
                # already mock content, not real LLM output. Now both
                # streams carry the warning.
                _elapsed_s = _time_mod.monotonic() - _llm_t0
                _msg = (
                    f"  ⚠ {self.AGENT_NAME} all LLM paths exhausted, falling back to mock "
                    f"(reason: {(_failure_reason or 'unknown')[:240]}; "
                    f"attempts={_llm_attempts}/2; elapsed={_elapsed_s:.0f}s)"
                )
                print(_msg, file=sys.stderr)
                print(_msg, flush=True)
                fallback = MockLLMClient()
                # BUG-34 (2026-04-23): pass language hint so the mock fallback
                # honours CLAUDE.md's 中文化铁律 — A-share reports must be all
                # Chinese, even when an agent's LLM call dies and we fall
                # through to templated text.
                _is_zh = bool(
                    agent_input.macro_context
                    and agent_input.macro_context.get("language") == "zh-CN"
                )
                raw = fallback.call_structured(
                    system_prompt="", user_message="",
                    tool_schema=JUDGMENT_TOOL_SCHEMA,
                    role=self.AGENT_NAME,
                    fallback_reason=(_failure_reason or ""),
                    language="zh-CN" if _is_zh else "en",
                )
                # Refactor 5: stamp the failure on the AgentOutput so
                # downstream consumers (renderers, gates, replay) can
                # recognize this card as fallback content rather than
                # parsing the mock text for "[规则模板兜底]" prefixes.
                _llm_fallback_active = True
                _llm_fallback_reason = (_failure_reason or "unknown")[:240]

        # BUG-A24 (2026-05-06): 8/0 first-pass auto-rescue.
        # Run #3 + earlier runs showed a recurring V4 pattern: model emits
        # observations[] in full (8 items, real content) then truncates the
        # inferences[] array (mid-stream truncation lands BETWEEN the two
        # arrays). `_repair_truncated_array` recovers observations cleanly
        # but leaves inferences=[]. Quality gate then triggers a full agent
        # retry — wasteful, since observations are already good. Instead,
        # do a SHORT inferences-only call passing the observations as input.
        # ~1-2 min vs ~5-10 min full retry.
        #
        # Skipped when: raw is mock fallback (no real obs to work from),
        # observations < 4 (orchestrator gate would retry anyway), or
        # inferences are already populated.
        if (not _llm_fallback_active
                and len(raw.get("observations") or []) >= 4
                and not (raw.get("inferences") or [])):
            try:
                _llm_attempts += 1
                _inf_t0 = _time_mod.monotonic()
                _is_zh = bool(
                    agent_input.macro_context
                    and agent_input.macro_context.get("language") == "zh-CN"
                )
                rescue_inf_schema = (
                    INFERENCES_FROM_OBSERVATIONS_SCHEMA_DEEP
                    if is_deep else INFERENCES_FROM_OBSERVATIONS_SCHEMA
                )
                obs_json = json.dumps(
                    {"observations": raw["observations"]},
                    ensure_ascii=False, indent=2,
                )
                rescue_msg = (
                    user_message
                    + "\n\n=== INFERENCES-ONLY RESCUE PASS ===\n"
                    + "Your prior call emitted these observations but the "
                    + "inferences[] array came back empty (likely truncated). "
                    + "These observations are FROZEN — reference them by index "
                    + "in based_on_observation_indices.\n\n"
                    + obs_json
                    + "\n\nNow produce ONLY: inferences[], counterarguments[], "
                    + "disconfirming_triggers[], cognitive_bias_self_check, "
                    + "self_reported_uncertainties[]"
                    + (", narrative_supplement (300-800 words)" if is_deep else "")
                    + ". Be specific and ground every inference in the indexed "
                    + "observations above."
                )
                rescue = self._llm.call_structured(
                    system_prompt=system_prompt,
                    user_message=rescue_msg,
                    tool_schema=rescue_inf_schema,
                    tool_name="inferences_rescue",
                    role=self.AGENT_NAME,
                    **_hint_kwargs,
                )
                # Merge: observations from first pass, everything else from
                # rescue. Keep first pass's follow_up_questions if it had any.
                merged = dict(rescue)
                merged["observations"] = raw["observations"]
                if raw.get("follow_up_questions"):
                    merged.setdefault("follow_up_questions", raw["follow_up_questions"])
                raw = merged
                _inf_dt = _time_mod.monotonic() - _inf_t0
                print(
                    f"  ⚠ {self.AGENT_NAME} 8/0 first-pass auto-rescued via "
                    f"inferences-only call ({_inf_dt:.0f}s)"
                )
            except Exception as _re:
                # Silent — the orchestrator's quality gate retry will pick up
                # the original 8/0. We tried, no harm done.
                _rescue_dt = _time_mod.monotonic() - _inf_t0
                print(
                    f"  ⚠ {self.AGENT_NAME} 8/0 inferences-only rescue failed "
                    f"({_rescue_dt:.0f}s, {type(_re).__name__}); falling back to "
                    f"orchestrator retry path",
                    file=sys.stderr,
                )

        # Parse LLM output into typed objects
        # Strip extra fields that LLMs (especially reasoning backends) add beyond the schema
        def _strip_extra(data: dict, model_cls: type) -> dict:
            allowed = set(model_cls.model_fields.keys())
            return {k: v for k, v in data.items() if k in allowed}

        # BUG-Y24/Y27 (2026-05-06): LLMs occasionally emit compound bucket
        # values like `medium_high` / `high_medium` / `mediumlow` that don't
        # match the strict `^(low|medium|high)$` Pydantic patterns
        # used on Inference.confidence and on all 4 CognitiveBiasSelfCheck
        # risk fields. Without normalization the entire agent falls back to
        # mock — losing the substantive output.
        # AUDIT-E1/B4 (2026-07-09): the low/med/high normalizer moved to
        # aegis.core._coerce (shared + unit-testable); normalize_strength is
        # the same treatment for Counterargument.strength, whose
        # weak|moderate|strong enum LLMs mix up with low|medium|high.
        #
        # BUG-Y26 (2026-05-06): coerce list-typed fields at the parse
        # boundary so a JSON-encoded-string list doesn't get char-iterated.
        # Without this, an `observations` field returned as a string would
        # produce one Observation per character (and every Observation
        # would be a single-char string, which then fails Observation's
        # `text: min_length=1` and aborts the whole agent).
        from aegis.core._coerce import (
            coerce_dict,
            coerce_list,
            normalize_low_med_high as _normalize_low_med_high,
            normalize_strength as _normalize_strength,
        )

        def _coerce_str_list(val: Any) -> list[str]:
            # AUDIT-B5 (2026-07-09): nested list[str] fields (Observation.
            # source_ids, Counterargument.evidence_ids, uncertainties,
            # mitigation steps) share BUG-Y25/Y26's string-for-list quirk one
            # level deeper — 'm_revenue' instead of ['m_revenue'] used to
            # ValidationError the whole agent. coerce_list + stringify
            # scalars, drop nested containers.
            return [str(x) for x in coerce_list(val) if isinstance(x, (str, int, float))]

        def _log_dropped(kind: str, err: Exception) -> None:
            # AUDIT-B5: per-item drop log — a single malformed element is
            # discarded instead of letting the ValidationError escape run()
            # (the orchestrator's broad except would silently swap the WHOLE
            # agent for the rule-based template, skipping the quality-gate
            # LLM retry).
            print(
                f"  ⚠ {self.AGENT_NAME} dropped malformed {kind}: "
                f"{type(err).__name__}: {str(err)[:160]}",
                file=sys.stderr,
            )

        # Skip non-dict elements (the JSON-string-as-list-of-strings case
        # produces single strings, which would fail Observation/Inference
        # construction — better to drop than crash).
        _obs_raw = [o for o in coerce_list(raw.get("observations", [])) if isinstance(o, dict)]
        observations: list[Observation] = []
        for o in _obs_raw:
            _o = _strip_extra(o, Observation)
            _o["source_ids"] = _coerce_str_list(_o.get("source_ids"))
            try:
                observations.append(Observation(**_o))
            except Exception as _bad:  # AUDIT-B5: drop item, keep the agent
                _log_dropped("observation", _bad)
        obs_count = len(observations)

        def _coerce_inference(data: dict) -> dict | None:
            """Coerce LLM-quirk inference data to match schema types.

            - Scalar indices → list (AUDIT-B5: `"based_on_observation_indices": 2`
              used to raise `TypeError: 'int' object is not iterable`)
            - Strings → ints (LLMs sometimes return "3" instead of 3)
            - Out-of-range indices clamped to valid bounds:
              · idx == obs_count (1-indexed LLM) → obs_count - 1
              · idx > obs_count (hallucinated) → dropped
              · idx < 0 → dropped
            - Empty/missing result defaults to [0] (so logic_critic doesn't
              block on "no grounding") only when there's at least one
              observation; with zero observations the inference is dropped
              (return None) instead of failing min_length=1 (AUDIT-B5).

            Without this clamping, an LLM quirk of 1-indexed references
            produces 3+ false-positive LOGIC_UNGROUNDED_INFERENCE blocks per
            run, eating the cumulative critic block budget and forcing
            otherwise-good reports into 'downgraded' status.
            """
            cleaned = _strip_extra(data, Inference)
            indices = coerce_list(cleaned.get("based_on_observation_indices"))
            coerced: list[int] = []
            for idx in indices:
                if isinstance(idx, int):
                    v = idx
                elif isinstance(idx, str) and idx.lstrip("-").isdigit():
                    v = int(idx)
                else:
                    continue  # Skip non-integer values
                if obs_count == 0:
                    continue
                if 0 <= v < obs_count:
                    coerced.append(v)
                elif v == obs_count:
                    # Off-by-one: LLM is 1-indexed, clamp to last obs
                    coerced.append(obs_count - 1)
                # else: out-of-range hallucination, drop silently
            # Dedupe while preserving order
            seen: set[int] = set()
            deduped = [x for x in coerced if not (x in seen or seen.add(x))]
            if not deduped:
                if obs_count > 0:
                    deduped = [0]
                else:
                    # AUDIT-B5: nothing to ground on — an empty list would
                    # fail Inference's min_length=1; drop this inference.
                    return None
            cleaned["based_on_observation_indices"] = deduped
            # BUG-Y24 (2026-05-06): LLM occasionally emits compound
            # confidence values like `medium_high` / `high_medium` /
            # `mediumlow` (DeepSeek V4 was the culprit, but seen on
            # other backends too). Pydantic strict pattern `^(low|medium|high)$`
            # rejects them → whole agent falls back to mock. Coerce to
            # the closest pattern-valid bucket so the LLM's substantive
            # output (text + indices) survives. Missing confidence lands
            # on the "medium" default instead of a ValidationError.
            cleaned["confidence"] = _normalize_low_med_high(cleaned.get("confidence", "medium"))
            return cleaned

        # BUG-Y26: same coercion for the rest of the list-typed fields.
        # AUDIT-B5: construction is per-item try/except now — one malformed
        # element gets dropped instead of discarding the agent's entire
        # real LLM output.
        _inf_raw = [i for i in coerce_list(raw.get("inferences", [])) if isinstance(i, dict)]
        _ca_raw = [c for c in coerce_list(raw.get("counterarguments", [])) if isinstance(c, dict)]
        _dc_raw = [d for d in coerce_list(raw.get("disconfirming_triggers", [])) if isinstance(d, dict)]
        inferences: list[Inference] = []
        for i in _inf_raw:
            _ci = _coerce_inference(i)
            if _ci is None:
                continue  # AUDIT-B5: ungroundable (obs_count == 0)
            try:
                inferences.append(Inference(**_ci))
            except Exception as _bad:
                _log_dropped("inference", _bad)
        counterarguments: list[Counterargument] = []
        for c in _ca_raw:
            _cc = _strip_extra(c, Counterargument)
            # AUDIT-B4 (2026-07-09): normalize strength — schema pattern is
            # ^(weak|moderate|strong)$ but LLMs mix it up with the sibling
            # low|medium|high confidence enum ("medium"/"very strong"/"STRONG"
            # all ValidationError'd the whole agent back to rule-based).
            _cc["strength"] = _normalize_strength(_cc.get("strength"))
            _cc["evidence_ids"] = _coerce_str_list(_cc.get("evidence_ids"))
            try:
                counterarguments.append(Counterargument(**_cc))
            except Exception as _bad:
                _log_dropped("counterargument", _bad)
        disconfirming: list[DisconfirmingTrigger] = []
        for d in _dc_raw:
            try:
                disconfirming.append(DisconfirmingTrigger(**_strip_extra(d, DisconfirmingTrigger)))
            except Exception as _bad:
                _log_dropped("disconfirming_trigger", _bad)
        # AUDIT-B5: JudgmentContract wants list[str] — stringify scalars so a
        # stray int doesn't fail the whole contract.
        uncertainties = _coerce_str_list(raw.get("self_reported_uncertainties", []))

        # BUG-Y27: same `medium_high` failure mode applies to all 4 bias
        # risk fields. Normalize at boundary so a single LLM-side compound
        # value doesn't collapse the whole agent.
        # AUDIT-B5 (2026-07-09): LLMs occasionally serialize the whole bias
        # object as a JSON STRING — calling .get() on it raised
        # AttributeError. BUG-Y25 dict 版（2026-07-13）：原内联 json.loads
        # 救援换成共享 coerce_dict（行为不变：救得回就 parse，救不回落 {}，
        # 所有字段回安全默认值）。
        bias_data = coerce_dict(raw.get("cognitive_bias_self_check", {}))
        bias_check = CognitiveBiasSelfCheck(
            anchoring_risk=_normalize_low_med_high(bias_data.get("anchoring_risk", "medium")),
            confirmation_bias_risk=_normalize_low_med_high(bias_data.get("confirmation_bias_risk", "medium")),
            recency_bias_risk=_normalize_low_med_high(bias_data.get("recency_bias_risk", "medium")),
            narrative_fallacy_risk=_normalize_low_med_high(bias_data.get("narrative_fallacy_risk", "medium")),
            mitigation_steps_taken=_coerce_str_list(bias_data.get("mitigation_steps_taken", [])),
        )

        # Parse follow-up questions (BUG-Y26: harden list boundary;
        # BUG-Y27: normalize priority via low/medium/high helper).
        follow_ups = []
        for fq in coerce_list(raw.get("follow_up_questions", [])):
            if not isinstance(fq, dict):
                continue
            _data_type = str(fq.get("data_type", "fact") or "fact").strip().lower()
            if _data_type not in ("metric", "segment", "time_series", "fact"):
                _data_type = "fact"
            try:
                follow_ups.append(FollowUpQuestion(
                    question=fq.get("question", ""),
                    data_type=_data_type,
                    data_key=fq.get("data_key", ""),
                    priority=_normalize_low_med_high(fq.get("priority", "medium")),
                ))
            except Exception:
                pass  # Skip malformed follow-up questions

        # Build JudgmentContract
        judgment = JudgmentContract(
            judgment_id=f"j_{self.AGENT_NAME}_{uuid4().hex[:8]}",
            agent_name=self.AGENT_NAME,
            agent_version=self.AGENT_VERSION,
            question_id=agent_input.question_id,
            run_id=agent_input.run_id,
            depends_on_judgment_ids=[j.judgment_id for j in agent_input.prior_judgments],
            observations=observations,
            inferences=inferences,
            counterarguments=counterarguments,
            disconfirming_triggers=disconfirming,
            used_metric_ids=list(agent_input.metric_results.keys()),
            used_evidence_ids=[ep.get("evidence_id", "") for ep in agent_input.evidence_packets if ep.get("evidence_id")],
            used_relationship_ids=[r.get("relationship_id", "") for r in agent_input.entity_relationships if r.get("relationship_id")],
            self_reported_uncertainties=uncertainties,
            cognitive_bias_self_check=bias_check,
            sector_context_applied=agent_input.sector_pack.get("sector_pack_id") if agent_input.sector_pack else None,
            judgment_status="complete",
            follow_up_questions=follow_ups,
        )

        # Validate using parent's constraint checker
        errors = self._validate_constraints(judgment, agent_input)

        # Extract narrative_supplement for deep mode
        narrative = raw.get("narrative_supplement", "") if is_deep else ""

        return AgentOutput(
            judgment=judgment,
            validation_passed=len(errors) == 0,
            validation_errors=errors,
            narrative_supplement=narrative,
            is_llm_fallback=_llm_fallback_active,
            llm_fallback_reason=_llm_fallback_reason,
        )

    def _call_split(
        self, system_prompt: str, user_message: str, is_deep: bool,
        hint_kwargs: dict[str, int] | None = None,
    ) -> dict:
        """TODO-6 two-step LLM call: observations first, then everything else.

        AUDIT-D3: `hint_kwargs` carries the caller's depth-tiered
        `max_tokens_hint` (or is empty for clients that can't absorb it) and
        is forwarded to both steps.

        Step 1: tight schema (observations only) → faster output, less
        thinking budget consumed by formatting.
        Step 2: receives the step-1 observations as JSON in the user message
        and is told the analytical leap is its job — produce inferences,
        counterarguments, disconfirming triggers, bias self-check, optional
        follow-up questions and (deep mode only) narrative supplement.

        Output is stitched into a single dict matching the original
        JUDGMENT_TOOL_SCHEMA(_DEEP), so the existing parser path is reused.
        """
        # Step 1: observations only.
        step1 = self._llm.call_structured(
            system_prompt=system_prompt,
            user_message=user_message + (
                "\n\n=== STEP 1 OF 2: OBSERVATIONS ONLY ===\n"
                "Produce ONLY the observations[] array right now. Do NOT "
                "produce inferences, counterarguments, triggers, or the bias "
                "self-check yet — those come in step 2 with the observations "
                "you generate here as context."
            ),
            tool_schema=OBSERVATIONS_ONLY_SCHEMA,
            tool_name="observations_only",
            role=self.AGENT_NAME,
            **(hint_kwargs or {}),
        )
        observations = step1.get("observations") or []

        # Step 2: inferences + the rest, conditioned on step-1 observations.
        step2_schema = (
            INFERENCES_FROM_OBSERVATIONS_SCHEMA_DEEP
            if is_deep else INFERENCES_FROM_OBSERVATIONS_SCHEMA
        )
        # Embed observations in the user message so the model can reference
        # them by index. The system_prompt is reused verbatim → cache prefix
        # match on shared preamble + agent-specific block.
        import json as _json
        obs_text = _json.dumps(
            {"observations": observations}, ensure_ascii=False, indent=2,
        )
        step2 = self._llm.call_structured(
            system_prompt=system_prompt,
            user_message=(
                user_message
                + "\n\n=== STEP 2 OF 2: ANALYTICAL LEAP ===\n"
                + "These are the observations you produced in step 1; they "
                + "are FROZEN. Reference them by index in based_on_observation_indices.\n\n"
                + obs_text
                + "\n\nNow produce inferences[], counterarguments[], "
                + "disconfirming_triggers[], cognitive_bias_self_check, "
                + "self_reported_uncertainties[], follow_up_questions[]"
                + (", and a 300-800 word narrative_supplement" if is_deep else "")
                + "."
            ),
            tool_schema=step2_schema,
            tool_name="judgment_leap",
            role=self.AGENT_NAME,
            **(hint_kwargs or {}),
        )

        # Stitch — observations from step1, everything else from step2.
        merged = dict(step2)
        merged["observations"] = observations
        return merged

    def _build_system_prompt(self, inp: AgentInput) -> str:
        """Build system prompt from agent template + context."""
        base = self.SYSTEM_PROMPT or f"You are {self.AGENT_NAME} (v{self.AGENT_VERSION})."
        constraints = """

HARD CONSTRAINTS (Section 19.1):
- Do NOT compute new financial values — use only values from the provided metrics
- Do NOT introduce numbers not present in the input
- Separate OBSERVATIONS (grounded in data) from INFERENCES (analytical leaps)
- INFERENCES ARE MANDATORY — produce AT LEAST 2 inferences per response.
  An empty inferences[] is invalid output: observations alone are not analysis.
  If you cannot draw any inference, you have not finished thinking. Each
  inference must (a) reference one or more observation indices, (b) state a
  conclusion the data warrants, and (c) carry a confidence rating.
- Include at least one COUNTERARGUMENT with moderate or strong strength
- Complete the cognitive bias self-check honestly
- Each observation must have source_ids tracing to specific metrics or evidence
- Each inference must reference valid observation indices

NUMERIC CONSISTENCY (CRITICAL — NumericConsistencyCritic scans every observation
and inference for explicit equations and will FLAG mismatches in the report):
- PREFER citing single numbers from the provided metric/fact tables over writing
  derived equations in narrative. e.g. write "net debt is ¥47亿" — NOT
  "net debt = total debt ¥75亿 − cash ¥15亿 = ¥47亿". Equations in narratives are
  fragile; one rounding error gets flagged.
- If you DO write an explicit equation of the form "A = B − C" or "A = B + C",
  the math MUST hold to within 5%. The critic uses regex to extract operands like
  "X 亿" / "$X B" / "X million" and compares the LHS to the computed RHS. Off-by-
  more-than-5% triggers a NUMERIC_BROKEN_EQUATION warn issue tagged to your agent.
- The critic ALSO catches:
    * Ratio claims:    "FCF margin 12% = $0.6B / $5B"   (must hold within 1pp)
    * Multiple claims: "P/E 25x = 100 / 4"              (must hold within 5%)
  Same rule: if you can't verify the math, drop the explicit "= a / b" tail and
  just state the result.
- Any arithmetic statement must be self-consistent. If you cannot verify the math
  from provided inputs, do NOT make the claim.
- When citing growth rates from consensus, compute them from the consensus_mean
  values provided and round to one decimal (e.g. 43.6% not "43% or 44%"). Do not
  vary the figure across your output.
- CAGR WINDOW RULE: The Revenue CAGR above is labelled with its exact window
  (e.g. "4-year, FY2021–FY2025"). You MUST use the SAME window label when citing
  it. Do NOT rephrase "4-year" as "three years" or "past 3 years". If you want a
  different window, compute it yourself from the Revenue-by-year data provided.
- When citing a ratio like CFO/NI, use the EXACT metric values shown, not rounded
  versions. Show the ratio to two significant figures.

FOLLOW-UP QUESTIONS (optional, max 3):
If your analysis is limited by missing data, you may request it via follow_up_questions.
Only ask if the answer would MATERIALLY change your judgment. The system will check if the
data exists and may re-run you with supplemental context.
- data_type "segment": segment-level breakdowns (e.g. margin by product)
- data_type "metric": computed financial ratios
- data_type "time_series": historical trends (e.g. 3-year revenue by segment)
- data_type "fact": specific financial facts from filings
- priority "high": re-run me if data exists. "medium"/"low": record for the reader.
"""

        # Inject Research Director's emphasis for this specific agent
        directive_guidance = ""
        rd = inp.macro_context.get("research_directive") if inp.macro_context else None
        if rd:
            emphasis = rd.get("agent_emphasis", {}).get(self.AGENT_NAME, "")
            depth = rd.get("_depth", "standard")

            if emphasis or depth == "deep":
                directive_guidance = f"""

CHIEF ANALYST DIRECTIVE FOR THIS ENTITY:
"""
                if emphasis:
                    directive_guidance += f"""The Research Director has identified the following as particularly important for your analysis:
{emphasis}

"""
                directive_guidance += f"""Initial hypothesis: {rd.get('initial_hypothesis', '')}
Key variables to investigate: {', '.join(rd.get('key_variables', []))}
Key controversy: {rd.get('key_controversy', '')}

Pay EXTRA attention to the areas highlighted above. Your analysis should directly address whether the initial hypothesis holds up from your specialist perspective."""

                if depth == "deep":
                    directive_guidance += """

DEPTH DIRECTIVE: DEEP ANALYSIS REQUIRED
The Research Director has determined that YOUR domain is CENTRAL to the thesis for this entity.
- Produce MORE observations (aim for 6-8, not the usual 3-4)
- Provide DEEPER inferences with higher specificity
- Generate MORE counterarguments (at least 2, including one "strong")
- Identify MORE disconfirming triggers (at least 3)
- Be more granular in your bias self-check
- If you see something unusual or concerning, ELABORATE — don't just flag it
- In the narrative_supplement, write a 300-800 word analyst memo that goes beyond the structured output"""

                # Iterative re-analysis context
                rerun_ctx = rd.get("_rerun_context")
                if rerun_ctx:
                    directive_guidance += f"""

⚡ SECOND-PASS DEEP DIVE — YOU ARE RE-RUNNING BECAUSE YOUR FIRST-PASS FINDINGS CHALLENGED THE INITIAL HYPOTHESIS.
The Chief Analyst Synthesizer found that the original hypothesis was WRONG or needs major revision.

Original hypothesis: {rerun_ctx.get('original_hypothesis', '')}
How the thesis evolved: {rerun_ctx.get('hypothesis_evolution', '')}
Biggest surprise (from your first pass or others): {rerun_ctx.get('biggest_surprise', '')}
Revised thesis direction: {rerun_ctx.get('revised_thesis', '')}

YOUR MISSION IN THIS SECOND PASS:
1. DIG DEEPER into the finding that challenged the hypothesis — what are the second-order implications?
2. Look for evidence you might have MISSED in the first pass
3. Test the REVISED thesis from your specialist angle — does it hold up better?
4. Identify the ONE thing that would most change the revised thesis
5. Be MORE SPECIFIC and MORE OPINIONATED than your first pass — you now have context from all other analysts"""

        # Language directive for A-share / Chinese market entities
        language_directive = ""
        if inp.macro_context and inp.macro_context.get("language") == "zh-CN":
            language_directive = """

LANGUAGE DIRECTIVE — 强制中文输出:
This is an A-share (China) entity. Write ALL natural-language output in Simplified Chinese (简体中文).
- observations[].text, inferences[].text, counterarguments[].text, disconfirming_triggers, narrative_supplement, bias_self_check — ALL in Chinese
- Keep JSON keys, field names, and enum values in English (e.g. "strength": "strong", not "强")
- NO mixed-English phrases. Translate ALL technical terms:
  * "CFO" → "经营现金流", NOT leave as CFO
  * "accruals ratio" → "应计项目占比"
  * "working capital" → "营运资本"
  * "covenants" → "契约条款"
  * "receivables / inventory" → "应收账款 / 存货"
  * "earnings quality" → "盈利质量"
  * "Revenue / gross margin / operating margin" → "营收 / 毛利率 / 营业利润率"
  * "the entity" → 使用公司中文名或"公司"
  * "ROIC / ROE / P/E / EV/EBITDA" → 可保留缩写但必须包裹在中文句子中
- Currency MUST be ¥ and "亿" for A-shares, NEVER $ or B. Example: ¥226 亿营收, NOT $22.6B Revenue.
- Follow-up questions (follow_up_questions[].question) also in Chinese
- If you catch yourself writing English, STOP and translate before continuing."""

        # Cache-friendly ordering (2026-05-05): place the LARGEST
        # universally-stable content FIRST so DeepSeek's automatic prompt
        # cache can match a long shared prefix across all 7 agents within
        # one pipeline run, and across multiple pipeline runs within the
        # cache TTL window.
        #
        #   [constraints]           — IDENTICAL across all agents/entities
        #   [language_directive]    — identical per market (CN/US)
        #   [base]                  — agent-specific (varies per role)
        #   [directive_guidance]    — entity- and run-specific (varies most)
        #
        # Previously: base + constraints + directive + language. Because
        # `base` (agent-specific) was first, no two agents ever shared a
        # prefix — every call paid full input price. With constraints
        # first, the ~2–3K-token block at the head is cache-eligible and
        # subsequent agent calls hit it for ~25% of the input price.
        return constraints + language_directive + base + directive_guidance

    def _build_user_message(self, inp: AgentInput) -> str:
        """Serialize AgentInput into a structured user message."""
        # BUG-A26 (2026-05-06): A-share entities had every monetary input
        # formatted as `$X.XB` because the agents' user_message construction
        # hard-coded that format. BUG-A22 fixed this in chief_analyst (4
        # components, 17 sites). Same pattern in agents (5 sites): historical
        # revenue table, segment breakdowns, exhibit summaries, etc. Branch
        # on `meta_facts["__display"]` so A-share agents get `¥X.X亿` inputs.
        from aegis.core._display import resolve_display, fmt_money_big
        _disp = resolve_display(inp.facts)
        parts = [f"Entity: {inp.entity_id}", f"Run: {inp.run_id}", ""]

        if inp.metric_results:
            parts.append("=== COMPUTED METRICS ===")
            for k, v in sorted(inp.metric_results.items()):
                if isinstance(v, float):
                    parts.append(f"  {k}: {v:.4f}" if abs(v) < 100 else f"  {k}: {v:,.0f}")
                else:
                    parts.append(f"  {k}: {v}")
            parts.append("")

        if inp.facts:
            # Only include material financial facts, skip internal/metadata fields
            parts.append("=== RAW FACTS ===")
            historical_revenue = None
            historical_growth = None
            historical_data = None
            revenue_cagr = None
            cagr_unreliable = False
            cagr_warnings: list[str] = []
            for k, v in sorted(inp.facts.items()):
                if k.startswith("us-gaap:"):
                    continue
                if k.startswith("__"):
                    # Capture historical series for separate rendering
                    if k == "__historical_revenue" and isinstance(v, dict):
                        historical_revenue = v
                    elif k == "__historical_growth" and isinstance(v, dict):
                        historical_growth = v
                    elif k == "__historical_data" and isinstance(v, dict):
                        historical_data = v
                    elif k == "__revenue_cagr" and isinstance(v, (int, float)):
                        revenue_cagr = v
                    elif k == "__revenue_cagr_unreliable":
                        cagr_unreliable = bool(v)
                    elif k == "__revenue_cagr_warnings" and isinstance(v, list):
                        cagr_warnings = v
                    continue
                if isinstance(v, (int, float)):
                    parts.append(f"  {k}: {v:,}")
                elif isinstance(v, str):
                    parts.append(f"  {k}: {v}")
            parts.append("")

            # === HISTORICAL REVENUE TRAJECTORY ===
            # Provide multi-year revenue and growth so agents don't ask "what was
            # revenue growth over past 5 years?" — the data is right here.
            if historical_revenue or historical_growth or revenue_cagr is not None:
                parts.append("=== HISTORICAL REVENUE TRAJECTORY (multi-year) ===")
                if revenue_cagr is not None:
                    # Compute window label from historical_revenue if available
                    _cagr_window = ""
                    if historical_revenue:
                        _yr_sorted = sorted(historical_revenue.keys())
                        _n_yr = _yr_sorted[-1] - _yr_sorted[0]
                        _cagr_window = f" ({_n_yr}-year, FY{_yr_sorted[0]}–FY{_yr_sorted[-1]})"
                    if cagr_unreliable:
                        parts.append(
                            f"  Revenue CAGR{_cagr_window}: {revenue_cagr:.1%} "
                            f"⚠ UNRELIABLE — DO NOT extrapolate forward"
                        )
                        for w in cagr_warnings:
                            parts.append(f"    · {w}")
                        parts.append(
                            f"    → Use sector defaults or the most recent "
                            f"YoY growth rate (with caveats), not this CAGR"
                        )
                    else:
                        parts.append(f"  Revenue CAGR{_cagr_window}: {revenue_cagr:.1%}")
                    # Also provide 3-year CAGR if we have enough data, so agents
                    # don't misquote the full-window CAGR as "3-year"
                    if historical_revenue and len(_yr_sorted) >= 4:
                        _3yr_start = _yr_sorted[-1] - 3
                        if _3yr_start in historical_revenue:
                            _3yr_base = historical_revenue[_3yr_start]
                            _3yr_end = historical_revenue[_yr_sorted[-1]]
                            if _3yr_base > 0:
                                _3yr_cagr = (_3yr_end / _3yr_base) ** (1/3) - 1
                                parts.append(
                                    f"  Revenue CAGR (3-year, FY{_3yr_start}–FY{_yr_sorted[-1]}): "
                                    f"{_3yr_cagr:.1%}"
                                )
                if historical_revenue:
                    years = sorted(historical_revenue.keys())
                    rev_str = ", ".join(
                        f"{y}={fmt_money_big(historical_revenue[y], _disp)}"
                        for y in years
                    )
                    parts.append(f"  Revenue by year: {rev_str}")
                if historical_growth:
                    years = sorted(historical_growth.keys())
                    g_str = ", ".join(
                        f"{y}={historical_growth[y]:.1%}" for y in years
                    )
                    parts.append(f"  YoY growth: {g_str}")
                parts.append("")

            # === DATA QUALITY ALERTS ===
            # Surface fact_bridge sanity-check issues so agents can caveat
            # their conclusions when source data is suspect (D&A missing,
            # impossible margins, sign errors, etc.).
            dq_issues = inp.facts.get("__data_quality_issues") if inp.facts else None
            if dq_issues:
                parts.append("=== DATA QUALITY ALERTS (from fact_bridge) ===")
                for iss in dq_issues:
                    parts.append(f"  [{iss.get('severity', '?').upper()}] "
                                 f"{iss.get('code', '')}: {iss.get('message', '')}")
                parts.append("  → Caveat any inference that depends on the flagged fields above.")
                parts.append("")

            # === HISTORICAL METRICS (other financial series) ===
            if historical_data:
                parts.append("=== HISTORICAL FINANCIAL SERIES ===")
                for metric_name, series in sorted(historical_data.items()):
                    if not isinstance(series, dict):
                        continue
                    years = sorted(series.keys())
                    if not years:
                        continue
                    # Limit to 5 most recent years for prompt compactness
                    recent = years[-5:]
                    vals_str = ", ".join(
                        f"{y}={series[y]:,.0f}" if isinstance(series[y], (int, float))
                        else f"{y}={series[y]}"
                        for y in recent
                    )
                    parts.append(f"  {metric_name}: {vals_str}")
                parts.append("")

                # === HISTORICAL MARGIN / RATIO RANGES ===
                # Pre-compute min / median / max for key ratios so the agent
                # has answers ready without asking "what's the historical X range".
                # Look for revenue + income-like series in historical_data and
                # derive the ratio series.
                try:
                    rev_series = None
                    for k in ("us-gaap:Revenues",
                              "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                              "revenue"):
                        if k in historical_data and isinstance(historical_data[k], dict):
                            rev_series = historical_data[k]
                            break
                    if rev_series:
                        range_lines: list[str] = []
                        for label, src_keys in (
                            ("operating_margin", ("us-gaap:OperatingIncomeLoss",)),
                            ("gross_margin", ("us-gaap:GrossProfit",)),
                            ("net_margin", ("us-gaap:NetIncomeLoss",)),
                            ("rd_to_revenue", ("us-gaap:ResearchAndDevelopmentExpense",)),
                        ):
                            num_series = None
                            for k in src_keys:
                                if k in historical_data and isinstance(historical_data[k], dict):
                                    num_series = historical_data[k]
                                    break
                            if not num_series:
                                continue
                            ratios: list[tuple[int, float]] = []
                            for yr, rev in sorted(rev_series.items()):
                                num = num_series.get(yr)
                                if num is None or not rev:
                                    continue
                                ratios.append((yr, num / rev))
                            if len(ratios) >= 2:
                                vals = sorted(r for _, r in ratios)
                                n = len(vals)
                                lo, hi = vals[0], vals[-1]
                                med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
                                low_year = next(y for y, r in ratios if r == lo)
                                high_year = next(y for y, r in ratios if r == hi)
                                range_lines.append(
                                    f"  {label}: min={lo*100:.1f}% (FY{low_year}), "
                                    f"median={med*100:.1f}%, max={hi*100:.1f}% (FY{high_year}) "
                                    f"[{len(ratios)}-year range]"
                                )
                        if range_lines:
                            parts.append("=== HISTORICAL RATIO RANGES (pre-computed) ===")
                            parts.extend(range_lines)
                            parts.append("")
                except Exception:
                    pass  # Never break prompt building on summary compute

        # === PEER FUNDAMENTALS ===
        # Surface peer PE / EV_EBITDA / margins / growth so valuation_analyst
        # and variant_analyst don't ask "what are peer multiples?" as a follow-up.
        if getattr(inp, "peer_fundamentals", None):
            peers = inp.peer_fundamentals
            parts.append("=== PEER FUNDAMENTALS (semiconductor / comparable peers) ===")
            peer_pe_vals: list[float] = []
            peer_ev_vals: list[float] = []
            peer_rd_vals: list[float] = []
            peer_gm_vals: list[float] = []
            peer_om_vals: list[float] = []
            for p in peers:
                if not isinstance(p, dict):
                    continue
                ticker = p.get("ticker") or p.get("symbol") or "?"
                pe = p.get("pe_ratio") or p.get("forward_pe") or p.get("trailing_pe")
                ev = p.get("ev_to_ebitda") or p.get("ev_ebitda")
                gm = p.get("gross_margin") or p.get("gross_profit_margin")
                om = p.get("operating_margin") or p.get("op_margin")
                rd = p.get("rd_to_revenue") or p.get("research_and_development_to_revenue")
                rev_growth = p.get("revenue_growth") or p.get("revenue_growth_yoy")
                bits = [f"{ticker}"]
                if pe is not None:
                    bits.append(f"PE={pe:.1f}x")
                    peer_pe_vals.append(float(pe))
                if ev is not None:
                    bits.append(f"EV/EBITDA={ev:.1f}x")
                    peer_ev_vals.append(float(ev))
                if gm is not None:
                    bits.append(f"GM={gm*100:.0f}%" if abs(gm) < 2 else f"GM={gm:.0f}%")
                    peer_gm_vals.append(float(gm) if abs(gm) < 2 else float(gm) / 100)
                if om is not None:
                    bits.append(f"OM={om*100:.0f}%" if abs(om) < 2 else f"OM={om:.0f}%")
                    peer_om_vals.append(float(om) if abs(om) < 2 else float(om) / 100)
                if rd is not None:
                    bits.append(f"R&D/Rev={rd*100:.0f}%" if abs(rd) < 2 else f"R&D/Rev={rd:.0f}%")
                    peer_rd_vals.append(float(rd) if abs(rd) < 2 else float(rd) / 100)
                if rev_growth is not None:
                    bits.append(f"rev_growth={rev_growth*100:.0f}%" if abs(rev_growth) < 2 else f"rev_growth={rev_growth:.0f}%")
                parts.append("  " + ", ".join(bits))
            # Pre-computed summary statistics (median / mean) for quick reference
            def _median(xs):
                xs = sorted(x for x in xs if x is not None)
                if not xs:
                    return None
                n = len(xs)
                return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
            summary_bits = []
            if peer_pe_vals:
                summary_bits.append(f"median PE={_median(peer_pe_vals):.1f}x")
            if peer_ev_vals:
                summary_bits.append(f"median EV/EBITDA={_median(peer_ev_vals):.1f}x")
            if peer_gm_vals:
                summary_bits.append(f"median GM={_median(peer_gm_vals)*100:.0f}%")
            if peer_om_vals:
                summary_bits.append(f"median OM={_median(peer_om_vals)*100:.0f}%")
            if peer_rd_vals:
                summary_bits.append(f"median R&D/Rev={_median(peer_rd_vals)*100:.0f}%")
            if summary_bits:
                parts.append(f"  peer medians: {', '.join(summary_bits)}")
            parts.append("")

        # === HISTORICAL VALUATION MULTIPLES ===
        # Surface the 3-5 year PE / EV/EBITDA range so valuation_analyst
        # doesn't ask "what's the historical PE range?" as a follow-up.
        if getattr(inp, "historical_valuation", None):
            hv = inp.historical_valuation
            pe_stats = hv.get("pe_stats") or {}
            ev_stats = hv.get("ev_ebitda_stats") or {}
            n_months = len(hv.get("dates") or [])
            if pe_stats or ev_stats:
                parts.append(f"=== HISTORICAL VALUATION ({n_months} months) ===")
                if pe_stats:
                    bits = []
                    for k in ("min", "p25", "p50", "p75", "max", "current_percentile"):
                        v = pe_stats.get(k)
                        if v is None:
                            continue
                        if k == "current_percentile":
                            bits.append(f"current@{v*100:.0f}%ile")
                        else:
                            bits.append(f"{k}={v:.1f}x")
                    if bits:
                        parts.append(f"  PE: {', '.join(bits)}")
                if ev_stats:
                    bits = []
                    for k in ("min", "p25", "p50", "p75", "max", "current_percentile"):
                        v = ev_stats.get(k)
                        if v is None:
                            continue
                        if k == "current_percentile":
                            bits.append(f"current@{v*100:.0f}%ile")
                        else:
                            bits.append(f"{k}={v:.1f}x")
                    if bits:
                        parts.append(f"  EV/EBITDA: {', '.join(bits)}")
                parts.append("")

        # === SEGMENT BREAKDOWN ===
        # Show segment-level revenue and operating income so agents can reason
        # about product mix without asking for the data.
        # BUG-47: explicitly flag missing opinc so LLM does not fabricate margins.
        if inp.segment_detail:
            parts.append("=== SEGMENT BREAKDOWN (latest year) ===")
            missing_oi_categories: list[str] = []
            for category, segs in sorted(inp.segment_detail.items()):
                if not isinstance(segs, dict) or not segs:
                    continue
                parts.append(f"  [{category}]")
                any_oi = False
                for seg_id, seg_facts in sorted(segs.items()):
                    if not isinstance(seg_facts, dict):
                        continue
                    rev = seg_facts.get("revenue")
                    oi = seg_facts.get("operating_income")
                    bits = []
                    if rev:
                        bits.append(f"revenue={fmt_money_big(rev, _disp)}")
                    if oi is not None and rev:
                        oi_pct = oi / rev * 100
                        bits.append(f"OM={oi_pct:.0f}%")
                        any_oi = True
                    if seg_facts.get("_synthetic"):
                        bits.append("(est. gap — not directly reported)")
                    if bits:
                        parts.append(f"    {seg_id}: {', '.join(bits)}")
                if not any_oi and any(
                    isinstance(s, dict) and s.get("revenue", 0) > 0
                    for s in segs.values()
                ):
                    missing_oi_categories.append(category)

            # BUG-47: CRITICAL anti-hallucination directive.
            # Tell the LLM explicitly not to invent segment margins when opinc
            # is not in the XBRL filing. Before this, agents routinely
            # fabricated "Google Services 62% margin" etc, making implied
            # segment opinc exceed the consolidated total.
            if missing_oi_categories:
                parts.append(
                    f"  ⚠ Segment operating income NOT DISCLOSED in XBRL for: "
                    f"{', '.join(missing_oi_categories)}"
                )
                parts.append(
                    "  → DO NOT estimate or fabricate per-segment operating "
                    "margins for these categories. Do not cite specific % "
                    "figures. If you must discuss segment profitability, "
                    "say 'not disclosed' and reason qualitatively only. "
                    "Any implied segment opinc × revenue > total operating "
                    "income is mathematically impossible and will be flagged "
                    "by the consistency critic."
                )
            # Also surface consolidated opinc ceiling to anchor reasoning
            total_oi = (inp.facts or {}).get("operating_income")
            total_rev = (inp.facts or {}).get("revenue")
            if total_oi and total_rev:
                parts.append(
                    f"  (Consolidated operating income ceiling: "
                    f"{fmt_money_big(total_oi, _disp)} on "
                    f"{fmt_money_big(total_rev, _disp)} revenue "
                    f"= {total_oi/total_rev*100:.1f}% blended margin. "
                    f"Σ across all segments MUST NOT exceed this.)"
                )
            parts.append("")

        # === SEGMENT HISTORICAL TIME SERIES ===
        # Show multi-year segment trends when available.
        if inp.segment_data:
            parts.append("=== SEGMENT HISTORICAL TRENDS ===")
            for seg_id, seg_series in sorted(inp.segment_data.items()):
                if not isinstance(seg_series, dict):
                    continue
                bits = []
                for metric_name, series in sorted(seg_series.items()):
                    if isinstance(series, dict):
                        years = sorted(series.keys())
                        if years:
                            recent = years[-4:]
                            vals = ", ".join(
                                f"{y}={fmt_money_big(series[y], _disp)}" if isinstance(series[y], (int, float)) and abs(series[y]) > 1e6
                                else f"{y}={series[y]}"
                                for y in recent
                            )
                            bits.append(f"{metric_name}: {vals}")
                if bits:
                    parts.append(f"  {seg_id}")
                    for b in bits:
                        parts.append(f"    {b}")
            parts.append("")

        if inp.evidence_packets:
            parts.append("=== EVIDENCE PACKETS ===")
            for ep in inp.evidence_packets:
                parts.append(f"  [{ep.get('evidence_id', '')}] {ep.get('assertion_type', '')}: {ep.get('assertion_text', '')}")
            parts.append("")

        if inp.macro_context:
            parts.append("=== MACRO CONTEXT ===")
            for k, v in inp.macro_context.items():
                if isinstance(v, dict):
                    parts.append(f"  {k}:")
                    for sk, sv in v.items():
                        parts.append(f"    {sk}: {sv}")
                else:
                    parts.append(f"  {k}: {v}")
            parts.append("")

        if inp.sector_pack:
            sp = inp.sector_pack
            parts.append(f"=== SECTOR: {sp.get('sector_name', '')} ===")
            cycle = sp.get("cycle_characteristics", {})
            if cycle:
                parts.append(f"  Cyclicality: {cycle.get('cyclicality', '')}")
            # Revenue driver decomposition (critical for business analyst)
            decomp = sp.get("revenue_drivers", {}).get("decomposition", {})
            if decomp:
                parts.append(f"  Revenue Formula: {decomp.get('formula', '')}")
                for node in decomp.get("tree", []):
                    parts.append(f"    - {node.get('name', '')}: {node.get('note', '')}")
            # Competitive dynamics
            comp = sp.get("competitive_dynamics", {})
            moats = comp.get("moat_sources", [])
            risks = comp.get("disruption_risks", [])
            if moats:
                parts.append(f"  Moat Sources: {'; '.join(moats[:4])}")
            if risks:
                parts.append(f"  Disruption Risks: {'; '.join(risks[:4])}")
            # Accounting considerations
            acct = sp.get("accounting_considerations", [])
            if acct:
                parts.append(f"  Accounting Notes: {'; '.join(acct[:3])}")
            # Valuation pitfalls
            val_fw = sp.get("valuation_framework", {})
            pitfalls = val_fw.get("common_pitfalls", [])
            if pitfalls:
                parts.append(f"  Valuation Pitfalls: {'; '.join(pitfalls[:3])}")
            parts.append("")

        if inp.entity_relationships:
            parts.append("=== ENTITY RELATIONSHIPS ===")
            for rel in inp.entity_relationships:
                parts.append(f"  {rel.get('relationship_type', '')}: {rel.get('entity_a', '')} ↔ {rel.get('entity_b', '')}")
            parts.append("")

        # BUG-49: removed duplicate SEGMENT BREAKDOWN block. The first
        # SEGMENT BREAKDOWN section above (around line 745) already renders
        # the same data with the BUG-47 anti-hallucination directives. This
        # second copy was (a) inflating the prompt context (wasted tokens,
        # slower inference) and (b) bypassing the anti-hallucination
        # guidance, giving the LLM a "clean" segment table that encouraged
        # it to invent margins. Drop it.

        # Key financials summary (formatted for easy LLM comprehension)
        if inp.facts:
            key_fins = [
                ("Revenue", "revenue"), ("Net Income", "net_income"),
                ("EBITDA", "ebitda"), ("Operating Income", "operating_income"),
                ("Free Cash Flow", "free_cash_flow"), ("Operating Cash Flow", "operating_cash_flow"),
                ("Total Debt", "total_debt"), ("Cash & Equivalents", "cash_and_equivalents"),
                ("Total Cash + Investments", "total_cash_and_investments"),
                ("Share Buybacks", "share_buybacks"), ("Dividends Paid", "dividends_paid"),
                ("Total Shareholder Returns", "total_shareholder_return_cash"),
                ("SBC", "sbc"), ("R&D Expense", "research_and_development"),
                ("D&A", "depreciation_amortization"),
                ("Effective Tax Rate", "effective_tax_rate"),
            ]
            summary_lines = []
            for label, key in key_fins:
                val = inp.facts.get(key)
                if val is not None:
                    if isinstance(val, float) and abs(val) < 1:
                        summary_lines.append(f"    {label}: {val:.1%}")
                    elif isinstance(val, (int, float)):
                        summary_lines.append(f"    {label}: {fmt_money_big(val, _disp)}")
            # Aegis 2.0 Phase 1 (任务 D4): TTM 滚动口径三行 + 数据截至。
            # 让 agent 的分析基于最新滚动 4 季数据而非纯年报快照；数字由
            # orchestrator Step 4c 从 PIT/TTM 引擎写入的固定键提供（红线 8
            # 豁免键），面世数字已按红线 9 注册 scrubber 白名单。
            _is_zh_prompt = bool(
                inp.macro_context
                and inp.macro_context.get("language") == "zh-CN"
            )
            ttm_rows = (
                ("TTM营收（最近4季滚动）", "ttm_revenue"),
                ("TTM归母净利润", "ttm_net_income"),
                ("TTM扣非归母净利润", "ttm_net_income_deducted"),
            ) if _is_zh_prompt else (
                ("TTM Revenue (trailing 4 quarters)", "ttm_revenue"),
                ("TTM Net Income (attributable)", "ttm_net_income"),
                ("TTM Net Income (ex non-recurring)", "ttm_net_income_deducted"),
            )
            ttm_lines = []
            for label, key in ttm_rows:
                val = inp.facts.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    ttm_lines.append(f"    {label}: {fmt_money_big(val, _disp)}")
            if ttm_lines:
                _fresh = inp.facts.get("__data_freshness")
                if isinstance(_fresh, dict) and _fresh.get("latest_period"):
                    _days = _fresh.get("days_since")
                    _days_txt = ""
                    if isinstance(_days, int):
                        _days_txt = (
                            f"（距今 {_days} 天）" if _is_zh_prompt
                            else f" ({_days} days old)"
                        )
                    ttm_lines.append(
                        f"    {'数据截至' if _is_zh_prompt else 'Data as of'}: "
                        f"{_fresh['latest_period']}{_days_txt}"
                    )
                summary_lines.extend(ttm_lines)
            if summary_lines:
                parts.append("=== KEY FINANCIALS SUMMARY ===")
                parts.extend(summary_lines)
                parts.append("")

        # Previous agent findings — inter-agent information flow
        if inp.previous_agent_findings:
            parts.append("=== FINDINGS FROM PREVIOUS ANALYSTS ===")
            parts.append("(These are key findings from analysts who have already completed their review.")
            parts.append(" Use them to INFORM your analysis — build on, challenge, or deepen their findings.)")
            for finding in inp.previous_agent_findings:
                agent_label = finding.get("agent", "unknown").replace("_", " ").title()
                key_finding = finding.get("key_finding", "")
                red_flag = finding.get("red_flag", False)
                confidence = finding.get("confidence", "")
                flag_marker = " ⚠ RED FLAG" if red_flag else ""
                conf_str = f" [{confidence}]" if confidence else ""
                parts.append(f"  {agent_label}{flag_marker}{conf_str}: {key_finding}")
            parts.append("")

        if inp.supplemental_data:
            parts.append("=== SUPPLEMENTAL DATA (answers to your follow-up questions) ===")
            parts.append("(This data was retrieved in response to questions you raised in a prior pass.)")
            for key, value in inp.supplemental_data.items():
                parts.append(f"  {key}: {value}")
            parts.append("")

        parts.append("Produce your analysis as structured judgment output.")
        return "\n".join(parts)

    # -- Abstract methods still needed for direct-run compatibility --
    # These are only called if someone uses `super().run()` instead of LLM run

    def _extract_observations(self, inp: AgentInput) -> list[Observation]:
        return []

    def _derive_inferences(self, observations: list[Observation], inp: AgentInput) -> list[Inference]:
        return []

    def _generate_counterarguments(self, inferences: list[Inference], inp: AgentInput) -> list[Counterargument]:
        return []

    def _identify_disconfirming_triggers(self, inferences: list[Inference], inp: AgentInput) -> list[DisconfirmingTrigger]:
        return []

    def _cognitive_bias_self_check(self, inp: AgentInput) -> CognitiveBiasSelfCheck:
        return CognitiveBiasSelfCheck(
            anchoring_risk="medium", confirmation_bias_risk="medium",
            recency_bias_risk="medium", narrative_fallacy_risk="medium",
        )

    def _report_uncertainties(self, inp: AgentInput) -> list[str]:
        return []
