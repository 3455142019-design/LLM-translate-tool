# -*- coding: utf-8 -*-
"""test_regression.py — P0-P5 新功能回归测试。

覆盖 2026-08-10/11 HOLLOWWALD 正式版教训对应的工具改进：
- validate._detect_misplaced：批内错位检测（遮罩 token 交叉匹配）
- verify_merge：合并残留/拼接检测
- qa.full_qa：全量验证器 10 类检查（漏译/假名/汉字截断/双反斜杠/术语
  变体/相邻同值/引擎键/换行拼接/黑名单/繁体开关）
- pun：双关回归库（add/check/to_key_lines）
- storage.merge_final：备份 + diff 报告
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from glossary import Glossary, GlossaryEntry
from protect import Protector
from schemas import Batch, Entry
from storage import RunStorage
from validate import validate_batch_output
from verify_merge import detect_residue, verify_merge
from qa.full_qa import run_full_qa

BS = chr(92)  # 字面反斜杠（测试数据避免转义歧义）


def _make_masked_entry(eid: str, key: str, src: str, cur: str = None) -> Entry:
    """构造带遮罩记录的 Entry（模拟 engine 遮罩后的状态）。"""
    e = Entry(id=eid, key=key, src=src, cur=cur)
    rec = Protector.mask(src)
    e.masked_src = rec.text
    e.extra["mask_record"] = rec
    return e


class TestMisplacedDetection(unittest.TestCase):
    """批内错位检测：模型把 A 的译文写到 B 上（平移式错位）。"""

    def test_token_crossmatch_misplaced(self):
        # 两条都带控制码：模型把 0 号译文写到 1 号（平移）
        a = _make_masked_entry("b_1", "こんにちは\\C[1]世界", "こんにちは\\C[1]世界")
        b = _make_masked_entry("b_2", "さようなら\\C[2]友よ", "さようなら\\C[2]友よ")
        batch = Batch(number=1, items=[a, b])
        # 取 a 的真实遮罩 token 构造错位输出：1 号位置写 0 号的译文
        from validate import _mask_token_set
        token_a = next(iter(_mask_token_set(a.masked_src)))
        raw = json.dumps({
            "0": f"你好{token_a}世界",
            "1": f"你好{token_a}世界",  # 应是 2 号原文的译文 -> 错位
        }, ensure_ascii=False)
        result = validate_batch_output(raw, batch)
        self.assertIn("b_2", result.misplaced, result.misplaced)
        self.assertNotIn("b_1", result.misplaced)

    def test_no_token_entries_not_misplaced(self):
        # 无控制码的正常译文不误报
        a = _make_masked_entry("b_1", "こんにちは", "こんにちは")
        b = _make_masked_entry("b_2", "さようなら", "さようなら")
        batch = Batch(number=1, items=[a, b])
        raw = json.dumps({"0": "你好", "1": "再见"}, ensure_ascii=False)
        result = validate_batch_output(raw, batch)
        self.assertEqual(result.misplaced, [])
        # 弱信号（无 token、字符重叠极低、长度比异常）进 suspicious 不自动 repair
        self.assertNotIn("b_1", result.misplaced)


class TestVerifyMerge(unittest.TestCase):
    """合并残留检测：旧值未删除、换行拼接。"""

    def test_residue_old_value_kept(self):
        # A2 模式：新译文后残留旧译文（游戏内换行后重复显示）
        issues = detect_residue("真是的！你就这么不想叫姐姐吗？\n不想叫姐姐吗？",
                                "不想叫姐姐吗？")
        self.assertTrue(any("残留" in i for i in issues), issues)

    def test_residue_reverse(self):
        issues = detect_residue("不想叫姐姐吗？",
                                "真是的！你就这么不想叫姐姐吗？\n不想叫姐姐吗？")
        self.assertTrue(any("残留" in i for i in issues), issues)

    def test_clean_replace_no_issue(self):
        self.assertEqual(detect_residue("你好，冒险者。", "你好，旅人。"), [])

    def test_multiline_concat(self):
        issues = detect_residue("蛋挞什么的。\n像蛋挞一样的东西。", "蛋挞什么的。")
        self.assertTrue(any("换行结构突变" in i for i in issues), issues)

    def test_verify_merge_counts(self):
        base = {"a": "1", "b": "2", "d": "4"}
        new = {"a": "1", "b": "2改", "c": "3"}
        report = verify_merge(new, base)
        self.assertEqual(report["counts"],
                         {"total": 3, "added": 1, "removed": 1,
                          "changed": 1, "unchanged": 1})
        self.assertEqual(report["added"], ["c"])
        self.assertEqual(report["removed"], ["d"])


class TestFullQa(unittest.TestCase):
    """全量验证器：10 类检查命中 + 正常译文零误报。"""

    def _glossary(self) -> Glossary:
        gl = Glossary()
        gl._entries.append(GlossaryEntry(source="サングイス", target="桑吉斯"))
        return gl

    def test_all_checkers_hit(self):
        data = {
            "こんにちは、冒険者さん。": "你好，冒险者。",
            "準備完了、出発！": "準備完了、出発！",              # 漏译
            "武器を買いたい。": "我想买武器だ。",                 # 假名残留
            ("ここは危険だ" + BS + "n気をつけて"): ("这里很危险" + BS + "n小心"),  # 双反斜杠
            "サングイスの店に寄る。": "去桑德司的店看看。",        # 术语缺失+变体
            "第一の門を開ける。": "打开第一扇门。",
            "第二の門を開ける。": "打开第一扇门。",                # 相邻同值
            "0": "打开",                                          # 引擎键误译
            "卵タルトのようなもの。": "蛋挞什么的。\n像蛋挞一样的东西。",  # 换行拼接
            "普通の会話です。": "很饥渴的对话。",                  # 黑名单
            "戦闘準備を完了する。": "戦闘準備",                    # 截断残留
        }
        rep = run_full_qa(data, glossary=self._glossary(),
                          forbidden_words=["饥渴"])
        types = set(rep.issues.keys())
        for t in ("untranslated", "kana_residue", "double_slash_n",
                  "term_variant", "dup_adjacent", "engine_key",
                  "multiline_concat", "forbidden_word", "kanji_residue"):
            self.assertIn(t, types, f"缺少 {t}: {types}")
        detail_terms = [i["detail"] for i in rep.issues.get("term_variant", [])]
        self.assertTrue(any("桑德司" in d for d in detail_terms), detail_terms)

    def test_clean_translation_zero_issue(self):
        data = {
            "こんにちは。": "你好。",
            "サングイスの店に寄る。": "去桑吉斯的店看看。",
            "レベル調整（50）": "等级调整（50）",      # 正常翻译（汉字重叠高）
            "灯り 3 1 20": "灯火 3 1 20",              # 正常翻译（含引擎参数）
            "速度：遅い": "速度：慢",                  # 正常翻译（部分汉字保留）
            "刻印製作": "刻印制作",                    # 正常翻译（汉字简体化）
        }
        rep = run_full_qa(data, glossary=self._glossary())
        self.assertEqual(rep.total_issues, 0, rep.issues)

    def test_traditional_off_by_default(self):
        data = {"これは何ですか？": "這是什麼呢？"}
        rep = run_full_qa(data)
        self.assertNotIn("traditional", rep.issues)
        rep_on = run_full_qa(data, check_traditional=True)
        self.assertIn("traditional", rep_on.issues)

    def test_norm_equal_untranslated(self):
        # 仅全半角括号差异 -> 漏译而非汉字残留
        data = {"レベル調整（50）": "レベル調整(50)"}
        rep = run_full_qa(data)
        self.assertIn("untranslated", rep.issues)
        self.assertNotIn("kanji_residue", rep.issues)

    def test_short_kana_term_skipped(self):
        # 「サン」不误伤「サンクタム」
        gl = Glossary()
        gl._entries.append(GlossaryEntry(source="サン", target="桑"))
        data = {"シェ・サンクタムへ行く。": "前往谢圣殿。"}
        rep = run_full_qa(data, glossary=gl)
        self.assertEqual(rep.total_issues, 0, rep.issues)


class TestPunManifest(unittest.TestCase):
    """双关回归库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_check(self):
        import pun
        pun.add_entry(self.root, "いた。", "pun", "板/居た双关",
                      "木板。", status="confirmed", source="game_test")
        data = {"いた。": "有、有东西在！"}
        issues = pun.check_translations(self.root, data)
        self.assertTrue(any("已确认译法被改动" in i["issue"] for i in issues))
        data_ok = {"いた。": "木板。"}
        self.assertEqual(pun.check_translations(self.root, data_ok), [])

    def test_to_key_lines(self):
        import pun
        pun.add_entry(self.root, "いた。", "pun", "板/居た双关",
                      "木板。", status="confirmed", source="game_test")
        pun.add_entry(self.root, "いろいろあった。", "slang", "待定",
                      "", status="pending")
        kl = pun.to_key_lines(pun.load(self.root))
        self.assertEqual(kl["いた。"]["mode"], "agent")
        self.assertEqual(kl["いた。"]["translation"], "木板。")
        self.assertIn("note", kl["いろいろあった。"])


