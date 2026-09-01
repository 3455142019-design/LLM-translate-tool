"""Shared provider protocol for the translation engine."""
from __future__ import annotations

from typing import Dict, List, Optional, Protocol


class TranslationProvider(Protocol):
    model: str
    protocol: str
    pricing: Optional[Dict[str, float]]

    def chat(
        self,
        messages: List[Dict[str, str]],
        thinking: str = "disabled",
        reasoning_effort: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 32_000,
        response_format: Optional[Dict] = None,
    ) -> object:
        ...

    def list_models(self) -> List[str]:
        ...
