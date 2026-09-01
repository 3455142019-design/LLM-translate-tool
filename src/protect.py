# -*- coding: utf-8 -*-
"""protect.py — 占位符三分类遮罩 / 恢复 / 校验（控制码保护层）。

背景：游戏文本含大量控制码（\\C[1] 颜色、\\N[2] 名字、%s 变量等），模型在翻译
过程中可能误改、漏掉或重排它们。本模块在发送给模型前把控制码替换成唯一
占位符 ID（如 __F0001_A1B2__），模型返回后再恢复，并严格校验。

三类占位符（规则不同，见 PlaceholderKind）：
- FIXED   : 演出/格式控制码，位置顺序严格固定（\\C[n] \\N[n] \\I[n] \\H 等）
- MOVABLE : 数值/文本变量，允许句内重排（%s %d {name} ${x} \\V[n]）
- PAIRED  : 成对嵌套标记，开闭数量+嵌套校验（<color=...>...</color>）

用法：
    rec = Protector.mask(src_text)          # -> (masked_text, record)
    out = Protector.restore(model_text, rec)  # 恢复占位符
    ok, issues = Protector.verify(model_text, rec)  # 校验集合/数量/顺序

依赖：schemas.PlaceholderKind；被 engine/validate 引用。
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from schemas import PlaceholderKind


@dataclass
class PlaceholderInfo:
    """一个被遮罩的占位符的完整记录。"""
    kind: PlaceholderKind
    original: str          # 原始控制码文本
    token: str             # 遮罩后的唯一 ID（__F0001_A1B2__ 形式）
    position: int          # 在原文中的出现顺序（从 0 起，同类内计数）
    text_pos: int = 0      # 在原文文本中的字符位置（mask 后按此排序，保证
                           # record.items 与文本出现顺序一致——校验顺序依赖它）


@dataclass
class MaskRecord:
    """一次 mask() 的结果：原文 -> 遮罩文本 的完整映射。"""
    text: str                                    # 遮罩后文本
    items: List[PlaceholderInfo] = field(default_factory=list)
    token_map: Dict[str, PlaceholderInfo] = field(default_factory=dict)  # token -> info

    def tokens(self) -> List[str]:
        """按原文出现顺序返回全部 token 列表。"""
        return [it.token for it in self.items]

    def tokens_of(self, kind: PlaceholderKind) -> List[str]:
        return [it.token for it in self.items if it.kind == kind]


class Protector:
    """占位符遮罩/恢复/校验器（无状态，纯函数式设计）。"""

    # ---- 正则表（按类别分组；如需扩展新增控制码，改这里即可） ----
    # FIXED: 仅带参数的 \\X[n] 控制码（\\C[2] \\N[2] \\V[12]）。
    # 单字符标记（\\H 演出、\\n 换行）不遮罩——实验证实模型对原文保留率 100%，
    # 且 \\n 是换行转义，遮罩会导致误拦（2026-08-02 实测 217 条误拦）。
    # {xxx} 模板变量与「」引号同样不遮罩（模型原生处理良好）。
    _RE_FIXED = re.compile(r"\\[A-Za-z]\[[^\]]*\]")
    # MOVABLE: %s/%d/%1$s、${name}（{xxx} 模板变量不遮罩——模型原生保留）
    _RE_MOVABLE = re.compile(r"%(?:[0-9]+\$)?[sdifxX]|\$\{[a-zA-Z_][a-zA-Z0-9_]*\}")
    # PAIRED: <tag ...>...</tag> 成对标签（不区分大小写，允许任意属性如 =red）
    _RE_PAIRED = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)[^<>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
    # 引号 / \\H / \\n / {xxx} 不遮罩（模型原生处理良好），模板变量与 \\H 由 validate.py 文本级校验

    # 排除项：上述正则可能误伤的文本（\H 演出标记 / \n 换行转义，不遮罩）
    _EXCLUDE_PATTERNS: List[re.Pattern] = [re.compile(r"\\H"), re.compile(r"\\n")]

    # 遮罩 ID 生成：__{类别字母}{序号}_{随机4位HEX}__
    _TOKEN_FMT = "__{letter}{seq:04d}_{rand}__"
    _LETTER = {PlaceholderKind.FIXED: "F", PlaceholderKind.MOVABLE: "V",
               PlaceholderKind.PAIRED: "P", PlaceholderKind.QUOTE: "Q"}

    # ---- 遮罩 ----
    @classmethod
    def mask(cls, text: str) -> MaskRecord:
        """把文本中所有控制码/变量替换为唯一占位符 ID。

        顺序保证：同一原文中，同类别占位符按出现顺序编号（FIXED/MOVABLE
        各自独立计数），恢复时按 token 精确还原，不依赖位置猜测。
        """
        record = MaskRecord(text=text)
        counters: Dict[PlaceholderKind, int] = {}
        rand_suffix = secrets.token_hex(2).upper()  # 每批一个随机后缀，防模型猜 token

        def _replace(m: re.Match, kind: PlaceholderKind) -> str:
            original = m.group(0)
            # 排除项检查（若命中则不遮罩，原样返回）
            for ex in cls._EXCLUDE_PATTERNS:
                if ex.fullmatch(original):
                    return original
            seq = counters.get(kind, 0)
            counters[kind] = seq + 1
            letter = cls._LETTER[kind]
            token = cls._TOKEN_FMT.format(letter=letter, seq=seq, rand=rand_suffix)
            record.items.append(PlaceholderInfo(kind=kind, original=original,
                                                token=token, position=seq,
                                                text_pos=m.start()))
            record.token_map[token] = record.items[-1]
            return token

        # 遮罩顺序：先 PAIRED（成对整体替换，防内部变量被拆），再 MOVABLE，
        # 再 FIXED（控制码）。引号/\\H/{xxx} 不遮罩（模型原生处理良好）。
        text = cls._RE_PAIRED.sub(lambda m: _replace(m, PlaceholderKind.PAIRED), text)
        text = cls._RE_MOVABLE.sub(lambda m: _replace(m, PlaceholderKind.MOVABLE), text)
        text = cls._RE_FIXED.sub(lambda m: _replace(m, PlaceholderKind.FIXED), text)
        # 关键：按文本出现位置排序（替换顺序 ≠ 文本顺序，如 \H 在句尾但
        # FIXED 先于 QUOTE 替换导致 record 顺序错位，校验顺序会误判）
        record.items.sort(key=lambda x: x.text_pos)
        record.text = text
        return record

    # ---- 恢复 ----
    @classmethod
    def restore(cls, text: str, record: MaskRecord) -> str:
        """把模型输出中的占位符 ID 替换回原始控制码。

        未知 token（模型臆造）保持原样并计入 issues 由 verify 报告。
        引号容错：模型可能把 __Q*__ 自行还原为「」符号，先归一化再恢复。
        返回 (恢复文本, 未知token列表)。
        """
        text, _ = cls._normalize_quotes(text, record)
        unknown: List[str] = []

        def _repl(m: re.Match) -> str:
            token = m.group(0)
            info = record.token_map.get(token)
            if info is None:
                unknown.append(token)
                return token
            return info.original

        out = re.sub(r"__[FVPQ]\d{4}_[0-9A-F]{4}__", _repl, text)
        # \H 自动恢复：模型丢弃的 \H 演出标记补回句尾（口上文本 \H 通常在句尾）。
        # 用替换前的 text 判断占位符是否被模型保留（out 已恢复，不能查 out）
        for tok in record.tokens_of(PlaceholderKind.FIXED):
            info = record.token_map[tok]
            if info.original == r"\H" and tok not in text:
                out = out + r"\H"
        return out, unknown

    # ---- QUOTE 归一化（引号容错） ----
    @classmethod
    def _normalize_quotes(cls, text: str, record: MaskRecord) -> Tuple[str, List[str]]:
        """把模型输出中的引号符号按出现顺序归一化为 QUOTE 占位符。

        背景（用户实测）：think 模式下模型可能把 __Q*__ 还原为「」，
        或按中文习惯本地化为“”——只要引号存在且数量一致，语义即保留。
        先归一化再走标准校验/恢复流程。

        孤立引号豁免：原文引号数为奇数（如只有「没有」——游戏文本跨条目
        分段导致），不要求模型保留（中文孤立引号无意义，强校验会误拦截
        大量正常条目）。此时不归一化、不报数量问题。
        返回 (归一化文本, 问题列表)。
        """
        quotes = record.tokens_of(PlaceholderKind.QUOTE)
        if not quotes:
            return text, []
        if len(quotes) % 2 == 1:
            # 孤立引号（跨条目分段）：豁免
            return text, []
        kept = re.findall(r"__Q\d{4}_[0-9A-F]{4}__", text)
        remaining = [t for t in quotes if t not in kept]
        # 合法引号形态：日式「」『』 + 中文“”‘’
        raw_quotes = re.findall(r"[「」『』“”‘’]", text)
        if len(raw_quotes) != len(remaining):
            return text, [f"引号数量不符: 原文 {len(quotes)} 输出 "
                          f"{len(kept)}占位符+{len(raw_quotes)}符号"]
        it = iter(remaining)
        norm = re.sub(r"[「」『』“”‘’]", lambda m: next(it), text)
        return norm, []

    # ---- 校验 ----
    @classmethod
    def verify(cls, model_text: str, record: MaskRecord) -> Tuple[bool, List[str]]:
        """校验模型输出中的占位符是否完整/正确。

        规则：
        - 集合一致：输出 token 集合 == 原文 token 集合（不允许多/少/臆造）
        - FIXED：出现顺序必须与原文一致
        - MOVABLE：允许重排（只查集合与数量）
        - PAIRED：开闭配对数量一致（由正则整体替换天然保证，集合一致即通过）
        - QUOTE：先归一化（容错模型自行还原的「」符号）再按 FIXED 语义校验；
          孤立引号（原文奇数个，跨条目分段）豁免
        返回 (是否通过, 问题列表)。
        """
        issues: List[str] = []
        model_text, q_issues = cls._normalize_quotes(model_text, record)
        issues.extend(q_issues)
        out_tokens = re.findall(r"__[FVPQ]\d{4}_[0-9A-F]{4}__", model_text)
        expected = record.tokens()
        # 孤立引号豁免：原文 QUOTE 数为奇数时，QUOTE 占位符不参与校验
        quotes = record.tokens_of(PlaceholderKind.QUOTE)
        if quotes and len(quotes) % 2 == 1:
            expected = [t for t in expected if t not in quotes]
        expected_set = set(expected)
        out_set = set(out_tokens)

        # 1. 集合一致
        missing = expected_set - out_set
        extra = out_set - expected_set
        # \H 演出标记豁免：口上专用句尾标记，模型频繁丢失。若缺失的占位符
        # 是 \H（无参数单字符演出标记），不拦截（restore 阶段自动补回句尾）
        h_missing = [t for t in missing
                     if record.token_map[t].kind == PlaceholderKind.FIXED
                     and record.token_map[t].original == r"\H"]
        missing = [t for t in missing if t not in h_missing]
        if missing:
            issues.append(f"缺失占位符 {len(missing)} 个: {sorted(missing)[:5]}...")
        if extra:
            issues.append(f"多余/未知占位符 {len(extra)} 个: {sorted(extra)[:5]}...")

        # 2. 数量一致（集合相同但出现次数不同）
        from collections import Counter
        c_expected, c_out = Counter(expected), Counter(out_tokens)
        for tok, n in c_expected.items():
            if c_out.get(tok, 0) != n:
                # \H 豁免：模型完全丢弃时数量不符也不拦截
                if record.token_map[tok].original == r"\H" and c_out.get(tok, 0) == 0:
                    continue
                issues.append(f"占位符 {tok} 数量不符: 原文 {n} 输出 {c_out.get(tok, 0)}")

        # 3. FIXED/QUOTE 顺序一致（按原文顺序过滤后与输出顺序比较）
        fixed_expected = [t for t in expected
                          if record.token_map[t].kind in (PlaceholderKind.FIXED, PlaceholderKind.QUOTE)]
        fixed_out = [t for t in out_tokens if t in expected_set
                     and record.token_map[t].kind in (PlaceholderKind.FIXED, PlaceholderKind.QUOTE)]
        if fixed_expected != fixed_out:
            issues.append(f"固定控制码/引号顺序被改动 (原文 {fixed_expected[:4]}... vs 输出 {fixed_out[:4]}...)")

        return (len(issues) == 0), issues
