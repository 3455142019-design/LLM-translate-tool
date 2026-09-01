"""Provider-aware reasoning effort discovery and explicit fallback mapping."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


ALL_EFFORTS: Tuple[str, ...] = (
    "auto", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
)

_RANK = {name: index for index, name in enumerate(ALL_EFFORTS) if name not in {"auto", "none"}}


@dataclass(frozen=True)
class ThinkingResolution:
    requested: str
    actual: str
    supported: Tuple[str, ...]

    @property
    def mapped(self) -> bool:
        return self.requested != self.actual

    @property
    def label(self) -> str:
        if not self.mapped:
            return self.actual
        return f"{self.requested} -> {self.actual}"


def supported_efforts(protocol: str, model: str) -> Tuple[str, ...]:
    normalized_model = model.lower()
    if protocol == "deepseek_chat":
        return ("none", "low", "high", "max")
    if protocol == "openai_responses":
        if "gpt-5" in normalized_model:
            return ("none", "minimal", "low", "medium", "high", "xhigh")
        return ("none", "low", "medium", "high")
    if protocol == "anthropic_messages":
        if any(marker in normalized_model for marker in ("4-6", "4-7", "4-8", "claude-5", "fable", "mythos")):
            return ("auto", "low", "medium", "high", "xhigh", "max")
        return ("none", "high", "max")
    return ("auto",)


def resolve_effort(protocol: str, model: str, requested: str) -> ThinkingResolution:
    requested = requested.lower().strip() or "auto"
    if requested not in ALL_EFFORTS:
        raise ValueError(f"Unsupported reasoning effort: {requested}")
    supported = supported_efforts(protocol, model)
    if requested == "auto":
        return ThinkingResolution(requested, "auto", supported)
    if requested in supported:
        return ThinkingResolution(requested, requested, supported)
    if requested == "none":
        return ThinkingResolution(requested, "auto", supported)
    ranked = [effort for effort in supported if effort in _RANK]
    if not ranked:
        return ThinkingResolution(requested, "auto", supported)
    requested_rank = _RANK[requested]
    lower_or_equal = [effort for effort in ranked if _RANK[effort] <= requested_rank]
    actual = lower_or_equal[-1] if lower_or_equal else ranked[0]
    return ThinkingResolution(requested, actual, supported)
