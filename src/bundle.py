# -*- coding: utf-8 -*-
"""bundle.py — 项目数据包（projects/<游戏名>/）的创建与加载。

背景：翻译工具由主控 agent 驱动。为避免"批量调用模型解构信息"带来的
上下文割裂与额外 token 消耗，改为工具提供脚手架（new-project 生成空 JSON），
由主控 agent 通读原文后自行按约定结构写入各文件：

    projects/<游戏名>/
    ├── project.json      # 元数据 + 各文件结构说明（agent 读这里了解约定）
    ├── glossary.json     # 术语表（Glossary.from_json 兼容格式）
    ├── world.json        # 世界观（势力/地名/系统）
    ├── chars.json        # 人物表（说话人/称谓/身份）
    ├── story_chain.json  # 剧情链路（章节/关键事件）
    ├── policies.json     # 风格策略（forbidden_words 黑名单等）
    ├── key_lines.json    # 重点翻译条目（双关/典故/俚语，agent 预填或 max 通道）
    └── talk/             # 口上数据（结构化或源文件）

依赖：config/glossary；被 cli 引用。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from glossary import Glossary

# 各数据文件的骨架（空结构，供主控 agent 按约定填写）
_SKELETONS: Dict[str, object] = {
    "glossary.json": [
        {"source": "", "target": "", "priority": "normal", "scope": "global",
         "notes": "", "forbidden_variants": []}
    ],
    "world.json": {"settings": [], "factions": [], "places": [], "systems": []},
    "chars.json": {"characters": []},
    "story_chain.json": {"chapters": []},
    "policies.json": {"forbidden_words": [], "style_notes": []},
    "key_lines.json": {},
}

# 各文件结构约定说明（写入 project.json，供主控 agent 读取）
_STRUCTURE_NOTES = {
    "glossary.json": "术语表。数组元素: {source(日文), target(中文), priority(normal|high), "
                     "scope(global), notes, forbidden_variants[禁止译法]}",
    "world.json": "世界观。{settings:[{term,desc}], factions:[{name,desc}], "
                  "places:[{name,desc}], systems:[{name,desc}]}",
    "chars.json": "人物表。{characters:[{name, aliases[], titles[], gender(male|female|"
                  "unknown), role, relation, notes}]}。gender 用于称呼/人称翻译防错"
                  "（2026-08-11 HOLLOWWALD 教训：ウィーウ(女性)三处被译『先生』）",
    "story_chain.json": "剧情链路。{chapters:[{order, title, summary, key_events[]}]}",
    "policies.json": "风格策略。{forbidden_words:[译文出现即报的黑名单词], "
                     "style_notes:[整体风格要求，注入 prompt]}",
    "key_lines.json": "重点翻译条目(双关/典故/俚语)。{原文: {note: 翻译要点, "
                      "mode: 'agent'|'max', translation: mode=agent 时预填译文}}。"
                      "pun_manifest.json（双关回归库）由 pun-add/pun-check 维护，"
                      "翻译时自动并入 key_lines，不建议手改",
    "talk/": "口上数据（结构化 {说话人,场景,文本} 或源 .rb 副本）",
}


def create_project(name: str) -> Path:
    """创建项目数据包骨架目录。返回根目录路径。"""
    root = config.PROJECTS_ROOT / name
    if root.exists():
        raise FileExistsError(f"项目包已存在: {root}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "talk").mkdir(exist_ok=True)

    # 空骨架文件（剔除空数组/空对象里的示例项，保持纯空容器）
    for filename, skeleton in _SKELETONS.items():
        _write_json_atomic(root / filename, _blank(skeleton))
    # 元数据 + 结构约定
    meta = {
        "name": name,
        "created_at": _now(),
        "description": "",
        "structure": _STRUCTURE_NOTES,
    }
    _write_json_atomic(root / "project.json", meta)
    return root


def _blank(value: object) -> object:
    """把骨架里的示例项清空为纯空容器（glossary 的示例条目移除）。"""
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {k: _blank(v) for k, v in value.items()}
    return value


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _write_json_atomic(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _flatten_to_text(prefix: str, value: object, depth: int = 0) -> List[str]:
    """把结构化数据压成可读的 prompt 文本行（控制体积，不展开深层）。"""
    lines: List[str] = []
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, list):
                if not val:
                    continue
                for item in val:
                    if isinstance(item, dict):
                        desc = "；".join(f"{k}:{v}" for k, v in item.items()
                                         if isinstance(v, (str, int, float)) and v not in ("", 0))
                        if desc:
                            lines.append(f"- {key}·{desc}")
                    elif isinstance(item, (str, int, float)):
                        lines.append(f"- {key}·{item}")
            elif isinstance(val, dict):
                lines.extend(_flatten_to_text(f"{prefix}{key}/", val, depth + 1))
            elif val not in ("", None, 0):
                lines.append(f"- {prefix}{key}: {val}")
    return lines[:80]  # 限制行数防 prompt 膨胀


@dataclass
class ProjectBundle:
    """一个游戏的项目数据包。"""

    name: str
    root: Path
    glossary: Glossary
    policies: dict = field(default_factory=dict)
    key_lines: dict = field(default_factory=dict)
    world_text: str = ""
    chars_text: str = ""
    story_text: str = ""
    chars_map: dict = field(default_factory=dict)   # 角色名/别名 -> {gender, role, ...}
    pun_entries: dict = field(default_factory=dict)  # pun_manifest.json 条目

    @property
    def forbidden_words(self) -> List[str]:
        """policies.json 的黑名单词（P1-3 检查器读取，agent 维护）。"""
        return list((self.policies or {}).get("forbidden_words", []) or [])

    @property
    def context_text(self) -> str:
        """世界观/人物/剧情链路的拼接文本（注入 prompt 用）。"""
        return "\n".join(t for t in (self.world_text, self.chars_text, self.story_text) if t)

    @property
    def merged_key_lines(self) -> Dict[str, Dict]:
        """key_lines.json + pun_manifest.json（confirmed 预填/pending 注入提示）。

        pun 清单条目优先（回归库是双关的权威来源，主控手工确认过）。
        """
        from pun import to_key_lines
        merged = dict(self.key_lines or {})
        for k, v in to_key_lines(self.pun_entries).items():
            merged[k] = v
        return merged

    @classmethod
    def load(cls, name: str) -> Optional["ProjectBundle"]:
        """从 projects/<name>/ 加载数据包。目录不存在返回 None。"""
        root = config.PROJECTS_ROOT / name
        if not root.is_dir():
            return None
        glossary = Glossary.from_json(root / "glossary.json")
        policies = _read_json(root / "policies.json") or {}
        key_lines = _read_json(root / "key_lines.json") or {}
        world = _read_json(root / "world.json") or {}
        chars = _read_json(root / "chars.json") or {}
        story = _read_json(root / "story_chain.json") or {}
        world_text = "\n".join(_flatten_to_text("", world))
        chars_text = "\n".join(_flatten_to_text("", chars))
        story_text = "\n".join(_flatten_to_text("", story))
        # 角色性别映射：name/aliases -> {gender, role, ...}（engine 按说话人注入）
        chars_map: Dict[str, dict] = {}
        for c in (chars.get("characters") or []):
            if not isinstance(c, dict) or not c.get("name"):
                continue
            info = {k: c[k] for k in ("gender", "role", "relation", "titles")
                    if c.get(k)}
            chars_map[c["name"]] = info
            for alias in (c.get("aliases") or []):
                if alias:
                    chars_map.setdefault(alias, info)
        # 双关回归库
        from pun import load as pun_load
        pun_entries = pun_load(root)
        return cls(name=name, root=root, glossary=glossary, policies=policies,
                   key_lines=key_lines, world_text=world_text,
                   chars_text=chars_text, story_text=story_text,
                   chars_map=chars_map, pun_entries=pun_entries)
