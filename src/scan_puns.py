# -*- coding: utf-8 -*-
"""scan_puns.py — 双关/俚语/语境梗批量筛查（scan-puns 命令实现）。

背景（2026-08-11 HOLLOWWALD 教训）：双关/俚语靠主控 agent 一次性通读
筛选，既贵又不可复用，且会漏（木板「いた。」、蛋挞、各种事情等反复复发）。
本命令用高思考模型对全部原文批量筛查，输出候选清单 JSON 供主控 agent
确认后写入 pun_manifest.json，形成可持续积累的回归库。

流程：读原文 -> 分批（带事件上下文）-> thinking high 请求 ->
解析候选清单 -> 汇总输出 runs/<run_id>/scan_puns/candidates.json。

依赖：providers/batcher/context；被 cli 引用。与 engine 解耦（输出不是
译文，不经过 QA 路由/回写）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from batcher import Batcher
from context import Occurrence, OccurrenceIndex, describe_occurrences
from providers.factory import make_client
from schemas import Entry
from validate import parse_model_output

_SCAN_SYSTEM = """你是一位精通日语双关、谐音梗、俚语的资深游戏本地化专家。
用户会给你一批日文游戏文本（可能附带语境说明）。你的任务是**只挑出**其中需要人工特别注意的条目：
1. 双关/谐音：同一读音可作多义（如「いた。」= 木板「板」/ 存在「居た」），或谐音梗
2. 俚语/俗语/惯用语：直译会丢失本义或造成误解
3. 语境梗：依赖画面/道具/剧情才能正确理解，脱离上下文必翻错
普通叙述、对话、说明文本一律不选。宁可漏报不可滥报（每批最多 20 条）。

严格输出 JSON 对象（键 candidates 为数组）：
{"candidates": [{"key": "日文原文", "type": "pun|slang|contextual",
  "reason": "双关/俚语说明", "suggestion": "建议译法"}]}
key 必须与输入条目完全一致；没有候选输出 {"candidates": []}。"""


def _strip_fence(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return m.group(0) if m else text


def _build_user_payload(items: List[Tuple[str, str, str]]) -> str:
    """构造 user payload：[["0", 原文, 语境], ...]（短序号协议）。"""
    payload = []
    for i, (key, ctx) in enumerate(items):
        row: List[str] = [str(i), key]
        if ctx:
            row.append(ctx)
        payload.append(row)
    return json.dumps(payload, ensure_ascii=False)


def scan_puns(input_path: Path, out_dir: Path, api_key: Optional[str],
              model: str, base_url: str = "",
              occurrence_index: Optional[Path] = None,
              batch_items: int = 60) -> Path:
    """执行筛查。返回候选清单文件路径。"""
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{input_path} 不是键值对 JSON 对象")
    keys = list(data.keys())

    # 事件上下文（可选）：apply_context 描述前/后句+说话人+场景
    index: Optional[OccurrenceIndex] = None
    if occurrence_index is not None and occurrence_index.exists():
        raw = json.loads(occurrence_index.read_text(encoding="utf-8"))
        index = OccurrenceIndex(
            map={t: [Occurrence(**o) for o in occs] for t, occs in raw.items()})

    client = make_client(protocol="deepseek_chat", api_key=api_key, model=model,
                         base_url=base_url)
    batcher = Batcher()
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates: List[Dict[str, str]] = []
    total_batches = (len(keys) + batch_items - 1) // batch_items
    for bi in range(total_batches):
        chunk = keys[bi * batch_items:(bi + 1) * batch_items]
        items: List[Tuple[str, str, str]] = []
        for key in chunk:
            ctx = ""
            if index is not None:
                e = Entry(id="", key=key, src=key)
                desc = describe_occurrences(e, index)
                ctx = desc or ""
            items.append((key, ctx))
        user = _build_user_payload(items)
        try:
            resp = client.chat(
                [{"role": "system", "content": _SCAN_SYSTEM},
                 {"role": "user", "content": user}],
                thinking="enabled", reasoning_effort="high",
                response_format={"type": "json_object"})
        except Exception as e:
            print(f"  ⚠ 第 {bi + 1}/{total_batches} 批请求失败: {e}，跳过")
            continue
        parsed: Optional[Dict[str, Any]] = None
        try:
            parsed = json.loads(_strip_fence(resp.content or ""))
        except json.JSONDecodeError:
            parsed = None
        batch_cands = (parsed or {}).get("candidates", []) if isinstance(parsed, dict) else []
        # 候选 key 必须真实存在于输入（防模型编造）
        key_set = set(chunk)
        valid = [c for c in batch_cands
                 if isinstance(c, dict) and c.get("key") in key_set]
        candidates.extend(valid)
        print(f"  第 {bi + 1}/{total_batches} 批: {len(chunk)} 条 -> 候选 {len(valid)} 条")
        time.sleep(0.3)  # 轻退避，避免触发限流

    out = out_dir / "candidates.json"
    payload = {"total_scanned": len(keys), "candidate_count": len(candidates),
               "candidates": candidates}
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    return out
