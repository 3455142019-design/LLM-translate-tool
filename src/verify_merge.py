# -*- coding: utf-8 -*-
"""verify_merge.py — 合并产物 vs 基准的逐键校验与残留检测。

背景（2026-08-10/11 HOLLOWWALD 正式版实测教训）：
1. 错位：合并/重翻产物中大量 key 的 value 是相邻条目的译文（4 种模式）
2. 旧文本残留：修复合并时旧值未整体替换，值内新旧两段译文并存
   （如「真是的！你就这么不想叫姐姐吗？\n不想叫姐姐吗？」），玩家
   在游戏里看到换行后重复的旧译文
3. 换行拼接：一条 value 内拼接了相邻 2–3 条 key 的译文（滚动合并式）

本模块提供 verify_merge(new, base)：
- 逐 key 报告 added / changed / unchanged / removed
- changed 条目做残留检测：新值是否「包含」旧值（旧文本没删干净）
- 换行结构突变检测：新值行数明显多于旧值且旧值无换行 -> 疑似拼接
- 输出机器可读 JSON 报告，供主控 agent 决策是否放行合并

依赖：无（纯标准库）。被 cli 引用。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional


def _norm(s: str) -> str:
    """归一化：去除全部空白与零宽字符（用于包含检测，容忍换行/空格差异）。"""
    return re.sub(r"[\s\u200b-\u200f\ufeff]+", "", s)


def _line_count(s: str) -> int:
    """按换行符分行数（\\n 字面反斜杠不算换行，双反斜杠问题由 full-qa 检测）。"""
    return len(re.split(r"\n", s))


def detect_residue(new_val: str, base_val: str) -> List[str]:
    """检测新旧值之间的残留特征。返回问题描述列表（空 = 正常替换）。

    检测项：
    - 旧值残留：新值归一化后包含旧值归一化后文本，且新值明显更长
      （旧文本未删除，新译文与旧译文拼接并存）
    - 反向残留：旧值包含新值（同样异常，说明旧值里还夹着别的内容）
    - 换行拼接：新值行数 >= 2 且旧值行数 < 2（一条值内多出了换行分段，
      疑似滚动合并式错位拼接）
    """
    issues: List[str] = []
    n_new, n_base = _norm(new_val), _norm(base_val)
    if not n_new or not n_base:
        return issues
    if n_new != n_base:
        if n_base in n_new and len(n_new) - len(n_base) >= 4:
            issues.append(
                f"疑似旧文本残留: 新值包含旧值全文且多出 {len(n_new) - len(n_base)} 字符"
                f"（旧值未删除，新旧译文拼接并存）")
        elif n_new in n_base and len(n_base) - len(n_new) >= 4:
            issues.append("疑似反向残留: 旧值包含新值全文（旧值内夹杂其他内容）")
    if _line_count(new_val) >= 2 and _line_count(base_val) < 2:
        issues.append("换行结构突变: 旧值单行 -> 新值多行（疑似滚动合并式拼接）")
    return issues


def verify_merge(new_data: Dict[str, str], base_data: Dict[str, str]
                 ) -> Dict[str, object]:
    """逐键对比合并产物与基准。

    返回报告 dict：
    {
      "counts": {total, added, removed, changed, unchanged},
      "added": [key...], "removed": [key...],
      "changed": [key...],
      "residue_suspects": [{key, base_value, new_value, issues}...],
      "residue_count": n,
    }
    """
    added = sorted(k for k in new_data if k not in base_data)
    removed = sorted(k for k in base_data if k not in new_data)
    changed = sorted(k for k in base_data
                     if k in new_data and new_data[k] != base_data[k])
    unchanged = len(base_data) - len(removed) - len(changed)

    suspects: List[Dict[str, object]] = []
    for k in changed:
        issues = detect_residue(new_data[k], base_data[k])
        if issues:
            suspects.append({
                "key": k,
                "base_value": base_data[k],
                "new_value": new_data[k],
                "issues": issues,
            })
    return {
        "counts": {"total": len(new_data), "added": len(added),
                   "removed": len(removed), "changed": len(changed),
                   "unchanged": unchanged},
        "added": added,
        "removed": removed,
        "changed": changed,
        "residue_suspects": suspects,
        "residue_count": len(suspects),
    }


def load_json(path: Path) -> Dict[str, str]:
    """读取 MTool 键值 JSON。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是键值对 JSON 对象")
    return data


def run(new_path: Path, base_path: Path, out_dir: Path) -> Path:
    """执行校验并写报告文件。返回报告路径。"""
    new_data = load_json(new_path)
    base_data = load_json(base_path)
    report = verify_merge(new_data, base_data)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "verify_merge_report.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(out)
    return out


def print_summary(report: Dict[str, object]) -> int:
    """打印人读摘要。返回退出码（残留/移除超阈值时非 0 提示复核）。"""
    c = report["counts"]
    print(f"合并校验: 总计 {c['total']} 键 | 新增 {c['added']} | "
          f"删除 {c['removed']} | 变更 {c['changed']} | 不变 {c['unchanged']}")
    print(f"残留/拼接疑似: {report['residue_count']} 条")
    if report["residue_count"]:
        for s in report["residue_suspects"][:10]:
            print(f"  ⚠ {s['key'][:40]} → {s['issues']}")
        if report["residue_count"] > 10:
            print(f"  … 共 {report['residue_count']} 条，详见报告文件")
    # removed 键较多时提示（可能是基准缺失或键被意外丢弃）
    if c["removed"] > max(5, c["total"] * 0.01):
        print(f"  ⚠ 删除键数 {c['removed']} 异常偏多，请确认是否误丢条目")
    return 1 if report["residue_count"] else 0
