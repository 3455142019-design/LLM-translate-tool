# -*- coding: utf-8 -*-
"""pun.py — 双关/俚语/语境梗回归库（pun_manifest.json）管理。

背景（2026-08-11 HOLLOWWALD 教训）：双关/俚语文本反复漏检误翻——
木板调查文本「いた。」（板/居た双关）在体验版修过、正式版再次复发；
酒瓶/袋子「各种事情。」、木桶「蛋挞什么的。」等均被误翻。根因：预筛
靠主控 agent 一次性通读，没有持久化清单，也没有产出后自动核对。

本模块把「已发现的双关/俚语/语境梗」固化为项目包内
projects/<游戏名>/pun_manifest.json：

{
  "entries": {
    "<日文原文>": {
      "type": "pun|slang|contextual",
      "note": "翻译要点/双关说明",
      "translation": "已确认译法（status=confirmed 时作为定稿）",
      "status": "confirmed|pending",
      "source": "demo|full|proofread|game_test",
      "updated_at": "ISO 时间"
    }
  }
}

用途：
- 翻译前：confirmed 条目并入 engine key_lines（mode=agent 预填译文直接复用；
  pending 条目以 note 注入 prompt 提示）
- 翻译后：pun-check 对照产物，清单内条目的译文与定稿不一致 -> 报告
  （防止已修复的双关在后续重翻/合并中复发）

依赖：无（纯标准库）。被 cli/bundle 引用。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

MANIFEST_NAME = "pun_manifest.json"
VALID_TYPES = ("pun", "slang", "contextual")
VALID_STATUS = ("confirmed", "pending")
VALID_SOURCES = ("demo", "full", "proofread", "game_test")


def manifest_path(bundle_root: Path) -> Path:
    """项目包内的清单路径。"""
    return Path(bundle_root) / MANIFEST_NAME


def load(bundle_root: Path) -> Dict[str, Dict]:
    """读取清单；不存在返回空。"""
    p = manifest_path(bundle_root)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def save(bundle_root: Path, entries: Dict[str, Dict]) -> Path:
    """原子写回清单。"""
    p = manifest_path(bundle_root)
    payload = {"entries": entries,
               "updated_at": datetime.now().isoformat(timespec="seconds")}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)
    return p


def add_entry(bundle_root: Path, key: str, pun_type: str, note: str,
              translation: str, status: str = "pending",
              source: str = "proofread") -> Dict[str, Dict]:
    """新增/更新一条清单条目。返回更新后的全部条目。"""
    if pun_type not in VALID_TYPES:
        raise ValueError(f"type 必须是 {VALID_TYPES} 之一")
    if status not in VALID_STATUS:
        raise ValueError(f"status 必须是 {VALID_STATUS} 之一")
    if source not in VALID_SOURCES:
        raise ValueError(f"source 必须是 {VALID_SOURCES} 之一")
    entries = load(bundle_root)
    entries[key] = {
        "type": pun_type,
        "note": note,
        "translation": translation,
        "status": status,
        "source": source,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save(bundle_root, entries)
    return entries


def check_translations(bundle_root: Path,
                       data: Dict[str, str]) -> List[Dict[str, str]]:
    """对照译文产物：清单内条目的当前译文与定稿不一致 -> 报告。

    - confirmed 且 translation 非空：当前值 != translation -> 复发/被改
    - confirmed 且 translation 为空：只提示存在
    - pending：当前值 == 原文（漏译）-> 提醒仍未处理
    """
    entries = load(bundle_root)
    issues: List[Dict[str, str]] = []
    for key, meta in entries.items():
        current = data.get(key)
        if current is None:
            issues.append({"key": key, "issue": "清单条目在产物中不存在",
                           "expected": meta.get("translation", ""),
                           "current": ""})
            continue
        expected = meta.get("translation", "")
        if meta.get("status") == "confirmed":
            if expected and current != expected:
                issues.append({
                    "key": key,
                    "issue": "已确认译法被改动/复发（双关误翻回归）",
                    "expected": expected, "current": current})
        elif meta.get("status") == "pending":
            if current == key:
                issues.append({"key": key, "issue": "pending 双关仍未翻译",
                               "expected": expected, "current": current})
    return issues


def to_key_lines(entries: Dict[str, Dict]) -> Dict[str, Dict]:
    """转换为 engine key_lines 格式（{原文: {note, mode, translation}}）。

    - confirmed + translation：mode=agent（预填译文直接复用，不进 API）
    - 其余：mode=note（只注入翻译要点提示）
    """
    key_lines: Dict[str, Dict] = {}
    for key, meta in entries.items():
        note = meta.get("note", "")
        translation = meta.get("translation", "")
        if meta.get("status") == "confirmed" and translation:
            key_lines[key] = {"note": note, "mode": "agent",
                              "translation": translation}
        elif note:
            key_lines[key] = {"note": note, "mode": "note"}
    return key_lines
