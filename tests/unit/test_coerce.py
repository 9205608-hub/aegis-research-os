"""Unit tests for the LLM-output coercion layer (AUDIT-E1).

aegis/core/_coerce.py is the direct fix for HANDOFF's #1 recurring bug type
(LLM 输出类型脆弱: BUG-Y24 confidence='medium_high', BUG-Y25/Y26
string-for-list char-iteration, BUG-Y27 bias fields) yet had ZERO direct
tests — a refactor regression would only surface in a live LLM run.

Covers:
  - coerce_list: all documented branches (None / list / JSON-string /
    comma-string / dict / scalar / tuple / set / empty string)
  - normalize_low_med_high: compound buckets, case/separator variants,
    unknown default (promoted from llm_agent_base.run() closure)
  - normalize_strength: AUDIT-B4 weak|moderate|strong normalization
    (LLMs mix it up with the low|medium|high confidence enum)
"""

from aegis.core._coerce import coerce_list, normalize_low_med_high, normalize_strength


class TestCoerceList:
    """coerce_list — every documented branch."""

    def test_none_returns_empty(self):
        assert coerce_list(None) == []

    def test_list_passes_through_unchanged(self):
        lst = [{"a": 1}, "b", 3]
        assert coerce_list(lst) is lst

    def test_empty_list(self):
        assert coerce_list([]) == []

    def test_json_string_of_list(self):
        # BUG-Y25/Y26: the char-iteration killer case
        assert coerce_list('["a", "b", "c"]') == ["a", "b", "c"]

    def test_json_string_of_list_of_dicts(self):
        assert coerce_list('[{"text": "x"}, {"text": "y"}]') == [
            {"text": "x"}, {"text": "y"},
        ]

    def test_json_string_never_char_iterated(self):
        # The original symptom: "Re-running 101 agents" when the list had 5
        result = coerce_list('["alpha", "beta"]')
        assert result == ["alpha", "beta"]
        assert all(len(x) > 1 for x in result)  # not single chars

    def test_comma_separated_string(self):
        assert coerce_list("a, b, c") == ["a", "b", "c"]

    def test_comma_separated_string_with_quotes(self):
        assert coerce_list('"a", \'b\'') == ["a", "b"]

    def test_single_plain_string(self):
        assert coerce_list("m_revenue") == ["m_revenue"]

    def test_empty_string_returns_empty(self):
        assert coerce_list("") == []
        assert coerce_list("   ") == []

    def test_dict_wrapped_as_single_element(self):
        d = {"text": "obs", "source_ids": ["m_x"]}
        assert coerce_list(d) == [d]

    def test_scalar_int_wrapped(self):
        # AUDIT-B5: "based_on_observation_indices": 2 → [2]
        assert coerce_list(2) == [2]

    def test_scalar_float_wrapped(self):
        assert coerce_list(1.5) == [1.5]

    def test_tuple_converted(self):
        assert coerce_list((1, 2)) == [1, 2]

    def test_set_converted(self):
        assert sorted(coerce_list({1, 2})) == [1, 2]

    def test_malformed_json_falls_back_to_comma_split(self):
        # Starts with [ but unparseable → comma-split fallback, never raises
        result = coerce_list("[broken, json")
        assert isinstance(result, list)
        assert result  # non-empty, no exception


class TestNormalizeLowMedHigh:
    """normalize_low_med_high — BUG-Y24/Y27 compound-bucket interception."""

    def test_valid_values_pass_through(self):
        assert normalize_low_med_high("low") == "low"
        assert normalize_low_med_high("medium") == "medium"
        assert normalize_low_med_high("high") == "high"

    def test_case_insensitive(self):
        assert normalize_low_med_high("HIGH") == "high"
        assert normalize_low_med_high("Medium") == "medium"

    def test_medium_high_maps_to_high(self):
        # BUG-Y24's exact production value
        assert normalize_low_med_high("medium_high") == "high"
        assert normalize_low_med_high("MEDIUM_HIGH") == "high"
        assert normalize_low_med_high("medium-high") == "high"
        assert normalize_low_med_high("medium high") == "high"
        assert normalize_low_med_high("mediumhigh") == "high"
        assert normalize_low_med_high("very_high") == "high"

    def test_medium_low_maps_to_low(self):
        assert normalize_low_med_high("medium_low") == "low"
        assert normalize_low_med_high("low-medium") == "low"
        assert normalize_low_med_high("very_low") == "low"

    def test_unknown_defaults_to_medium(self):
        # 'critical' is the test_follow_up_questions poster child
        assert normalize_low_med_high("critical") == "medium"
        assert normalize_low_med_high("banana") == "medium"

    def test_none_and_empty_default_to_medium(self):
        assert normalize_low_med_high(None) == "medium"
        assert normalize_low_med_high("") == "medium"

    def test_non_string_input_never_raises(self):
        assert normalize_low_med_high(3) == "medium"
        assert normalize_low_med_high({"level": "high"}) == "medium"


