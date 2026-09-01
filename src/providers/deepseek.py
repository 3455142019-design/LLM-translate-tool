# -*- coding: utf-8 -*-
"""providers/deepseek.py — DeepSeek API 封装。

职责：
- 构造请求体（thinking 开关 / reasoning_effort / temperature / response_format）
- 发送请求（requests，非流式）
- 解析响应：content / reasoning_content / usage（含缓存命中/未命中/reasoning）/ system_fingerprint / finish_reason
- 错误分类：ApiError(kind)，kind 见 ApiErrorKind（engine 按 kind 分流重试策略）

关键事实（官方文档确认）：
- 思考模式默认开启；必须显式传 {"thinking": {"type": "disabled"}} 才是非思考
- 思考模式下 temperature/top_p 不生效（不会报错，只是被忽略）
- reasoning token 计入 completion_tokens（按输出价计费）
- 模型名是原地升级的（flash -> 0731），system_fingerprint 用于漂移检测

依赖：config；被 engine 引用。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

import config
from schemas import Usage


class ApiErrorKind:
    """API 错误分类（engine 按类别分流重试策略）。"""
    BAD_REQUEST = "bad_request"          # 400：参数错误，不重试，终止
    AUTH = "auth"                        # 401/403：不重试，终止
    QUOTA = "quota"                      # 402：余额不足，终止整个 run
    RATE_LIMIT = "rate_limit"            # 429：尊重 Retry-After，退避重试
    SERVER = "server"                    # 5xx / insufficient_system_resource：退避重试
    NETWORK_UNSENT = "network_unsent"    # 请求未发送成功：可安全重试
    NETWORK_UNCERTAIN = "network_uncertain"  # 发送后断连：计费状态未知，标记 uncertain
    TIMEOUT = "timeout"                  # 读取超时：计费状态未知
    EMPTY_RESPONSE = "empty_response"    # 模型返回空内容：重试
    JSON_INVALID = "json_invalid"        # 响应非合法 JSON（validate 层报出）
    CLOUDFLARE_BLOCK = "cloudflare_block"  # 403 + Cloudflare 1010 风控（可重试，非 key 失效）


class ApiError(Exception):
    def __init__(self, kind: str, message: str, status_code: Optional[int] = None,
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass
class ApiResponse:
    """一次成功（或部分成功）的 API 响应。"""
    content: str
    reasoning_content: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    system_fingerprint: Optional[str] = None


def _is_cloudflare_block(resp: Optional[requests.Response]) -> bool:
    """判定 403 是否为 Cloudflare 风控（code 1010 / CF 错误页），而非认证失败。

    2026-08-08 实测：默认端点 opencode.ai/zen/go 返回 403 + error code 1010
    （Cloudflare 拦截 urllib 指纹）。此类应退避重试，不能当 AUTH 终止。
    """
    if resp is None or not resp.text:
        return False
    body = resp.text.lower()
    return "1010" in body or "cloudflare" in body or "cf-error-code" in body


class DeepSeekClient:
    """DeepSeek Chat Completions 客户端（requests 直连，无 SDK 依赖）。"""

    protocol = "deepseek_chat"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 base_url: Optional[str] = None, timeout: float = 300.0,
                 pricing: Optional[Dict[str, float]] = None):
        self.api_key = api_key or os.environ.get("SR_TRANSLATE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise ValueError("缺少 API key：设置环境变量 DEEPSEEK_API_KEY 或传入 api_key")
        self.model = model or config.DEFAULT_MODEL
        self.base_url = base_url or config.API_BASE_URL
        self.timeout = timeout
        self.pricing = pricing
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": config.HTTP_USER_AGENT,  # 默认端点走 Cloudflare，需浏览器 UA 防 1010
        })

    def list_models(self) -> List[str]:
        try:
            resp = self._session.get(self.base_url.rstrip("/") + "/models", timeout=self.timeout)
        except requests.exceptions.ConnectionError:
            raise ApiError(ApiErrorKind.NETWORK_UNSENT, "模型列表请求未发送成功")
        except requests.exceptions.Timeout:
            raise ApiError(ApiErrorKind.TIMEOUT, f"模型列表请求超时（{self.timeout}s）")
        except requests.exceptions.RequestException as error:
            raise ApiError(ApiErrorKind.NETWORK_UNCERTAIN, f"模型列表请求失败: {error}")
        if resp.status_code != 200:
            self._raise_http_error(resp.status_code, resp.text[:500], resp)
        try:
            data = resp.json()
        except ValueError:
            raise ApiError(ApiErrorKind.JSON_INVALID, "模型列表响应不是合法 JSON")
        return sorted(str(item["id"]) for item in data.get("data", []) if item.get("id"))

    # ---- 请求 ----
    def chat(self, messages: List[Dict[str, str]],
             thinking: str = "disabled",          # enabled / disabled
             reasoning_effort: Optional[str] = None,  # low/high/max（thinking=enabled 时有效）
             temperature: Optional[float] = None,  # 思考模式下官方忽略
             max_tokens: int = 32_000,
             response_format: Optional[Dict] = None) -> ApiResponse:
        """发送一次 chat 请求。错误以 ApiError 抛出（kind 分类见 ApiErrorKind）。"""
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": thinking},
        }
        if thinking == "enabled" and reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if thinking == "disabled" and temperature is not None:
            body["temperature"] = temperature
        if response_format:
            body["response_format"] = response_format

        try:
            resp = self._session.post(
                self.base_url + config.API_CHAT_ENDPOINT,
                data=json.dumps(body, ensure_ascii=False),
                timeout=self.timeout,
            )
        except requests.exceptions.ConnectionError:
            raise ApiError(ApiErrorKind.NETWORK_UNSENT, "连接失败（请求未发送）")
        except requests.exceptions.Timeout:
            raise ApiError(ApiErrorKind.TIMEOUT, f"请求超时（>{self.timeout}s），计费状态未知")
        except requests.exceptions.ChunkedEncodingError as e:
            # 响应中途断流（如代理/网关中断）：内容可能部分生成，计费状态未知
            raise ApiError(ApiErrorKind.NETWORK_UNCERTAIN,
                           f"响应中断（ChunkedEncodingError）: {e}")
        except requests.exceptions.RequestException as e:
            # 其余网络层异常（连接重置/协议错误等）：计费状态未知，保守处理
            raise ApiError(ApiErrorKind.NETWORK_UNCERTAIN,
                           f"网络异常: {type(e).__name__}: {e}")

        # ---- HTTP 层错误分类 ----
        if resp.status_code != 200:
            self._raise_http_error(resp.status_code, resp.text[:500], resp)

        # ---- 解析 ----
        try:
            data = resp.json()
        except ValueError:
            raise ApiError(ApiErrorKind.JSON_INVALID, "响应非合法 JSON")
        try:
            choice = data["choices"][0]
            msg = choice.get("message", {})
            usage_raw = data.get("usage", {})
            details = usage_raw.get("completion_tokens_details", {}) or {}
        except (KeyError, IndexError) as e:
            raise ApiError(ApiErrorKind.BAD_REQUEST, f"响应结构异常: {e}")

        usage = Usage(
            prompt_tokens=usage_raw.get("prompt_tokens", 0),
            prompt_cache_hit_tokens=usage_raw.get("prompt_cache_hit_tokens", 0),
            prompt_cache_miss_tokens=usage_raw.get("prompt_cache_miss_tokens", 0),
            completion_tokens=usage_raw.get("completion_tokens", 0),
            reasoning_tokens=details.get("reasoning_tokens", 0),
            model=data.get("model", self.model),
            system_fingerprint=data.get("system_fingerprint"),
        )
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        if not content.strip() and not reasoning.strip():
            raise ApiError(ApiErrorKind.EMPTY_RESPONSE, "模型返回空内容")

        return ApiResponse(
            content=content,
            reasoning_content=reasoning,
            usage=usage,
            finish_reason=choice.get("finish_reason", "stop"),
            system_fingerprint=data.get("system_fingerprint"),
        )

    def _raise_http_error(self, status: int, text: str,
                          resp: Optional[requests.Response] = None) -> None:
        if status == 400:
            raise ApiError(ApiErrorKind.BAD_REQUEST, f"400 参数错误: {text}", status)
        if status in (401, 403):
            if status == 403 and _is_cloudflare_block(resp):
                raise ApiError(ApiErrorKind.CLOUDFLARE_BLOCK,
                               f"403 Cloudflare 风控拦截（非 key 失效，退避重试）: {text}", status)
            raise ApiError(ApiErrorKind.AUTH, f"{status} 认证/权限失败: {text}", status)
        if status == 402:
            raise ApiError(ApiErrorKind.QUOTA, f"402 余额不足: {text}", status)
        if status == 429:
            retry_after = None
            if resp is not None:
                try:
                    raw = resp.headers.get("Retry-After")
                    retry_after = float(raw) if raw else None
                except (ValueError, TypeError):
                    retry_after = None
            raise ApiError(ApiErrorKind.RATE_LIMIT, f"429 限流: {text}", status, retry_after)
        if status >= 500:
            raise ApiError(ApiErrorKind.SERVER, f"{status} 服务端错误: {text}", status)
        raise ApiError(ApiErrorKind.BAD_REQUEST, f"HTTP {status}: {text}", status)

    def close(self) -> None:
        self._session.close()
