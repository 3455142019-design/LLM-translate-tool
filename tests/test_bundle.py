# -*- coding: utf-8 -*-
"""test_bundle.py — 项目数据包创建/加载/合并测试。"""
import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config
from bundle import ProjectBundle, create_project
from glossary import Glossary, GlossaryEntry


class TestProjectBundle(unittest.TestCase):
    NAME = "_test_bundle_xyz"

    def setUp(self):
        root = config.PROJECTS_ROOT / self.NAME
        if root.exists():
            shutil.rmtree(root)

    def tearDown(self):
        root = config.PROJECTS_ROOT / self.NAME
        if root.exists():
            shutil.rmtree(root)

    def test_create_generates_skeleton(self):
        root = create_project(self.NAME)
        for fn in ("project.json", "glossary.json", "world.json", "chars.json",
                   "story_chain.json", "policies.json", "key_lines.json"):
            self.assertTrue((root / fn).exists(), f"缺少 {fn}")
        self.assertTrue((root / "talk").is_dir())

    def test_load_parses_files(self):
        root = create_project(self.NAME)
        (root / "glossary.json").write_text(json.dumps(
            [{"source": "ギルゴーン", "target": "吉尔贡", "priority": "high",
              "forbidden_variants": ["吉尔冈", "基尔冈"]}]), encoding="utf-8")
        (root / "key_lines.json").write_text(json.dumps(
            {"おまえ、ギルゴーンか？": {"note": "双关", "mode": "agent",
                                       "translation": "喂，你是吉尔贡吗？"}}), encoding="utf-8")
        (root / "policies.json").write_text(json.dumps(
            {"forbidden_words": ["饥渴", "淫靡"]}), encoding="utf-8")
        bundle = ProjectBundle.load(self.NAME)
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle.glossary._by_source["ギルゴーン"].target, "吉尔贡")
        self.assertEqual(bundle.forbidden_words, ["饥渴", "淫靡"])
        self.assertIn("おまえ、ギルゴーンか？", bundle.key_lines)
        self.assertEqual(bundle.key_lines["おまえ、ギルゴーンか？"]["mode"], "agent")

    def test_load_missing_returns_none(self):
        self.assertIsNone(ProjectBundle.load("__not_exist__"))

    def test_glossary_merge_override(self):
        base = Glossary()
        base._entries = [GlossaryEntry(source="ギルゴーン", target="吉尔贡")]
        base._by_source = {e.source: e for e in base._entries}
        override = Glossary()
        override._entries = [GlossaryEntry(source="ギルゴーン", target="吉尔贡EX")]
        override._by_source = {e.source: e for e in override._entries}
        base.merge(override)
        self.assertEqual(base._by_source["ギルゴーン"].target, "吉尔贡EX")


if __name__ == "__main__":
    unittest.main()