class TestNormalizeStrength:
    """normalize_strength — AUDIT-B4 Counterargument.strength normalization."""

    def test_valid_values_pass_through(self):
        assert normalize_strength("weak") == "weak"
        assert normalize_strength("moderate") == "moderate"
        assert normalize_strength("strong") == "strong"

    def test_case_insensitive(self):
        # 实测 ValidationError 的三个值之一
        assert normalize_strength("STRONG") == "strong"
        assert normalize_strength("Weak") == "weak"
        assert normalize_strength("MODERATE") == "moderate"

    def test_medium_maps_to_moderate(self):
        # The cross-enum mixup: confidence's low|medium|high leaking in
        assert normalize_strength("medium") == "moderate"

    def test_moderate_strong_maps_to_moderate(self):
        assert normalize_strength("moderate_strong") == "moderate"
        assert normalize_strength("moderate-strong") == "moderate"

    def test_very_strong_maps_to_strong(self):
        # 实测 ValidationError: 'very strong'
        assert normalize_strength("very strong") == "strong"
        assert normalize_strength("very_strong") == "strong"
        assert normalize_strength("strongest") == "strong"

    def test_cross_enum_low_high(self):
        assert normalize_strength("high") == "strong"
        assert normalize_strength("low") == "weak"

    def test_mild_and_very_weak_map_to_weak(self):
        assert normalize_strength("mild") == "weak"
        assert normalize_strength("very_weak") == "weak"
        assert normalize_strength("very weak") == "weak"

    def test_unknown_defaults_to_moderate(self):
        assert normalize_strength("overwhelming") == "moderate"
        assert normalize_strength("中等") == "moderate"

    def test_none_and_empty_default_to_moderate(self):
        assert normalize_strength(None) == "moderate"
        assert normalize_strength("") == "moderate"

    def test_non_string_input_never_raises(self):
        assert normalize_strength(2) == "moderate"
        assert normalize_strength(["strong"]) == "moderate"


class TestCoerceDict:
    """BUG-Y25 dict 版（2026-07-13 R7 宁德实锤）：Director 的 agent_depth
    被 LLM 序列化成 JSON 字符串 → orchestrator agent_depth.get(n) 炸掉整条
    run（'str' object has no attribute 'get'）。"""

    def test_passthrough_dict(self):
        from aegis.core._coerce import coerce_dict
        d = {"business_analyst": "deep"}
        assert coerce_dict(d) is d

    def test_json_string_object_parsed(self):
        from aegis.core._coerce import coerce_dict
        s = '{"business_analyst": "deep", "risk_analyst": "standard"}'
        assert coerce_dict(s) == {
            "business_analyst": "deep", "risk_analyst": "standard"}

    def test_garbage_and_none_return_empty(self):
        from aegis.core._coerce import coerce_dict
        assert coerce_dict(None) == {}
        assert coerce_dict("deep") == {}
        assert coerce_dict("{broken json") == {}
        assert coerce_dict('["a", "b"]') == {}
        assert coerce_dict(42) == {}

    def test_director_parse_boundary_wires_coercion(self):
        # 解析边界回归：字符串形态的 agent_depth 必须在 Director 内被归一，
        # orchestrator 侧 .get() 不再可能收到 str。
        from unittest.mock import MagicMock

        from aegis.core.chief_analyst.research_director import ResearchDirector
        d = ResearchDirector()
        d._llm = MagicMock()
        d._llm.call_structured.return_value = {
            "initial_hypothesis": "h",
            "agent_depth": '{"business_analyst": "deep"}',
            "agent_emphasis": '{"business_analyst": "关注毛利"}',
        }
        directive = d.direct(
            entity_id="300750", entity_name="宁德时代",
            meta_facts={}, computed_metrics={}, macro_context={},
            sector_pack={}, segment_detail=None, market_data={},
        )
        assert directive.agent_depth == {"business_analyst": "deep"}
        assert directive.agent_depth.get("risk_analyst") is None  # .get 可用
        assert directive.agent_emphasis == {"business_analyst": "关注毛利"}
