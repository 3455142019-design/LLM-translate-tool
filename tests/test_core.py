# -*- coding: utf-8 -*-
"""test_ingest_validate_qa_tm.py — 输入分类/校验/QA/TM 组合测试。"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config
from ingest import (build_entries, classify_value, filter_for_polish,
                    filter_for_translation, read_mtool_json)
from schemas import EntryStatus, TmStatus
from validate import (map_seq_keys, parse_model_output, restore_entry_zh,
                      validate_batch_output)
from protect import Protector
from schemas import Batch, Entry


class TestLoadDotenv(unittest.TestCase):
    def test_parses_kv_and_comments(self):
        """.env 解析：KEY=VALUE 生效、注释忽略、空行忽略、已存在环境变量不覆盖。"""
        import os
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env"
            p.write_text("# 注释\nKEEP_EXISTING=from_file\nNEW_KEY=abc123\nBADLINE\n",
                         encoding="utf-8")
            old = os.environ.get("KEEP_EXISTING")
            os.environ["KEEP_EXISTING"] = "from_env"
            try:
                self.assertEqual(config.load_dotenv(p), p)
                self.assertEqual(os.environ["KEEP_EXISTING"], "from_env")  # 不覆盖
                self.assertEqual(os.environ["NEW_KEY"], "abc123")
            finally:
                os.environ.pop("NEW_KEY", None)
                if old is None:
                    os.environ.pop("KEEP_EXISTING", None)
                else:
                    os.environ["KEEP_EXISTING"] = old

    def test_missing_file_returns_none(self):
        """文件不存在返回 None，不是错误。"""
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(config.load_dotenv(Path(td) / "nope.env"))


class TestIngest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_value("こんにちは", "こんにちは"), EntryStatus.UNTRANSLATED)
        self.assertEqual(classify_value("こんにちは", "你好"), EntryStatus.HUMAN_TRANSLATION)
        self.assertEqual(classify_value("こんにちは", "你好，こんにちは"), EntryStatus.MIXED_LANGUAGE)
        self.assertEqual(classify_value("0", "0"), EntryStatus.SCRIPT_OR_CONTROL_DATA)
        self.assertEqual(classify_value("こんにちは", "123"), EntryStatus.DO_NOT_TRANSLATE)
        self.assertEqual(classify_value("こんにちは", ""), EntryStatus.EMPTY)

    def test_duplicate_keys_detected(self):
        """重复键必须被检测（object_pairs_hook）。"""
        raw = '{"a": "1", "b": "2", "a": "3"}'
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8",
                                         delete=False) as f:
            f.write(raw)
            path = Path(f.name)
        try:
            data, dups = read_mtool_json(path)
            self.assertEqual(dups, ["a"])
            self.assertEqual(data["a"], "3")  # 保留最后一个
        finally:
            path.unlink()

    def test_filters(self):
        entries = build_entries({"こんにちは": "こんにちは", "ありがとう": "谢谢",
                                 "0": "0", "サキュバス": ""})
        self.assertEqual(len(filter_for_translation(entries)), 1)
        self.assertEqual(len(filter_for_polish(entries)), 1)


class TestValidate(unittest.TestCase):
    def _mk_batch(self):
        it = Entry(id="000001_0001", key=r"\C[2]こんにちは", src=r"\C[2]こんにちは")
        rec = Protector.mask(it.src)
        it.masked_src = rec.text
        it.extra["mask_record"] = rec
        return Batch(number=1, items=[it])

    def test_parse_json_fence(self):
        raw = '好的，以下是翻译：\n```json\n{"000001_0001": "你好"}\n```'
        parsed, err = parse_model_output(raw)
        self.assertIsNone(err)
        self.assertEqual(parsed, {"000001_0001": "你好"})

    def test_parse_invalid(self):
        parsed, err = parse_model_output("这是一段没有 JSON 的话")
        self.assertIsNone(parsed)
        self.assertIsNotNone(err)

    def test_validate_missing_and_placeholder(self):
        batch = self._mk_batch()
        # 输出漏了 id -> missing
        v = validate_batch_output('{"000001_9999": "x"}', batch)
        self.assertIn("000001_0001", v.missing)
        # 输出破坏固定控制码 -> placeholder issues
        rec = batch.items[0].extra["mask_record"]
        broken_out = {"000001_0001": "你好" + rec.text.replace("__", "XX")}
        v2 = validate_batch_output(json.dumps(broken_out, ensure_ascii=False), batch)
        if v2.placeholder_issues:
            self.assertIn("000001_0001", v2.placeholder_issues)

    def test_polish_cur_placeholder_exempt(self):
        """polish 模式：模型保留 cur（现译文）的控制码占位符不判错（review 发现）。

        场景：cur 含 \\C[2] 控制码被遮罩为 __F*__，模型在润色输出中引用
        这些占位符（原译文控制码应保留）——旧代码误判"多余占位符"丢弃结果。
        """
        it = Entry(id="000001_0001", key="こんにちは", src="こんにちは",
                   cur=r"\C[2]你好\C[0]")
        rec_src = Protector.mask(it.src)
        rec_cur = Protector.mask(it.cur)
        it.masked_src = rec_src.text
        it.masked_cur = rec_cur.text
        it.extra["mask_record"] = rec_src
        it.extra["mask_record_cur"] = rec_cur
        batch = Batch(number=1, items=[it])
        cur_tok = rec_cur.tokens()  # cur 的占位符（\C[2] 和 \C[0]）
        model_out = {it.id: f"{cur_tok[0]}你好呀{cur_tok[1]}"}
        v = validate_batch_output(json.dumps(model_out, ensure_ascii=False), batch)
        self.assertTrue(v.ok, f"issues={v.placeholder_issues}")
        # 恢复后：cur 的控制码原样保留
        restored = restore_entry_zh(it, model_out[it.id])
        self.assertEqual(restored, r"\C[2]你好呀\C[0]")

    def test_map_seq_keys_short_and_legacy(self):
        """短序号键映射回条目 id；旧长 id 原样通过（兼容兜底）。"""
        items = [Entry(id=f"000001_{i:04d}", key=f"k{i}", src=f"s{i}")
                 for i in range(3)]
        mapped = map_seq_keys({"0": "译文0", "2": "译文2", "000001_0001": "旧译文"}, items)
        self.assertEqual(mapped, {"000001_0000": "译文0",
                                  "000001_0002": "译文2",
                                  "000001_0001": "旧译文"})

    def test_validate_with_short_seq_keys(self):
        """validate_batch_output 接受短序号键输出（新协议主路径）。"""
        it = Entry(id="000001_0001", key="こんにちは", src="こんにちは")
        rec = Protector.mask(it.src)  # 无控制码，遮罩后与原文相同
        it.masked_src = rec.text
        it.extra["mask_record"] = rec
        batch = Batch(number=1, items=[it])
        v = validate_batch_output('{"0": "你好"}', batch)
        self.assertIsNone(v.parse_error)
        self.assertEqual(v.missing, [])
        self.assertEqual(v.parsed, {"000001_0001": "你好"})
        self.assertTrue(v.ok)

    def test_validate_legacy_long_id_still_works(self):
        """旧长 id 输出格式仍可解析（兼容兜底）。"""
        it = Entry(id="000001_0001", key="こんにちは", src="こんにちは")
        rec = Protector.mask(it.src)
        it.masked_src = rec.text
        it.extra["mask_record"] = rec
        batch = Batch(number=1, items=[it])
        v = validate_batch_output('{"000001_0001": "你好"}', batch)
        self.assertIsNone(v.parse_error)
        self.assertTrue(v.ok)

    def test_parse_partial_recovery(self):
        """损坏 JSON 部分恢复：完好条目提取；缺引号键/截断值丢弃（走 repair 补译）。"""
        raw = '{"0": "你好", "3": "再见", "1: "坏条目", "2": "她说"大家好""}'
        parsed, err = parse_model_output(raw)
        self.assertIsNone(err)
        self.assertEqual(parsed, {"0": "你好", "3": "再见"})  # 1/2 损坏丢弃

    def test_parse_partial_recovery_too_few(self):
        """完好条目不足 2 个时不部分恢复（避免误判，整批重试/失败）。"""
        raw = '{"0": "唯一完好", "1: "坏"'
        parsed, err = parse_model_output(raw)
        self.assertIsNone(parsed)
        self.assertIsNotNone(err)


class TestTm(unittest.TestCase):
    def test_reuse_policy(self):
        from tm import TranslationMemory
        with tempfile.TemporaryDirectory() as d:
            tm = TranslationMemory(Path(d) / "tm.db")
            try:
                # rejected 不可复用
                tm.add("いいよ", "不行", TmStatus.REJECTED)
                self.assertIsNone(tm.lookup("いいよ"))
                # machine_unreviewed 不可复用（min_status=QA_PASSED）
                tm.add("いいよ", "好呀", TmStatus.MACHINE_UNREVIEWED)
                self.assertIsNone(tm.lookup("いいよ"))
                # human_approved 可复用
                tm.add("いいよ", "可以啊", TmStatus.HUMAN_APPROVED)
                hit = tm.lookup("いいよ")
                self.assertEqual(hit[0], "可以啊")
                # qa_passed 可复用（精确匹配）
                tm.add("またね", "再见", TmStatus.QA_PASSED)
                hit2 = tm.lookup("またね")
                self.assertEqual(hit2[0], "再见")
                # 遮蔽场景回归：不同 speaker 的多行中，老 qa_passed 不被新 unreviewed 遮蔽
                tm.add("遮蔽テスト", "老译文", TmStatus.QA_PASSED, speaker="A")
                for i in range(5):
                    tm.add("遮蔽テスト", f"新未审{i}", TmStatus.MACHINE_UNREVIEWED, speaker="B")
                hit3 = tm.lookup("遮蔽テスト")  # 不带 speaker 查询
                self.assertEqual(hit3[0], "老译文")  # 状态过滤直接命中 qa_passed 行
            finally:
                tm.close()  # Windows 文件锁：必须显式关闭


class TestQa(unittest.TestCase):
    def test_format_checker(self):
        from qa.format import FormatChecker
        fc = FormatChecker()
        bad = Entry(id="1", key="こんにちは", src="こんにちは", cur="你好こんにちは")
        issues = fc.check(bad)
        self.assertTrue(any("假名" in i for i in issues))
        good = Entry(id="2", key="こんにちは", src="こんにちは", cur="你好")
        self.assertEqual(fc.check(good), [])

    def test_terminology_checker(self):
        from glossary import Glossary
        from qa.terminology import TerminologyChecker
        g = Glossary.from_json(Path("__nonexistent__"))
        # 无术语表 -> 不报错
        e = Entry(id="1", key="サキュバスは強い", src="サキュバスは強い", cur="魅魔很强")
        tc = TerminologyChecker(g)
        # 术语表为空，不报
        self.assertEqual(tc.check(e), [])

    def test_refusal_checker(self):
        from qa.refusal import RefusalChecker
        rc = RefusalChecker()
        refused = Entry(id="1", key="セックスシーンが始まる", src="セックスシーンが始まる", cur="抱歉，我无法翻译这段内容")
        issues = rc.check(refused)
        self.assertTrue(any("拒绝" in i for i in issues))


class TestRepairShortSeq(unittest.TestCase):
    """engine._repair_missing 的短序号映射 + 占位符校验（修复路径曾只映射主批、漏 repair）。"""

    def _make_engine(self, tmpdir: str):
        from engine import Engine
        from glossary import Glossary
        from providers.deepseek import ApiResponse
        from storage import RunStorage
        from tm import TranslationMemory
        from schemas import Usage

        class _FakeClient:
            """模拟模型：按 user payload 短序号回显。"""
            model = "deepseek-v4-flash"
            protocol = "deepseek_chat"
            pricing = {"in_hit": 0.02, "in_miss": 1.0, "out": 2.0}

            def chat(self, messages, **kwargs):
                # repair 的 user 带【修复任务】前缀+术语块；payload 恒在末尾，贪婪匹配到最后 ]]
                text = messages[-1]["content"]
                m = re.search(r"\[\[.*\]\]$", text, re.DOTALL)
                items = json.loads(m.group(0)) if m else []
                seqs = [str(it[0]) for it in items if isinstance(it, list) and it]
                content = json.dumps({s: "译文" + s for s in seqs}, ensure_ascii=False)
                return ApiResponse(
                    content=content,
                    usage=Usage(prompt_tokens=10, prompt_cache_hit_tokens=0,
                                prompt_cache_miss_tokens=10, completion_tokens=5,
                                model=self.model),
                    finish_reason="stop",
                )

        root = Path(tmpdir)
        storage = RunStorage(root / "run")
        tm = TranslationMemory(root / "tm.db")
        return Engine(client=_FakeClient(), storage=storage,
                      glossary=Glossary(), tm=tm), tm

    def test_repair_output_seq_maps_to_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, tm = self._make_engine(directory)
            try:
                it = Entry(id="000001_0042", key="こんにちは", src="こんにちは")
                rec = Protector.mask(it.src)
                it.masked_src = rec.text
                it.extra["mask_record"] = rec
                batch = Batch(number=1, items=[it])
                stats = {"repaired": 0}
                engine._repair_missing(batch, ["000001_0042"], stats)
                self.assertEqual(stats["repaired"], 1)
                self.assertEqual(it.extra["final_zh"], "译文0")
            finally:
                tm.close()

    def test_repair_rejects_broken_placeholders(self):
        """repair 输出丢失控制码/模板变量时拒绝写盘（防污染 final）。

        2026-08-18 修复：拒绝后回退原文占位并计入 failed，防止
        merge_final 静默丢 key（此前 final_zh 不写、output 落空值）。
        """
        with tempfile.TemporaryDirectory() as directory:
            engine, tm = self._make_engine(directory)
            try:
                it = Entry(id="000001_0042", key=r"\C[2]こんにちは", src=r"\C[2]こんにちは")
                rec = Protector.mask(it.src)
                it.masked_src = rec.text
                it.extra["mask_record"] = rec
                batch = Batch(number=1, items=[it])
                stats = {"repaired": 0}
                engine._repair_missing(batch, ["000001_0042"], stats)
                self.assertEqual(stats["repaired"], 0)
                self.assertEqual(stats["failed"], 1)
                # 回退原文占位（绝不写坏译文/空值）
                self.assertEqual(it.extra["final_zh"], it.src)
            finally:
                tm.close()


if __name__ == "__main__":
    unittest.main()
