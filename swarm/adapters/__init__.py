from ..config import AgentConfig
from .antigravity import AntigravityAdapter
from .base import RATE_LIMIT_RE, Adapter, RunSpec  # noqa: F401
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter
from .generic import GenericAdapter
from .grok import GrokAdapter
from .perplexity import PerplexityAdapter

_REGISTRY = {"claude": ClaudeAdapter, "codex": CodexAdapter, "antigravity": AntigravityAdapter,
             "gemini": GeminiAdapter, "grok": GrokAdapter,
             "perplexity": PerplexityAdapter, "generic": GenericAdapter}


def get_adapter(agent_cfg: AgentConfig) -> Adapter:
    cls = _REGISTRY.get(agent_cfg.provider)
    if cls is None:
        raise ValueError(f"unknown provider {agent_cfg.provider}")
    return cls(agent_cfg)
