# -*- coding: utf-8 -*-
"""proofread.py — 全量审查与抽查（proofread / sample-check 命令实现）。

背景（2026-08-10/11 HOLLOWWALD 教训）：
- 审查由子代理执行时缺统一模板：块1 只报 796 条样例，实际错位 1,866 条
- 审查 prompt 无问题分类枚举、无统一 schema，产物格式混乱无法合并
- 抽查靠主控手动组织多轮「每 agent N 条」，不可复用
- 子代理曾直接改动交付文件（fix_flow_15.json），只能回滚

本模块提供：
- run_proofread：全量/分片审查。**只读**：读译文文件，写 issues JSON
  报告到输出目录，绝不改动输入。prompt 内建问题分类枚举与统一 schema。
- run_sample_check：随机不重复抽 N 条送审，统计问题率，超阈值退出码 1
  （质量闸门：问题率 <1% 才算通过，见 2026-08-11 会话用户要求）。

依赖：providers/batcher；被 cli 引用。
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from providers.factory import make_client
from validate import parse_model_output

_ISSUE_TYPES = ("misaligned", "missing", "mistranslation", "term_inconsistent",
                "gender_error", "format", "forbidden_word", "garbled",
                "context_break")


def _load_system(project_name: str, glossary_text: str = "") -> str:
    text = (Path(__file__).resolve().parent / "prompts" / "proofread.txt"
            ).read_text(encoding="utf-8")
    text = text.replace("{project_name}", project_name)
    if glossary_text:
        text += (
            "\n\n【项目术语与风格约束（必须遵守；出现即判 term_inconsistent/forbidden_word）】\n"
            + glossary_text + "\n")
    return text


def _strip_fence(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return m.group(0) if m else text


def _parse_issues(raw: str, batch_keys: List[str]) -> List[Dict[str, str]]:
    """解析模型输出的 issues；校验 idx/key 与批内条目一致，防编造。"""
    try:
        parsed = json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        return []
    issues = parsed.get("issues", []) if isinstance(parsed, dict) else []
    if not isinstance(issues, list):
        return []
    valid: List[Dict[str, str]] = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        idx = str(it.get("idx", ""))
        key = it.get("key", "")
        issue_type = it.get("issue", "")
        # idx 必须是批内序号，key 必须与批内原文一致（防模型编造条目）
        if not idx.isdigit():
            continue
        pos = int(idx)
        if not (0 <= pos < len(batch_keys)):
            continue
        if key and key != batch_keys[pos]:
            continue
        if issue_type not in _ISSUE_TYPES:
            continue
        valid.append({
            "idx": idx,
            "key": batch_keys[pos],
            "cn_old": it.get("cn_old", ""),
            "issue": issue_type,
            "note": it.get("note", ""),
            "cn_new": it.get("cn_new", ""),
        })
    return valid


def _client_for(api_key: Optional[str], model: str, base_url: str):
    return make_client(protocol="deepseek_chat", api_key=api_key, model=model,
                       base_url=base_url)


def _select_batch(client, system: str, batch: List[Tuple[str, str]],
                  batch_no: int, total: int) -> List[Dict[str, str]]:
    """送一批 [key, value] 审查。返回该批 issues。"""
    payload = [[str(i), k, v] for i, (k, v) in enumerate(batch)]
    user = json.dumps(payload, ensure_ascii=False)
    try:
        resp = client.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            thinking="enabled", reasoning_effort="high",
            response_format={"type": "json_object"})
    except Exception as e:
        print(f"  ⚠ 第 {batch_no}/{total} 批请求失败: {e}，跳过")
        return []
    issues = _parse_issues(resp.content or "", [k for k, _ in batch])
    print(f"  第 {batch_no}/{total} 批: {len(batch)} 条 -> 问题 {len(issues)} 条")
    time.sleep(0.3)
    return issues


def run_proofread(input_path: Path, out_dir: Path, api_key: Optional[str],
                  model: str, base_url: str = "", batch_items: int = 60,
                  shard: Optional[Tuple[int, int]] = None,
                  project_name: str = "", glossary_text: str = "") -> Path:
    """全量审查（可 --shard i/N 分片）。只读输入，输出 issues JSON。"""
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{input_path} 不是键值对 JSON 对象")
    items = list(data.items())
    if shard is not None:
        i, n = shard
        step = (len(items) + n - 1) // n
        start = i * step
        items = items[start:start + step]
        print(f"  分片 {i}/{n}: 条目 {start}..{start + len(items) - 1} "
              f"（共 {len(items)} 条）")

    client = _client_for(api_key, model, base_url)
    system = _load_system(project_name or "本项目", glossary_text)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_issues: List[Dict[str, str]] = []
    total_batches = (len(items) + batch_items - 1) // batch_items
    for bi in range(total_batches):
        chunk = items[bi * batch_items:(bi + 1) * batch_items]
        all_issues.extend(_select_batch(client, system, chunk, bi + 1, total_batches))

    shard_tag = f"_shard{shard[0]}_{shard[1]}" if shard else ""
    out = out_dir / f"issues{shard_tag}.json"
    payload = {
        "checked": len(items),
        "issue_count": len(all_issues),
        "issue_rate": round(len(all_issues) / len(items), 4) if items else 0.0,
        "issues": all_issues,
    }
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    return out


def run_sample_check(input_path: Path, out_dir: Path, api_key: Optional[str],
                     model: str, base_url: str = "", sample_n: int = 500,
                     seed: Optional[int] = None, batch_items: int = 60,
                     max_issue_rate: float = 0.01,
                     project_name: str = "",
                     glossary_text: str = "") -> Tuple[Path, float, bool]:
    """随机不重复抽 N 条送审，统计问题率。返回 (报告路径, 问题率, 是否通过)。"""
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{input_path} 不是键值对 JSON 对象")
    all_items = list(data.items())
    rng = random.Random(seed)
    sampled = rng.sample(all_items, min(sample_n, len(all_items)))

    client = _client_for(api_key, model, base_url)
    system = _load_system(project_name or "本项目", glossary_text)
    out_dir.mkdir(parents=True, exist_ok=True)

    issues: List[Dict[str, str]] = []
    total_batches = (len(sampled) + batch_items - 1) // batch_items
    for bi in range(total_batches):
        chunk = sampled[bi * batch_items:(bi + 1) * batch_items]
        issues.extend(_select_batch(client, system, chunk, bi + 1, total_batches))

    rate = len(issues) / len(sampled) if sampled else 0.0
    passed = rate <= max_issue_rate
    out = out_dir / "sample_check_report.json"
    payload = {
        "sampled": len(sampled),
        "seed": seed,
        "issue_count": len(issues),
        "issue_rate": round(rate, 4),
        "max_issue_rate": max_issue_rate,
        "passed": passed,
        "issues": issues,
    }
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    print(f"抽查: 样本 {len(sampled)} 条 | 问题 {len(issues)} 条 | "
          f"问题率 {rate:.2%}（阈值 {max_issue_rate:.1%}）"
          + (" ✅ 通过" if passed else " ❌ 未通过"))
    return out, rate, passed
