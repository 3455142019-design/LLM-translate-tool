# -*- coding: utf-8 -*-
"""test_protect.py — 占位符遮罩/恢复/校验测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from protect import Protector
from schemas import PlaceholderKind


class TestProtect(unittest.TestCase):
    def test_mask_fixed_and_movable(self):
        """\\C[2] 固定控制码 + %d 可移动变量 同时遮罩。"""
        src = r"\C[2]サキュバス\C[0]に%dダメージを与えた！"
        rec = Protector.mask(src)
        # 遮罩后不含原始控制码
        self.assertNotIn(r"\C[", rec.text)
        self.assertNotIn("%d", rec.text)
        # 占位符数量：2 个固定 + 1 个可移动
        fixed = rec.tokens_of(PlaceholderKind.FIXED)
        movable = rec.tokens_of(PlaceholderKind.MOVABLE)
        self.assertEqual(len(fixed), 2)
        self.assertEqual(len(movable), 1)
        # 原文顺序：固定码位置应在文本中保持先后
        self.assertLess(rec.text.find(fixed[0]), rec.text.find(fixed[1]))

    def test_restore_roundtrip(self):
        """遮罩 -> 恢复 应得到原文。"""
        src = r"\C[2]サキュバス\C[0]に%dダメージを与えた！"
        rec = Protector.mask(src)
        restored, unknown = Protector.restore(rec.text, rec)
        self.assertEqual(restored, src)
        self.assertEqual(unknown, [])

    def test_verify_fixed_order_broken(self):
        """固定控制码顺序被改动 -> 校验失败。"""
        src = r"\C[2]A\C[0]B"
        rec = Protector.mask(src)
        fixed = rec.tokens_of(PlaceholderKind.FIXED)
        broken = f"{fixed[1]}A{fixed[0]}B"  # 交换顺序
        ok, issues = Protector.verify(broken, rec)
        self.assertFalse(ok)
        self.assertTrue(any("顺序" in i for i in issues))

    def test_verify_movable_permutation_ok(self):
        """可移动变量重排 -> 校验通过。"""
        src = "%sのHPが%d回復した"
        rec = Protector.mask(src)
        mv = rec.tokens_of(PlaceholderKind.MOVABLE)
        permuted = f"{mv[1]}的HP恢复了{mv[0]}点"  # 重排
        ok, issues = Protector.verify(permuted, rec)
        self.assertTrue(ok, f"issues={issues}")

    def test_verify_missing_token_fails(self):
        """模型漏掉占位符 -> 校验失败。"""
        src = r"\C[2]A\C[0]B"
        rec = Protector.mask(src)
        fixed = rec.tokens_of(PlaceholderKind.FIXED)
        dropped = f"{fixed[0]}AB"  # 漏掉第二个
        ok, issues = Protector.verify(dropped, rec)
        self.assertFalse(ok)
        self.assertTrue(any("缺失" in i for i in issues))

    def test_paired_tags(self):
        """成对标签整体遮罩 + 恢复。"""
        src = "<color=red>警告</color>发生了！"
        rec = Protector.mask(src)
        self.assertEqual(len(rec.tokens_of(PlaceholderKind.PAIRED)), 1)
        restored, _ = Protector.restore(rec.text, rec)
        self.assertEqual(restored, src)

    def test_real_talk_marker(self):
        """口上真实标记：\\H 与 {myname} 不遮罩（模型原生保留——2026-08-02 实测）。"""
        src = r"\Hあの{myname}は…"
        rec = Protector.mask(src)
        self.assertIn(r"\H", rec.text)
        self.assertIn("{myname}", rec.text)
        restored, _ = Protector.restore(rec.text, rec)
        self.assertEqual(restored, src)

    def test_jp_quotes_not_masked(self):
        """引号「」不遮罩（模型原生处理良好，遮罩反而被当噪声丢弃）。"""
        src = "「……あれま、もう限界でしたか。\n　では、今夜はおやすみなさい」"
        rec = Protector.mask(src)
        self.assertIn("「", rec.text)
        self.assertIn("」", rec.text)
        self.assertEqual(len(rec.tokens_of(PlaceholderKind.QUOTE)), 0)

    def test_mixed_fixed_quote_text_level(self):
        """F+Q 混合文本：\\H 与引号不遮罩，其余控制码仍遮罩（实测回归）。"""
        src = "「ほらほら、ギブアップしちゃえ～\\H」"
        rec = Protector.mask(src)
        self.assertIn(r"\H", rec.text)
        self.assertIn("「", rec.text)
        # 无 %s/控制码 -> 无占位符，verify 恒通过（文本级校验在 validate.py）
        ok, issues = Protector.verify("来来，快认输吧～\\H", rec)
        self.assertTrue(ok, f"issues={issues}")


if __name__ == "__main__":
    unittest.main()
