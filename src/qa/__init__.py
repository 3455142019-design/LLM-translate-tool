# -*- coding: utf-8 -*-
"""qa/__init__.py — QA 聚合入口：运行全部检查器，产出 risk_score 排序。

流程：
1. format / terminology / refusal（单条级）
2. style（批次级统计）
3. 汇总每条 risk_score = 命中问题数加权（format 1.0 / terminology 1.5 / style 1.0 / refusal 2.0）
4. 输出 {entry_id: (score, issues)}，engine 按 score 阈值路由：
   - score >= HIGH_RISK_THRESHOLD -> review_hard（thinking high）
   - score >= LOW_RISK_THRESHOLD -> review_ambiguous（thinking low）
   - 其余 -> 正常 polish（非思考）
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from glossary import Glossary
from schemas import Entry

from .format import FormatChecker
from .refusal import RefusalChecker
from .style import StyleChecker
from .terminology import TerminologyChecker

# 风险阈值（冒烟测试阶段校准）
LOW_RISK_THRESHOLD = 1.0   # score >= 1 进 thinking low 审校
HIGH_RISK_THRESHOLD = 2.5  # score >= 2.5 进 thinking high 审校
# 各检查器权重
WEIGHTS = {"format": 1.0, "terminology": 1.5, "style": 1.0, "refusal": 2.0}


def run_qa(items: List[Entry], glossary: Glossary,
           forbidden_words: List[str] | None = None) -> Dict[str, Tuple[float, List[str]]]:
    """运行全部检查器，返回 {entry_id: (risk_score, issues)}。

    forbidden_words：项目包 policies.json 的黑名单词（P1-3），格式检查器读取。
    """
    results: Dict[str, Tuple[float, List[str]]] = {it.id: (0.0, []) for it in items}

    # 单条级
    fmt = FormatChecker(forbidden_words)
    term = TerminologyChecker(glossary)
    ref = RefusalChecker()
    for it in items:
        issues: List[str] = []
        for name, checker, weight in (("format", fmt, WEIGHTS["format"]),
                                      ("terminology", term, WEIGHTS["terminology"]),
                                      ("refusal", ref, WEIGHTS["refusal"])):
            found = checker.check(it)
            issues.extend(found)
            it.extra.setdefault("qa_issues", {})[name] = found
        score = sum(WEIGHTS[n] for n, found in it.extra["qa_issues"].items() for _ in found)
        it.risk_score = score
        results[it.id] = (score, issues)

    # 批次级（漂移检测限定术语表专名，避免普通外来词误报）
    style = StyleChecker(glossary_sources=[e.source for e in glossary._entries])
    style_map = style.check_batch(items)
    for eid, s_issues in style_map.items():
        if s_issues:
            it = next(x for x in items if x.id == eid)
            it.extra.setdefault("qa_issues", {})["style"] = s_issues
            it.risk_score += WEIGHTS["style"] * len(s_issues)
            results[eid] = (it.risk_score, results[eid][1] + s_issues)

    return results
