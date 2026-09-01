# -*- coding: utf-8 -*-
"""config.py — 全局配置（价格表 / 模型白名单 / 模式策略 / 批次限制）。

- 价格表带 pricing_version，官方调价后只需改这里（峰谷定价未实施，peak_pricing=False）
- 模型白名单：deepseek-v4-pro 是预览版，代码层禁止
- MODE_POLICY：思考模式与温度的按模式路由（思考模式下 temperature 官方不生效）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

# ---------------------------------------------------------------------------
# .env 加载（项目根 .env，API key 持久化；不覆盖已有环境变量）
# ---------------------------------------------------------------------------
def load_dotenv(path: str | Path | None = None) -> Path | None:
    """加载项目根 .env 到环境变量（KEY=VALUE 行，支持 # 注释；不覆盖已有变量）。

    返回加载的文件路径；文件不存在返回 None（不是错误，环境变量可来自外部）。
    仅接受 KEY=VALUE 简单格式，不解析引号/export 前缀——.env 约定为纯键值。
    """
    env_path = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return None
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ and value:
            os.environ[key] = value
    return env_path


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
API_BASE_URL = "https://opencode.ai/zen/go/v1"
API_CHAT_ENDPOINT = "/chat/completions"

# 模型白名单（pro 预览版禁止，正式版发布后由用户手动加入）
MODEL_ALLOWLIST = ["deepseek-v4-flash"]
DEFAULT_MODEL = "deepseek-v4-flash"

# ---------------------------------------------------------------------------
# 价格表（元 / 百万 token）。官方：https://api-docs.deepseek.com/zh-cn/quick_start/pricing
# ---------------------------------------------------------------------------
PRICING: Dict[str, Dict] = {
    "version": "2026-08-02",
    "peak_pricing": False,  # 峰谷定价未实施（随 pro 正式版发布），实施后置 True 并更新 in_hit/in_miss/out 为峰值价
    "models": {
        "deepseek-v4-flash": {
            "in_hit": 0.02, "in_miss": 1.0, "out": 2.0,
            "status": "stable",
        },
        "deepseek-v4-pro": {
            "in_hit": 0.025, "in_miss": 3.0, "out": 6.0,
            "status": "preview-disabled",  # 预览版，禁止使用
        },
    },
}

# ---------------------------------------------------------------------------
# 模式策略（思考模式路由）
# ---------------------------------------------------------------------------
# 说明：
# - 思考模式（thinking enabled）下 temperature/top_p 官方不生效，所以 review 档不配温度
# - translate/polish 非思考 + 低温度：控制批次间术语/语气漂移
# - review_hard 仅用于：多检查器同判高风险 / 原译与原文差异大 / 机翻重灾区
MODE_POLICY: Dict[str, Dict] = {
    "translate": {"thinking": "disabled", "temperature": 0.25},
    "polish": {"thinking": "disabled", "temperature": 0.45},
    "review_ambiguous": {"thinking": "enabled", "reasoning_effort": "low"},
    "review_hard": {"thinking": "enabled", "reasoning_effort": "high"},
    "repair": {"thinking": "disabled", "temperature": 0.25},  # 漏项局部补译
}

# ---------------------------------------------------------------------------
# 批次限制（token 驱动，条目数只作软/硬上限）
# ---------------------------------------------------------------------------
BATCH_LIMITS: Dict[str, int] = {
    "target_input_tokens": 16_000,    # 8K-16K 区间取保守上界
    "target_output_tokens": 10_000,
    "soft_max_items": 250,
    "hard_max_items": 600,
}

# ---------------------------------------------------------------------------
# 预算与保护（engine.py 使用）
# 预算与保护（engine.py 使用）
DEFAULT_MAX_COST_CNY = 80.0          # 单 run 预算硬上限，超限自动暂停
DEFAULT_MAX_STAGE_COST_CNY = 25.0    # 单阶段（初翻/润色）预算上限
STOP_ON_CACHE_MISS_RATE = 0.9        # 缓存未命中率超此值暂停（缓存异常失效保护）
MAX_REASONING_RATIO = 0.6            # reasoning/completion 比例上限（思考膨胀保护）
CIRCUIT_BREAKER_LIMIT = 5            # 连续失败批次数，达到即熔断暂停
LOG_MAX_MB = 20.0                    # journal.jsonl 大小上限（MB），超限轮转删最早
# thinking 模式（review）批次收紧：思考生成极慢，批次必须小
REVIEW_BATCH_LIMITS = {
    "target_input_tokens": 8_000,
    "target_output_tokens": 4_000,
    "soft_max_items": 30,
    "hard_max_items": 60,
}
RETRY_POLICY = {
    "max_attempts": 2,               # 可重试错误的最大重试次数
    "backoff_base": 2.0,             # 指数退避基数（秒）
    "backoff_jitter": 0.5,           # 抖动系数
}

# finish_reason -> 处理策略（engine.py 分支）
FINISH_REASON_POLICY = {
    "stop": "validate",                              # 正常，走校验
    "length": "bisect",                              # 截断：自动二分批次
    "content_filter": "bisect_block",                # 内容过滤：二分定位 + blocked 标记
    "insufficient_system_resource": "retry_backoff", # 资源不足：退避重试
    "content_filter_all": "block",                   # 全批被过滤
}

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
PROJECTS_ROOT = Path("projects")      # 项目数据包根目录（bundle --bundle <name> 加载于此）
RUNS_ROOT = "runs"                    # 相对项目根；可用 --out 覆盖
PROMPTS_DIR = "prompts"
SNAPSHOT_DIR = "snapshot"

# ---------------------------------------------------------------------------
# HTTP（opencode go 网关走 Cloudflare，需浏览器 UA 防 1010 拦截）
# ---------------------------------------------------------------------------
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def get_pricing(model: str) -> Dict[str, float]:
    """取模型单价表；不在白名单/预览版直接抛错（代码层禁止）。"""
    if model not in MODEL_ALLOWLIST:
        raise ValueError(
            f"模型 {model} 不在白名单 {MODEL_ALLOWLIST}（pro 预览版禁止使用）"
        )
    p = PRICING["models"].get(model)
    if p is None or p.get("status") != "stable":
        raise ValueError(f"模型 {model} 未配置或非稳定版")
    return {"in_hit": p["in_hit"], "in_miss": p["in_miss"], "out": p["out"]}
