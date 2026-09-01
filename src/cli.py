# -*- coding: utf-8 -*-
"""cli.py — sr_translate 命令行入口。

用法示例：
    # 初翻（日→中，非思考）
    python cli.py translate --input E:/.../ManualTransFile.json --out runs/r1
    # 润色（分类驱动：低风险非思考，高风险 thinking 审校）
    python cli.py polish --input ... --out runs/r1
    # 全流程（初翻 -> QA -> 润色/审校 -> 合并）
    python cli.py pipeline --input ... --out runs/final
    # 只跑 QA 筛查（零成本，输出风险清单）
    python cli.py detect --input ...
    # 导入历史语料到翻译记忆
    python cli.py import-corpus --legacy SR1028.json --out runs/tm
    # 扫描口上 rb 建出现索引
    python cli.py scan-talk --talk-dir E:/.../Mod_Talk --out runs/idx
    # 冒烟测试（4 组批次规模对比，需 API key；--dry-run 无 key 验证流程）
    python cli.py smoke --sample 口上样本.json --out runs/smoke

通用参数：--resume 断点续跑 / --max-cost-cny 预算上限 / --model（默认 flash）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

# 允许直接 `python cli.py` 运行（src 目录在脚本同级）
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 强制 stdout/stderr 行缓冲：后台重定向到文件时避免块缓冲导致日志长时间不刷出、
# 被误判为卡死（2026-08-08 会话踩坑：python 不带 -u 跑后台翻译，日志停滞）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except (AttributeError, OSError, ValueError):
        pass

import config
from batcher import Batcher
from bundle import ProjectBundle, create_project
from context import (Occurrence, OccurrenceIndex, apply_context,
                    describe_occurrences, scan_rb_files)
from engine import Engine
from glossary import Glossary
from ingest import (build_entries, classify_value, filter_for_polish,
                    filter_for_translation, read_mtool_json)
from project import ProjectStore, TranslationProject
from providers.factory import PROTOCOLS, make_client
from qa import HIGH_RISK_THRESHOLD, LOW_RISK_THRESHOLD, run_qa
from qa.full_qa import FullQaReport, print_summary as full_qa_summary, run_full_qa, write_report
from rmmz import scan_rmmz_events
from schemas import Entry, EntryStatus, RunMeta, TmStatus, Usage
from storage import RunStorage
from thinking import ALL_EFFORTS, resolve_effort
from tm import TranslationMemory
from verify_merge import load_json as verify_load_json, print_summary as verify_summary, run as verify_run


def _make_run_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _load_entries(input_path: Path, source_tag: str | None = None) -> tuple[list[Entry], list[str]]:
    data, dups = read_mtool_json(input_path)
    entries = build_entries(data, source_tag=source_tag)
    return entries, dups


def _apply_occurrence_index(entries: list[Entry], index_path: str, inject_context: bool = False) -> None:
    """加载 occurrence index 并应用到条目（speaker/scene/多语境）。

    inject_context=True 时，把前/后句+说话人+场景的描述写入
    entry.extra["context_desc"]（engine._entry_context 优先使用），
    让模型看到该句在对话中的上下文，翻译更连贯。
    """
    if not index_path:
        return
    p = Path(index_path)
    if not p.exists():
        print(f"  ⚠ occurrence index 不存在: {p}，跳过语境注入")
        return
    raw = json.loads(p.read_text(encoding="utf-8"))
    index = OccurrenceIndex(map={t: [Occurrence(**o) for o in occs]
                                  for t, occs in raw.items()})
    apply_context(entries, index)
    n_ctx = sum(1 for e in entries if e.speaker or e.multi_context)
    print(f"  语境注入: {n_ctx}/{len(entries)} 条获得说话人/场景/多语境信息")
    if inject_context:
        n_desc = 0
        for e in entries:
            desc = describe_occurrences(e, index)
            if desc:
                e.extra["context_desc"] = desc
                n_desc += 1
        print(f"  上下文描述注入: {n_desc}/{len(entries)} 条（前/后句+说话人+场景）")


def _make_storage(out: Path, project: TranslationProject | None = None,
                  resume: bool = False) -> RunStorage:
    base = (project.path / "runs" if project else out)
    if resume:
        # 复用最近的含 journal 的 run 目录（completed_batch_numbers 才能识别旧批次）。
        # 2026-08-10 修复：此前 resume 总是新建 run 目录，旧批次无法跳过。
        candidates = [d for d in base.glob("*") if d.is_dir() and (d / "journal.jsonl").exists()]
        if candidates:
            latest = max(candidates, key=lambda d: d.stat().st_mtime)
            print(f"  [resume] 复用 run 目录: {latest}")
            return RunStorage(latest)
    run_id = _make_run_id()
    return RunStorage(base / run_id)


def _pricing_from_args(args) -> dict | None:
    values = (args.price_cache_hit, args.price_input, args.price_output)
    if not any(values):
        return None
    if any(value < 0 for value in values):
        raise ValueError("价格必须是非负的人民币/百万 Token 数值")
    return {"in_hit": args.price_cache_hit, "in_miss": args.price_input, "out": args.price_output}


def _make_runtime(args) -> tuple[RunStorage, TranslationMemory, ProjectStore | None]:
    project = None
    if args.project:
        source_value = args.input or args.legacy or args.sample
        if not source_value:
            raise ValueError("项目模式需要输入文件")
        project = TranslationProject.create_or_open(Path(args.out), Path(source_value), args.project_name)
    storage = _make_storage(Path(args.out), project, resume=getattr(args, "resume", False))
    store = None
    if project is not None:
        resolution = resolve_effort(args.protocol, args.model, args.thinking_effort)
        store = ProjectStore(
            project=project,
            run_id=storage.run_dir.name,
            provider=args.protocol,
            model=args.model,
            requested_effort=args.thinking_effort,
            resolved_effort=resolution.actual,
            pricing=_pricing_from_args(args),
        )
        args._project_store = store
    tm = TranslationMemory(project.tm_path if project else storage.run_dir / "tm.db")
    args._translation_memory = tm
    return storage, tm, store


def _make_engine(args, storage: RunStorage, glossary: Glossary, tm: TranslationMemory,
                 project_store: ProjectStore | None = None) -> Engine:
    if getattr(args, "dry_run", False):
        # dry-run：无 API key 验证全流程（echo mock）
        from smoke import _EchoClient
        client = _EchoClient(model=args.model)
    else:
        api_key = getattr(args, "api_key", "") or None
        client = make_client(
            protocol=args.protocol,
            api_key=api_key,
            model=args.model,
            base_url=args.base_url,
            pricing=_pricing_from_args(args),
        )
    batcher = None
    if (getattr(args, "batch_target_input_tokens", 0) > 0
            or getattr(args, "batch_hard_max_items", 0) > 0
            or getattr(args, "batch_target_output_tokens", 0) > 0
            or getattr(args, "batch_max_maps", 0) > 0):
        from batcher import Batcher
        limits = dict(config.BATCH_LIMITS)
        if getattr(args, "batch_target_input_tokens", 0) > 0:
            limits["target_input_tokens"] = args.batch_target_input_tokens
        if getattr(args, "batch_hard_max_items", 0) > 0:
            limits["hard_max_items"] = args.batch_hard_max_items
        if getattr(args, "batch_target_output_tokens", 0) > 0:
            limits["target_output_tokens"] = args.batch_target_output_tokens
        if getattr(args, "batch_max_maps", 0) > 0:
            limits["max_maps"] = args.batch_max_maps
        batcher = Batcher(limits=limits)
        print(f"  ⚙ 批次预算覆盖: target_input={limits['target_input_tokens']:,}"
              f" target_output={limits['target_output_tokens']:,}"
              f" hard_max={limits['hard_max_items']:,}"
              + (f" max_maps={limits['max_maps']:,}" if limits.get('max_maps') else "")
              + f"（默认 {config.BATCH_LIMITS['target_input_tokens']:,}/"
                f"{config.BATCH_LIMITS['target_output_tokens']:,}/"
                f"{config.BATCH_LIMITS['hard_max_items']:,}）")
    bundle = getattr(args, "_bundle", None)
    return Engine(client=client, storage=storage, glossary=glossary, tm=tm,
                  max_cost_cny=args.max_cost_cny,
                  thinking_effort=args.thinking_effort,
                  project_store=project_store, batcher=batcher,
                  context_chain=getattr(args, "context_chain", False),
                  chain_budget=getattr(args, "chain_budget_tokens", 950_000),
                  # pun_manifest（双关回归库）并入 key_lines：confirmed 预填复用，
                  # pending 注入提示（2026-08-11 教训：双关反复复发，需要回归库）
                  key_lines=bundle.merged_key_lines if bundle else None,
                  world_context=bundle.context_text if bundle else "",
                  project_name=bundle.name if bundle else "Succubus Rhapsodia",
                  chars_info=bundle.chars_map if bundle else None)


def _print_summary(stage: str, stats: dict, engine: Engine) -> None:
    u = engine.total_usage
    print(f"[{stage}] 完成: 新译 {stats['translated']} / TM复用 {stats['reused']} / "
          f"修复 {stats['repaired']} / 失败 {stats['failed']}")
    print(f"  成本: {engine.cost_cny:.4f} 元 | 输入 {u.prompt_tokens:,} "
          f"(命中 {u.prompt_cache_hit_tokens:,} 未命中 {u.prompt_cache_miss_tokens:,}) | "
          f"输出 {u.completion_tokens:,} (思考 {u.reasoning_tokens:,})")


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def _positive_int(value: str) -> int:
    """argparse type：正整数校验（--workers 0/负数显式报错）。"""
    v = int(value)
    if v < 1:
        raise argparse.ArgumentTypeError("必须 >= 1")
    return v


def _worker_cmd(args, stage: str, shard_file: Path, part_dir: Path, n: int) -> list[str]:
    """构造 worker 子进程命令：透传模型/密钥/价格/术语表等参数，预算均分。

    顶层参数（--model/--protocol/价格/预算）放子命令前；子命令级参数
    （--input/--out/--dry-run/--api-key/术语表等）放子命令后。
    """
    cmd = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
           "--model", args.model,
           "--max-cost-cny", str(args.max_cost_cny / n),
           "--max-stage-cost", str(args.max_stage_cost / n)]
    if args.protocol != "deepseek_chat":
        cmd += ["--protocol", args.protocol]
    if args.base_url:
        cmd += ["--base-url", args.base_url]
    if args.thinking_effort != "auto":
        cmd += ["--thinking-effort", args.thinking_effort]
    for flag, val in (("--price-cache-hit", args.price_cache_hit),
                      ("--price-input", args.price_input),
                      ("--price-output", args.price_output)):
        if val:
            cmd += [flag, str(val)]
    cmd += [stage, "--input", str(shard_file), "--out", str(part_dir)]
    if args.dry_run:
        cmd += ["--dry-run"]
    # API key 不拼进命令行（Windows 进程列表可读明文）；由 _run_parallel
    # 通过环境变量 SR_TRANSLATE_API_KEY 传给 worker（2026-08-18 修复）。
    if getattr(args, "occurrence_index", ""):
        cmd += ["--occurrence-index", args.occurrence_index]
    if getattr(args, "inject_context", False):
        cmd += ["--inject-context"]
    if args.glossary:
        cmd += ["--glossary", args.glossary]
    if args.glossary_json:
        cmd += ["--glossary-json", args.glossary_json]
    if getattr(args, "bundle", ""):
        cmd += ["--bundle", args.bundle]
    if getattr(args, "source_tag", ""):
        cmd += ["--source-tag", args.source_tag]
    if getattr(args, "batch_target_input_tokens", 0) > 0:
        cmd += ["--batch-target-input-tokens", str(args.batch_target_input_tokens)]
    if getattr(args, "batch_hard_max_items", 0) > 0:
        cmd += ["--batch-hard-max-items", str(args.batch_hard_max_items)]
    if getattr(args, "batch_target_output_tokens", 0) > 0:
        cmd += ["--batch-target-output-tokens", str(args.batch_target_output_tokens)]
    if getattr(args, "batch_max_maps", 0) > 0:
        cmd += ["--batch-max-maps", str(args.batch_max_maps)]
    if getattr(args, "context_chain", False):
        cmd += ["--context-chain"]
    return cmd


def _run_parallel(args, stage: str) -> int:
    """--workers N：分片并行执行 translate/polish（子进程隔离）。

    流程：加载/过滤（与串行同口径）-> 按 batcher 语义批次轮转分片 ->
    各 worker 独立 run 目录执行 -> 汇总 batches output（经 input.json 的
    id->key 映射）为 final/ManualTransFile_zh.json；成本按各 worker journal
    的 api_usage 事件汇总（meta.json 的 cost 是累计值，相加会虚高）。
    """
    n = max(2, args.workers)
    if getattr(args, "project", False):
        print("--project 模式暂不支持 --workers（多进程写项目库会冲突），请去掉 --workers 或 --project")
        return 1
    if args.resume:
        print("--workers 模式下 --resume 无意义（worker 均为新 run 目录），请去掉 --workers 或 --resume")
        return 1
    if stage == "translate":
        entries, dups = _load_entries(Path(args.input), source_tag=None)
    else:
        entries, dups = _load_entries(Path(args.input), source_tag=args.source_tag)
    _apply_occurrence_index(entries, getattr(args, "occurrence_index", ""),
                            getattr(args, "inject_context", False))
    todo = (filter_for_translation(entries) if stage == "translate"
            else filter_for_polish(entries))
    print(f"总条目 {len(entries)}，待{stage} {len(todo)}，重复键 {len(dups)}，workers={n}")
    if len(todo) < n * 8:
        print(f"条目过少（{len(todo)} < {n * 8}），回退串行")
        args.workers = 1  # 防 cmd_translate/cmd_polish 入口再次进入 _run_parallel
        return cmd_translate(args) if stage == "translate" else cmd_polish(args)

    # 条目级轮转分片：永不空转，各片语义与全集同分布
    # （worker 内 batcher 仍会按语义组重新排序分批；术语一致性由全局术语表保证）
    shards: list[list[Entry]] = [[] for _ in range(n)]
    for idx, e in enumerate(todo):
        shards[idx % n].append(e)

    # 随机后缀防同秒重跑同一 --out 时复用旧 base 目录（旧 worker 产物会被重复并入）
    run_id = f"{_make_run_id()}_{random.randrange(10000):04d}"
    base = Path(args.out) / run_id
    parts_dir = base / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    procs: list[tuple[int, subprocess.Popen]] = []
    for i, shard in enumerate(shards):
        if not shard:
            continue
        shard_file = parts_dir / f"part_{i:02d}.json"
        data = ({e.key: e.key for e in shard} if stage == "translate"
                else {e.key: (e.cur or "") for e in shard})
        shard_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        part_dir = base / f"part_{i:02d}"
        log = open(parts_dir / f"part_{i:02d}.log", "w", encoding="utf-8")
        cmd = _worker_cmd(args, stage, shard_file, part_dir, n)
        env = os.environ.copy()
        if args.api_key:
            # 密钥走环境变量，不进入子进程命令行（防进程列表明文泄露）
            env["SR_TRANSLATE_API_KEY"] = args.api_key
        procs.append((i, subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                          env=env)))
        log.close()  # 子进程持有句柄副本，主进程及时释放
    failed = []
    for i, p in procs:
        if p.wait() != 0:
            failed.append(i)

    # 汇总：合并各 part 产物 + 成本（journal api_usage，与 meta 累计值交叉校验）
    merged: dict[str, str] = {}
    total_cost = 0.0
    meta_cost = 0.0
    for i, _ in procs:
        part_dir = base / f"part_{i:02d}"
        for run_dir in part_dir.iterdir():
            if not run_dir.is_dir():
                continue
            for inp in run_dir.glob("batches/*/*.input.json"):
                try:
                    input_data = json.loads(inp.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                id2key = {it["id"]: it["key"] for it in input_data}
                out_path = inp.with_name(inp.name.replace(".input.json", ".output.json"))
                if not out_path.exists():
                    continue
                out_data = json.loads(out_path.read_text(encoding="utf-8"))
                for eid, zh in out_data.items():
                    if eid in id2key and zh:
                        merged[id2key[eid]] = zh
            journal = run_dir / "journal.jsonl"
            if journal.exists():
                for line in journal.read_text(encoding="utf-8").splitlines():
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("event") == "api_usage":
                        total_cost += float(ev.get("cost", 0.0))
            # meta.json 的 cost_cny 是累计值，取每 worker 最大值作为交叉校验基准
            for meta in run_dir.glob("batches/*/*.meta.json"):
                try:
                    meta_cost = max(meta_cost, float(json.loads(
                        meta.read_text(encoding="utf-8")).get("cost_cny", 0.0)))
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
    final_dir = base / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    out = final_dir / "ManualTransFile_zh.json"
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{stage}×{n}] 合并: {len(merged)} 条 -> {out}")
    print(f"[{stage}×{n}] 总成本: {total_cost:.4f} 元 | 失败 worker: {failed or '无'}")
    if abs(meta_cost - total_cost) > 0.01:
        print(f"  ⚠ journal 成本 {total_cost:.4f} 元 与 meta 累计 {meta_cost:.4f} 元 偏差 >0.01 元"
              f"（journal 轮转可能丢早期事件，成本以 meta 累计值为准）")
    for i, _ in procs:
        print(f"  worker {i} 日志: {parts_dir / f'part_{i:02d}.log'}")
    return 0 if not failed else 1


def cmd_translate(args) -> int:
    if getattr(args, "workers", 1) > 1:
        return _run_parallel(args, "translate")
    entries, dups = _load_entries(Path(args.input), source_tag=None)
    _apply_occurrence_index(entries, getattr(args, "occurrence_index", ""),
                            getattr(args, "inject_context", False))
    todo = filter_for_translation(entries)
    print(f"总条目 {len(entries)}，待翻译 {len(todo)}，重复键 {len(dups)}")
    if dups:
        print(f"  ⚠ 发现重复键 {len(dups)} 个（已保留最后一个值）: {dups[:5]}...")
    storage, tm, project_store = _make_runtime(args)
    glossary = _load_glossary(args)
    engine = _make_engine(args, storage, glossary, tm, project_store)
    stats = engine.run_stage("translate", todo, resume=args.resume,
                             max_stage_cost_cny=args.max_stage_cost)
    _print_summary("translate", stats, engine)
    return 0


def cmd_polish(args) -> int:
    if getattr(args, "workers", 1) > 1:
        return _run_parallel(args, "polish")
    entries, dups = _load_entries(Path(args.input), source_tag=args.source_tag)
    _apply_occurrence_index(entries, getattr(args, "occurrence_index", ""),
                            getattr(args, "inject_context", False))
    todo = filter_for_polish(entries)
    print(f"总条目 {len(entries)}，待润色 {len(todo)}（来源标记: {args.source_tag or '未知'}）")
    storage, tm, project_store = _make_runtime(args)
    glossary = _load_glossary(args)
    engine = _make_engine(args, storage, glossary, tm, project_store)
    # 润色阶段：先 QA 路由，再分低/高风险处理
    stats = engine.run_stage("polish", todo, resume=args.resume,
                             max_stage_cost_cny=args.max_stage_cost)
    _print_summary("polish", stats, engine)
    return 0


def cmd_pipeline(args) -> int:
    """初翻 -> QA -> 润色(非思考) + 高风险审校(thinking) -> 合并。"""
    if getattr(args, "workers", 1) > 1:
        print("pipeline 暂不支持 --workers（多阶段有依赖），请对 translate/polish 分别使用")
        return 1
    entries, dups = _load_entries(Path(args.input), source_tag=None)
    _apply_occurrence_index(entries, getattr(args, "occurrence_index", ""),
                            getattr(args, "inject_context", False))
    storage, tm, project_store = _make_runtime(args)
    glossary = _load_glossary(args)
    engine = _make_engine(args, storage, glossary, tm, project_store)

    # 阶段1：初翻未翻译部分
    todo = filter_for_translation(entries)
    print(f"[pipeline] 阶段1 初翻: {len(todo)} 条")
    s1 = engine.run_stage("translate", todo, resume=args.resume,
                          max_stage_cost_cny=args.max_stage_cost)
    _print_summary("translate", s1, engine)

    # 阶段2：QA 路由润色/审校（对全部已译条目）
    to_polish = []
    for e in entries:
        zh = e.extra.get("final_zh")
        if zh is not None and e.status in (EntryStatus.UNTRANSLATED, EntryStatus.MIXED_LANGUAGE):
            e.cur = zh  # 新译文作为润色输入
            e.masked_cur = None
            to_polish.append(e)
    to_polish += filter_for_polish(entries)
    print(f"[pipeline] 阶段2 QA 路由: {len(to_polish)} 条")
    bundle = getattr(args, "_bundle", None)
    qa_map = run_qa(to_polish, glossary,
                    forbidden_words=bundle.forbidden_words if bundle else None)
    low, mid, high = [], [], []
    for e in to_polish:
        score, _ = qa_map.get(e.id, (0.0, []))
        if score >= HIGH_RISK_THRESHOLD:
            high.append(e)
        elif score >= LOW_RISK_THRESHOLD:
            mid.append(e)
        else:
            low.append(e)
    print(f"  低风险 {len(low)} / 中风险 {len(mid)} / 高风险 {len(high)}")
    s2 = engine.run_stage("polish", low, resume=args.resume,
                          max_stage_cost_cny=args.max_stage_cost)
    s3 = {"translated": 0, "reused": 0, "failed": 0, "repaired": 0, "reviewed": 0}
    if mid:
        s3m = engine.run_stage("review_ambiguous", mid, resume=args.resume,
                               max_stage_cost_cny=args.max_stage_cost)
        s3["translated"] += s3m["translated"]
    if high:
        s3h = engine.run_stage("review_hard", high, resume=args.resume,
                               max_stage_cost_cny=args.max_stage_cost)
        s3["translated"] += s3h["translated"]
    _print_summary("polish+review", s2, engine)
    print(f"[pipeline] 审校: 中风险 {len(mid)} 高风险 {len(high)}")

    # 阶段3：合并
    out = storage.merge_final(entries, "ManualTransFile_zh.json")
    print(f"[pipeline] 终稿: {out}")
    print(f"[pipeline] 总成本: {engine.cost_cny:.4f} 元")
    return 0


def cmd_detect(args) -> int:
    """零成本 QA 筛查：只跑规则，不调 API。输出 suspects.json。"""
    entries, dups = _load_entries(Path(args.input), source_tag=args.source_tag)
    glossary = _load_glossary(args)
    bundle = getattr(args, "_bundle", None)
    # detect 对象 = 全部已有译文（含旧译文），risk_score 排序
    targets = [e for e in entries if e.cur and e.cur.strip()
               and e.status in (EntryStatus.HUMAN_TRANSLATION, EntryStatus.MACHINE_TRANSLATION)]
    print(f"QA 筛查 {len(targets)} 条（零成本）")
    qa_map = run_qa(targets, glossary,
                    forbidden_words=bundle.forbidden_words if bundle else None)
    suspects = []
    for e in targets:
        score, issues = qa_map[e.id]
        if score > 0:
            suspects.append({"key": e.key, "cur": e.cur, "risk_score": score,
                             "issues": issues, "speaker": e.speaker, "scene": e.scene})
    suspects.sort(key=lambda x: -x["risk_score"])
    out = Path(args.out) / "suspects.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(suspects, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"疑似风险 {len(suspects)} 条 -> {out}")
    print(f"  高风险(>= {HIGH_RISK_THRESHOLD}): "
          f"{sum(1 for s in suspects if s['risk_score'] >= HIGH_RISK_THRESHOLD)} 条")
    return 0


def cmd_import_corpus(args) -> int:
    """把历史译文（SR1028 等）导入 TM corpus，标记状态。"""
    legacy_path = Path(args.legacy)
    data, dups = read_mtool_json(legacy_path)
    tm = TranslationMemory(Path(args.out) / "tm.db")
    args._translation_memory = tm
    status = TmStatus.MACHINE_LEGACY if args.tag == "legacy" else TmStatus.EXISTING_UNKNOWN
    n = tm.import_corpus(list(data.items()), default_status=status, source_file=legacy_path.name)
    print(f"导入语料 {n} 条（状态 {status.value}），重复键 {len(dups)}")
    print(f"corpus 统计: {tm.corpus_stats()}")
    return 0


def cmd_scan_talk(args) -> int:
    """扫描口上 rb 文件建 occurrence index；--check-missing 追加漏翻审计。"""
    talk_dir = Path(args.talk_dir)
    if not talk_dir.is_dir():
        print(f"  ⚠ talk 目录不存在: {talk_dir}")
        return 1
    index = scan_rb_files(talk_dir)
    result = _save_index(index, Path(args.out), "口上")
    if getattr(args, "check_missing", False):
        _check_talk_missing(index, Path(args.out))
    return result


def _check_talk_missing(index, out_dir: Path) -> None:
    """口上漏翻审计：报告仍含日文假名的文本（汉化后口上应为中文）。

    局限：纯日文汉字句（无假名）与中文同形无法区分，不在本审计覆盖内
    （需项目包 glossary/语境判定，留给主控 agent 人工核查）。
    """
    missing = []
    for text, occs in index.map.items():
        if not any(0x3040 <= ord(c) <= 0x30FF for c in text):
            continue
        for o in occs[:3]:
            missing.append({"text": text, "file": o.file, "line": o.line,
                            "method": o.method, "speaker": o.speaker_candidate})
    target = out_dir / "missing_talk.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(missing, ensure_ascii=False, indent=1), encoding="utf-8")
    unique = len({m["text"] for m in missing})
    print(f"口上漏翻审计: {len(missing)} 处出现（{unique} 个唯一日文残留文本） -> {target}")


def cmd_scan_events(args) -> int:
    """扫描 RPG Maker MZ data 目录建 occurrence index（事件版）。

    输出格式与 scan-talk 相同（{文本: [Occurrence...]}），可直接用于
    translate/polish/pipeline 的 --occurrence-index 与 --inject-context。
    """
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"  ⚠ data 目录不存在: {data_dir}")
        return 1
    index = scan_rmmz_events(data_dir)
    return _save_index(index, Path(args.out), "事件")


def cmd_seq_align(args) -> int:
    """事件文本与 MTool 翻译表对齐：逐条 401 查表（模拟 MTool 控制码切分规则），
    找出翻译表缺失的文本（游戏内会显示日文）并落盘序列审查文件。

    --input: 翻译表（ManualTransFile_zh.json）
    --data-dir: 游戏 data 目录（Map*.json/CommonEvents.json）
    --out: 输出目录（seq_*.json + report.json）
    """
    import importlib.util
    tools = Path(__file__).resolve().parents[1] / "tools"
    spec = importlib.util.spec_from_file_location("seq_align_tool", tools / "seq_align.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not args.input:
        print("  ⚠ --input 需要翻译表路径（ManualTransFile_zh.json）", file=sys.stderr)
        return 1
    if not args.data_dir:
        print("  ⚠ --data-dir 需要游戏 data 目录", file=sys.stderr)
        return 1
    report = str(Path(args.out) / "report.json")
    return mod.run(args.data_dir, args.input, args.out, report)


def cmd_misalign(args) -> int:
    """键值错位检测：角色名互斥（键含日文名但译文无译名/译文含译名但键无源）
    + 场景序列复核块。--input 翻译表；--glossary-json 术语表；
    --occurrence-index 事件索引；--out 报告 JSON 路径。"""
    import importlib.util
    tools = Path(__file__).resolve().parents[1] / "tools"
    spec = importlib.util.spec_from_file_location("check_misalign_tool", tools / "check_misalign.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not args.input or not args.glossary_json:
        print("  ⚠ --input 与 --glossary-json 均为必填", file=sys.stderr)
        return 1
    return mod.run(args.input, args.glossary_json,
                   args.occurrence_index or None, args.out)


def _save_index(index, out_dir: Path, kind: str) -> int:
    """保存 occurrence index 为紧凑 JSON，输出统计。scan-talk/scan-events 共用。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {text: [o.__dict__ for o in occs] for text, occs in index.map.items()}
    target = out_dir / "occurrence_index.json"
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))  # 紧凑格式
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"扫描完成: {len(index.map)} 个唯一文本 -> {target} ({size_mb:.0f} MB)")
    multi = sum(1 for t, o in index.map.items() if index.is_multi_context(t))
    print(f"多语境冲突文本: {multi} 个 ({multi/max(1,len(index.map))*100:.1f}%)")
    return 0


