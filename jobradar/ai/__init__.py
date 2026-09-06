"""AI integration package for JobRadar.

Provider details are kept away from the Tkinter GUI. The AI layer contains
configuration, evaluation-result storage, semantic memory/embedding
infrastructure, and a tool registry for future agent/chat workflows.
"""

from .models import AiEvaluationResult, AiMemory, AiModelConfig, EmbeddingConfig
from .service import AiEvaluationService
from .tools import AiToolRegistry, AiToolResult, AiToolSpec
from .agent import AgentController, AgentRunConfig, AgentTaskQueue, AgentTools

__all__ = [
    "AiEvaluationResult",
    "AiMemory",
    "AiModelConfig",
    "EmbeddingConfig",
    "AiEvaluationService",
    "AiToolRegistry",
    "AiToolResult",
    "AiToolSpec",
    "AgentController",
    "AgentRunConfig",
    "AgentTaskQueue",
    "AgentTools",
]
