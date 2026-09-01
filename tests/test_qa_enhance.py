# -*- coding: utf-8 -*-
"""test_qa_enhance.py — 新增检查器/分类测试（漏翻/黑名单/译名漂移/日文汉字残留）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ingest import classify_value
from schemas import Entry, EntryStatus
from glossary import Glossary, GlossaryEntry
from qa.format import FormatChecker
from qa.refusal import RefusalChecker
from qa.style import StyleChecker
from qa.terminology import TerminologyChecker


class TestIngestKanjiResidue(unittest.TestCase):
    def test_kanji_only_residue_is_mixed(self):
        # 日文汉字残留（value 是原句的一部分，无假名、与原文字符重叠高）
        self.assertEqual(classify_value("準備完了、出発！", "準備完了"),
                         EntryStatus.MIXED_LANGUAGE)

    def test_identical_value_is_untranslated(self):
        # 完全未翻译仍走 UNTRANSLATED（原分类行为不变）
        self.assertEqual(classify_value("準備完了", "準備完了"), EntryStatus.UNTRANSLATED)

    def test_real_translation_not_mixed(self):
        self.assertEqual(classify_value("準備完了", "准备完毕"), EntryStatus.HUMAN_TRANSLATION)

    def test_kana_residue_still_mixed(self):
        self.assertEqual(classify_value("準備完了", "準備完了！"), EntryStatus.MIXED_LANGUAGE)


class TestFormatEnhance(unittest.TestCase):
    def test_kanji_residue_detected(self):
        fmt = FormatChecker()
        entry = Entry(id="a", key="準備完了", src="準備完了", cur="準備完了")
        issues = fmt.check(entry)
        self.assertTrue(any("漏翻" in i or "日文汉字残留" in i for i in issues), issues)

    def test_forbidden_word_detected(self):
        fmt = FormatChecker(["饥渴"])
        entry = Entry(id="b", key="サキュバスが来た", src="サキュバスが来た",
                      cur="饥渴的魅魔来了")
        issues = fmt.check(entry)
        self.assertTrue(any("饥渴" in i for i in issues), issues)

    def test_forbidden_word_absent_ok(self):
        fmt = FormatChecker(["饥渴"])
        entry = Entry(id="c", key="サキュバスが来た", src="サキュバスが来た", cur="魅魔来了")
        self.assertEqual(fmt.check(entry), [])


class TestStyleNameDrift(unittest.TestCase):
    def test_drift_detected(self):
        items = [
            Entry(id="1", key="ギルゴーンが来た", src="ギルゴーンが来た", cur="基尔冈来了"),
            Entry(id="2", key="ギルゴーンと戦う", src="ギルゴーンと戦う", cur="与吉尔冈战斗"),
            Entry(id="3", key="ギルゴーン様", src="ギルゴーン様", cur="吉尔贡大人"),
        ]
        issues = StyleChecker().check_batch(items)
        self.assertTrue(any(issues.values()), "应检测到译名漂移")

    def test_consistent_not_detected(self):
        items = [
            Entry(id="1", key="ギルゴーンが来た", src="ギルゴーンが来た", cur="吉尔贡来了"),
            Entry(id="2", key="ギルゴーンと戦う", src="ギルゴーンと戦う", cur="与吉尔贡战斗"),
        ]
        issues = StyleChecker().check_batch(items)
        self.assertFalse(any(issues.values()), "一致译名不应报漂移")


class TestTerminologyEnhance(unittest.TestCase):
    """术语检查器防误报（2026-08-16 HOLLOWWALD 审计教训）。"""

    def _glossary(self):
        return Glossary([
            GlossaryEntry(source="サン", target="桑",
                          forbidden_variants=["桑德", "桑德斯"]),
            GlossaryEntry(source="サンデス", target="桑德司",
                          forbidden_variants=["桑德", "桑德斯"]),
            GlossaryEntry(source="ミシュパタル", target="米修帕塔尔",
                          forbidden_variants=["米修"]),
        ])

    def test_correct_term_not_flagged_by_substring_forbidden(self):
        """正确译名含禁止子串（桑德司含'桑德'）不误报。"""
        checker = TerminologyChecker(self._glossary())
        entry = Entry(id="1", key="ササンデスガ？", src="ササンデスガ？",
                      cur="桑德司嘎？")
        self.assertEqual(checker.check(entry), [])

    def test_real_forbidden_variant_still_flagged(self):
        """独立使用禁止译法仍要报。"""
        checker = TerminologyChecker(self._glossary())
        entry = Entry(id="2", key="サンデス", src="サンデス", cur="桑德斯")
        issues = checker.check(entry)
        self.assertTrue(any("桑德斯" in i for i in issues), issues)

    def test_compound_loanword_source_not_flagged(self):
        """复合外来语内嵌短术语（サンダーブラスト 里的 サン）不报术语缺失。"""
        checker = TerminologyChecker(self._glossary())
        entry = Entry(id="3", key="サンダーブラスト", src="サンダーブラスト",
                      cur="雷霆爆破")
        self.assertEqual(checker.check(entry), [])

    def test_standalone_source_missing_target_flagged(self):
        """独立成词且未被指代词替代的术语缺失译名仍要报。"""
        checker = TerminologyChecker(self._glossary())
        entry = Entry(id="4", key="サンが来た", src="サンが来た", cur="那个人来了")
        issues = checker.check(entry)
        self.assertTrue(any("サン" in i for i in issues), issues)

    def test_pronoun_reference_not_flagged(self):
        """对话中短人名被 你/他/她 指代属自然译法，不报术语缺失。"""
        checker = TerminologyChecker(self._glossary())
        entry = Entry(id="4b", key="サンをよく観察してる", src="サンをよく観察してる",
                      cur="我一直在观察你")
        self.assertEqual(checker.check(entry), [])

    def test_forbidden_inside_target_and_standalone_mixed(self):
        """同一译文里正确译名与独立禁止译法并存：只报独立出现。"""
        checker = TerminologyChecker(self._glossary())
        entry = Entry(id="5", key="ミシュパタル", src="ミシュパタル",
                      cur="米修帕塔尔和米修大人")
        issues = checker.check(entry)
        self.assertTrue(any("米修" in i for i in issues), issues)


class TestRefusalEnhance(unittest.TestCase):
    """拒绝/净化检测防误报（2026-08-16 HOLLOWWALD 审计教训）。"""

    def _check(self, src, zh):
        return RefusalChecker().check(
            Entry(id="r", key=src, src=src, cur=zh))

    def test_game_question_not_flagged(self):
        """游戏内普通问句（"不能回答吗"）不得误报为拒绝。"""
        self.assertEqual(self._check("返事、できないか。", "不能回答吗。"), [])
        self.assertEqual(self._check("労働か……", "我能不能回答得上呢。"), [])
        self.assertEqual(self._check("まだ応えられないから……", "因为还不能回答……"), [])

    def test_game_line_with_guarantee_not_flagged(self):
        """游戏台词"无法保证/不会放你回去"不得误报。"""
        self.assertEqual(
            self._check("x", "不过，你们是想继续往前走吧？\n那段路上的安全我可无法保证。"), [])
        self.assertEqual(
            self._check("x", "服务已经开始了呱！\n不让你满足，就不会放你回去呱！"), [])

    def test_real_refusal_still_flagged(self):
        """真实拒答（道歉+拒绝动作+内容指代）仍要报。"""
        for zh in ("抱歉，我无法翻译这类内容。",
                   "对不起，我无法提供这方面的帮助。",
                   "抱歉，我不能继续生成此类内容。"):
            self.assertTrue(self._check("セックス", zh), zh)
        self.assertTrue(self._check("セックス", "这个内容不适合，涉及违规。"))


class TestStyleInterjection(unittest.TestCase):
    """纯叹词/拟声短句共用译文不报泛化（2026-08-16 审计教训）。"""

    def test_interjections_not_flagged(self):
        items = [Entry(id=str(i), key=s, src=s, cur=c) for i, (s, c) in enumerate([
            ("おおっと。", "哦哦。"), ("ふふ。", "呵呵。"), ("うっ……", "唔……"),
            ("うぐ……", "唔……"), ("ん？", "嗯？"), ("あっ……", "啊……"),
        ])]
        issues = StyleChecker().check_batch(items)
        self.assertFalse(any("泛化" in i for v in issues.values() for i in v), issues)


if __name__ == "__main__":
    unittest.main()