def _build_rename_mapping(args) -> dict:
    """构建 {旧译名: 新译名} 映射：glossary 的 forbidden_variants + --mapping。"""
    mapping = {}
    glossary = _load_glossary(args)
    for entry in glossary._entries:
        for variant in entry.forbidden_variants:
            mapping[variant] = entry.target
    if getattr(args, "mapping", ""):
        for pair in args.mapping.split(","):
            pair = pair.strip()
            if ":" not in pair:
                continue
            old, _, new = pair.partition(":")
            mapping[old.strip()] = new.strip()
    return {old: new for old, new in mapping.items() if old and new and old != new}


def _rename_replace(text: str, mapping: dict, hits: dict) -> str:
    for old, new in mapping.items():
        if not old:
            continue
        count = text.count(old)
        if count:
            text = text.replace(old, new)
            hits[old] += count
    return text


def cmd_apply_rename(args) -> int:
    """按术语表禁止译法（或 --mapping）批量统一译名，先备份。

    输入：--input 单个文件（MTool JSON 或 .rb）或 --talk-dir 口上目录。
    全文件替换（MTool 键为日文，不会被中文旧译名命中）。--dry-run 只统计不写。
    """
    mapping = _build_rename_mapping(args)
    if not mapping:
        print("[错误] 没有可用替换映射（--glossary-json 的 forbidden_variants 或 --mapping）",
              file=sys.stderr)
        return 1
    files: list[Path] = []
    if args.input and Path(args.input).exists():
        files.append(Path(args.input))
    if args.talk_dir and Path(args.talk_dir).is_dir():
        files.extend(Path(args.talk_dir).rglob("*.rb"))
    files = [fp for fp in files if fp.is_file()]
    if not files:
        print("[错误] 未提供有效 --input 或 --talk-dir", file=sys.stderr)
        return 1
    backup_dir = Path(args.backup_dir or f"runs/rename_backup_{_make_run_id()}")
    hits = {old: 0 for old in mapping}
    changed = 0
    for fp in files:
        try:
            original = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"  ⚠ 跳过 {fp.name}: {e}")
            continue
        new_text = _rename_replace(original, mapping, hits)
        if new_text == original:
            continue
        changed += 1
        if not args.dry_run:
            backup_dir.mkdir(parents=True, exist_ok=True)
            (backup_dir / fp.name).write_text(original, encoding="utf-8")
            fp.write_text(new_text, encoding="utf-8")
    print(f"apply-rename: {changed}/{len(files)} 个文件改动"
          + ("（--dry-run 未写入）" if args.dry_run else ""))
    hit_items = sorted(((old, n) for old, n in hits.items() if n), key=lambda x: -x[1])
    for old, n in hit_items:
        print(f"  {old} -> {mapping[old]}: {n} 处")
    if not args.dry_run and changed:
        print(f"  备份目录: {backup_dir}")
    return 0