class TestMergeFinalProtection(unittest.TestCase):
    """merge_final 备份 + diff 报告。"""

    def test_backup_and_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run1"
            storage = RunStorage(run_dir)
            # 先合并一版
            e1 = Entry(id="a", key="keyA", src="keyA")
            e1.extra["final_zh"] = "旧译文A"
            e2 = Entry(id="b", key="keyB", src="keyB")
            e2.extra["final_zh"] = "旧译文B"
            out = storage.merge_final([e1, e2], "final.json")
            # 再合并新版本（改了 keyA、删了 keyB、加了 keyC），传 base 出 diff
            n1 = Entry(id="a", key="keyA", src="keyA")
            n1.extra["final_zh"] = "新译文A"
            n3 = Entry(id="c", key="keyC", src="keyC")
            n3.extra["final_zh"] = "新译文C"
            storage.merge_final([n1, n3], "final.json", backup=True, base_path=out)
            backups = list((storage.final_dir / "backups").glob("*.bak"))
            self.assertEqual(len(backups), 1, "应备份旧产物")
            diff = json.loads(
                (storage.final_dir / "final.json.diff_report.json").read_text("utf-8"))
            self.assertEqual(diff["counts"],
                             {"total": 2, "added": 1, "removed": 1,
                              "changed": 1, "unchanged": 0})
            # key 级精确合并：不按序号错位
            merged = json.loads(
                (storage.final_dir / "final.json").read_text("utf-8"))
            self.assertEqual(merged["keyA"], "新译文A")
            self.assertEqual(merged["keyC"], "新译文C")


if __name__ == "__main__":
    unittest.main()
