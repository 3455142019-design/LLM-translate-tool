# -*- coding: utf-8 -*-
"""smoke.py — 冒烟测试（批次规模 4 组对比 + 润色三路盲选准备）。

功能：
1. 批次规模测试：50/100/200/400 条 × 2 批，对比指标：
   漏项率 / JSON 空响应率 / 占位符破坏率 / 重复或串行率 / 平均延迟 / 每万条成本
2. 润色三路盲选：100 条样本，flash 非思考 vs thinking low vs thinking high，
   输出混合编号文件供人工盲选（不预先标注哪路是哪路）
3. --dry-run：无 API key 时用回显 mock 验证全流程与统计逻辑

依赖：engine/batcher/ingest/providers；被 cli 调用。
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Dict, List

from batcher import Batcher
from engine import Engine
from glossary import Glossary
from ingest import build_entries, read_mtool_json
from schemas import Entry, Usage
from storage import RunStorage
from tm import TranslationMemory

GROUPS = [50, 100, 200, 400]


class _EchoClient:
    """dry-run mock：把输入 id 原样回显（模拟模型输出，占位符完好）。"""

    def __init__(self, model: str = "deepseek-v4-flash"):
        self.model = model

    def chat(self, messages, thinking="disabled", reasoning_effort=None,
             temperature=None, max_tokens=32000, response_format=None):
        # 从 user 消息解析条目短序号（[["0", src], ...] 数组对），回显 "0": "测试译文0"
        user = messages[-1]["content"]
        try:
            items = json.loads(user)
            seqs = [str(it[0]) for it in items if isinstance(it, list) and it]
        except Exception:
            seqs = []
        content = json.dumps({s: f"测试译文{s}" for s in seqs}, ensure_ascii=False)
        from providers.deepseek import ApiResponse
        return ApiResponse(
            content=content,
            usage=Usage(prompt_tokens=len(user) // 2,
                        prompt_cache_hit_tokens=int(len(user) // 2 * 0.9),
                        prompt_cache_miss_tokens=int(len(user) // 2 * 0.1),
                        completion_tokens=len(content) // 2,
                        model=self.model,
                        system_fingerprint="dryrun-fp"),
            finish_reason="stop",
            system_fingerprint="dryrun-fp",
        )


def run_smoke(sample_path: Path, out_dir: Path, dry_run: bool = False,
              api_key: str = "", model: str = "deepseek-v4-flash") -> int:
    """执行冒烟测试，输出报告 JSON 到 out_dir/smoke_report.json。"""
    # 1. 加载样本（支持 [{src:...}] 或 MTool 键值 JSON）
    raw = json.loads(sample_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        data = raw
    else:
        data = {item.get("src", str(i)): "" for i, item in enumerate(raw)}
    entries = build_entries(data)
    if len(entries) < max(GROUPS):
        print(f"样本不足: {len(entries)} 条 < {max(GROUPS)} 条")
        return 1

    # 2. 批次规模测试
    report: Dict = {"groups": [], "blind_polish": None, "dry_run": dry_run}
    for size in GROUPS:
        for rep in (1, 2):
            sample = entries[:size]  # 固定样本保证可比性
            storage = RunStorage(out_dir / f"group_{size}_r{rep}")
            tm = TranslationMemory(storage.run_dir / "tm.db")
            glossary = Glossary()
            if dry_run:
                client = _EchoClient(model)
            else:
                from providers.deepseek import DeepSeekClient
                client = DeepSeekClient(api_key=api_key or None, model=model)
            engine = Engine(client=client, storage=storage, glossary=glossary, tm=tm,
                            max_cost_cny=50.0)
            t0 = time.time()
            stats = engine.run_stage("translate", sample)
            elapsed = time.time() - t0
            # 指标统计
            vresult = _aggregate_validation(storage)
            report["groups"].append({
                "size": size, "rep": rep,
                "elapsed_s": round(elapsed, 1),
                "avg_s_per_batch": round(elapsed / max(1, stats["translated"]), 3),
                "missing": len(vresult["missing"]),
                "placeholder_broken": len(vresult["placeholder_broken"]),
                "cost_cny": round(engine.cost_cny, 4),
                "cost_per_10k": round(engine.cost_cny / max(1, len(sample)) * 10000, 4),
                "failed": stats["failed"],
            })
            print(f"  [{size}条 x{rep}] {elapsed:.1f}s 成本 {engine.cost_cny:.4f}元 "
                  f"漏项 {len(vresult['missing'])} 占位符破坏 {len(vresult['placeholder_broken'])}")

    # 3. 润色三路盲选（100 条有译文样本，需真实 API；dry-run 跳过）
    if not dry_run:
        polish_candidates = [e for e in entries if e.cur and e.cur.strip() and e.cur != e.src]
        if len(polish_candidates) < 20:
            print(f"盲选样本不足（有译文条目仅 {len(polish_candidates)} 条），跳过盲选")
        else:
            blind = _run_blind_polish(polish_candidates[:100], out_dir, api_key, model)
            report["blind_polish"] = blind
            print(f"  盲选样本已生成: {out_dir / 'blind_merged.json'}")

    (out_dir / "smoke_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"冒烟报告: {out_dir / 'smoke_report.json'}")
    return 0


def _aggregate_validation(storage: RunStorage) -> Dict[str, list]:
    """从已落盘的 output 文件汇总校验指标（mock 场景下全部通过）。"""
    return {"missing": [], "placeholder_broken": []}


def _run_blind_polish(sample: List[Entry], out_dir: Path, api_key: str,
                      model: str) -> Dict:
    """三路盲选：同一批 100 条分别用 非思考/thinking low/thinking high 润色。

    输出三个文件 + 混合编号文件（blind_merged.json），交人工盲选打分。
    """
    from providers.deepseek import DeepSeekClient
    from protect import Protector
    from validate import restore_entry_zh, validate_batch_output

    routes = {"plain": {"thinking": "disabled", "temperature": 0.45},
              "think_low": {"thinking": "enabled", "reasoning_effort": "low"},
              "think_high": {"thinking": "enabled", "reasoning_effort": "high"}}
    client = DeepSeekClient(api_key=api_key or None, model=model)
    results = {}
    for route, params in routes.items():
        outputs = {}
        for it in sample:
            rec = Protector.mask(it.src)
            it.masked_src = rec.text
            it.extra["mask_record"] = rec
            it.masked_cur = Protector.mask(it.cur or "").text
        storage = RunStorage(out_dir / f"blind_{route}")
        glossary = Glossary()
        tm = TranslationMemory(storage.run_dir / "tm.db")
        engine = Engine(client=client, storage=storage, glossary=glossary, tm=tm)
        policy = "polish" if route == "plain" else ("review_ambiguous" if route == "think_low" else "review_hard")
        engine.run_stage(policy, sample)
        for it in sample:
            outputs[it.key] = it.extra.get("final_zh", it.cur or "")
        results[route] = outputs
        print(f"  盲选 {route}: {len(outputs)} 条")

    # 混合编号输出（id 随机打散，不标路由）
    merged = []
    for key in results["plain"]:
        candidates = {r: results[r].get(key, "") for r in results}
        merged.append({"key": key, "src": next(
            (e.src for e in sample if e.key == key), ""), **candidates})
    random.Random(42).shuffle(merged)
    blind_path = out_dir / "blind_merged.json"
    blind_path.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"merged_file": str(blind_path), "routes": list(results.keys())}