def cmd_verify_merge(args) -> int:
    """合并产物 vs 基准逐键校验（残留/拼接/删除异常检测）。"""
    if not args.input or not getattr(args, "base", ""):
        print("[错误] verify-merge 需要 --input（新产物）与 --base（基准文件）",
              file=sys.stderr)
        return 1
    out_dir = Path(args.out)
    report_path = verify_run(Path(args.input), Path(getattr(args, "base")), out_dir)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print(f"报告: {report_path}")
    return verify_summary(report)


def cmd_full_qa(args) -> int:
    """合并前全量验证器（漏译/假名/繁体/术语变体/串位/引擎键闸门）。"""
    if not args.input:
        print("[错误] full-qa 需要 --input（译文产物文件）", file=sys.stderr)
        return 1
    data = verify_load_json(Path(args.input))
    bundle = getattr(args, "_bundle", None)
    glossary = _load_glossary(args) if (bundle or args.glossary or args.glossary_json) else None
    # 无显式术语表时不传 Glossary（跳过术语类检查）
    if glossary is not None and not glossary._entries and not bundle:
        glossary = None
    report = run_full_qa(data, glossary=glossary,
                         forbidden_words=bundle.forbidden_words if bundle else None,
                         check_traditional=getattr(args, "with_traditional", False))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = write_report(report, out_dir / "full_qa_issues.json")
    print(f"报告: {report_path}")
    max_rate = getattr(args, "max_issue_rate", 0.01)
    return full_qa_summary(report, max_rate)


