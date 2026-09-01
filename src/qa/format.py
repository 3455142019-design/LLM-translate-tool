# -*- coding: utf-8 -*-
"""qa/format.py — 格式检查器（单条级别）。

检查项（产出 issue 列表，每条 issue 带分数权重）：
- 假名/日文残留（片假名/平假名出现在中文译文里）
- 中文标点异常（日式标点「」『』・、混入、全角半角括号不一致）
- 长度异常（原文长译文极短 / 原文短译文超长）
- 空值 / 空白译文
"""
from __future__ import annotations

import re
from typing import List

from schemas import Entry

_HIRAGANA_RE = re.compile(r"[ぁ-ん]")
_KATAKANA_RE = re.compile(r"[ァ-ヶ]")
# 日式标点：仅检测真正的日文标点（「」『』是引号，模型保留原文引号可接受——
# 2026-08-02 口上 QA 实测 94,833 条误报，放宽）
_JP_PUNCT_RE = re.compile(r"[・｡､･]")
# 中文文本典型长度范围（相对原文字符数）
_MIN_OUT_RATIO = 0.2
_MAX_OUT_RATIO = 2.5

# 添油加醋检测：译文含强程度词，但原文无对应日文程度表达 -> 疑似脑补
# （弱程度词如"好"误报率高，只检测强程度词）
# 2026-08-16 HOLLOWWALD 接手审计修正：
# - 「异常」是状态异常术语（状態異常），不是程度词，移除
# - 「特别/格外」常为 特別/やけに 的直译，移到日文对应表，不再单方面告警
_STRONG_INTENSIFIERS = ["非常", "极其", "超级", "无比",
                        "炽热", "猛烈", "极度", "极为"]
_JA_INTENSIFIERS = ["とても", "とっても", "すご", "すっご", "かなり", "めちゃ", "めっちゃ",
                    "超", "激", "非常に", "極", "大変", "すっかり", "あまりにも", "実に",
                    "よほど", "ひど", "スーパー", "特別", "とてつもな",
                    "やけに", "ことさら", "猛烈", "さぞ", "大歓迎", "大満足",
                    "大真面目", "よく", "ごく", "底抜け", "そんなに", "ガンガン",
                    "しょうがない", "それはそれは", "本当に"]

# 未翻译/日文汉字残留检测阈值：译文与原文的字符重叠比例（见 ingest._char_overlap_ratio）
_MIN_UNTRANSLATED_OVERLAP = 0.6


def _char_overlap_ratio(a: str, b: str) -> float:
    set_a = set(a)
    if not set_a:
        return 0.0
    return len(set_a & set(b)) / len(set_a)


class FormatChecker:
    """单条格式检查。check(entry) -> List[str]（空列表 = 通过）。"""

    NAME = "format"

    def __init__(self, forbidden_words: List[str] | None = None):
        """forbidden_words：项目包 policies.json 的黑名单词（agent 维护），
        译文出现且原文无对应即报"疑似自加词"。"""
        self.forbidden_words = list(forbidden_words or [])

    def check(self, entry: Entry) -> List[str]:
        zh = entry.masked_cur if entry.masked_cur is not None else (entry.cur or "")
        src = entry.masked_src or entry.src
        issues: List[str] = []

        if not zh.strip():
            return ["译文为空"]

        # 1. 假名残留（排除原文本身就是假名短句的情况：短键数字等由 ingest 过滤）
        hira = _HIRAGANA_RE.search(zh)
        kata = _KATAKANA_RE.search(zh)
        if hira or kata:
            frag = (hira or kata).group(0)
            issues.append(f"译文残留日文假名: {frag}")

        # 1b. 未翻译/日文汉字残留：无假名但与原文字符重叠极高（纯汉字日文句漏翻）
        if not (hira or kata) and len(src) >= 2 and len(zh) >= 2:
            overlap = _char_overlap_ratio(zh, src)
            if overlap >= _MIN_UNTRANSLATED_OVERLAP:
                issues.append(f"疑似漏翻/日文汉字残留: 译文与原文重叠 {overlap:.0%}")

        # 2. 日式标点混入
        jp = _JP_PUNCT_RE.findall(zh)
        if jp:
            issues.append(f"译文含日式标点: {''.join(set(jp))[:6]}")

        # 3. 长度异常（占位符已遮罩，原文/译文长度可比）
        len_src = len(src)
        len_zh = len(zh)
        if len_src >= 4:  # 短句（<=3 字符）不做比例判断，误报率高
            ratio = len_zh / len_src
            if ratio < _MIN_OUT_RATIO:
                issues.append(f"译文过短: 原文 {len_src} 字符 -> 译文 {len_zh} 字符 (比 {ratio:.2f})")
            elif ratio > _MAX_OUT_RATIO:
                issues.append(f"译文过长: 原文 {len_src} 字符 -> 译文 {len_zh} 字符 (比 {ratio:.2f})")

        # 4. 添油加醋检测：译文含强程度词但原文无对应日文程度表达
        #    敬体感谢/道歉句（ありがとうございました/申し訳ありませんでした）
        #    译成"非常~"是敬语自然译法，2026-08-16 审计后豁免
        found_int = [w for w in _STRONG_INTENSIFIERS if w in zh]
        has_ja_int = any(w in src for w in _JA_INTENSIFIERS)
        is_polite = ("ありがとう" in src or "申し訳" in src or "すみません" in src)
        if found_int and not has_ja_int and not is_polite:
            ints = "、".join(found_int[:3])
            issues.append(f"疑似添油加醋: 译文含『{ints}』但原文无对应程度表达")

        # 5. 项目包黑名单词（policies.json forbidden_words，agent 维护）：
        #    译文出现且原文无对应日文 -> 疑似模型自加词（色气词等）
        for word in self.forbidden_words:
            if word in zh:
                issues.append(f"疑似自加词(黑名单): {word}")

        return issues
