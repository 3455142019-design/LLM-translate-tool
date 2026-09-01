# -*- coding: utf-8 -*-
"""estimate_cost.py — 正式版翻译预算测算工具（8/10 发售当天用）。

用法：
    python src/tools/estimate_cost.py --input <MTool 导出的正式版 ManualTransFile.json>
        [--mode translate|polish] [--model deepseek-v4-flash]
        [--cache-hit-rate 0.85] [--thinking-out-mult 1.0]
        [--safety-margin 1.2]

输入：MTool 键值对（键=日文原文，值=现译文/原文）。
输出：token 明细（输入/输出/思考）+ 费用估算（缓存命中率可调）+
      建议充值金额（含安全余量）。

估算口径（与 batcher.py 一致）：
- 输入 = 键 token 总和 + 每条结构开销 6 token
- 输出 = 值 token 总和（translate 未译时按 键×0.7 日→中比率）
- 思考模式：reasoning token ≈ 输出 × thinking-out-mult（按模型价格计费）
- 费用 = 输入×命中率×in_hit + 输入×未命中率×in_miss + (输出+思考)×out

基准（2026-08-05 体验版实测）：10,509 键 / 原文 79,724 token /
全量重翻 ≈ 0.14-0.17 元（flash）；正式版预计文本量 1.5-3 倍。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from batcher import Batcher


def main() -> int:
    ap = argparse.ArgumentParser(description="正式版翻译预算测算（token + 费用 + 建议充值额）")
    ap.add_argument("--input", required=True, help="MTool 导出 JSON（键=日文原文）")
    ap.add_argument("--mode", choices=("translate", "polish"), default="translate")
    ap.add_argument("--model", default=config.DEFAULT_MODEL)
    ap.add_argument("--cache-hit-rate", type=float, default=0.85,
                    help="输入缓存命中率（system 静态+术语表固定时实测可达 88.6%）")
    ap.add_argument("--thinking-out-mult", type=float, default=0.0,
                    help="思考模式额外输出倍率（reasoning/输出；low≈0.5 medium≈1.0 high≈2.0）")
    ap.add_argument("--safety-margin", type=float, default=1.2,
                    help="建议充值安全余量（默认 1.2 倍）")
    ap.add_argument("--peak", action="store_true",
                    help="按高峰时段计价（北京 9:00-12:00 / 14:00-18:00 价格 ×2）")
    args = ap.parse_args()

    pricing = config.get_pricing(args.model)
    mult = 2.0 if args.peak else 1.0
    if args.peak:
        pricing = {k: v * mult for k, v in pricing.items()}
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    b = Batcher()

    key_tok = sum(b.count_tokens(k) for k in data)
    val_tok = sum(b.count_tokens(v) for v in data.values())
    struct = 6
    in_tok = key_tok + struct * len(data)
    if args.mode == "translate":
        # 未译条目（值==键）按日→中比率 0.7 估输出
        untranslated = [k for k, v in data.items() if v == k]
        if untranslated:
            utok = sum(b.count_tokens(k) for k in untranslated)
            out_tok = int(utok * 0.7)
        else:
            out_tok = 0
    else:
        out_tok = val_tok
    think_tok = int(out_tok * args.thinking_out_mult)

    hit = in_tok * args.cache_hit_rate
    miss = in_tok * (1 - args.cache_hit_rate)
    cost = (hit / 1e6 * pricing["in_hit"] + miss / 1e6 * pricing["in_miss"]
            + (out_tok + think_tok) / 1e6 * pricing["out"])

    print(f"===== 预算测算（{args.model} / {args.mode}）=====")
    print(f"条目数: {len(data)}")
    print(f"原文 token: {key_tok:,}（平均 {key_tok/max(1,len(data)):.1f}/条）")
    print(f"译文 token: {val_tok:,}（平均 {val_tok/max(1,len(data)):.1f}/条）")
    print(f"输入 token: {in_tok:,}（原文 + 结构开销 {struct}×{len(data)}）")
    print(f"输出 token: {out_tok:,}" + (f" + 思考 {think_tok:,}" if think_tok else ""))
    print(f"缓存命中率: {args.cache_hit_rate:.0%}")
    print(f"费用明细: 输入命中 {hit/1e6*pricing['in_hit']:.3f} 元 + "
          f"输入未命中 {miss/1e6*pricing['in_miss']:.3f} 元 + "
          f"输出 {out_tok/1e6*pricing['out']:.3f} 元"
          + (f" + 思考 {think_tok/1e6*pricing['out']:.3f} 元" if think_tok else ""))
    print(f"预计总费用: {cost:.3f} 元" + ("（高峰时段价 ×2 已计入）" if args.peak else ""))
    if not args.peak:
        print("⚠ 若在高峰时段运行（北京 9:00-12:00 / 14:00-18:00），费用 ×2，建议加 --peak 重算")
    print(f"建议充值: {cost*args.safety_margin:.2f} 元（安全余量 ×{args.safety_margin}）")
    print(f"（若正式版需 thinking 审校双关条目，可再加跑 2-3 元余量）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