def cmd_pun_add(args) -> int:
    """把双关/俚语/语境梗条目写入回归库 pun_manifest.json。"""
    import pun
    if not args.bundle:
        print("[错误] pun-add 需要 --bundle <项目包名>", file=sys.stderr)
        return 1
    if not args.key:
        print("[错误] pun-add 需要 --key <日文原文>", file=sys.stderr)
        return 1
    if not args.note and not args.translation:
        print("[错误] pun-add 需要 --note 或 --translation（至少其一）", file=sys.stderr)
        return 1
    root = config.PROJECTS_ROOT / args.bundle
    entries = pun.add_entry(root, args.key, args.type, args.note,
                            args.translation, args.status, args.source)
    print(f"回归库已更新: {pun.manifest_path(root)}（共 {len(entries)} 条）")
    print(f"  键: {args.key[:50]}")
    print(f"  类型: {args.type} | 状态: {args.status} | 译法: {args.translation or '（未定）'}")
    return 0


def cmd_pun_check(args) -> int:
    """对照译文产物检查回归库条目（已确认译法被改/复发，pending 未处理）。"""
    import pun
    if not args.bundle or not args.input:
        print("[错误] pun-check 需要 --bundle 与 --input（译文产物）", file=sys.stderr)
        return 1
    data = verify_load_json(Path(args.input))
    root = config.PROJECTS_ROOT / args.bundle
    issues = pun.check_translations(root, data)
    entries = pun.load(root)
    print(f"双关回归库: {len(entries)} 条 | 产物异常 {len(issues)} 条")
    for it in issues[:20]:
        print(f"  ⚠ [{it['issue']}] {it['key'][:40]}")
        if it.get("expected"):
            print(f"      期望: {it['expected'][:60]}")
        if it.get("current"):
            print(f"      当前: {it['current'][:60]}")
    max_issues = getattr(args, "max_issues", 0)
    return 1 if (max_issues and len(issues) >= max_issues) else 0


