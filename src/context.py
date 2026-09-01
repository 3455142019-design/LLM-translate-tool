# -*- coding: utf-8 -*-
"""context.py — 口上 .rb 扫描与 occurrence index（出现索引）。

背景（v3 评审）：MTool 键值结构限制——同一日文原文无论出现多少次，
最终只能对应一个译文。若同一句被不同角色/场景共用（如「いいよ」），
直接按某一处语境翻译会污染其他场景。因此先扫描口上源文件建立索引：

    source_text -> [Occurrence(file, line, method, preceding, following, speaker_candidate)]

然后给每个 Entry 决策：
- 唯一出现            -> 用该处上下文 + 角色（speaker 可信）
- 多次出现且语境一致  -> 合并上下文，统一译法
- 多次出现且语境不同  -> multi_context=True（中性译法，prompt 里注明"多语境共用"）
- 提取不到角色       -> speaker_unknown（不猜测）

依赖：schemas；被 cli/engine 引用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from schemas import Entry

# 口上文件内的字符串字面量（单引号/双引号，含转义）
_STR_RE = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")
# 方法定义（RPG Maker 口上脚本按方法组织）
_METHOD_RE = re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_!?]*)")
# 说话人线索：常见赋值模式（角色变量/立绘调用），扩展后按项目校准
_SPEAKER_HINT_RE = re.compile(
    r"(?:speaker|voice|character|actor|chara|立绘|立ち絵)\s*[=:]\s*['\"]?([^'\"\s,;\)]+)"
    r"|draw_picture\s*\(\s*(\d+)\s*,\s*['\"]?([^'\"]+)", re.IGNORECASE)
# 消息窗口调用（RPG Maker VX/VX Ace 风格）
_MSG_RE = re.compile(r"\b(message|msgbox|draw_text|text)\s*\(", re.IGNORECASE)


@dataclass
class Occurrence:
    """一次出现的位置与上下文。"""
    file: str
    line: int
    method: Optional[str] = None
    preceding: str = ""       # 前一句（同一方法内上一个字符串）
    following: str = ""       # 后一句
    speaker_candidate: Optional[str] = None
    scene: Optional[str] = None  # 文件名（不含路径）作为粗场景


@dataclass
class OccurrenceIndex:
    """文本 -> 出现列表 的索引。"""
    map: Dict[str, List[Occurrence]] = field(default_factory=dict)

    def get(self, text: str) -> List[Occurrence]:
        return self.map.get(text, [])

    def is_multi_context(self, text: str) -> bool:
        """多次出现且语境（speaker/场景）不一致。"""
        occs = self.map.get(text, [])
        if len(occs) <= 1:
            return False
        sigs = {(o.speaker_candidate or "?", o.scene or "?") for o in occs}
        return len(sigs) > 1


# 上下文文本截断长度（控制索引体积；SR 口上全量索引约 74 万次出现）
_CTX_MAX_CHARS = 60


def scan_rb_files(rb_dir: Path, max_files: Optional[int] = None) -> OccurrenceIndex:
    """扫描目录下所有 .rb 口上文件，构建出现索引。

    实现：逐行解析——行内字符串字面量作为候选文本；方法边界追踪 method；
    行内说话人线索提取 speaker_candidate；方法内前一条字符串作为 preceding。

    注意：索引可能很大（SR 口上约 74 万次出现），上下文文本截断到
    _CTX_MAX_CHARS 以控制体积；如需全量上下文可调大该值。
    """
    index = OccurrenceIndex()
    files = sorted(rb_dir.rglob("*.rb"))
    if max_files:
        files = files[:max_files]
    for fp in files:
        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        method: Optional[str] = None
        last_str: Optional[str] = None
        for ln, line in enumerate(lines, 1):
            m = _METHOD_RE.match(line)
            if m:
                method = m.group(1)
                last_str = None
                continue
            # 说话人线索（行级，取最近）
            speaker = None
            sm = _SPEAKER_HINT_RE.search(line)
            if sm:
                speaker = sm.group(1) or sm.group(3)
            for smatch in _STR_RE.finditer(line):
                text = smatch.group(1) or smatch.group(2)
                if not text or len(text) < 2 or not any(
                        ord(c) > 0x2E7F for c in text):  # 过滤非日文/非 CJK 字符串
                    continue
                occ = Occurrence(
                    file=fp.name, line=ln, method=method,
                    preceding=(last_str or "")[:_CTX_MAX_CHARS],
                    following="",  # following 由下一轮填充
                    speaker_candidate=speaker,
                    scene=fp.name,
                )
                index.map.setdefault(text, []).append(occ)
                if last_str and index.map.get(last_str):
                    index.map[last_str][-1].following = text[:_CTX_MAX_CHARS]
                last_str = text
    return index


def apply_context(entries: List[Entry], index: OccurrenceIndex) -> None:
    """把 occurrence index 应用到 Entry（填充 speaker/scene/occurrences/multi_context）。

    - occurrences=1 且 speaker 可提取：填入 speaker
    - 多语境：multi_context=True（prompt 层提示中性译法）
    - 无法提取：speaker=None（speaker_unknown）
    """
    for e in entries:
        occs = index.get(e.src)
        e.occurrences = len(occs)
        if not occs:
            continue
        e.scene = occs[0].scene
        if len(occs) == 1 and occs[0].speaker_candidate:
            e.speaker = occs[0].speaker_candidate
        if index.is_multi_context(e.src):
            e.multi_context = True


def describe_occurrences(entry: Entry, index: OccurrenceIndex, max_ctx: int = 2) -> str:
    """为 prompt 生成上下文描述（scene/speaker/前后句）。无信息返回空串。"""
    occs = index.get(entry.src)
    if not occs:
        return ""
    parts = []
    for o in occs[:max_ctx]:
        ctx = []
        if o.scene:
            ctx.append(f"文件:{o.scene}")
        if o.method:
            ctx.append(f"方法:{o.method}")
        if o.speaker_candidate:
            ctx.append(f"说话人:{o.speaker_candidate}")
        if o.preceding:
            ctx.append(f"前句:{o.preceding[:30]}")
        if o.following:
            ctx.append(f"后句:{o.following[:30]}")
        parts.append(" ".join(ctx))
    return " | ".join(parts)
