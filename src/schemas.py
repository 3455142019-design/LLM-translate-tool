# -*- coding: utf-8 -*-
"""schemas.py — 全局数据结构定义。

本模块是项目地基之一，任何模块均可引用；禁止本模块引用其他业务模块。
所有核心对象：Entry（单条文本）、Batch（一批）、Usage（API 用量）、RunMeta（运行元数据）。
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 枚举：条目状态 / TM 状态 / 占位符类别 / 任务模式
# ---------------------------------------------------------------------------
class EntryStatus(str, enum.Enum):
    """ingest 阶段的文本状态分类（避免把优质译文送去全量润色）。"""
    UNTRANSLATED = "untranslated"            # 未翻译（值==键）
    MIXED_LANGUAGE = "mixed_language"        # 中/日混合残留
    HUMAN_TRANSLATION = "human_translation"  # 已确认人工译文
    MACHINE_TRANSLATION = "machine_translation"  # 疑似机翻（旧汉化来源）
    DO_NOT_TRANSLATE = "do_not_translate"    # 不需要翻译（纯数字/符号/命令）
    SCRIPT_OR_CONTROL_DATA = "script_or_control_data"  # 脚本/控制数据
    EMPTY = "empty"                          # 空文本


class TmStatus(str, enum.Enum):
    """翻译记忆状态机。

    自动复用策略见 tm.py：
    human_approved / qa_passed 可复用；machine_unreviewed 仅参考；
    machine_legacy / existing_unknown / rejected 禁止复用。
    """
    HUMAN_APPROVED = "human_approved"
    QA_PASSED = "qa_passed"
    MACHINE_UNREVIEWED = "machine_unreviewed"  # 新 API 译文，未审校
    MACHINE_LEGACY = "machine_legacy"          # 旧机翻
    EXISTING_UNKNOWN = "existing_unknown"      # 来源不明旧译文
    REJECTED = "rejected"


class PlaceholderKind(str, enum.Enum):
    """占位符三分类（protect.py 使用）。"""
    FIXED = "fixed"      # 控制码：顺序/位置严格固定（\C[1] 等）
    MOVABLE = "movable"  # 可移动变量：允许句内重排（%s %d {name}）
    PAIRED = "paired"    # 成对嵌套标记：开闭数量+嵌套校验（<color>...</color>）
    QUOTE = "quote"      # 日式引号符号（「」『』）：必须原样保留


class Mode(str, enum.Enum):
    """任务模式（cli.py 入口对应）。"""
    TRANSLATE = "translate"
    POLISH = "polish"
    REVIEW = "review"
    PIPELINE = "pipeline"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Entry:
    """单条文本条目。id 在批次内唯一（稳定 ID 用于局部修复）。"""
    id: str                            # 批次内唯一 ID，如 "000001_0042"
    key: str                           # MTool 原始键 = 日文原文
    src: str                           # 原文（遮罩前）
    cur: Optional[str] = None          # 现译文（translate 模式为 None；polish 模式必填）
    masked_src: Optional[str] = None   # 遮罩后原文（protect.py 填充）
    masked_cur: Optional[str] = None   # 遮罩后现译文（润色用）
    status: EntryStatus = EntryStatus.UNTRANSLATED
    speaker: Optional[str] = None      # 说话人（occurrence index 提供；未知为 None）
    scene: Optional[str] = None        # 场景/文件来源（如 Mod_Talk/xxx.rb）
    occurrences: int = 1               # 出现次数（occurrence index）
    multi_context: bool = False        # 多语境冲突（multi_context_ambiguous）
    risk_score: float = 0.0            # QA 风险分 0-1（qa/ 模块填充）
    tm_status: Optional[TmStatus] = None  # 翻译记忆命中状态
    blocked: bool = False              # content_filter 拦截标记（保留原文不写空值）
    extra: Dict[str, Any] = dataclasses.field(default_factory=dict)  # 备用扩展字段

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Batch:
    """一批待请求的 Entry 集合。"""
    number: int                        # 批号（从 1 起）
    items: List[Entry]
    subtag: str = ""                   # 子批标记（repair/二分/重试），文件名隔离防冲突
    # 预算（token，估算值；实际以 API usage 为准）
    est_input_tokens: int = 0
    est_output_tokens: int = 0
    # 状态
    status: str = "pending"            # pending/done/failed/blocked
    attempts: int = 0                  # 已尝试次数（重试上限见 engine）

    def item_ids(self) -> List[str]:
        return [it.id for it in self.items]


@dataclasses.dataclass
class Usage:
    """一次 API 调用的 token 明细（来自响应 usage 字段）。"""
    prompt_tokens: int = 0
    prompt_cache_hit_tokens: int = 0   # 缓存命中输入
    prompt_cache_miss_tokens: int = 0  # 缓存未命中输入
    completion_tokens: int = 0         # 输出（含 reasoning）
    reasoning_tokens: int = 0          # 思考 token（completion 子集）
    model: str = ""
    system_fingerprint: Optional[str] = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def cost_cny(self, pricing: Dict[str, float]) -> float:
        """按价格表（元/百万 token）计算本次费用。"""
        return (
            self.prompt_cache_hit_tokens / 1e6 * pricing["in_hit"]
            + self.prompt_cache_miss_tokens / 1e6 * pricing["in_miss"]
            + self.completion_tokens / 1e6 * pricing["out"]
        )


@dataclasses.dataclass
class RunMeta:
    """run 级元数据（manifest.json 内容）。"""
    run_id: str
    mode: str
    model: str
    started_at: str
    inputs: List[str] = dataclasses.field(default_factory=list)
    batch_count: int = 0
    total_usage: Usage = dataclasses.field(default_factory=Usage)
    cost_cny: float = 0.0
    system_fingerprint: Optional[str] = None  # 首次响应指纹，漂移监控基准
    snapshot_hashes: Dict[str, str] = dataclasses.field(default_factory=dict)
    status: str = "running"            # running/paused/done/failed/stopped
    stop_reason: Optional[str] = None