def cmd_pun_list(args) -> int:
    """列出回归库全部条目。"""
    import pun
    if not args.bundle:
        print("[错误] pun-list 需要 --bundle <项目包名>", file=sys.stderr)
        return 1
    entries = pun.load(config.PROJECTS_ROOT / args.bundle)
    print(f"双关回归库: {len(entries)} 条")
    for key, meta in sorted(entries.items(), key=lambda kv: kv[0]):
        print(f"  [{meta.get('status', '?')}/{meta.get('type', '?')}] {key[:44]}")
        if meta.get("translation"):
            print(f"      → {meta['translation'][:50]}")
    return 0


def cmd_scan_puns(args) -> int:
    """高思考模型批量筛查双关/俚语/语境梗候选（输出清单供人工确认）。"""
    from scan_puns import scan_puns
    if not args.input:
        print("[错误] scan-puns 需要 --input（原文文件）", file=sys.stderr)
        return 1
    api_key = args.api_key or None
    occ_index = Path(getattr(args, "occurrence_index", "")) \
        if getattr(args, "occurrence_index", "") else None
    out = scan_puns(Path(args.input), Path(args.out) / "scan_puns",
                    api_key, args.model, base_url=args.base_url,
                    occurrence_index=occ_index,
                    batch_items=getattr(args, "batch_items", 60))
    print(f"候选清单: {out}")
    print("确认后可用 pun-add 逐条写入回归库（--status confirmed 直接复用）")
    return 0


