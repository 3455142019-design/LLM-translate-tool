# -*- coding: utf-8 -*-
"""glossary.py — 术语表：解析 / 按批检索 / 注入 prompt。

术语来源：
1. 现有 `scripts/verify_reports/SR_汉化审计/术语表_初稿.md`（Markdown 表格）
2. 手工补充（GLOSSARY_EXTRA 常量，或运行目录下的 glossary.json）

按批检索策略（不把数千条术语全塞进 prompt）：
- 核心全局术语（优先级 high）始终注入
- 当前批文本中实际出现的术语（source 子串命中）注入
- 禁译清单（forbidden_variants）始终注入

依赖：无（纯标准库）。被 engine/prompts 引用。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ---- 手工补充术语（项目特有，最终以确认版为准） ----
# 注意：多个项目共用本工具时，SR 项目术语会污染其他项目的 prompt。
# 设置环境变量 SR_GLOSSARY_EXTRA=0 可禁用（多项目隔离）。
# 运行时动态求值：同一进程内切换项目（如 GUI 顺序启动 CLI）也能生效。
def _extra_entries() -> List[Dict[str, str]]:
    if os.environ.get("SR_GLOSSARY_EXTRA", "1") == "0":
        return []
    return [
        {"source": "ロウラット", "target": "劳拉特", "priority": "high",
         "scope": "global", "notes": "待统一（旧译：萝菈特/罗乌拉特）"},
        {"source": "ラーミル", "target": "拉米尔", "priority": "high",
         "scope": "global", "notes": "女性，师匠"},
        {"source": "金色の夢魔", "target": "金色梦魔", "priority": "high", "scope": "global"},
        {"source": "夢干渉", "target": "梦境干涉", "priority": "high", "scope": "global",
         "notes": "待统一（旧译：梦干涉）"},
        {"source": "サキュバス", "target": "魅魔", "priority": "high", "scope": "global"},
        {"source": "ルーン", "target": "符文", "priority": "high", "scope": "global"},
    ]


@dataclass
class GlossaryEntry:
    """一条术语。"""
    source: str
    target: str
    priority: str = "normal"           # high/normal
    scope: str = "global"              # global / speaker:<名> / scene:<场景>
    notes: str = ""
    forbidden_variants: List[str] = field(default_factory=list)  # 禁止译法

    def to_prompt_line(self) -> str:
        base = f"{self.source} → {self.target}"
        if self.forbidden_variants:
            base += f"（禁止: {'/'.join(self.forbidden_variants)}）"
        return base


class Glossary:
    """术语表集合：解析多来源 -> 按批检索。"""

    def __init__(self, entries: Optional[List[GlossaryEntry]] = None):
        self._entries: List[GlossaryEntry] = entries or []
        self._by_source: Dict[str, GlossaryEntry] = {}
        for e in self._entries:
            self._by_source.setdefault(e.source, e)
        self._high: List[GlossaryEntry] = [e for e in self._entries if e.priority == "high"]
        self._forbidden: List[str] = [fv for e in self._entries for fv in e.forbidden_variants]

    # ---- 解析 ----
    @classmethod
    def from_markdown(cls, md_path: Path) -> "Glossary":
        """解析术语表_初稿.md 的 Markdown 表格（| 日文 | 建议 | 状态 | ...）。"""
        entries: List[GlossaryEntry] = []
        if not md_path.exists():
            return cls()
        lines = md_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2 or cells[0].startswith("---") or cells[0] in ("日文", "日文原文"):
                continue
            src, tgt = cells[0], cells[1]
            if not src or not tgt or src == tgt:
                continue
            entries.append(GlossaryEntry(source=src, target=tgt, priority="normal"))
        return cls(entries + [GlossaryEntry(**g) for g in _extra_entries()])

    @classmethod
    def from_json(cls, json_path: Path) -> "Glossary":
        """解析自定义 glossary.json：[{source,target,priority,scope,notes,forbidden_variants}]"""
        if not json_path.exists():
            return cls()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        entries = [GlossaryEntry(**d) for d in data]
        return cls(entries + [GlossaryEntry(**g) for g in _extra_entries()])

    # ---- 按批检索 ----
    def for_batch(self, texts: List[str]) -> List[GlossaryEntry]:
        """返回该批应注入的术语：核心高频 + 文本实际命中的 + 禁译清单。

        texts 为批内全部原文（未遮罩前或遮罩后均可，source 为日文不受影响）。
        """
        selected: List[GlossaryEntry] = []
        seen: set[str] = set()
        for e in self._high:  # 核心术语始终注入
            if e.source not in seen:
                selected.append(e); seen.add(e.source)
        joined = "\n".join(texts)
        for e in self._entries:
            if e.source in seen:
                continue
            if e.source in joined:  # 批内实际出现
                selected.append(e); seen.add(e.source)
        return selected

    def forbidden_lines(self) -> List[str]:
        return [f"禁止译法: {fv}" for fv in self._forbidden]

    def merge(self, other: "Glossary") -> "Glossary":
        """合并 other 的条目到自身；source 冲突时 other 覆盖自身。

        用于项目包术语（bundle）与显式 --glossary/--glossary-json 的合并，
        显式术语优先于项目包（调用方传序：base.merge(explicit)）。
        """
        for e in other._entries:
            existing = self._by_source.get(e.source)
            if existing is not None and existing in self._entries:
                self._entries.remove(existing)
            self._entries.append(e)
            self._by_source[e.source] = e
        self._high = [e for e in self._entries if e.priority == "high"]
        self._forbidden = [fv for e in self._entries for fv in e.forbidden_variants]
        return self

    def to_prompt_block(self, texts: List[str]) -> str:
        """生成注入 prompt 的术语表文本块。"""
        entries = self.for_batch(texts)
        if not entries:
            return ""
        lines = ["【术语表（必须严格遵守）】"]
        lines += [f"- {e.to_prompt_line()}" for e in entries]
        lines += self.forbidden_lines()
        return "\n".join(lines)
