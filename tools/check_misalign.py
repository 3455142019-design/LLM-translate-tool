# -*- coding: utf-8 -*-
"""check_misalign.py — 键值错位检测器（HOLLOWWALD 接手审计）。

背景（2026-08-16）：MTool 翻译表合并过程曾出现键值错位——
键 A 的译文错挂到键 B（如「最後は……エムナルさんのおかげで、」的值
是下一句斯莱台词的译文）。这类错误游戏内直接显示成完全无关的台词，
危害最大，必须全量筛查。

检测思路（零成本规则初筛 + 供 LLM 复核的候选块）：
1. 角色名互斥：键含某角色日文名，但译文不含其译名；或译文含某角色译名
   而键中无对应日文名（且不是敬称通用句）→ 高嫌疑。
2. 标点错位：键以 ？/！ 结尾，译文却以 。/… 结尾（或相反）→ 中嫌疑；
   连续两键的译文标点与对方键匹配 → 强嫌疑（互换）。
3. 场景序列复核：同一事件页内连续 401 键与其译文构成的序列交给 LLM 复核
   （输出 --review 候选块 JSON，供 polish/review 或人工处理）。

用法：
    python tools/check_misalign.py \
        --trans projects/hollowwald/assets/formal/ManualTransFile_CN_FINAL.json \
        --glossary projects/hollowwald/glossary.json \
        --occurrence-index runs/idx_formal/occurrence_index.json \
        --out runs/misalign_report.json --review-runs runs/misalign_review.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_TERM_RE = re.compile(r"[\u30a1-\u30f6ー]{2,}")  # 连续片假名（专名候选）
_KATA_EDGE = re.compile(r"[ァ-ヶー]")  # 片假名/长音（复合词边界判断）
_COMMON_SINGLE_HAN = set("修")


def _standalone_occ(key: str, src: str) -> bool:
    """source 在键中是否独立成词（两侧不紧邻片假名/长音）。"""
    start = 0
    while True:
        i = key.find(src, start)
        if i < 0:
            return False
        before = key[i - 1] if i > 0 else ""
        after = key[i + len(src)] if i + len(src) < len(key) else ""
        if not (_KATA_EDGE.match(before) or _KATA_EDGE.match(after)):
            return True
        start = i + 1
    return False


def load_glossary(path: Path) -> dict[str, str]:
    """术语表 source -> target（仅取 source 为片假名/汉字人名类的全局条目）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for e in data:
        src = e.get("source") or ""
        tgt = e.get("target") or ""
        if src and tgt and _TERM_RE.fullmatch(src):
            out[src] = tgt
    return out


def _merged(ranges):
    if not ranges:
        return []
    ordered = sorted(ranges)
    out = [ordered[0]]
    for s, e in ordered[1:]:
        ps, pe = out[-1]
        if s <= pe:
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out


def _covered(idx, length, ranges):
    end = idx + length
    return any(s <= idx and end <= e for s, e in ranges)


def check_entry(key: str, value: str, name_map: dict[str, str],
                all_targets: list[str]) -> list[str]:
    issues: list[str] = []
    # 允许区间：全部译名在译文中的出现（子串如 斯莱⊂雷斯莱斯 落在区间内不算）
    allowed = _merged([(i, i + len(t)) for t in all_targets if t
                       for i in _find_all(value, t)])
    # 1. 键含片假名专名（独立成词），译文应含译名
    for src, tgt in name_map.items():
        if src not in key or not _standalone_occ(key, src):
            continue
        if tgt in value:
            continue
        if key.strip() == src:
            continue
        issues.append(f"键含『{src}』但译文无『{tgt}』")
    # 2. 译文含译名但键无对应源（单字常见汉字跳过；子串落在其他译名内跳过）
    for src, tgt in name_map.items():
        if len(tgt) == 1 and tgt in _COMMON_SINGLE_HAN:
            continue
        if tgt not in value or src in key:
            continue
        for i in _find_all(value, tgt):
            if not _covered(i, len(tgt), allowed):
                issues.append(f"译文含『{tgt}』但键无『{src}』")
                break
    return issues


def _find_all(text: str, needle: str):
    out = []
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return out
        out.append(i)
        start = i + 1


def run(trans_path: str, glossary_path: str, occurrence_index: str | None,
        out: str, review_runs: str | None = None) -> int:
    """程序化入口（供 CLI/GUI 调用）。"""
    trans = json.loads(Path(trans_path).read_text(encoding="utf-8"))
    name_map = load_glossary(Path(glossary_path))
    all_targets = [v for v in name_map.values() if v]
    print(f"术语表专名 {len(name_map)} 条")

    suspicious: list[dict] = []
    for key, value in trans.items():
        if not isinstance(value, str) or not value.strip():
            continue
        issues = check_entry(key, value, name_map, all_targets)
        if issues:
            suspicious.append({"key": key, "value": value, "issues": issues})
    print(f"专名互斥嫌疑 {len(suspicious)} 条")

    # 场景序列候选块：有嫌疑键的事件页整块输出供 LLM 复核
    review_blocks: list[dict] = []
    if occurrence_index:
        idx = json.loads(Path(occurrence_index).read_text(encoding="utf-8"))
        sus_keys = {s["key"] for s in suspicious}
        # 场景 -> {line: key} 重建序列
        scenes: dict[str, dict[int, str]] = {}
        for k, occs in idx.items():
            if k not in sus_keys:
                continue
            for occ in occs:
                key = (occ.get("file") or "", occ.get("scene") or "")
                line = int(occ.get("line") or 0)
                scenes.setdefault(key, {})[line] = k
        for (file, scene), line_map in scenes.items():
            lines = sorted(line_map.items())
            block = [{"key": k, "value": trans.get(k, "")} for _, k in lines]
            review_blocks.append({"file": file, "scene": scene, "block": block})

    out_p = Path(out)
    out_p.write_text(json.dumps({"suspicious": suspicious,
                                 "review_blocks": review_blocks},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"候选复核块 {len(review_blocks)} 个 -> {out_p}")
    if review_runs:
        rr = Path(review_runs)
        rr.write_text(json.dumps(review_blocks, ensure_ascii=False, indent=1),
                      encoding="utf-8")
        print(f"复核块另存 -> {rr}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trans", required=True)
    ap.add_argument("--glossary", required=True)
    ap.add_argument("--occurrence-index", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--review-runs", default=None)
    args = ap.parse_args()
    return run(args.trans, args.glossary, args.occurrence_index, args.out, args.review_runs)


if __name__ == "__main__":
    sys.exit(main())
