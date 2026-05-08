"""Numeric Consistency Critic.

Catches LLM-fabricated arithmetic in agent narratives — e.g.
"net debt 47B = total debt 75B - cash 15B" where 75-15=60, not 47.
"""

from aegis.core.critics.numeric_consistency_critic.critic import NumericConsistencyCritic

__all__ = ["NumericConsistencyCritic"]
