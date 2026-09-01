# -*- coding: utf-8 -*-
"""ingest.py — 输入读取与文本状态分类。

职责：
1. read_mtool_json：读取 MTool ManualTransFile.json，用 object_pairs_hook
   检测重复键（普通 json.load 遇重复键静默丢前面的值，必须显式报告）
2. classify：按值内容分类（untranslated/mixed_language/human_translation/
   machine_translation/do_not_translate/script_or_control_data/empty）
   —— 防止把优质人工译文送去全量润色，也防止把纯数字键当翻译对象
3. 支持标记来源（旧汉化 SR1028 -> machine_translation，新翻 -> 待定）

依赖：schemas；被 cli 引用。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from schemas import Entry, EntryStatus


def _has_kana(s: str) -> bool:
    return any(0x3040 <= ord(c) <= 0x30FF for c in s)


def _has_kanji(s: str) -> bool:
    return any(0x4E00 <= ord(c) <= 0x9FFF for c in s)


def _is_pure_digit_or_symbol(s: str) -> bool:
    """纯数字/标点/空白（游戏内部值，不需要翻译）。"""
    return all(not c.isalpha() for c in s) and bool(s.strip())


def _char_overlap_ratio(a: str, b: str) -> float:
    """a 中多大比例字符也出现在 b（字符级重叠，检测日文汉字残留漏翻）。

    日文汉字与中文汉字同一 Unicode 区间，仅凭字符集无法区分；但未翻译的
    日文汉字句与原文的字符重叠会远高于正常中文译文（繁简/助词差异拉低重叠）。
    """
    set_a = set(a)
    if not set_a:
        return 0.0
    return len(set_a & set(b)) / len(set_a)


def read_mtool_json(path: Path) -> Tuple[Dict[str, str], List[str]]:
    """读取 MTool 翻译表 JSON。

    返回 (键值对字典, 重复键列表)。重复键时保留最后一个值（与 MTool 行为一致），
    但显式报告重复，由调用方决定是否告警。
    """
    duplicates: List[str] = []
    data: Dict[str, str] = {}

    def _hook(pairs):
        for k, v in pairs:
            if k in data:
                duplicates.append(k)
            data[k] = v
        return data

    raw = path.read_text(encoding="utf-8")
    json.loads(raw, object_pairs_hook=_hook)
    return data, duplicates


def classify_value(key: str, value: str, source_tag: Optional[str] = None) -> EntryStatus:
    """按键值内容分类单条文本。"""
    if not key:
        return EntryStatus.EMPTY
    if not value or not value.strip():
        return EntryStatus.EMPTY
    if not _has_kana(key) and not _has_kanji(key):
        # 键无日文内容：纯数字/ASCII 键 = 脚本数据；其余看值
        return EntryStatus.SCRIPT_OR_CONTROL_DATA
    if value == key:
        return EntryStatus.UNTRANSLATED
    if _is_pure_digit_or_symbol(value):
        return EntryStatus.DO_NOT_TRANSLATE
    if _has_kana(value) or any(0x30A0 <= ord(c) <= 0x30FF for c in value):
        return EntryStatus.MIXED_LANGUAGE
    # 日文汉字残留漏翻：value 无假名，但与原文字符重叠极高 -> 未翻译的日文汉字句
    # （如 value="準備完了" 无假名、与原文重叠高，会被当已翻译；此判定拉回 MIXED）
    if len(value) >= 2 and _char_overlap_ratio(value, key) >= 0.6:
        return EntryStatus.MIXED_LANGUAGE
    if source_tag == "legacy":
        return EntryStatus.MACHINE_TRANSLATION  # 旧汉化来源（含机翻风险）
    return EntryStatus.HUMAN_TRANSLATION        # 来源未知，默认人工（保守）


def build_entries(data: Dict[str, str],
                  source_tag: Optional[str] = None,
                  start_index: int = 0) -> List[Entry]:
    """把键值对转为 Entry 列表（带 id）。"""
    entries: List[Entry] = []
    for i, (k, v) in enumerate(data.items()):
        entries.append(Entry(
            id=f"{start_index:06d}_{i:04d}",
            key=k,
            src=k,
            cur=v,
            status=classify_value(k, v, source_tag),
        ))
    return entries


def filter_for_translation(entries: List[Entry]) -> List[Entry]:
    """筛出需要翻译的条目（未翻译/混合语言；排除脚本数据/纯符号/空）。"""
    return [e for e in entries
            if e.status in (EntryStatus.UNTRANSLATED, EntryStatus.MIXED_LANGUAGE)]


def filter_for_polish(entries: List[Entry]) -> List[Entry]:
    """筛出需要润色的条目（人工/机翻译文；排除未翻译、脚本数据、空）。"""
    return [e for e in entries
            if e.status in (EntryStatus.HUMAN_TRANSLATION, EntryStatus.MACHINE_TRANSLATION)
            and e.cur and e.cur.strip()]
