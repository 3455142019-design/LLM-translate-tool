# -*- coding: utf-8 -*-
"""batcher.py — token 预算驱动 + 语义分组的分批器。

v3 评审结论：条目数不能反映 token 量（1500 个菜单项和 1500 段剧情完全不同），
因此按 token 预算切批，条目数只作软/硬上限：
- target_input_tokens=16K / target_output_tokens=10K / soft_max=250 / hard_max=600
- 语义分组：同地图/事件/文本类型的条目尽量同批（key 前缀启发式，无元数据时
  退化为按 key 字符类别分组）

token 估算用官方 DeepSeek tokenizer（Rust 绑定，快且准）：
    scripts/deepseek_v3_tokenizer.json（V4 未公开 tokenizer，V3 同系近似）

依赖：config/schemas；被 engine 引用。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

from schemas import Batch, Entry
import config

# tokenizer 路径：项目根下的 tokenizer 文件（与 scripts/deepseek_v3_tokenizer.json 同一份）
TOKENIZER_PATH = Path(__file__).resolve().parent.parent / "deepseek_v3_tokenizer.json"
_FALLBACK_TOKENIZER_PATH = Path(__file__).resolve().parent.parent.parent / "deepseek_v3_tokenizer.json"

# 每条条目的 JSON 结构开销（短序号数组对 ["0", ...] 的引号+逗号+换行；
# 旧格式 {"id":..,"src":..} 实测约 15，新协议省去 id 键名降至 ~6）
_STRUCT_TOKENS_PER_ITEM = 6
# 日→中 输出/输入 token 比率（实测：已翻译样本 值token/键字符=0.521，此处按 token 计约 0.7）
_OUT_IN_RATIO_JA_ZH = 0.7
# 润色模式输出 ≈ 现译文量（按原文估算的 0.7 倍）
_POLISH_OUT_RATIO = 0.7


class Batcher:
    """分批器。无状态，输入条目列表，输出 Batch 列表。"""

    def __init__(self, limits: Optional[Dict[str, int]] = None):
        self.limits = limits or dict(config.BATCH_LIMITS)
        self._tok = None
        self._tok_path = None
        self._load_tokenizer()

    # ---- tokenizer 惰性加载 ----
    def _load_tokenizer(self) -> None:
        try:
            from tokenizers import Tokenizer
        except ImportError:
            # tokenizers 库缺失：退化按字符估算（与注释一致）
            self._tok = None
            self._tok_path = None
            return
        path = TOKENIZER_PATH if TOKENIZER_PATH.exists() else _FALLBACK_TOKENIZER_PATH
        if not path.exists():
            # 无 tokenizer 文件时退化：按字符数估算（1 汉字 ≈ 1 token）
            self._tok = None
            self._tok_path = None
            return
        try:
            self._tok = Tokenizer.from_file(str(path))
            self._tok_path = path
        except Exception:
            # tokenizer 文件损坏/格式不支持：退化估算，不阻塞工具
            self._tok = None
            self._tok_path = None

    def count_tokens(self, text: str) -> int:
        """精确 token 计数（tokenizer 不可用时按字符近似）。"""
        if self._tok is None:
            return max(1, len(text) // 2)
        return len(self._tok.encode(text).ids)

    # ---- 语义分组 ----
    @staticmethod
    def grouping_key(entry: Entry) -> str:
        """语义分组键（尽力而为）。

        优先级：
        1. entry.scene（occurrence index 提供的事件归属，如 Map026/ev21）——
           **取地图级前缀**（Map026 / CommonEvents），保证同一张地图内的
           所有事件同批，地图内上下文完整连贯（RPG Maker MZ 文本极短，
           单张地图通常远小于 1M 上下文预算）
        2. 退化启发式（无元数据时）：key 含路径分隔取目录段 / 数字开头 /
           含 \\H 演出标记 / 按首字符类别（平假名/片假名/汉字/ASCII）
        """
        if entry.scene:
            return f"map:{entry.scene.split('/')[0]}"
        k = entry.key
        m = re.match(r"^([A-Za-z_]+[/\\])", k)
        if m:
            return m.group(1).rstrip("/\\")
        if k[:1].isdigit():
            return "numeric"
        if "\\" in k:
            return "talk"
        first = k[:1]
        if re.match(r"[ぁ-ん]", first):
            return "hiragana"
        if re.match(r"[ァ-ヶ]", first):
            return "katakana"
        if re.match(r"[\u4e00-\u9fff]", first):
            return "kanji"
        return "ascii"

    # ---- 大包模式（地图级包 + 编号连贯） ----
    @staticmethod
    def _map_num(entry: Entry) -> int:
        """地图编号（数字序）：Map026/ev21 -> 26；CommonEvents/ev33 -> 99_999；无 scene -> None。"""
        if not entry.scene:
            return None
        stem = entry.scene.split("/")[0]           # Map026 或 CommonEvents
        if stem == "CommonEvents":
            return 99_999
        digits = "".join(ch for ch in stem if ch.isdigit())
        return int(digits) if digits else 99_999

    def _build_pack_batches(self, items: List[Entry], mode: str,
                            limits: Dict[str, int]) -> List[Batch]:
        """大包模式：同一地图的条目进同一包；包按地图编号数字序排列；
        切包只发生在**地图边界**，条件 = 地图数 / 输入预算 / 输出预算（防
        截断，默认 350K）/ 条数硬上限。无 scene 的条目（物品/系统文本）走
        普通小批（不进大包，避免稀释上下文）。
        """
        scened = [e for e in items if e.scene]
        others = [e for e in items if not e.scene]
        pack_max_maps = limits.get("max_maps", 250)
        target_in = limits.get("target_input_tokens", 900_000)
        target_out = limits.get("target_output_tokens", 350_000)
        hard_max = limits.get("hard_max_items", 20_000)

        batches: List[Batch] = []
        current: List[Entry] = []
        cur_map = None
        maps_in = 0
        est_in = 0
        est_out = 0

        def flush() -> None:
            nonlocal current, cur_map, maps_in, est_in, est_out
            if not current:
                return
            batches.append(Batch(number=len(batches) + 1, items=current,
                                 est_input_tokens=est_in, est_output_tokens=est_out))
            current, cur_map, maps_in, est_in, est_out = [], None, 0, 0, 0

        # 地图编号数字序（Map1 < Map2 < ... < Map370 < CommonEvents）
        ordered = sorted(scened, key=lambda e: (self._map_num(e) or 0, e.id))
        for e in ordered:
            src_tok = self.count_tokens(e.masked_src or e.src)
            out_tok = int(src_tok * (_POLISH_OUT_RATIO if mode == "polish" else _OUT_IN_RATIO_JA_ZH))
            in_tok = src_tok + _STRUCT_TOKENS_PER_ITEM
            m = self._map_num(e)
            new_map = (m != cur_map)
            if new_map:
                cur_map = m
                maps_in += 1
                # 包满则在地图边界切包（同地图绝不拆散）
                if current and (maps_in > pack_max_maps
                                or est_in + in_tok > target_in
                                or est_out + out_tok > target_out
                                or len(current) >= hard_max):
                    flush()
            current.append(e)
            est_in += in_tok
            est_out += out_tok
        flush()

        # 无 scene 条目：普通小批（原启发式分组，16K 预算）。
        # 必须用默认 BATCH_LIMITS 而非大包 limits——此前误传大包 limits
        # （如 target_input=350K），无 scene 条目会被并成 350K 级大批，
        # 可能超上下文/截断（2026-08-18 审计修复）。
        if others:
            rest = self._build_batches_plain(others, mode, dict(config.BATCH_LIMITS))
            for b in rest:
                b.number = len(batches) + 1
                batches.append(b)
        return batches

    def _build_batches_plain(self, items: List[Entry], mode: str,
                             limits: Dict[str, int]) -> List[Batch]:
        """原逻辑分批（无 pack 语义），供大包模式的无 scene 条目复用。"""
        items_sorted = sorted(items, key=lambda e: (self.grouping_key(e), e.id))
        group_total_in: Dict[str, int] = {}
        for e in items_sorted:
            g = self.grouping_key(e)
            group_total_in[g] = group_total_in.get(g, 0) + self.count_tokens(e.masked_src or e.src)
        _SCENE_BUDGET_MARGIN = 1.3

        batches: List[Batch] = []
        current: List[Entry] = []
        cur_group = None
        est_in = 0
        est_out = 0

        def flush() -> None:
            nonlocal current, cur_group, est_in, est_out
            if not current:
                return
            batches.append(Batch(number=len(batches) + 1, items=current,
                                 est_input_tokens=est_in, est_output_tokens=est_out))
            current, cur_group, est_in, est_out = [], None, 0, 0

        for e in items_sorted:
            g = self.grouping_key(e)
            src_tok = self.count_tokens(e.masked_src or e.src)
            if mode == "polish":
                out_tok = int(src_tok * _POLISH_OUT_RATIO)
            else:
                out_tok = int(src_tok * _OUT_IN_RATIO_JA_ZH)
            in_tok = src_tok + _STRUCT_TOKENS_PER_ITEM
            new_group = (g != cur_group)
            over_soft = (len(current) + 1) > limits["soft_max_items"]
            over_budget_in = (est_in + in_tok) > limits["target_input_tokens"]
            over_budget_out = (est_out + out_tok) > limits["target_output_tokens"]
            over_hard = (len(current) + 1) > limits["hard_max_items"]
            keep_scene = (not new_group and cur_group in group_total_in
                          and group_total_in[cur_group] <= limits["target_input_tokens"] * _SCENE_BUDGET_MARGIN)
            budget_cut = (over_budget_in or over_budget_out) and not keep_scene
            if current and (new_group or over_soft or budget_cut or over_hard):
                flush()
            current.append(e)
            cur_group = g
            est_in += in_tok
            est_out += out_tok
        flush()
        return batches

    # ---- 分批 ----
    def build_batches(self, items: List[Entry], mode: str = "translate") -> List[Batch]:
        """按 token 预算 + 语义分组切批。mode 影响输出 token 估算（translate/polish）。

        thinking 模式（review_*）批次大幅收紧：思考生成极慢且输出大，
        用 REVIEW_BATCH_LIMITS 覆盖默认限制（实测 100 条 high 思考可达 15 分钟+）。
        """
        if not items:
            return []
        limits = self.limits
        if mode in ("review_hard", "review_ambiguous", "review"):
            limits = dict(config.REVIEW_BATCH_LIMITS)
        pack_max_maps = limits.get("max_maps", 0)   # >0 开启大包模式（地图级包）
        if pack_max_maps:
            return self._build_pack_batches(items, mode, limits)
        # 1. 语义分组排序（同组相邻）
        items_sorted = sorted(items, key=lambda e: (self.grouping_key(e), e.id))

        # 场景不切批：预计算每组的输入 token 总量；同场景且总量在预算×1.3
        # 以内时，允许批次超预算直到场景结束（真实对话场景通常远小于预算，
        # 切批会割裂上下文；超限的大场景仍按预算切，防单批过大）。
        group_total_in: Dict[str, int] = {}
        for e in items_sorted:
            g = self.grouping_key(e)
            group_total_in[g] = group_total_in.get(g, 0) + self.count_tokens(e.masked_src or e.src)
        _SCENE_BUDGET_MARGIN = 1.3

        batches: List[Batch] = []
        current: List[Entry] = []
        cur_group = None
        est_in = 0
        est_out = 0

        def flush() -> None:
            nonlocal current, cur_group, est_in, est_out
            if not current:
                return
            batches.append(Batch(number=len(batches) + 1, items=current,
                                 est_input_tokens=est_in, est_output_tokens=est_out))
            current, cur_group, est_in, est_out = [], None, 0, 0

        for e in items_sorted:
            g = self.grouping_key(e)
            # 本条输入/输出估算
            src_tok = self.count_tokens(e.masked_src or e.src)
            if mode == "polish":
                out_tok = int(src_tok * _POLISH_OUT_RATIO)
            else:
                out_tok = int(src_tok * _OUT_IN_RATIO_JA_ZH)
            in_tok = src_tok + _STRUCT_TOKENS_PER_ITEM

            # 语义组切换时强制切批（不在连续对话中间断批）
            new_group = (g != cur_group)
            over_soft = (len(current) + 1) > limits["soft_max_items"]
            over_budget_in = (est_in + in_tok) > limits["target_input_tokens"]
            over_budget_out = (est_out + out_tok) > limits["target_output_tokens"]
            over_hard = (len(current) + 1) > limits["hard_max_items"]
            # 同场景且场景总量在预算内 → 豁免预算切批（保证场景完整）
            keep_scene = (not new_group and cur_group in group_total_in
                          and group_total_in[cur_group] <= limits["target_input_tokens"] * _SCENE_BUDGET_MARGIN)
            budget_cut = (over_budget_in or over_budget_out) and not keep_scene

            if current and (new_group or over_soft or budget_cut or over_hard):
                flush()
            current.append(e)
            cur_group = g
            est_in += in_tok
            est_out += out_tok
        flush()
        return batches
