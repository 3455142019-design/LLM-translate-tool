"""Anthropic Messages API provider adapter."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests

import config
from providers.deepseek import ApiError, ApiErrorKind, ApiResponse
from schemas import Usage


class AnthropicMessagesClient:
    protocol = "anthropic_messages"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "",
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 300.0,
        pricing: Optional[Dict[str, float]] = None,
    ):
        self.api_key = api_key or os.environ.get("SR_TRANSLATE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ValueError("Missing API key: set ANTHROPIC_API_KEY or provide api_key")
        if not model:
            raise ValueError("A model is required for the Anthropic Messages API")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.pricing = pricing
        self._session = requests.Session()
        self._session.headers.update({
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": config.HTTP_USER_AGENT,
        })

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def list_models(self) -> List[str]:
        data = self._get_json("models")
        return sorted(str(item["id"]) for item in data.get("data", []) if item.get("id"))

    def chat(
        self,
        messages: List[Dict[str, str]],
        thinking: str = "disabled",
        reasoning_effort: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 32_000,
        response_format: Optional[Dict] = None,
    ) -> ApiResponse:
        system_parts = [message.get("content", "") for message in messages if message.get("role") == "system"]
        converted_messages = [message for message in messages if message.get("role") != "system"]
        body: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": converted_messages,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if thinking == "enabled":
            if self._uses_adaptive_thinking():
                body["thinking"] = {"type": "adaptive"}
                if reasoning_effort:
                    body["output_config"] = {"effort": reasoning_effort}
            else:
                budget = 32_000 if reasoning_effort in {"max", "ultra"} else 16_000
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif not self._uses_adaptive_thinking():
            body["thinking"] = {"type": "disabled"}
        if temperature is not None and thinking != "enabled":
            body["temperature"] = temperature
        data = self._post_json("messages", body)
        content, reasoning = self._extract_content(data)
        if not content.strip():
            raise ApiError(ApiErrorKind.EMPTY_RESPONSE, "Anthropic Messages API returned no text output")
        usage_raw = data.get("usage", {}) or {}
        prompt_tokens = int(usage_raw.get("input_tokens", 0) or 0)
        cache_hit = int(usage_raw.get("cache_read_input_tokens", 0) or 0)
        cache_create = int(usage_raw.get("cache_creation_input_tokens", 0) or 0)
        # Anthropic 标准 usage 不拆分 thinking token（计入 output_tokens）。
        # 用字符数估算 reasoning_tokens，使 engine 的思考膨胀监控
        # （MAX_REASONING_RATIO）对 Anthropic 协议同样生效（2026-08-18 修复）。
        reasoning_estimate = max(0, len(reasoning) // 2)
        usage = Usage(
            prompt_tokens=prompt_tokens,
            prompt_cache_hit_tokens=cache_hit,
            prompt_cache_miss_tokens=max(0, prompt_tokens - cache_hit) + cache_create,
            completion_tokens=int(usage_raw.get("output_tokens", 0) or 0),
            reasoning_tokens=reasoning_estimate,
            model=self.model,
        )
        stop_reason = data.get("stop_reason", "end_turn")
        finish_reason = "length" if stop_reason in {"max_tokens", "model_context_window_exceeded"} else "stop"
        return ApiResponse(content=content, reasoning_content=reasoning, usage=usage, finish_reason=finish_reason)

    def _uses_adaptive_thinking(self) -> bool:
        model = self.model.lower()
        return any(marker in model for marker in ("4-6", "4-7", "4-8", "claude-5", "fable", "mythos"))

    @staticmethod
    def _extract_content(data: Dict[str, Any]) -> tuple[str, str]:
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        for block in data.get("content", []) or []:
            if block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif block.get("type") == "thinking":
                reasoning_parts.append(str(block.get("thinking", "")))
        return "".join(text_parts), "\n".join(reasoning_parts)

    def _get_json(self, path: str) -> Dict[str, Any]:
        try:
            response = self._session.get(self._url(path), timeout=self.timeout)
        except requests.exceptions.ConnectionError:
            raise ApiError(ApiErrorKind.NETWORK_UNSENT, "Connection failed before request delivery")
        except requests.exceptions.Timeout:
            raise ApiError(ApiErrorKind.TIMEOUT, f"Request timed out after {self.timeout}s")
        except requests.exceptions.RequestException as error:
            raise ApiError(ApiErrorKind.NETWORK_UNCERTAIN, f"Request failed: {error}")
        self._ensure_success(response)
        try:
            return response.json()
        except ValueError:
            raise ApiError(ApiErrorKind.JSON_INVALID, "Provider returned invalid JSON")

    def _post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = self._session.post(self._url(path), data=json.dumps(body, ensure_ascii=False), timeout=self.timeout)
        except requests.exceptions.ConnectionError:
            raise ApiError(ApiErrorKind.NETWORK_UNSENT, "Connection failed before request delivery")
        except requests.exceptions.Timeout:
            raise ApiError(ApiErrorKind.TIMEOUT, f"Request timed out after {self.timeout}s")
        except requests.exceptions.ChunkedEncodingError as error:
            raise ApiError(ApiErrorKind.NETWORK_UNCERTAIN, f"Response stream interrupted: {error}")
        except requests.exceptions.RequestException as error:
            raise ApiError(ApiErrorKind.NETWORK_UNCERTAIN, f"Request failed: {error}")
        self._ensure_success(response)
        try:
            return response.json()
        except ValueError:
            raise ApiError(ApiErrorKind.JSON_INVALID, "Provider returned invalid JSON")

    @staticmethod
    def _ensure_success(response: requests.Response) -> None:
        if response.status_code == 200:
            return
        text = response.text[:500]
        if response.status_code in (401, 403):
            raise ApiError(ApiErrorKind.AUTH, f"Authentication failed: {text}", response.status_code)
        if response.status_code == 429:
            raw = response.headers.get("Retry-After")
            try:
                retry_after = float(raw) if raw else None
            except ValueError:
                retry_after = None
            raise ApiError(ApiErrorKind.RATE_LIMIT, f"Rate limited: {text}", response.status_code, retry_after)
        if response.status_code >= 500:
            raise ApiError(ApiErrorKind.SERVER, f"Provider server error: {text}", response.status_code)
        raise ApiError(ApiErrorKind.BAD_REQUEST, f"HTTP {response.status_code}: {text}", response.status_code)

    def close(self) -> None:
        self._session.close()
