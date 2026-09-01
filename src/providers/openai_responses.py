"""OpenAI Responses API provider adapter."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests

import config
from providers.deepseek import ApiError, ApiErrorKind, ApiResponse
from schemas import Usage


class OpenAIResponsesClient:
    protocol = "openai_responses"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 300.0,
        pricing: Optional[Dict[str, float]] = None,
    ):
        self.api_key = api_key or os.environ.get("SR_TRANSLATE_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise ValueError("Missing API key: set OPENAI_API_KEY or provide api_key")
        if not model:
            raise ValueError("A model is required for the Responses API")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.pricing = pricing
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {self.api_key}",
                                      "Content-Type": "application/json",
                                      "User-Agent": config.HTTP_USER_AGENT})

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
        body: Dict[str, Any] = {
            "model": self.model,
            "input": messages,
            "max_output_tokens": max_tokens,
        }
        if thinking == "enabled" and reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        if temperature is not None:
            body["temperature"] = temperature
        if response_format and response_format.get("type") == "json_object":
            body["text"] = {"format": {"type": "json_object"}}
        data = self._post_json("responses", body)
        content = self._extract_text(data)
        if not content.strip():
            raise ApiError(ApiErrorKind.EMPTY_RESPONSE, "Responses API returned no text output")
        usage_raw = data.get("usage", {}) or {}
        input_details = usage_raw.get("input_tokens_details", {}) or {}
        output_details = usage_raw.get("output_tokens_details", {}) or {}
        prompt_tokens = int(usage_raw.get("input_tokens", 0) or 0)
        cache_hit = int(input_details.get("cached_tokens", 0) or 0)
        usage = Usage(
            prompt_tokens=prompt_tokens,
            prompt_cache_hit_tokens=cache_hit,
            prompt_cache_miss_tokens=max(0, prompt_tokens - cache_hit),
            completion_tokens=int(usage_raw.get("output_tokens", 0) or 0),
            reasoning_tokens=int(output_details.get("reasoning_tokens", 0) or 0),
            model=data.get("model", self.model),
        )
        status = data.get("status", "completed")
        finish_reason = "length" if status == "incomplete" else "stop"
        return ApiResponse(content=content, usage=usage, finish_reason=finish_reason)

    @staticmethod
    def _extract_text(data: Dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        parts: List[str] = []
        for output in data.get("output", []) or []:
            for content in output.get("content", []) or []:
                if content.get("type") in {"output_text", "text"}:
                    text = content.get("text", "")
                    if isinstance(text, str):
                        parts.append(text)
        return "".join(parts)

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
        if response.status_code == 402:
            raise ApiError(ApiErrorKind.QUOTA, f"Quota exhausted: {text}", response.status_code)
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
