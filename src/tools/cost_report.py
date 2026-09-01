# -*- coding: utf-8 -*-
"""cost_report.py — run 成本构成审计（真实 API usage 汇总）。

用法：
    python src/tools/cost_report.py <run_dir> [--model deepseek-v4-flash]

扫描 <run_dir>/batches/<stage>/*.response.raw.json 中落盘的完整响应
（含 usage 字段，见 engine._save_response / storage.save_batch_response_raw），
按阶段汇总：
    - 请求数 / prompt(命中/未命中) / 输出(含思考) / 思考 token
    - 缓存命中率、思考 token 占输出比
    - 按 config 价格表折算人民币费用（元）
    - repair/retry 子批请求数与费用（质量/成本指标）

旧格式响应（仅 content 字段，无 usage）自动跳过并在末尾提示数量；
不读取 meta.json 的累计值（避免与逐请求计数重复）。

退出码：0 正常；1 参数错误/run 目录不存在。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402


def _usage_cost(usage: Dict, pricing: Dict[str, float]) -> float:
    """按价格表折算单次请求费用（元）。与 schemas.Usage.cost_cny 同公式。"""
    return (
        int(usage.get("prompt_cache_hit_tokens", 0)) / 1e6 * pricing["in_hit"]
        + int(usage.get("prompt_cache_miss_tokens", 0)) / 1e6 * pricing["in_miss"]
        + int(usage.get("completion_tokens", 0)) / 1e6 * pricing["out"]
    )


def _collect(run_dir: Path, pricing: Dict[str, float]) -> Tuple[Dict[str, Dict], Dict]:
    """单次扫描全部批次响应，返回 ({阶段: 统计}, {repair/retry 子批汇总})。

    统计键：n/prompt/hit/miss/completion/reasoning/cost；旧格式（无 usage）跳过。
    """
    stages: Dict[str, Dict] = {}
    sub = {"n": 0, "cost": 0.0}
    for resp in sorted(run_dir.glob("batches/*/*.response.raw.json")):
        try:
            data = json.loads(resp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        usage = data.get("usage")
        if not usage:
            continue
        stage = resp.parent.name
        s = stages.setdefault(stage, {"n": 0, "prompt": 0, "hit": 0, "miss": 0,
                                      "completion": 0, "reasoning": 0, "cost": 0.0})
        s["n"] += 1
        # usage 字段键名：prompt_tokens / prompt_cache_hit_tokens /
        # prompt_cache_miss_tokens / completion_tokens / reasoning_tokens
        s["prompt"] += int(usage.get("prompt_tokens", 0))
        s["hit"] += int(usage.get("prompt_cache_hit_tokens", 0))
        s["miss"] += int(usage.get("prompt_cache_miss_tokens", 0))
        s["completion"] += int(usage.get("completion_tokens", 0))
        s["reasoning"] += int(usage.get("reasoning_tokens", 0))
        cost = _usage_cost(usage, pricing)
        s["cost"] += cost
        if ".repair" in resp.name or ".retry" in resp.name:
            sub["n"] += 1
            sub["cost"] += cost
    return stages, sub


def _count_legacy(run_dir: Path) -> int:
    """统计无 usage 的旧格式响应数（仅提示用）。"""
    n = 0
    for resp in sorted(run_dir.glob("batches/*/*.response.raw.json")):
        try:
            data = json.loads(resp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not data.get("usage"):
            n += 1
    return n


def main(argv: List[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    run_dir = Path(args[0]) if args else None
    model = config.DEFAULT_MODEL
    for a in argv:
        if a.startswith("--model="):
            model = a.split("=", 1)[1]
    if run_dir is None or not run_dir.exists():
        print("用法: python src/tools/cost_report.py <run_dir> [--model <模型>]")
        print(f"run 目录不存在或未指定: {run_dir}")
        return 1

    try:
        pricing = config.get_pricing(model)
    except ValueError as e:
        print(f"模型价格不可用: {e}")
        return 1

    stages, sub = _collect(run_dir, pricing)
    if not stages:
        print(f"{run_dir}: 未找到含 usage 的批次响应（旧格式 response.raw.json 已跳过）")
        return 0

    print(f"模型: {model} | 价格(元/M): 缓存命中 {pricing['in_hit']} / 未命中 {pricing['in_miss']} / 输出 {pricing['out']}")
    print(f"{'阶段':<18} {'请求':>4} {'prompt':>10} {'命中':>10} {'未命中':>10} "
          f"{'输出':>10} {'思考':>8} {'命中率':>7} {'思考占输出':>8} {'费用(元)':>10}")
    print("-" * 110)
    total = {"n": 0, "prompt": 0, "hit": 0, "miss": 0, "completion": 0, "reasoning": 0, "cost": 0.0}
    for stage in sorted(stages):
        s = stages[stage]
        hit_rate = 100.0 * s["hit"] / max(s["prompt"], 1)
        reason_ratio = 100.0 * s["reasoning"] / max(s["completion"], 1)
        print(f"{stage:<18} {s['n']:>4} {s['prompt']:>10} {s['hit']:>10} {s['miss']:>10} "
              f"{s['completion']:>10} {s['reasoning']:>8} {hit_rate:>6.1f}% "
              f"{reason_ratio:>7.1f}% {s['cost']:>10.4f}")
        for key in ("n", "prompt", "hit", "miss", "completion", "reasoning", "cost"):
            total[key] += s[key]
    print("-" * 110)
    hit_rate = 100.0 * total["hit"] / max(total["prompt"], 1)
    reason_ratio = 100.0 * total["reasoning"] / max(total["completion"], 1)
    print(f"{'合计':<18} {total['n']:>4} {total['prompt']:>10} {total['hit']:>10} "
          f"{total['miss']:>10} {total['completion']:>10} {total['reasoning']:>8} "
          f"{hit_rate:>6.1f}% {reason_ratio:>7.1f}% {total['cost']:>10.4f}")
    if sub["n"]:
        print(f"其中 repair/retry 子批: {sub['n']} 请求, {sub['cost']:.4f} 元 "
              f"({100.0 * sub['cost'] / max(total['cost'], 1e-9):.1f}% 费用)")
    legacy = _count_legacy(run_dir)
    if legacy:
        print(f"注: {legacy} 个旧格式响应（无 usage 字段）未计入")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
