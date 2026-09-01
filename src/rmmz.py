# -*- coding: utf-8 -*-
"""rmmz.py — RPG Maker MZ 事件解析（occurrence index 的事件版）。

背景：RPG Maker MZ 游戏（如 HOLLOWWALD《ホロウワルド の誓い》）的剧情/调查
文本分布在 data/MapXXX.json 与 data/CommonEvents.json 的事件脚本中。
同一日文原文可能出现在多个事件（多语境），也可能只在单个事件内出现
（语境唯一）。解析事件脚本建立

    source_text -> [Occurrence(file, line, method, preceding, following, speaker_candidate, scene)]

索引，供 context.apply_context 填充 Entry.speaker/scene/multi_context，
配合 batcher 场景分组与 prompt 前/后句注入，实现"同一上下文的文本
同批翻译、连贯一致"。

事件结构（RPG Maker MZ）：
    MapXXX.json    { id, events: [null, {id, name, pages: [{list: [...]}]}] }
    CommonEvents.json 为同构数组
命令：
    code 101 显示头像（parameters[0] = 立绘文件名，说话人线索）
    code 401 显示文本（parameters[0] = 文本内容）
    code 408 注释（跳过）
同一事件页内的连续 401 文本视为同一场景；preceding/following 取页内
前后句（截断 _CTX_MAX_CHARS，与 context.py 一致控制索引体积）。

依赖：schemas；被 cli 引用（与 context.py 同层，仅允许向下依赖）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from context import Occurrence, OccurrenceIndex, _CTX_MAX_CHARS


def _iter_event_pages(data_dir: Path) -> Iterator[Tuple[str, str, int, str, List[dict]]]:
    """遍历 data 目录下所有事件页。

    yield (源文件名, 场景前缀, 事件id, 事件名, 事件页命令列表)。
    防御性处理：事件数组含 null、事件页缺失/非 dict、命令缺失等。
    """
    # MapInfos.json：地图 id -> 名称（数组，索引 0 为 null）
    map_names: Dict[int, str] = {}
    infos_path = data_dir / "MapInfos.json"
    if infos_path.exists():
        try:
            raw = json.loads(infos_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for mid, info in enumerate(raw):
                    if isinstance(info, dict) and info.get("name"):
                        map_names[mid] = info["name"]
        except (OSError, json.JSONDecodeError):
            pass

    for fp in sorted(data_dir.glob("Map*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        mid = data.get("id", 0)
        prefix = f"{fp.stem}/ev"  # 如 Map026/ev
        for ev in data.get("events") or []:
            if not isinstance(ev, dict) or not ev.get("id"):
                continue
            evid = ev["id"]
            evname = str(ev.get("name") or "")
            for page in ev.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                lst = page.get("list")
                if isinstance(lst, list) and lst:
                    yield fp.name, prefix, evid, evname, lst

    ce_path = data_dir / "CommonEvents.json"
    if ce_path.exists():
        try:
            data = json.loads(ce_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, list):
            for ev in data:
                if not isinstance(ev, dict) or not ev.get("id"):
                    continue
                evid = ev["id"]
                evname = str(ev.get("name") or "")
                for page in ev.get("pages") or []:
                    if not isinstance(page, dict):
                        continue
                    lst = page.get("list")
                    if isinstance(lst, list) and lst:
                        yield "CommonEvents.json", "CommonEvents/ev", evid, evname, lst


def scan_rmmz_events(data_dir: Path) -> OccurrenceIndex:
    """扫描 RPG Maker MZ data 目录，构建出现索引。

    每个事件页视为一个场景单元：
    - scene = 源文件/事件id（如 Map026/ev21，全局唯一，作分组键）
    - method = 事件名（如"調べる"，供 prompt 描述）
    - speaker_candidate = code 101 的立绘名（最近一次，同页内生效）
    - preceding/following = 事件页内前后句
    """
    index = OccurrenceIndex()
    for fn, prefix, evid, evname, lst in _iter_event_pages(data_dir):
        scene = f"{prefix}{evid}"
        face: Optional[str] = None
        last_text: Optional[str] = None
        seq = 0
        for cmd in lst:
            if not isinstance(cmd, dict):
                continue
            code = cmd.get("code")
            params = cmd.get("parameters")
            if code == 101 and params:
                face = str(params[0])
            elif code == 401 and params:
                seq += 1
                text = str(params[0])
                if not text:
                    continue
                occ = Occurrence(
                    file=fn,
                    line=seq,
                    method=evname or None,
                    preceding=(last_text or "")[:_CTX_MAX_CHARS],
                    following="",  # following 由下一轮填充
                    speaker_candidate=face,
                    scene=scene,
                )
                index.map.setdefault(text, []).append(occ)
                if last_text and index.map.get(last_text):
                    index.map[last_text][-1].following = text[:_CTX_MAX_CHARS]
                last_text = text
    return index
