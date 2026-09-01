# -*- coding: utf-8 -*-
"""test_tools.py — seq-align / check-misalign 工具回归测试（2026-08-16 审计产出）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from seq_align import is_covered, _lookup  # noqa: E402
from check_misalign import check_entry, load_glossary  # noqa: E402


class TestSeqAlignMtoolRules(unittest.TestCase):
    """MTool 键切分规则（从 HOLLOWWALD 导出表逆向归纳）的回归测试。"""

    TRANS = {
        "名を": "名为",
        "真白神社": "真白神社",
        "といいます。": "被称为。",
        "といいます。\nそして私はこの神社の巫女、テン。\nよろしくお願いいたします、": "，就是这座神社的名字。\n我是这间神社的巫女，天。\n请多关照，",
        "よろしくお願いいたします、": "请多关照，",
        "様。": "大人。",
        "むにゃ……": "呜喵……",
        "ん？": "嗯？",
        "僕は……": "我……",
        "何なんですか？": "到底怎么回事？",
    }

    def test_color_segment_split_covered(self):
        # 彩色段内层独立成键 + 非彩文本拼接键
        jp = "名を\\C[17]真白神社\\C[0]といいます。\nそして私はこの神社の巫女、テン。\nよろしくお願いいたします、"
        self.assertTrue(is_covered(jp, self.TRANS))

    def test_name_variable_split_covered(self):
        # \N[10] 被剥成空，剩余逐段查表
        jp = "よろしくお願いいたします、\\N[10]様。"
        self.assertTrue(is_covered(jp, self.TRANS))

    def test_wait_code_split_covered(self):
        # \! 切分，前后段分别查表
        jp = "むにゃ……\\!ん？"
        self.assertTrue(is_covered(jp, self.TRANS))

    def test_crossline_color_block_covered(self):
        # 跨行彩色段：拼接键缺失但逐行键都在
        jp = "\\C[17]僕は……\n何なんですか？\\C[0]"
        self.assertTrue(is_covered(jp, self.TRANS))

    def test_truly_missing_detected(self):
        self.assertFalse(is_covered("存在しない未知の文章", self.TRANS))

    def test_lookup_returns_translation(self):
        cn, ok = _lookup("むにゃ……\\!ん？", self.TRANS)
        self.assertTrue(ok)
        self.assertEqual(cn, "呜喵……嗯？")


class TestCheckMisalign(unittest.TestCase):
    """错位检测规则回归测试。"""

    GLOSSARY = str(Path(__file__).resolve().parent.parent
                   / "projects" / "hollowwald" / "glossary.json")

    def setUp(self):
        self.name_map = load_glossary(Path(self.GLOSSARY))
        self.targets = [v for v in self.name_map.values() if v]

    def test_name_in_key_missing_target_flagged(self):
        issues = check_entry("サンが来た", "那个人来了", self.name_map, self.targets)
        self.assertTrue(any("サン" in i for i in issues), issues)

    def test_name_translated_ok(self):
        issues = check_entry("サンが来た", "桑来了", self.name_map, self.targets)
        self.assertFalse(any("サン" in i and "未" in i for i in issues), issues)

    def test_compound_loanword_not_flagged(self):
        issues = check_entry("サンダーブラスト", "雷霆爆破", self.name_map, self.targets)
        self.assertFalse(issues, issues)

    def test_forbidden_substring_inside_target_not_flagged(self):
        # 斯莱 ⊂ 雷斯莱斯：落在允许区间内不报
        issues = check_entry("レイスレス化した魔物", "雷斯莱斯化的魔物",
                             self.name_map, self.targets)
        self.assertFalse(any("斯莱" in i for i in issues), issues)


if __name__ == "__main__":
    unittest.main()
