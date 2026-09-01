# -*- coding: utf-8 -*-
"""validate.py — 模型输出校验与局部修复。

职责：
1. 解析模型输出为 {id: zh}（处理 ```json 围栏 / 前后杂文 / 空响应）
2. 校验：键数一致（missing/extra）、占位符完整（protect.verify）
3. 生成 ValidationResult；缺失条目由 engine 走 repair 模式局部补译
   （缺 7 条只重发 7 条，不整批重付输出费用）

依赖：schemas/protect；被 engine 引用。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from protect import MaskRecord, Protector
from schemas import Batch, Entry


@dataclass
class ValidationResult:
    """一批输出的校验结果。"""
    ok: bool = False
    parsed: Dict[str, str] = field(default_factory=dict)   # id -> 译文（校验前原始）
    missing: List[str] = field(default_factory=list)       # 缺的 id
    extra: List[str] = field(default_factory=list)         # 多余/未知 id
    placeholder_issues: Dict[str, List[str]] = field(default_factory=dict)  # id -> 问题
    blocked_ids: List[str] = field(default_factory=list)   # content_filter 标记条目
    parse_error: Optional[str] = None
    misplaced: List[str] = field(default_factory=list)     # 疑似错位（遮罩 token 交叉匹配）→ 走 repair
    suspicious: List[str] = field(default_factory=list)    # 弱信号可疑条目（仅记日志，不自动修复）


# 提取 JSON：优先全文解析；失败则找 ```json 围栏或首 { 到末 } 的子串
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
# 部分恢复：完整解析失败时提取所有 "键": "值" 对（容忍键后引号丢失等局部损坏，
# 如模型偶发输出 "71: " 缺闭合引号）。lookahead 要求值闭合后紧跟 , } 或空白，
# 值内含未转义引号被截断的条目（"她说"大家好""）不匹配 -> 落入 missing 走 repair，
# 防止截断译文静默写盘。损坏条目缺失 -> 落入 missing 走 repair 小批补译，
# 避免整批 3 次重试后全废（实测 184 条批次因 1 条格式错误全批失败的教训）。
_SEQ_PAIR_RE = re.compile(r'"([^"\\]+)"\s*:\s*"((?:[^"\\]|\\.)*)"(?=[,}\s])')

# 模板变量（{xxx} 形式）不遮罩，改文本级校验：必须全部保留
_TEMPLATE_VAR_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
# \H 演出标记不遮罩，文本级校验 + 自动恢复
_H_MARK_RE = re.compile(r"\\H")


def check_template_vars(src: str, out: str) -> List[str]:
    """校验输出保留原文全部模板变量（{xxx}）。返回缺失变量列表。"""
    vars_src = set(_TEMPLATE_VAR_RE.findall(src))
    vars_out = set(_TEMPLATE_VAR_RE.findall(out))
    return sorted(vars_src - vars_out)


def restore_h_markers(entry: Entry, zh: str) -> str:
    """\\H 演出标记自动恢复：原文有而译文缺失时补回句尾。"""
    n_src = len(_H_MARK_RE.findall(entry.src))
    n_out = len(_H_MARK_RE.findall(zh))
    for _ in range(n_src - n_out):
        zh += r"\H"
    return zh


def parse_model_output(raw: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """从模型输出中解析 {id: zh} 字典。返回 (解析结果或 None, 错误信息)。"""
    candidates = [raw]
    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        candidates.append(fence.group(1))
    obj = _JSON_OBJECT_RE.search(raw)
    if obj:
        candidates.append(obj.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
                return data, None
        except json.JSONDecodeError:
            continue
    # 完整解析失败：部分恢复。提取全部 "键": "值" 对（跳过损坏条目，由 repair 补译）
    pairs = _SEQ_PAIR_RE.findall(raw)
    if len(pairs) >= 2:
        return dict(pairs), None
    return None, "无法从模型输出中解析 JSON 对象"


def validate_batch_output(raw: str, batch: Batch) -> ValidationResult:
    """完整校验一批输出：解析 -> 键数 -> 占位符。"""
    result = ValidationResult()
    expected_ids = set(batch.item_ids())

    parsed, err = parse_model_output(raw)
    if parsed is None:
        result.parse_error = err
        return result

    # 短序号键（"0"、"1"…）映射回 Entry.id；旧长 id 格式原样通过（兼容兜底）
    parsed = map_seq_keys(parsed, batch.items)
    result.parsed = parsed
    got_ids = set(parsed.keys())
    result.missing = sorted(expected_ids - got_ids)
    result.extra = sorted(got_ids - expected_ids)

    # 占位符校验（逐条：条目原文遮罩记录 vs 模型译文）+ 模板变量校验
    for it in batch.items:
        src_rec, cur_rec = _mask_records_of(it)
        if it.id not in parsed:
            continue
        zh = parsed[it.id]
        # 豁免 cur（现译文）的占位符：polish/review 模式下模型保留原译文的
        # 控制码属正常引用，先恢复为原始文本再按 src 记录校验
        zh = _strip_cur_tokens(zh, cur_rec)
        if src_rec is not None:
            ok, issues = Protector.verify(zh, src_rec)
            if not ok:
                result.placeholder_issues[it.id] = issues
        # 模板变量完整性（不遮罩，文本级校验）
        missing_vars = check_template_vars(it.src, zh)
        if missing_vars:
            result.placeholder_issues.setdefault(it.id, []).append(
                f"模板变量缺失: {missing_vars}")

    # 错位检测：模型自行移位/漏 id 后按序回填的典型产物是「译文与自身原文
    # 无关、却与批内另一条原文高度匹配」（2026-08-10 HOLLOWWALD 正式版实测：
    # 8,792 条错位，最终只能全面重翻）。遮罩 token 是条目独有的强信号，
    # 交叉匹配确定性强，误报率低，可放心送 repair 补译。与占位符校验失败
    # 交叉的条目同样进 repair（repair 内自带占位符复验），避免静默丢译文。
    if not result.parse_error:
        result.misplaced, result.suspicious = _detect_misplaced(parsed, batch)

    result.ok = (not result.missing and not result.extra
                 and not result.placeholder_issues and result.parse_error is None)
    return result


_MASK_TOKEN_RE = re.compile(r"__[FVPQ]\d{4}_[0-9A-F]{4}__")


def _mask_token_set(text: str) -> set:
    """提取文本中的遮罩 token 集合（条目独有控制码，错位检测强信号）。"""
    return set(_MASK_TOKEN_RE.findall(text))


def _detect_misplaced(parsed: Dict[str, str], batch: Batch) -> Tuple[List[str], List[str]]:
    """批内错位检测。

    强信号（misplaced → 走 repair）：
        译文与自身原文的遮罩 token 交集不足一半，但与批内另一条原文的
        token 交集超过一半 —— 该译文其实是另一条条目的译文（平移/串位）。
    弱信号（suspicious → 仅记日志）：
        无遮罩 token 的条目无法用 token 交叉验证，用「与自身原文共享字符
        比例极低 + 长度比异常」标记，交由 full-qa/审查阶段人工确认，
        避免误送 repair 造成成本浪费。

    返回 (misplaced_ids, suspicious_ids)。
    """
    entries = {it.id: it for it in batch.items}
    src_tokens: Dict[str, set] = {}
    for it in batch.items:
        toks = _mask_token_set(it.masked_src or it.src)
        if toks:
            src_tokens[it.id] = toks

    misplaced: List[str] = []
    suspicious: List[str] = []
    for eid, zh in parsed.items():
        it = entries.get(eid)
        if it is None:
            continue
        if eid not in src_tokens:
            # 自身原文无 token：若译文带 token 且与批内某原文 token 匹配 → 强错位
            zh_toks = _mask_token_set(zh)
            if zh_toks:
                for other_id, other_toks in src_tokens.items():
                    if other_id != eid and zh_toks & other_toks:
                        misplaced.append(eid)
                        break
                continue
            # 弱信号：无 token 时用共享字符比例 + 长度比粗筛
            src = it.masked_src or it.src
            if len(src) >= 4:
                set_src, set_zh = set(src), set(zh)
                ratio = (len(set_src & set_zh) / len(set_src)) if set_src else 1.0
                len_ratio = len(zh) / max(len(src), 1)
                if ratio < 0.3 and (len_ratio < 0.2 or len_ratio > 2.5):
                    suspicious.append(eid)
            continue
        # 自身原文有 token：译文 token 与自身交集 < 一半，与另一条原文交集 >= 一半
        own = src_tokens[eid]
        zh_toks = _mask_token_set(zh)
        if not zh_toks or overlap_own_ok(zh_toks, own):
            continue
        for other_id, other_toks in src_tokens.items():
            if other_id == eid:
                continue
            if overlap_own_ok(zh_toks, other_toks):
                misplaced.append(eid)
                break
    return misplaced, suspicious


def overlap_own_ok(zh_toks: set, ref_toks: set) -> bool:
    """译文 token 与参考集合的交集是否达到一半以上（视为匹配）。"""
    return len(zh_toks & ref_toks) * 2 >= len(zh_toks)


def map_seq_keys(parsed: Dict[str, str], items: List[Entry]) -> Dict[str, str]:
    """把模型输出的短序号键（"0"、"1"…）映射回条目 id（按 items 顺序）。

    短序号 = 输入 payload 中的批内序号（0 起），由 engine._build_messages 生成；
    旧长 id 格式（如 "000001_0042"）不在序号映射中，原样通过（兼容兜底）。
    供 validate_batch_output 与 engine._repair_missing 共用，保证映射一致。
    """
    seq_map = {str(i): it.id for i, it in enumerate(items)}
    mapped: Dict[str, str] = {}
    for k, v in parsed.items():
        target = seq_map.get(k)
        if target is None:
            try:
                target = seq_map.get(str(int(k)))
            except (ValueError, TypeError):
                target = None
        mapped[target if target is not None else k] = v
    return mapped


def _mask_records_of(entry: Entry) -> Tuple[Optional[MaskRecord], Optional[MaskRecord]]:
    """取条目的遮罩记录对（src 记录, cur 记录）。

    - mask_record：原文（src）的遮罩记录，校验基准
    - mask_record_cur：现译文（cur）的遮罩记录，polish/review 模式存在，
      模型输出中属于它的占位符应豁免（引用原译文控制码属正常）
    """
    src_rec = entry.extra.get("mask_record")
    cur_rec = entry.extra.get("mask_record_cur")
    return (src_rec if isinstance(src_rec, MaskRecord) else None,
            cur_rec if isinstance(cur_rec, MaskRecord) else None)


def _strip_cur_tokens(text: str, cur_rec: Optional[MaskRecord]) -> str:
    """把文本中属于 cur 遮罩记录的占位符恢复为原始文本（豁免处理）。"""
    if cur_rec is None:
        return text

    def _repl(m: re.Match) -> str:
        tok = m.group(0)
        info = cur_rec.token_map.get(tok)
        return info.original if info else tok

    return re.sub(r"__[FVPQ]\d{4}_[0-9A-F]{4}__", _repl, text)


def _mask_record_of(entry: Entry) -> Optional[MaskRecord]:
    """取条目的 src 遮罩记录（兼容旧调用）。"""
    return _mask_records_of(entry)[0]


def restore_entry_zh(entry: Entry, zh: str) -> str:
    """恢复条目的占位符（模型译文 -> 原始控制码）。未知 token 原样保留。

    polish/review 模式：先豁免 cur 的占位符（模型引用的原译文控制码），
    再按 src 记录恢复；最后补回缺失的 \\H 演出标记。
    """
    src_rec, cur_rec = _mask_records_of(entry)
    zh = _strip_cur_tokens(zh, cur_rec)
    if src_rec is not None:
        zh, _unknown = Protector.restore(zh, src_rec)
    zh = restore_h_markers(entry, zh)
    return zh
