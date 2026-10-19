"""AI layer: private context, read-only tools, strict output contract, fallback and orchestration."""

from core_service.ai.circuit_breaker import BreakerState, CircuitBreaker, CircuitOpenError
from core_service.ai.context_builder import ContextBuilder
from core_service.ai.default_tools import build_default_tools
from core_service.ai.llm_client import (
    AnthropicMessagesClient,
    LLMClient,
    LLMError,
    LLMResponse,
    OpenAICompatibleClient,
    ToolCall,
)
from core_service.ai.orchestrator import AIAnalysisService, AnalysisReport
from core_service.ai.privacy_guard import PrivacyGuard
from core_service.ai.rule_analyzer import RuleBasedAnalyzer
from core_service.ai.schemas import (
    AIFinding,
    AnalysisOutput,
    AnalysisRequest,
    AnalysisResponseValidator,
    Anomaly,
    InvalidAnalysisError,
    RecommendedAction,
)
from core_service.ai.tools import Tool, ToolRegistry

__all__ = [
    "AIAnalysisService",
    "AIFinding",
    "AnalysisOutput",
    "AnalysisReport",
    "AnalysisRequest",
    "AnalysisResponseValidator",
    "Anomaly",
    "AnthropicMessagesClient",
    "OpenAICompatibleClient",
    "BreakerState",
    "CircuitBreaker",
    "CircuitOpenError",
    "ContextBuilder",
    "InvalidAnalysisError",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "PrivacyGuard",
    "RecommendedAction",
    "RuleBasedAnalyzer",
    "Tool",
    "ToolCall",
    "ToolRegistry",
    "build_default_tools",
]
