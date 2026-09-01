# -*- coding: utf-8 -*-
"""qa/refusal.py — 成人内容拒绝/净化检测（SR 为成人游戏，必须防模型拒答）。

DeepSeek 对 R18 文本可能返回：
- finish_reason=content_filter（部分内容被过滤省略）——engine 层二分定位
- 即使 finish_reason=stop，模型也可能输出道歉/政策说明/淡化译文

本模块检测特征（单条级别）：
- 原文明显有内容，译文极短（拒绝式回答特征）
- 道歉/政策句（"抱歉"+"无法"+"协助"+"不适合"等组合）
- 多条不同原文得到相同泛化译文（在 style.py 已覆盖泛化检测，这里聚焦单条）
- 露骨语义淡化：原文含成人词（可配置词表）而译文缺失对应表达

检测命中 -> entry.blocked=True，不自动合入终稿。
"""
from __future__ import annotations

import re
from typing import List

from schemas import Entry

# 拒绝句特征：必须"道歉/无能"与"拒绝动作"两组词同时出现。
# 2026-08-16 HOLLOWWALD 审计教训：旧单组模式 (不能|无法).{0,6}(回答|帮助)
# 把大量普通游戏对话误报为拒绝（"返事、できないか。"→"不能回答吗。"、
# "労働か……\n僕に答えられるかな。"→"我能不能回答得上呢。"等问句），
# 导致 QA 路由把正常台词送进审校浪费预算。新规则：
#   组A（道歉/申明无力）+ 组B（拒绝提供），或 组C（政策声明）+ 组D（内容指代），
# 且明确是游戏内问句（句尾 ？/かな/吗 的问句）直接放行。
_APOLOGY_RE = re.compile(r"(抱歉|对不起|申し?訳|ごめん|遗憾|没法|无法|不能)")
_REFUSE_ACT_RE = re.compile(r"(翻译|提供|生成|协助|处理)")
_POLICY_RE = re.compile(r"(政策|规定|安全准则|使用条款|伦理|道德|内容限制|不适合|不允许|无法提供)")
_CONTENT_RE = re.compile(r"(这段|该|此|这个|这种|内容|文本|对话|请求|话题)")
_QUESTION_RE = re.compile(r"[？?]$|吗[。！!？?]?$|かな[。！!]?$|でしょう[。！!]?$|だろう[。！!]?$")
_TOPIC_RE = re.compile(r"(这|此|方面|种|些|您|你|该|以上|那|问题|内容|文本|对话)")


def _looks_like_refusal(zh: str) -> bool:
    """判定译文是否像模型拒答/政策回复。

    2026-08-16 校准：要求"无力申明+拒绝动作"组合，且拒绝动作后跟内容指代，
    或"无法/没法+翻译/提供/生成"强组合；游戏内反问（能不能/能否）与
    原因从句（还不能）放行，普通问句放行。
    """
    if _QUESTION_RE.search(zh):
        return False
    if "能不能" in zh or "能否" in zh or "还不能" in zh:
        return False
    m_ap = _APOLOGY_RE.search(zh)
    m_act = _REFUSE_ACT_RE.search(zh)
    if m_ap and m_act:
        after = zh[m_act.end():m_act.end() + 6]
        if _TOPIC_RE.search(after):
            return True
        if m_ap.group(0) in ("无法", "没法", "抱歉", "对不起", "遗憾") \
                and m_act.group(0) in ("翻译", "提供", "生成"):
            return True
    if _POLICY_RE.search(zh) and _CONTENT_RE.search(zh):
        return True
    return False
# 成人内容词（原文侧检测用，可按游戏调整；出现即原文有实质内容）
_EXPLICIT_TERMS = ["セックス", "性器", "射精", "挿入", "フェラ", "乳", "子宮",
                   "処女", "勃起", "精液", "膣", "クリトリス", "陵辱"]
# 译文过短阈值（原文>=10 字符时）
_MIN_ZH_RATIO = 0.15


class RefusalChecker:
    """拒绝/净化检测。check(entry) -> List[str]"""

    NAME = "refusal"

    def __init__(self, explicit_terms: List[str] | None = None):
        self.explicit_terms = explicit_terms or _EXPLICIT_TERMS

    def check(self, entry: Entry) -> List[str]:
        src = entry.masked_src or entry.src
        zh = entry.masked_cur if entry.masked_cur is not None else (entry.cur or "")
        issues: List[str] = []

        # 1. 拒绝句特征（双组词同时出现；问句放行）
        if _looks_like_refusal(zh):
            issues.append(f"疑似拒绝/政策回复: {zh[:40]}...")

        # 2. 原文有实质成人内容，译文过短（淡化/跳过）
        has_explicit = any(t in src for t in self.explicit_terms)
        if has_explicit and len(src) >= 10:
            if len(zh) < len(src) * _MIN_ZH_RATIO:
                issues.append(f"原文含成人内容但译文过短（疑似被净化）: 原文 {len(src)} 字符 -> 译文 {len(zh)} 字符")

        # 3. 成人词缺失检查（原文成人词 -> 译文应保留语义；只提示不硬报，避免误杀）
        if has_explicit and not zh.strip():
            issues.append("原文含成人内容但译文为空")

        return issues
