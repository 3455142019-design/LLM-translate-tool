# -*- coding: utf-8 -*-
"""storage.py — 事务式批次存储 + journal + 原子合并 + run 快照。

设计（v3 评审）：runs/<run_id>/ 下逐批不可变落盘，最终文件只在全部批次
完成后做一次确定性合并，通过「写临时文件 -> 校验 -> 原子重命名」发布，
避免中途崩溃产生半成品/新旧混杂。

目录结构：
runs/<run_id>/
├── manifest.json        # run 元数据 + 全部批次状态（RunMeta）
├── journal.jsonl        # 追加式事件日志（每批/每次请求一行）
├── batches/000001.{input,request,response.raw,output,meta}.json
├── failed/              # 重试耗尽/被过滤批次
├── snapshot/            # prompt/术语表/配置快照（恢复时校验 hash）
└── final/               # 合并产物

依赖：schemas/config；被 engine/cli 引用。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from schemas import Batch, Entry, RunMeta, Usage

# 每个批次要落盘的文件后缀
BATCH_FILES = ("input", "request", "response.raw", "output", "meta")


class RunStorage:
    """单个 run 的存储管理器。

    stage 隔离：同一 run 内不同阶段（translate/polish/review）写入
    batches/<stage>/ 与 failed/<stage>/，避免批号冲突（各阶段都从 1 编号）。
    """

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.stage = "common"
        self.batches_dir = self.run_dir / "batches" / self.stage
        self.failed_dir = self.run_dir / "failed" / self.stage
        self.snapshot_dir = self.run_dir / "snapshot"
        self.final_dir = self.run_dir / "final"
        for d in (self.run_dir, self.batches_dir, self.failed_dir,
                  self.snapshot_dir, self.final_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.run_dir / "manifest.json"
        self.journal_path = self.run_dir / "journal.jsonl"

    def set_stage(self, stage: str) -> None:
        """切换当前阶段目录（批号在该阶段内从 1 起）。"""
        if stage == self.stage:
            return
        self.stage = stage
        self.batches_dir = self.run_dir / "batches" / stage
        self.failed_dir = self.run_dir / "failed" / stage
        self.batches_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)

    # ---- journal（追加式事件日志，带大小轮转） ----
    def log(self, event: str, **fields: Any) -> None:
        row = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
        # 大小轮转：超过 LOG_MAX_MB 时保留最近一半行（删除最早文本，防无限膨胀）
        if self.journal_path.exists() and \
                self.journal_path.stat().st_size > config.LOG_MAX_MB * 1024 * 1024:
            try:
                lines = self.journal_path.read_text(encoding="utf-8").splitlines()
                keep = lines[len(lines) // 2:]  # 保留后半（最新）
                self.journal_path.write_text("\n".join(keep) + "\n", encoding="utf-8")
            except OSError:
                pass  # 轮转失败不阻塞日志写入
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- manifest ----
    def save_manifest(self, meta: RunMeta) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta.__dict__, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        os.replace(tmp, self.manifest_path)  # 原子替换

    def load_manifest(self) -> Optional[Dict[str, Any]]:
        if not self.manifest_path.exists():
            return None
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    # ---- 批次落盘（不可变：写入后不修改；subtag 隔离子批） ----
    def save_batch_input(self, batch: Batch) -> None:
        self._write_batch_file(batch.number, "input",
                               [it.to_dict() for it in batch.items], batch.subtag)

    def save_batch_request(self, batch: Batch, messages: List[Dict[str, str]],
                           params: Dict[str, Any]) -> None:
        self._write_batch_file(batch.number, "request",
                               {"messages": messages, "params": params}, batch.subtag)

    def save_batch_response_raw(self, batch: Batch, payload: Dict[str, Any]) -> None:
        """保存完整 API 响应（content/reasoning/usage/finish_reason/指纹）。

        含 usage 明细（prompt 命中/未命中/completion/reasoning），
        供 cost_report.py 事后审计与账单对账。
        """
        self._write_batch_file(batch.number, "response.raw", payload, batch.subtag)

    def save_batch_output(self, batch: Batch, output: Dict[str, str]) -> None:
        self._write_batch_file(batch.number, "output", output, batch.subtag)

    def save_batch_meta(self, batch: Batch, meta: Dict[str, Any]) -> None:
        self._write_batch_file(batch.number, "meta", meta, batch.subtag)

    def _write_batch_file(self, number: int, kind: str, data: Any,
                          subtag: str = "") -> Path:
        """写入批次文件。subtag 区分同一批号的子批（repair/二分/重试），防冲突。"""
        path = self.batches_dir / f"{number:06d}{subtag}.{kind}.json"
        # 不可变：已存在则拒绝覆盖（除非该批号未完成——中断残留，允许重跑覆盖）。
        if path.exists():
            has_output = (self.batches_dir / f"{number:06d}.output.json").exists()
            if has_output:
                raise FileExistsError(f"批次文件已存在，禁止覆盖: {path}")
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        os.replace(tmp, path)
        return path

    def save_failed(self, batch: Batch, reason: str) -> None:
        """重试耗尽/被过滤批次：整体移动到 failed/，附原因。"""
        path = self.failed_dir / f"{batch.number:06d}.failed.json"
        payload = {
            "reason": reason,
            "items": [it.to_dict() for it in batch.items],
            "attempts": batch.attempts,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
        self.log("batch_failed", batch=batch.number, reason=reason)

    # ---- run 快照（漂移保护/恢复校验） ----
    def snapshot_files(self, files: Dict[str, Path]) -> Dict[str, str]:
        """把 prompt/术语表/配置等文件复制进 snapshot/，返回 {名称: sha256}。"""
        hashes: Dict[str, str] = {}
        for name, src in files.items():
            if not src.exists():
                continue
            data = src.read_bytes()
            hashes[name] = hashlib.sha256(data).hexdigest()[:16]
            dst = self.snapshot_dir / name
            tmp = dst.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, dst)
        self.log("snapshot_saved", files=sorted(hashes.keys()))
        return hashes

    def verify_snapshot(self, expected: Dict[str, str]) -> List[str]:
        """恢复/续跑时校验快照 hash；不匹配返回问题列表。"""
        problems: List[str] = []
        for name, h in expected.items():
            p = self.snapshot_dir / name
            if not p.exists():
                problems.append(f"快照缺失: {name}")
                continue
            actual = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            if actual != h:
                problems.append(f"快照被修改: {name} (期望 {h} 实际 {actual})")
        return problems

    # ---- 最终合并（确定性 + 原子发布 + 备份/diff 保护） ----
    def merge_final(self, entries: List[Entry], output_name: str = "final.json",
                    backup: bool = True,
                    base_path: Optional[Path] = None) -> Path:
        """合并全部条目为最终字典并原子发布到 final/。

        entries 需按 key 确定性排序（同 key 取最后一次出现，保证可复现）。
        key 级精确合并：每个 key 只对应一条值，绝不按序号拼接/错位。

        保护（2026-08-10 HOLLOWWALD 教训：合并错位 + 旧文本残留导致
        10,468 条全面重翻）：
        - backup=True 且产物已存在：先备份到 final/backups/<名称>.<时间戳>.bak
        - base_path 提供：产出 diff_report.json（added/changed/unchanged/removed），
          合并前后逐 key 可比，异常可回溯
        返回最终文件路径。
        """
        merged: Dict[str, str] = {}
        for it in sorted(entries, key=lambda e: e.key):
            zh = it.extra.get("final_zh")
            if zh is not None:
                merged[it.key] = zh
            elif it.cur is not None:  # 无新译文（TM 复用/未处理）保留现译文
                merged[it.key] = it.cur
        out = self.final_dir / output_name

        # 1. 备份现有产物（防覆盖后无法回溯）
        if backup and out.exists():
            backup_dir = self.final_dir / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(out, backup_dir / f"{output_name}.{stamp}.bak")
            self.log("merge_backup", path=str(backup_dir / f"{output_name}.{stamp}.bak"))

        # 2. diff 报告（base_path = 合并前基准译文文件）
        if base_path is not None and base_path.exists():
            base = json.loads(base_path.read_text(encoding="utf-8"))
            added = sorted(k for k in merged if k not in base)
            removed = sorted(k for k in base if k not in merged)
            changed = sorted(k for k in base if k in merged and merged[k] != base[k])
            unchanged = len(base) - len(removed) - len(changed)
            report = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "output": str(out),
                "base": str(base_path),
                "added": added,
                "removed": removed,
                "changed": changed,
                "counts": {"total": len(merged), "added": len(added),
                           "removed": len(removed), "changed": len(changed),
                           "unchanged": unchanged},
            }
            report_path = self.final_dir / f"{output_name}.diff_report.json"
            tmp = report_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, report_path)
            self.log("merge_diff", path=str(report_path), counts=report["counts"])

        # 3. 原子发布
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, out)  # 原子发布
        self.log("final_merged", path=str(out), entries=len(merged))
        return out

    # ---- 断点状态 ----
    def completed_batch_numbers(self) -> set:
        """已成功落盘 output 的批号（用于 --resume）。

        只认主批文件 `NNNNNN.output.json`：子批（.repair/.b1/.b2/.retry1）
        的 output 不代表主批完成——content_filter/length 二分后主批 output
        永不写盘，若把 .b1 误判为主批完成，--resume 会跳过整批导致
        .b2 条目永久丢失（2026-08-18 审计发现）。
        """
        done: set = set()
        for p in self.batches_dir.glob("*.output.json"):
            name = p.name
            if name.endswith(".output.json") and name[:-len(".output.json")].isdigit():
                done.add(int(name[:-len(".output.json")]))
        return done
