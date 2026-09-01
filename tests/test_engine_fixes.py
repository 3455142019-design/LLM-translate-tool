# -*- coding: utf-8 -*-
"""test_engine_fixes.py — 2026-08-18 审计修复的回归测试。

覆盖：
1. storage.completed_batch_numbers 只认主批 output（子批不误判完成）
2. engine._restore_batch_output（--resume 从磁盘恢复 final_zh）
3. engine._handle_length（finish_reason=length 二分，不再整批 failed）
4. engine._repair_missing 失败回退原文占位 + failed 计数（防 merge 丢 key）
5. engine._finish_batch meta 记录实际 thinking 状态
6. batcher 大包模式无 scene 条目用默认 16K 预算
7. cli._worker_cmd 不把 API key 拼进子进程命令行
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config
from batcher import Batcher
from engine import Engine
from glossary import Glossary
from protect import Protector
from providers.deepseek import ApiResponse
from schemas import Batch, Entry, TmStatus, Usage
from storage import RunStorage
from tm import TranslationMemory


class _FakeClient:
    """可编程 FakeClient：按调用次数返回不同 finish_reason/content。"""

    protocol = "deepseek_chat"
    model = "deepseek-v4-flash"
    pricing = {"in_hit": 0.02, "in_miss": 1.0, "out": 2.0}

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = 0

    def chat(self, messages, thinking="disabled", reasoning_effort=None,
             temperature=None, response_format=None):
        self.calls += 1
        if self.responses:
            resp = self.responses.pop(0)
        else:
            resp = {"finish_reason": "stop", "content": "{}"}
        usage = Usage(prompt_tokens=10, prompt_cache_hit_tokens=5,
                      prompt_cache_miss_tokens=5, completion_tokens=10)
        return ApiResponse(content=resp["content"],
                           usage=usage,
                           finish_reason=resp["finish_reason"])


def _make_engine(tmpdir, client, key_lines=None):
    storage = RunStorage(Path(tmpdir) / "run")
    tm = TranslationMemory(Path(tmpdir) / "tm.db")
    return Engine(client=client, storage=storage, glossary=Glossary(), tm=tm,
                  key_lines=key_lines or {}), tm


def _entry(i: int, src: str) -> Entry:
    it = Entry(id=f"000001_{i:04d}", key=src, src=src)
    rec = Protector.mask(src)
    it.masked_src = rec.text
    it.extra["mask_record"] = rec
    return it


class TestCompletedBatchNumbers(unittest.TestCase):
    def test_subtag_output_not_counted(self):
        """子批（.b1/.repair）output 不得被当作主批完成（--resume 丢数据回归）。"""
        with tempfile.TemporaryDirectory() as td:
            storage = RunStorage(Path(td) / "run")
            (storage.batches_dir / "000001.b1.output.json").write_text("{}", encoding="utf-8")
            (storage.batches_dir / "000001.repair.output.json").write_text("{}", encoding="utf-8")
            self.assertEqual(storage.completed_batch_numbers(), set())
            (storage.batches_dir / "000001.output.json").write_text("{}", encoding="utf-8")
            self.assertEqual(storage.completed_batch_numbers(), {1})


class TestResumeRestore(unittest.TestCase):
    def test_restore_batch_output_fills_final_zh(self):
        """--resume 跳过已完成批次时必须从磁盘恢复 final_zh。"""
        with tempfile.TemporaryDirectory() as td:
            engine, tm = _make_engine(td, _FakeClient())
            try:
                it = _entry(0, "こんにちは")
                batch = Batch(number=1, items=[it])
                # 模拟已完成批次落盘
                engine.storage.save_batch_input(batch)
                engine.storage.save_batch_output(batch, {it.id: "你好"})
                engine._restore_batch_output(batch)
                self.assertEqual(it.extra["final_zh"], "你好")
            finally:
                tm.close()

    def test_restore_missing_files_no_crash(self):
        """input/output 缺失时只记日志不抛错（该批重新请求）。"""
        with tempfile.TemporaryDirectory() as td:
            engine, tm = _make_engine(td, _FakeClient())
            try:
                it = _entry(0, "こんにちは")
                engine._restore_batch_output(Batch(number=9, items=[it]))
                self.assertNotIn("final_zh", it.extra)
            finally:
                tm.close()


class TestLengthBisect(unittest.TestCase):
    def test_length_bisects_and_recovers(self):
        """finish_reason=length：大批二分后子批成功，不再整批 failed。"""
        with tempfile.TemporaryDirectory() as td:
            # 第 1 次调用（主批 4 条）返回 length；之后两个子批各返回 stop
            client = _FakeClient(responses=[
                {"finish_reason": "length", "content": "{}"},
                {"finish_reason": "stop", "content": '{"0": "甲", "1": "乙"}'},
                {"finish_reason": "stop", "content": '{"0": "丙", "1": "丁"}'},
            ])
            engine, tm = _make_engine(td, client)
            try:
                items = [_entry(i, f"原文{i}") for i in range(4)]
                batch = Batch(number=1, items=items)
                stats = {"translated": 0, "reused": 0, "failed": 0,
                         "repaired": 0, "reviewed": 0}
                engine._process_batch(batch, "translate", stats)
                self.assertEqual(stats["failed"], 0)
                self.assertEqual(stats["translated"], 4)
                self.assertEqual(client.calls, 3)
                for it in items:
                    self.assertTrue(it.extra.get("final_zh"))
            finally:
                tm.close()

    def test_length_single_item_fails_with_original(self):
        """二分到底（<=2 条仍 length）时保留原文占位，绝不写空值。"""
        with tempfile.TemporaryDirectory() as td:
            client = _FakeClient(responses=[
                {"finish_reason": "length", "content": "{}"},
                {"finish_reason": "length", "content": "{}"},
                {"finish_reason": "length", "content": "{}"},
            ])
            engine, tm = _make_engine(td, client)
            try:
                it = _entry(0, "長い原文")
                batch = Batch(number=1, items=[it])
                stats = {"translated": 0, "reused": 0, "failed": 0,
                         "repaired": 0, "reviewed": 0}
                engine._process_batch(batch, "translate", stats)
                self.assertEqual(stats["failed"], 1)
                self.assertEqual(it.extra["final_zh"], it.src)
            finally:
                tm.close()


class TestMetaThinkingState(unittest.TestCase):
    def test_meta_records_actual_thinking(self):
        """meta.json 的 thinking 字段反映实际请求状态（review_hard 不再写 disabled）。"""
        with tempfile.TemporaryDirectory() as td:
            client = _FakeClient(responses=[
                {"finish_reason": "stop", "content": '{"0": "译文"}'},
            ])
            engine, tm = _make_engine(td, client)
            try:
                engine.thinking_effort = "high"
                it = _entry(0, "こんにちは")
                batch = Batch(number=1, items=[it])
                stats = {"translated": 0, "reused": 0, "failed": 0,
                         "repaired": 0, "reviewed": 0}
                engine._process_batch(batch, "review_hard", stats)
                meta_path = engine.storage.batches_dir / "000001.meta.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                self.assertEqual(meta["thinking"], "enabled")
                self.assertEqual(meta["reasoning_effort"], "high")
            finally:
                tm.close()


class TestPackModeOthersLimits(unittest.TestCase):
    def test_no_scene_items_use_default_limits(self):
        """大包模式下无 scene 条目必须用默认 16K 预算，不用大包 350K 预算。"""
        limits = dict(config.BATCH_LIMITS)
        limits["max_maps"] = 200
        limits["target_input_tokens"] = 350_000
        limits["hard_max_items"] = 20_000
        batcher = Batcher(limits=limits)
        items = [_entry(i, f"メニュー項目{i}") for i in range(50)]
        batches = batcher.build_batches(items, mode="translate")
        self.assertTrue(batches)
        for b in batches:
            # 无 scene 条目走默认预算：单批输入估算不得超过默认 16K×1.3 场景余量
            self.assertLessEqual(b.est_input_tokens,
                                 config.BATCH_LIMITS["target_input_tokens"] * 1.3 + 100)


class TestWorkerCmdNoApiKey(unittest.TestCase):
    def test_worker_cmd_excludes_api_key(self):
        """worker 子进程命令行不得包含明文 API key（改走环境变量）。"""
        import argparse
        from cli import _worker_cmd
        args = argparse.Namespace(
            model="deepseek-v4-flash", max_cost_cny=80.0, max_stage_cost=25.0,
            protocol="deepseek_chat", base_url="", thinking_effort="auto",
            price_cache_hit=0.0, price_input=0.0, price_output=0.0,
            dry_run=True, api_key="sk-SECRET", occurrence_index="",
            inject_context=False, glossary="", glossary_json="", bundle="",
            source_tag="", batch_target_input_tokens=0, batch_hard_max_items=0,
            batch_target_output_tokens=0, batch_max_maps=0, context_chain=False,
        )
        cmd = _worker_cmd(args, "translate", Path("part.json"), Path("out"), 2)
        joined = " ".join(cmd)
        self.assertNotIn("sk-SECRET", joined)
        self.assertNotIn("--api-key", joined)


class _AlwaysFailClient(_FakeClient):
    """每次请求都抛可重试错误（模拟网络失败）。"""

    def chat(self, *args, **kwargs):
        from providers.deepseek import ApiError, ApiErrorKind
        raise ApiError(ApiErrorKind.NETWORK_UNSENT, "连接失败（请求未发送）")


class TestCircuitBreaker(unittest.TestCase):
    def test_circuit_breaker_pauses_after_limit(self):
        """连续 CIRCUIT_BREAKER_LIMIT 批失败后熔断暂停。"""
        with tempfile.TemporaryDirectory() as td:
            storage = RunStorage(Path(td) / "run")
            tm = TranslationMemory(Path(td) / "tm.db")
            # 小批次限制：12 条 -> 6 批，保证能触发连续 5 批失败熔断
            batcher = Batcher(limits={"target_input_tokens": 16_000,
                                      "target_output_tokens": 10_000,
                                      "soft_max_items": 2,
                                      "hard_max_items": 2})
            engine = Engine(client=_AlwaysFailClient(), storage=storage,
                            glossary=Glossary(), tm=tm, batcher=batcher)
            try:
                items = [_entry(i, f"原文{i}") for i in range(12)]
                stats = engine.run_stage("translate", items)
                self.assertIsNotNone(engine.paused_reason)
                self.assertIn("熔断", engine.paused_reason)
                self.assertGreaterEqual(stats["failed"], config.CIRCUIT_BREAKER_LIMIT)
            finally:
                tm.close()


class TestKeyLineAndTmReuse(unittest.TestCase):
    def test_key_line_agent_prefill_skips_api(self):
        """key_lines mode=agent 预填译文直接采用，不进 API。"""
        with tempfile.TemporaryDirectory() as td:
            client = _FakeClient()
            engine, tm = _make_engine(td, client, key_lines={
                "いた。": {"mode": "agent", "translation": "木板。"}})
            try:
                it = _entry(0, "いた。")
                stats = engine.run_stage("translate", [it])
                self.assertEqual(stats["reused"], 1)
                self.assertEqual(it.extra["final_zh"], "木板。")
                self.assertEqual(client.calls, 0)
            finally:
                tm.close()

    def test_tm_hit_skips_api(self):
        """TM 精确命中（human_approved）直接复用，不进 API。"""
        with tempfile.TemporaryDirectory() as td:
            client = _FakeClient()
            engine, tm = _make_engine(td, client)
            try:
                tm.add("いいよ", "可以啊", TmStatus.HUMAN_APPROVED)
                it = _entry(0, "いいよ")
                stats = engine.run_stage("translate", [it])
                self.assertEqual(stats["reused"], 1)
                self.assertEqual(it.extra["final_zh"], "可以啊")
                self.assertEqual(client.calls, 0)
            finally:
                tm.close()


class TestProviderErrorClassification(unittest.TestCase):
    def test_http_error_kinds(self):
        """HTTP 错误分类：400/401/402/403-cloudflare/429/5xx。"""
        from providers.deepseek import (ApiError, ApiErrorKind, DeepSeekClient,
                                        _is_cloudflare_block)
        client = DeepSeekClient(api_key="k")
        try:
            cases = [
                (400, "", ApiErrorKind.BAD_REQUEST),
                (401, "", ApiErrorKind.AUTH),
                (402, "", ApiErrorKind.QUOTA),
                (403, "<html>Cloudflare Ray ID: x</html>", ApiErrorKind.CLOUDFLARE_BLOCK),
                (403, "forbidden", ApiErrorKind.AUTH),
                (429, "", ApiErrorKind.RATE_LIMIT),
                (500, "", ApiErrorKind.SERVER),
            ]
            for status, text, kind in cases:
                resp = None
                if status == 403:
                    resp = type("R", (), {"text": text})()
                with self.assertRaises(ApiError) as cm:
                    client._raise_http_error(status, text, resp)
                self.assertEqual(cm.exception.kind, kind, f"status={status}")
            self.assertTrue(_is_cloudflare_block(type("R", (), {"text": "code 1010"})()))
            self.assertFalse(_is_cloudflare_block(type("R", (), {"text": "forbidden"})()))
        finally:
            client.close()


class TestWorkersResumeConflict(unittest.TestCase):
    def test_workers_rejected_with_resume(self):
        """--workers 与 --resume 冲突：退出码 1。"""
        import cli
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.json"
            src.write_text('{"こんにちは": "こんにちは"}', encoding="utf-8")
            result = cli.main(["--workers", "2", "translate", "--input", str(src),
                               "--out", str(Path(td) / "out"), "--resume", "--dry-run"])
            self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