def _project_name_for(args) -> str:
    """proofread/sample-check 的项目名：--project-name 优先，其次 --bundle 名。"""
    if getattr(args, "project_name", ""):
        return args.project_name
    bundle = getattr(args, "_bundle", None)
    return bundle.name if bundle else ""


def _proofread_glossary_text(args) -> str:
    """proofread/sample-check 注入项目包术语表+风格策略+黑名单（2026-08-16）。"""
    bundle = getattr(args, "_bundle", None)
    if bundle is None:
        return ""
    parts: list[str] = []
    terms = []
    for e in bundle.glossary._entries:
        line = f"{e.source}→{e.target}"
        if e.forbidden_variants:
            line += f"（禁止：{'、'.join(e.forbidden_variants)}）"
        terms.append(line)
    if terms:
        parts.append("术语表: " + "；".join(terms))
    notes = list((bundle.policies or {}).get("style_notes", []) or [])
    if notes:
        parts.append("风格策略: " + "；".join(notes))
    fw = bundle.forbidden_words
    if fw:
        parts.append("黑名单词: " + "、".join(fw))
    return "\n".join(parts)


def cmd_proofread(args) -> int:
    """全量审查（原文+译文对照）。只写 issues 报告，绝不改动输入文件。"""
    from proofread import run_proofread
    if not args.input:
        print("[错误] proofread 需要 --input（译文产物文件）", file=sys.stderr)
        return 1
    shard = None
    if getattr(args, "shard", ""):
        try:
            i, n = args.shard.split("/")
            shard = (int(i), int(n))
            if not (0 <= shard[0] < shard[1]):
                raise ValueError
        except ValueError:
            print("[错误] --shard 格式为 i/N（如 0/6）", file=sys.stderr)
            return 1
    out = run_proofread(Path(args.input), Path(args.out) / "proofread",
                        args.api_key or None, args.model,
                        base_url=args.base_url,
                        batch_items=getattr(args, "batch_items", 60),
                        shard=shard, project_name=_project_name_for(args),
                        glossary_text=_proofread_glossary_text(args))
    print(f"审查产物: {out}")
    return 0


def cmd_sample_check(args) -> int:
    """随机抽查 N 条并统计问题率（质量闸门：超阈值退出码 1）。"""
    from proofread import run_sample_check
    if not args.input:
        print("[错误] sample-check 需要 --input（译文产物文件）", file=sys.stderr)
        return 1
    report_path, rate, passed = run_sample_check(
        Path(args.input), Path(args.out) / "sample_check",
        args.api_key or None, args.model, base_url=args.base_url,
        sample_n=getattr(args, "n", 500), seed=getattr(args, "seed", None),
        batch_items=getattr(args, "batch_items", 60),
        max_issue_rate=getattr(args, "max_issue_rate", 0.01),
        project_name=_project_name_for(args),
        glossary_text=_proofread_glossary_text(args))
    print(f"抽查报告: {report_path}")
    return 0 if passed else 1


