"""Provider construction and protocol defaults."""
from __future__ import annotations

from typing import Dict, Optional

import config
from providers.anthropic import AnthropicMessagesClient
from providers.deepseek import DeepSeekClient
from providers.openai_responses import OpenAIResponsesClient


PROTOCOLS = {
    "deepseek_chat": "DeepSeek Chat Completions",
    "openai_responses": "OpenAI Responses API",
    "anthropic_messages": "Anthropic Messages API",
}


def default_base_url(protocol: str) -> str:
    if protocol == "deepseek_chat":
        # 默认端点走 opencode go 网关（config.API_BASE_URL）；DeepSeekClient 兜底同源。
        # 2026-08-09 修复：此前硬编码 api.deepseek.com，导致 opencode key 发往官方端点 401。
        return config.API_BASE_URL
    if protocol == "openai_responses":
        return "https://api.openai.com/v1"
    if protocol == "anthropic_messages":
        return "https://api.anthropic.com/v1"
    raise ValueError(f"Unsupported provider protocol: {protocol}")


def make_client(
    protocol: str,
    api_key: Optional[str],
    model: str,
    base_url: str = "",
    pricing: Optional[Dict[str, float]] = None,
):
    kwargs = {"api_key": api_key, "model": model, "base_url": base_url or default_base_url(protocol), "pricing": pricing}
    if protocol == "deepseek_chat":
        return DeepSeekClient(**kwargs)
    if protocol == "openai_responses":
        return OpenAIResponsesClient(**kwargs)
    if protocol == "anthropic_messages":
        return AnthropicMessagesClient(**kwargs)
    raise ValueError(f"Unsupported provider protocol: {protocol}")
