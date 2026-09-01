# -*- coding: utf-8 -*-
"""qa/terminology.py — 术语一致性检查器。

检查项：
- 术语 source 独立成词出现在原文中，但译文未使用该术语 target
- 译文使用了禁止译法（且该出现位置不处于正确译名 target 内部）

防误报规则（2026-08-16 HOLLOWWALD 接手审计教训）：
1. 短术语（サン/コール/レア/ラヴ/シザー…）常作为复合外来语的一部分出现
   （サンダーブラスト/エーテルコール/バーニングフレア），此时不要求译文含 target。
   判定：source 出现位置两侧紧邻片假名/长音 → 视为复合词内部，跳过该次出现。
2. 禁止译法常是正确译名的子串（禁止 桑德/桑德斯，正确 桑德司；
   禁止 米修，正确 米修帕塔尔）。若 fv 的每次出现都完全落在 target 的出现
   区间内 → 不报；只有独立出现（如「桑德斯」「米修大人」）才报。
"""
from __future__ import annotations

import re
from typing import List, Tuple

from glossary import Glossary
from schemas import Entry

_KATA = re.compile(r"[ァ-ヶー]")  # 片假名（含长音）
# 对话指代词：短人名术语在对话中被"你/您/她/他"指代时属自然译法，不报术语缺失
# （2026-08-16 HOLLOWWALD 审计：サンをよく観察→我一直在观察你 等 7 条误报）
_PRONOMINAL_RE = re.compile(r"(你|您|她|他|我)")
# 只有短人名（<=4 字）才启用指代豁免；长词（如 ミシュパタル）被指代替代仍应报
_PRONOUN_SRC_MAX = 4


def _occurrences(text: str, needle: str) -> List[Tuple[int, int]]:
    """返回 needle 在 text 中所有出现的 (start, end) 区间（非重叠）。"""
    if not needle:
        return []
    out: List[Tuple[int, int]] = []
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return out
        out.append((i, i + len(needle)))
        start = i + 1
    return out


def _standalone(occs: List[Tuple[int, int]], src: str) -> bool:
    """是否存在至少一次出现，两侧不紧邻片假名/长音（独立成词）。"""
    for s, e in occs:
        before = src[s - 1] if s > 0 else ""
        after = src[e] if e < len(src) else ""
        if not (_KATA.match(before) or _KATA.match(after)):
            return True
    return False


def _covered(idx: int, length: int, ranges: List[Tuple[int, int]]) -> bool:
    """出现区间 [idx, idx+length) 是否完全落在某个允许区间内。"""
    end = idx + length
    return any(s <= idx and end <= e for s, e in ranges)


def _merged(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """合并重叠/相邻区间（覆盖判定用并集，防跨术语子串误报）。"""
    if not ranges:
        return []
    ordered = sorted(ranges)
    out = [ordered[0]]
    for s, e in ordered[1:]:
        ps, pe = out[-1]
        if s <= pe:  # 重叠或相邻
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out


class TerminologyChecker:
    """单条术语检查。需要 Glossary 实例提供术语表。"""

    NAME = "terminology"

    def __init__(self, glossary: Glossary):
        self.glossary = glossary

    def check(self, entry: Entry) -> List[str]:
        src = entry.masked_src or entry.src
        zh = entry.masked_cur if entry.masked_cur is not None else (entry.cur or "")
        issues: List[str] = []

        # 允许区间：全部术语 target 在译文中的出现（跨条目取并集）——
        # 例如「桑德」是另一条目正确译名「桑德司」的子串，不应报。
        allowed_ranges = _merged(
            [occ for e2 in self.glossary._entries
             for occ in _occurrences(zh, e2.target)]
        )
        for e in self.glossary._entries:
            src_occs = _occurrences(src, e.source)
            if not src_occs:
                continue
            # 术语在原文中独立成词出现 -> 译文必须用 target
            if _standalone(src_occs, src) and e.target not in zh:
                # 对话指代豁免：短人名被 你/您/她/他/我 指代时不报
                if not (len(e.source) <= _PRONOUN_SRC_MAX
                        and _PRONOMINAL_RE.search(zh)):
                    issues.append(f"术语 [{e.source}→{e.target}] 在原文出现但译文未使用")
            # 禁止译法命中：只有不完全落在允许区间内的出现才报
            for fv in e.forbidden_variants:
                for s, _ in _occurrences(zh, fv):
                    if not _covered(s, len(fv), allowed_ranges):
                        issues.append(f"译文使用了禁止译法: {fv}（应为 {e.target}）")
                        break
        return issues
