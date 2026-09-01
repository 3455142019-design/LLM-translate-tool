# -*- coding: utf-8 -*-
"""test_rmmz.py — RPG Maker MZ 事件解析 + 场景分组测试（合成数据，零 API）。

覆盖：
- scan_rmmz_events：事件页解析（scene/speaker/前句/后句/事件名）
- 多事件共用文本 -> multi_context
- apply_context 填充 Entry
- batcher.grouping_key 优先 scene
- 同场景同批（预算内不切批）；大场景超预算仍切批但按场景边界
- 无 scene 条目退化启发式（原有行为不破坏）
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from batcher import Batcher
from context import apply_context
from ingest import build_entries
from rmmz import scan_rmmz_events
from schemas import Batch, Entry


def _cmd(code, params):
    """构造一条 RPG Maker MZ 事件命令。"""
    return {"code": code, "indent": 0, "parameters": params}


def _make_data_dir(tmp: Path) -> Path:
    """合成最小 RPG Maker MZ data 目录。"""
    data = tmp / "data"
    data.mkdir()
    # Map001：ev1 调查事件（1 条文本）；ev2 对话事件（3 条文本，B2 与公共事件共用）
    map001 = {
        "id": 1,
        "events": [
            None,
            {"id": 1, "name": "調べる", "pages": [{"conditions": {}, "list": [
                _cmd(101, ["face01", 0, 0, 2, "サン"]),
                _cmd(401, ["いた。"]),
            ]}]},
            {"id": 2, "name": "村人との会話", "pages": [{"conditions": {}, "list": [
                _cmd(101, ["face02", 0, 0, 2, "村人"]),
                _cmd(401, ["こんにちは。"]),
                _cmd(401, ["今日はいい天気ですね。"]),
                _cmd(401, ["また明日会いましょう。"]),
            ]}]},
        ],
    }
    (data / "Map001.json").write_text(json.dumps(map001, ensure_ascii=False), encoding="utf-8")
    # MapInfos：id=1 -> テストの村
    (data / "MapInfos.json").write_text(
        json.dumps([None, {"name": "テストの村"}], ensure_ascii=False), encoding="utf-8")
    # CommonEvents：ev33 两条文本，其中一条与 Map001/ev2 共用（多语境）
    common = [
        None,
        {"id": 33, "name": "天気予報", "pages": [{"conditions": {}, "list": [
            _cmd(101, ["face03", 0, 0, 2, "ナレーター"]),
            _cmd(401, ["今日はいい天気ですね。"]),
            _cmd(401, ["明日は雨でしょう。"]),
        ]}]},
    ]
    (data / "CommonEvents.json").write_text(json.dumps(common, ensure_ascii=False), encoding="utf-8")
    return data


class TestScanEvents(unittest.TestCase):
    """scan_rmmz_events 解析正确性。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = _make_data_dir(Path(cls.tmp.name))
        cls.index = scan_rmmz_events(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_basic_occurrence(self):
        occs = self.index.get("いた。")
        self.assertEqual(len(occs), 1)
        o = occs[0]
        self.assertEqual(o.scene, "Map001/ev1")       # 场景=文件/事件id
        self.assertEqual(o.speaker_candidate, "face01")  # 立绘名=说话人线索
        self.assertEqual(o.method, "調べる")            # 事件名
        self.assertEqual(o.file, "Map001.json")
        self.assertEqual(o.preceding, "")               # 首句无前句

    def test_preceding_following(self):
        occs = self.index.get("今日はいい天気ですね。")
        # 两个 occurrence（Map001/ev2 与 CommonEvents/ev33）
        self.assertEqual(len(occs), 2)
        by_scene = {o.scene: o for o in occs}
        m = by_scene["Map001/ev2"]
        self.assertEqual(m.preceding, "こんにちは。")
        self.assertEqual(m.following, "また明日会いましょう。")
        c = by_scene["CommonEvents/ev33"]
        self.assertEqual(c.preceding, "")
        self.assertEqual(c.following, "明日は雨でしょう。")

    def test_multi_context(self):
        self.assertTrue(self.index.is_multi_context("今日はいい天気ですね。"))
        self.assertFalse(self.index.is_multi_context("いた。"))


class TestApplyContext(unittest.TestCase):
    """apply_context 填充 Entry。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = _make_data_dir(Path(cls.tmp.name))
        cls.index = scan_rmmz_events(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_apply(self):
        entries = build_entries({
            "いた。": "いた。",
            "今日はいい天気ですね。": "今日はいい天気ですね。",
            "木箱だ。": "木箱だ。",  # 索引外条目
        })
        apply_context(entries, self.index)
        by_key = {e.key: e for e in entries}
        e1 = by_key["いた。"]
        self.assertEqual(e1.scene, "Map001/ev1")
        self.assertEqual(e1.speaker, "face01")
        self.assertEqual(e1.occurrences, 1)
        self.assertFalse(e1.multi_context)
        e2 = by_key["今日はいい天気ですね。"]
        self.assertEqual(e2.scene, "Map001/ev2")  # 取第一个 occurrence
        self.assertIsNone(e2.speaker)             # 多语境不猜说话人
        self.assertTrue(e2.multi_context)
        e3 = by_key["木箱だ。"]
        self.assertIsNone(e3.scene)
        self.assertEqual(e3.occurrences, 0)


class TestBatcherScene(unittest.TestCase):
    """batcher 场景分组与不切批。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = _make_data_dir(Path(cls.tmp.name))
        cls.index = scan_rmmz_events(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def _entry(key: str, scene=None, speaker=None) -> Entry:
        e = Entry(id=key, key=key, src=key)
        e.scene = scene
        e.speaker = speaker
        return e

    def test_grouping_key_prefers_scene(self):
        # 地图级分组：Map001/ev1 -> map:Map001；CommonEvents/ev33 -> map:CommonEvents
        self.assertEqual(Batcher.grouping_key(self._entry("いた。", scene="Map001/ev1")),
                         "map:Map001")
        self.assertEqual(Batcher.grouping_key(self._entry("いた。", scene="CommonEvents/ev33")),
                         "map:CommonEvents")
        # 无 scene 退化：日文开头 -> hiragana
        self.assertEqual(Batcher.grouping_key(self._entry("いた。")), "hiragana")

    def test_same_scene_same_batch(self):
        batcher = Batcher()
        items = [
            self._entry("こんにちは。", scene="Map001/ev2"),
            self._entry("今日はいい天気ですね。", scene="Map001/ev2"),
            self._entry("また明日会いましょう。", scene="Map001/ev2"),
            self._entry("いた。", scene="Map001/ev1"),
            self._entry("メニュー項目", scene=None),
        ]
        batches = batcher.build_batches(items)
        # 地图级分组：Map001 下 ev1+ev2 全部同批（地图内上下文完整）
        map_batch = next(b for b in batches
                         if any(i.scene and i.scene.startswith("Map001") for i in b.items))
        map_ids = [i.key for i in map_batch.items]
        self.assertIn("こんにちは。", map_ids)
        self.assertIn("今日はいい天気ですね。", map_ids)
        self.assertIn("また明日会いましょう。", map_ids)
        self.assertIn("いた。", map_ids)
        # 任何批次不混地图（分组边界强制切批）
        for b in batches:
            maps = {(i.scene or "").split("/")[0] for i in b.items}
            self.assertLessEqual(len(maps), 1)

    def test_large_scene_splits_by_budget(self):
        """超预算×1.3 的大地图允许切批，但切批边界仍按地图（不混入他地图）。"""
        batcher = Batcher()
        # 400 条长文本（≈30K+ 字符 ≈ 25K+ token > 16K×1.3=20.8K），soft_max=250
        big_scene = [self._entry(f"とても長い台詞テキストその{n}。"
                                 "ここは長い会話が続くので予算を超えるはずです。"
                                 "文脈を保つために切れ目を探します。", scene="Map001/ev2")
                     for n in range(400)]
        other = [self._entry("いた。", scene="Map999/ev1")]
        batches = batcher.build_batches(big_scene + other)
        self.assertGreater(len(batches), 1)  # 确实被切批
        for b in batches:
            maps = {(i.scene or "").split("/")[0] for i in b.items}
            self.assertLessEqual(len(maps), 1)  # 边界仍按地图


class TestPackMode(unittest.TestCase):
    """大包模式：地图编号连贯 + 地图边界切包 + 无 scene 条目分离。"""

    @staticmethod
    def _entry(key: str, scene=None) -> Entry:
        e = Entry(id=key, key=key, src=key)
        e.scene = scene
        return e

    def _batcher(self, max_maps=2):
        return Batcher(limits={
            "target_input_tokens": 900_000, "target_output_tokens": 350_000,
            "soft_max_items": 250, "hard_max_items": 20_000, "max_maps": max_maps,
        })

    def test_map_number_order(self):
        """地图编号数字序：Map001 < Map002 < Map010；max_maps=2 → 2 包。"""
        b = self._batcher()
        items = [
            self._entry("10の台詞", "Map010/ev1"),
            self._entry("2の台詞", "Map002/ev1"),
            self._entry("1の台詞", "Map001/ev1"),
        ]
        batches = b.build_batches(items)
        self.assertEqual(len(batches), 2)  # {Map001,Map002} / {Map010}
        first_maps = [i.scene for i in batches[0].items]
        self.assertEqual(first_maps, ["Map001/ev1", "Map002/ev1"])  # 数字序 + 同批
        self.assertEqual(batches[1].items[0].scene, "Map010/ev1")

    def test_pack_boundary_by_maps(self):
        """max_maps=2：Map001+Map002 一包，Map010 另包；同地图不拆散。"""
        b = self._batcher(max_maps=2)
        items = [
            self._entry("10a", "Map010/ev1"), self._entry("10b", "Map010/ev2"),
            self._entry("2a", "Map002/ev1"), self._entry("2b", "Map002/ev2"), self._entry("2c", "Map002/ev3"),
            self._entry("1a", "Map001/ev1"),
        ]
        batches = b.build_batches(items)
        self.assertEqual(len(batches), 2)  # {Map001,Map002} / {Map010}
        for bt in batches:
            maps = {i.scene.split("/")[0] for i in bt.items}
            self.assertLessEqual(len(maps), 2)  # 每包 ≤ max_maps 地图
        # Map002 的 3 条同批（同地图不拆散）
        pack1 = batches[0]
        map2_items = [i.key for i in pack1.items if i.scene.startswith("Map002")]
        self.assertEqual(map2_items, ["2a", "2b", "2c"])

    def test_no_scene_separated(self):
        """无 scene 条目（物品/系统文本）不进大包，独立普通批。"""
        b = self._batcher()
        items = [
            self._entry("1a", "Map001/ev1"),
            self._entry("アイテム説明", None),
            self._entry("1b", "Map001/ev2"),
        ]
        batches = b.build_batches(items)
        pack = next(bt for bt in batches if any(i.scene for i in bt.items))
        plain = next(bt for bt in batches if any(not i.scene for i in bt.items))
        self.assertTrue(all(i.scene for i in pack.items))
        self.assertTrue(all(not i.scene for i in plain.items))


class TestContextChain(unittest.TestCase):
    """上下文链：跨包多轮历史 + 链预算断链。"""

    def _entry(self, key: str, scene=None) -> Entry:
        e = Entry(id=key, key=key, src=key)
        e.scene = scene
        return e

    def test_resolve_chain_within_budget(self):
        """链预算充足时返回历史；超出则断链清空（预算只算输入侧）。"""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from engine import Engine
        import types
        eng = object.__new__(Engine)
        eng.context_chain = True
        eng.chain_budget = 1000
        eng._chain = [{"role": "user", "content": "前包の台詞" * 50},
                      {"role": "assistant", "content": "前包の訳" * 30}]
        eng.storage = types.SimpleNamespace(log=lambda *a, **k: None)
        b = Batcher()
        eng.batcher = b
        small = Batch(number=1, items=[self._entry("小さな台詞" * 20)])  # ~数百 token
        self.assertIsNotNone(eng._resolve_chain(small))
        big = Batch(number=2, items=[self._entry("とても長い台詞" * 2000)])  # ~7000 token，必超预算
        self.assertIsNone(eng._resolve_chain(big))
        self.assertEqual(eng._chain, [])

    def test_chain_append_truncates(self):
        from engine import Engine
        import types
        eng = object.__new__(Engine)
        eng.context_chain = True
        eng.chain_budget = 500
        eng._chain = []
        eng.storage = types.SimpleNamespace(log=lambda *a, **k: None)
        b = Batcher()
        eng.batcher = b
        eng._chain_append({"role": "user", "content": "中" * 3000}, "译" * 3000)
        self.assertEqual(eng._chain, [])  # 超预算自动断链


if __name__ == "__main__":
    unittest.main()
