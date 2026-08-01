"""Regression: PEER FUNDAMENTALS prompt 段必须在 dataclass 输入下存活。

get_peer_fundamentals() 返回 PeerFundamentals dataclass 实例，orchestrator
原样注入 AgentInput（auto_research.py:3218），replay 的 pickle 同样保留
dataclass 形态。旧的 dict-only isinstance 过滤把每个 peer 静默丢弃——美股
run 的 PEER FUNDAMENTALS 段只剩空表头。字段名漂移（dataclass 的
pe_trailing/pe_forward vs 消费端的 trailing_pe/forward_pe）叠加：即便
asdict() 转换后也取不到 PE。两处一并回归。
"""

from aegis.core.acquisition.connectors.openbb_connector import PeerFundamentals
from aegis.core.agents.base import AgentInput
from aegis.core.agents.llm_agent_base import LLMAgentBase


class _PromptAgent(LLMAgentBase):
    AGENT_NAME = "peer_prompt_test_agent"
    AGENT_VERSION = "0.0.1"
    SYSTEM_PROMPT = "You are a test agent."

    def __init__(self):  # bypass LLMAgentBase.__init__ (no env config)
        self._llm = None


def _msg(peers) -> str:
    inp = AgentInput(
        entity_id="e_test",
        run_id="r_test",
        question_id="q_test",
        peer_fundamentals=peers,
    )
    return _PromptAgent()._build_user_message(inp)


class TestPeerDataclassSurvival:
    def test_dataclass_peers_render(self):
        peers = [
            PeerFundamentals(
                symbol="AMD",
                pe_trailing=45.2,
                ev_to_ebitda=30.1,
                gross_margin=0.52,
                operating_margin=0.22,
                revenue_growth_yoy=0.18,
            )
        ]
        msg = _msg(peers)
        assert "PEER FUNDAMENTALS" in msg
        assert "AMD" in msg
        assert "PE=45.2x" in msg
        assert "EV/EBITDA=30.1x" in msg

    def test_pe_forward_fallback(self):
        msg = _msg([PeerFundamentals(symbol="TXN", pe_forward=25.0)])
        assert "TXN" in msg
        assert "PE=25.0x" in msg

    def test_dict_peers_still_render(self):
        msg = _msg([{"ticker": "AVGO", "pe_ratio": 38.0}])
        assert "AVGO" in msg
        assert "PE=38.0x" in msg

    def test_garbage_elements_skipped(self):
        msg = _msg(["junk", 42, None, {"ticker": "NVDA", "pe_ratio": 50.0}])
        assert "NVDA" in msg
        assert "PE=50.0x" in msg

    def test_mixed_dataclass_and_dict(self):
        msg = _msg(
            [
                PeerFundamentals(symbol="AMD", pe_trailing=45.2),
                {"ticker": "AVGO", "pe_ratio": 38.0},
            ]
        )
        assert "AMD" in msg
        assert "AVGO" in msg