def cmd_new_project(args) -> int:
    """创建项目数据包骨架目录（含空 JSON 模板，供主控 agent 通读原文后填入）。"""
    if not args.name:
        print("new-project 需要 --name <游戏名>（对应 projects/<名>/ 目录）", file=sys.stderr)
        return 1
    try:
        root = create_project(args.name)
    except FileExistsError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1
    print(f"项目包已创建: {root}")
    for fn in sorted(p.name for p in root.iterdir()):
        print(f"  - {fn}")
    print("各文件结构约定见 project.json 的 structure 字段，由主控 agent 通读原文后填入。")
    return 0


def _load_glossary(args) -> Glossary:
    """加载术语表：项目包 base + 显式 --glossary/--glossary-json 覆盖。

    项目包 glossary（含内置 _extra_entries 兜底）为 base；显式传入的术语
    优先级最高（merge 覆盖同 source）。
    """
    bundle = getattr(args, "_bundle", None)
    base = bundle.glossary if bundle is not None else Glossary()
    if args.glossary and Path(args.glossary).exists():
        return base.merge(Glossary.from_markdown(Path(args.glossary)))
    if args.glossary_json and Path(args.glossary_json).exists():
        return base.merge(Glossary.from_json(Path(args.glossary_json)))
    return base


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SR 批量翻译/润色工具（DeepSeek flash）")
    p.add_argument("--protocol", choices=sorted(PROTOCOLS), default="deepseek_chat")
    p.add_argument("--base-url", default="")
    p.add_argument("--model", default=config.DEFAULT_MODEL)
    p.add_argument("--thinking-effort", choices=ALL_EFFORTS, default="auto")
    p.add_argument("--price-cache-hit", type=float, default=0.0)
    p.add_argument("--price-input", type=float, default=0.0)
    p.add_argument("--price-output", type=float, default=0.0)
    p.add_argument("--max-cost-cny", type=float, default=config.DEFAULT_MAX_COST_CNY)
    p.add_argument("--max-stage-cost", type=float, default=config.DEFAULT_MAX_STAGE_COST_CNY)
    p.add_argument("--workers", type=_positive_int, default=1,
                   help="分片并行 worker 数（translate/polish；pipeline 不支持；预算均分给各 worker）")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, help_ in (("translate", "日→中初翻"), ("polish", "中→中润色"),
                        ("pipeline", "初翻+QA+润色+审校全流程"), ("detect", "零成本 QA 筛查"),
                        ("import-corpus", "导入历史语料"), ("scan-talk", "扫描口上 rb"),
                        ("scan-events", "扫描 RPG Maker MZ 事件"),
                        ("seq-align", "事件文本与翻译表对齐（查漏译/错位，零成本）"),
                        ("misalign", "键值错位检测（角色名互斥+场景复核块）"),
                        ("apply-rename", "按译名映射批量统一（先备份）"),
                        ("verify-merge", "合并产物 vs 基准逐键校验（残留/拼接检测）"),
                        ("full-qa", "合并前全量验证器（漏译/格式/术语变体/串位闸门）"),
                        ("pun-add", "双关/俚语条目写入回归库"),
                        ("pun-check", "对照产物检查双关回归库条目"),
                        ("pun-list", "列出双关回归库条目"),
                        ("scan-puns", "高思考模型批量筛查双关/俚语候选"),
                        ("proofread", "全量审查（原文+译文对照，只写报告不改文件）"),
                        ("sample-check", "随机抽查 N 条并统计问题率"),
                        ("smoke", "冒烟测试")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--input", type=str, default="")
        sp.add_argument("--out", type=str, default="runs")
        sp.add_argument("--glossary", type=str, default="")
        sp.add_argument("--glossary-json", type=str, default="")
        sp.add_argument("--resume", action="store_true")
        sp.add_argument("--source-tag", type=str, default="",
                        help="来源标记: legacy=旧汉化(机翻风险)")
        sp.add_argument("--legacy", type=str, default="")       # import-corpus
        sp.add_argument("--tag", type=str, default="legacy")    # import-corpus
        sp.add_argument("--talk-dir", type=str, default="")     # scan-talk
        sp.add_argument("--data-dir", type=str, default="")     # scan-events
        sp.add_argument("--check-missing", action="store_true",
                        help="scan-talk 追加口上漏翻审计（报告仍含日文假名的文本）")
        sp.add_argument("--inject-context", action="store_true")  # 前/后句注入 prompt
        sp.add_argument("--sample", type=str, default="")       # smoke
        sp.add_argument("--dry-run", action="store_true")       # smoke/流程无 key 验证
        sp.add_argument("--api-key", type=str, default="")      # 显式传 key
        sp.add_argument("--occurrence-index", type=str, default="")  # 口上语境索引
        sp.add_argument("--batch-target-input-tokens", type=int, default=0,
                        help="覆盖批次输入预算（地图级上下文包容量，0=默认 16K）")
        sp.add_argument("--batch-hard-max-items", type=int, default=0,
                        help="覆盖批次条数硬上限（大地图条目多时调大，0=默认 600）")
        sp.add_argument("--batch-target-output-tokens", type=int, default=0,
                        help="覆盖批次输出预算（防截断，0=默认 10K）")
        sp.add_argument("--batch-max-maps", type=int, default=0,
                        help="大包模式：每包最大地图数（地图编号连贯切包，0=关闭）")
        sp.add_argument("--context-chain", action="store_true",
                        help="上下文链：前一包的 user+assistant 附加到下一包请求"
                             "（链预算默认 1M×80%%，--chain-budget-tokens 可调）")
        sp.add_argument("--chain-budget-tokens", type=int, default=950_000,
                        help="上下文链输入预算上限（默认 950000≈1M×95%%，链 2×350K 包）")
        sp.add_argument("--project", action="store_true")
        sp.add_argument("--project-name", type=str, default="")
        sp.add_argument("--bundle", type=str, default="",
                        help="项目数据包名（projects/<名>/：术语表/世界观/重点句/风格策略等，"
                             "由主控 agent 通读原文后按约定写入）")
        sp.add_argument("--mapping", type=str, default="",
                        help="apply-rename 显式映射（逗号分隔 旧译名:新译名）")
        sp.add_argument("--backup-dir", type=str, default="",
                        help="apply-rename 备份目录（默认 runs/rename_backup_<时间>）")

    np = sub.add_parser("new-project", help="创建项目数据包骨架（空 JSON 模板）")
    np.add_argument("--name", type=str, default="", help="游戏/项目名（对应 projects/<名>/ 目录）")

    models = sub.add_parser("models", help="Fetch available models")
    models.add_argument("--api-key", type=str, default="")
    _add_extra_arguments(sub)
    return p


def _add_extra_arguments(sub) -> None:
    """为各新命令挂载专属参数（--input/--out/--bundle 等已在循环中统一添加）。"""
    def add(parser_name: str, *args, **kwargs):
        sub.choices[parser_name].add_argument(*args, **kwargs)

    add("verify-merge", "--base", type=str, default="",
        help="基准文件（合并前的旧译文）")
    add("full-qa", "--max-issue-rate", type=float, default=0.01,
        help="问题率阈值，超限退出码 1 阻止合并（默认 0.01）")
    add("full-qa", "--with-traditional", action="store_true",
        help="启用繁体检测（默认关闭：简繁差异不影响多数用户理解，"
             "硬编码字表方案不理想，2026-08 暂停迭代）")
    add("pun-add", "--key", type=str, default="", help="日文原文（清单键）")
    add("pun-add", "--type", type=str, default="pun", choices=("pun", "slang", "contextual"),
        help="类型: pun=双关 / slang=俚语 / contextual=语境梗")
    add("pun-add", "--note", type=str, default="", help="翻译要点/双关说明")
    add("pun-add", "--translation", type=str, default="", help="已确认译法")
    add("pun-add", "--status", type=str, default="confirmed", choices=("confirmed", "pending"),
        help="confirmed=定稿直接复用 / pending=待人工确认")
    add("pun-add", "--source", type=str, default="proofread",
        choices=("demo", "full", "proofread", "game_test"), help="发现来源")
    add("pun-check", "--max-issues", type=int, default=0,
        help="不一致条目数达到该值则退出码 1（0=不设闸门）")
    add("scan-puns", "--batch-items", type=int, default=60,
        help="每批筛查条数（默认 60，thinking high 批次宜小）")
    add("proofread", "--shard", type=str, default="",
        help="分片 i/N（如 0/6），子代理并行审查时各跑一片")
    add("proofread", "--batch-items", type=int, default=60,
        help="每批审查条数（默认 60）")
    add("sample-check", "--n", type=int, default=500, help="抽样条数（默认 500）")
    add("sample-check", "--seed", type=int, default=None, help="随机种子（可复现）")
    add("sample-check", "--batch-items", type=int, default=60, help="每批审查条数")
    add("sample-check", "--max-issue-rate", type=float, default=0.01,
        help="问题率阈值，超限退出码 1（默认 0.01）")


def cmd_models(args) -> int:
    """Fetch models exposed by the configured provider without changing projects."""
    client = make_client(
        protocol=args.protocol,
        api_key=args.api_key or None,
        model=args.model,
        base_url=args.base_url,
        pricing=_pricing_from_args(args),
    )
    models = client.list_models()
    print(json.dumps({"protocol": args.protocol, "models": models}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    config.load_dotenv()  # 加载项目根 .env（API key 持久化，不覆盖已有环境变量）
    args = _build_parser().parse_args(argv)
    # 加载项目数据包（new-project/models 不需要）
    args._bundle = None
    if args.cmd != "new-project" and getattr(args, "bundle", ""):
        bundle = ProjectBundle.load(args.bundle)
        if bundle is None:
            print(f"[错误] 项目包不存在: projects/{args.bundle}/（先运行 new-project --name {args.bundle}）",
                  file=sys.stderr)
            return 1
        args._bundle = bundle
        print(f"项目包: {args.bundle}（术语 {len(bundle.glossary._entries)} 条"
              f" / 重点句 {len(bundle.key_lines)} 条"
              f" / 黑名单 {len(bundle.forbidden_words)} 词）")
    result = 1
    try:
        if args.cmd == "translate":
            result = cmd_translate(args)
        elif args.cmd == "polish":
            result = cmd_polish(args)
        elif args.cmd == "pipeline":
            result = cmd_pipeline(args)
        elif args.cmd == "detect":
            result = cmd_detect(args)
        elif args.cmd == "import-corpus":
            result = cmd_import_corpus(args)
        elif args.cmd == "scan-talk":
            result = cmd_scan_talk(args)
        elif args.cmd == "scan-events":
            result = cmd_scan_events(args)
        elif args.cmd == "seq-align":
            result = cmd_seq_align(args)
        elif args.cmd == "misalign":
            result = cmd_misalign(args)
        elif args.cmd == "apply-rename":
            result = cmd_apply_rename(args)
        elif args.cmd == "verify-merge":
            result = cmd_verify_merge(args)
        elif args.cmd == "full-qa":
            result = cmd_full_qa(args)
        elif args.cmd == "pun-add":
            result = cmd_pun_add(args)
        elif args.cmd == "pun-check":
            result = cmd_pun_check(args)
        elif args.cmd == "pun-list":
            result = cmd_pun_list(args)
        elif args.cmd == "scan-puns":
            result = cmd_scan_puns(args)
        elif args.cmd == "proofread":
            result = cmd_proofread(args)
        elif args.cmd == "sample-check":
            result = cmd_sample_check(args)
        elif args.cmd == "new-project":
            result = cmd_new_project(args)
        elif args.cmd == "smoke":
            result = cmd_smoke(args)
        elif args.cmd == "models":
            result = cmd_models(args)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[错误] {type(e).__name__}: {e}", file=sys.stderr)
        result = 1
    finally:
        store = getattr(args, "_project_store", None)
        if store is not None:
            store.finish("completed" if result == 0 else "failed")
        memory = getattr(args, "_translation_memory", None)
        if memory is not None:
            memory.close()
    return result


def cmd_smoke(args) -> int:
    """冒烟测试：4 组批次规模（50/100/200/400 条）各跑 2 批，对比指标。

    --dry-run 时不调 API（用回显 mock），验证全流程逻辑与指标统计。
    真实运行需 --api-key 或环境变量，样本用 --sample 提供（含 src 的 JSON 数组）。
    """
    from smoke import run_smoke
    sample_path = Path(args.sample)
    if not sample_path.exists():
        print("冒烟测试需要 --sample 文件（[{src:...}] 或 MTool 键值 JSON）")
        return 1
    return run_smoke(sample_path, Path(args.out), dry_run=args.dry_run,
                     api_key=args.api_key, model=args.model)


if __name__ == "__main__":
    sys.exit(main())
