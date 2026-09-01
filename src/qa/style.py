# -*- coding: utf-8 -*-
"""qa/style.py — 风格/一致性检查器（批次级）。

单条级检查难以发现系统性问题，本模块在批次维度做统计：
- 原文不同但译文大量完全相同（多条不同原文 -> 相同泛化译文）
- 连续多句完全相同句式（重复开头词/结尾词）
- 角色自称/称呼漂移（批次内同一原文的不同译文）
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List

from schemas import Entry

_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
# \u7247\u5047\u540d\u4e13\u540d\uff08\u8fde\u7eed\u7247\u5047\u540d\u4e32 >=3 \u5b57\u7b26\uff0c\u901a\u5e38\u662f\u4e13\u6709\u540d\u8bcd\u5019\u9009\uff09
_KATAKANA_RUN_RE = re.compile(r"[\u30a1-\u30f6ー]{3,}")  # 含长音ー（U+30FC），否则ディサーン被切成ディサ
# 常见片假名普通词/后缀（不是专名，上下文不同译法不同是正常现象）——
# 2026-08-16 HOLLOWWALD 审计：テスト/マップ/イベント/アイコン 等被误报"译名漂移"
_KATAKANA_COMMON = {
    "テスト", "マップ", "イベント", "アイコン", "アニメ", "ション",
    "オプション", "ショップ", "ランク", "ダウン", "レベル", "システム",
    "スキル", "アイテム", "バトル", "ステータス", "メニュー", "ウィンドウ",
    "ゲージ", "カウンター", "エフェクト", "モンスター", "エネミー",
    "パーティ", "セーブ", "ロード", "タイトル", "クエスト", "ミッション",
    "リザルト", "シチュエーション", "コマンド", "ポイント", "ゴールド",
    "マジック", "アタック", "ガード", "リカバー", "ポーション",
    # 2026-08-16 HOLLOWWALD 审计补充（普通外来语/UI 词，按上下文译法不同属正常）
    "トアイコン", "オブジェクト", "アップ", "テキスト", "シスタ", "ベッド",
    "メンバ", "スキップ", "エンディング", "キャンセル", "バランス",
    "ボタン", "デバフ", "クラス", "エリア", "レシピ", "カテゴリ",
    "スクロ", "コモン", "ホント", "プライベ", "ツイスト", "ファウンド",
    "ブレス", "パイズリ", "フェラチオ",
    "ホロウワルド",  # 游戏名：标题中文名「虚之世界」/版本行英文名并存，属有意设计
}
# 纯叹词/拟声短句（无汉字、短），多个不同原文共用同一译文属正常——
# 2026-08-16 HOLLOWWALD 审计：おおっと/ふふ/うっ/ん？ 等大量误报"泛化译文"
_INTERJECTION_RE = re.compile(r"^[ぁ-んァ-ヶー…っ！？!?。、♪♥〜~…・\s]{1,6}$")


class StyleChecker:
    """批次级风格检查。check_batch(items) -> {entry_id: [issues]}"""

    NAME = "style"

    # 相同译文阈值：同一译文出现在 >= 5 条不同原文即报（游戏文本模板句正常，
    # 阈值调高避免误报；冒烟测试阶段再校准）
    SAME_ZH_THRESHOLD = 5

    def __init__(self, glossary_sources=None):
        """glossary_sources：术语表 source 集合。
        提供时专名漂移检测只查术语表内的词（高价值低噪音）；
        不提供时对全部连续片假名词做启发式检查（用于独立调用/测试）。"""
        self.glossary_sources = set(glossary_sources) if glossary_sources else None

    @staticmethod
    def _name_norm(zh: str) -> str:
        """译名归一：去掉 *N 变体与 , 列表后缀，取首片段。"""
        s = re.sub(r"\*\d+", "", zh)
        s = s.split(",")[0].strip()
        return s

    @staticmethod
    def _lcs_len(a: str, b: str) -> int:
        if not a or not b:
            return 0
        previous = [0] * (len(b) + 1)
        best = 0
        for i in range(1, len(a) + 1):
            current = [0] * (len(b) + 1)
            for j in range(1, len(b) + 1):
                if a[i - 1] == b[j - 1]:
                    current[j] = previous[j - 1] + 1
                    best = max(best, current[j])
            previous = current
        return best

    def check_batch(self, items: List[Entry]) -> Dict[str, List[str]]:
        issues_map: Dict[str, List[str]] = {it.id: [] for it in items}

        # 1. 原文不同但译文完全相同
        zh_counter: Counter = Counter()
        zh_to_ids: Dict[str, List[str]] = {}
        for it in items:
            # 未遮罩条目（detect 模式）直接取 cur；遮罩后取 masked_cur
            src = it.masked_src or it.src
            zh = (it.masked_cur if it.masked_cur is not None else (it.cur or "")) or ""
            if not zh.strip():
                continue
            # 纯叹词/拟声短句（おおっと/うっ/ん？…）共用译文属正常，豁免
            if _INTERJECTION_RE.fullmatch(src or ""):
                continue
            zh_counter[zh] += 1
            zh_to_ids.setdefault(zh, []).append(it.id)
        for zh, n in zh_counter.items():
            if n >= self.SAME_ZH_THRESHOLD:
                for eid in zh_to_ids[zh][:3]:  # 只报前 3 条避免刷屏
                    issues_map[eid].append(
                        f"译文与 {n-1} 条不同原文完全相同（疑似泛化译文）: {zh[:20]}...")

        # 2. 批次内同一原文被翻成多个译文（称谓漂移/不一致）
        src_zh_map: Dict[str, set] = {}
        for it in items:
            src = it.masked_src or it.src
            zh = (it.masked_cur if it.masked_cur is not None else (it.cur or "")) or ""
            if not zh.strip():
                continue
            src_zh_map.setdefault(src, set()).add(zh)
        for src, zh_set in src_zh_map.items():
            if len(zh_set) > 1:
                for eid in [it.id for it in items if (it.masked_src or it.src) == src]:
                    issues_map[eid].append(
                        f"同一原文出现 {len(zh_set)} 种不同译文: {sorted(zh_set)[:2]}")

        # 3. 片假名专名译名一致性：同一专名（连续片假名串，含长音）在各条译文间
        #    归一化译名不同、且公共子串过短 -> 疑似多种译法
        katakana_groups: Dict[str, List[str]] = {}
        for it in items:
            src = it.masked_src or it.src
            zh = (it.masked_cur if it.masked_cur is not None else (it.cur or "")) or ""
            if not zh.strip():
                continue
            for kata in _KATAKANA_RUN_RE.findall(src):
                if self.glossary_sources is not None:
                    if kata not in self.glossary_sources:
                        continue  # 只查术语表专名，普通外来词不报
                elif kata in _KATAKANA_COMMON:
                    continue  # 普通词/后缀：不同上下文译法不同属正常，不报漂移
                katakana_groups.setdefault(kata, []).append(zh)
        for kata, zh_list in katakana_groups.items():
            if len(zh_list) < 2:
                continue
            sample = zh_list[:5]  # 采样避免 O(n²) 爆炸
            norms = {self._name_norm(z) for z in sample}
            best_common = max((self._lcs_len(a, b)
                               for i, a in enumerate(sample)
                               for b in sample[i + 1:]), default=0)
            # 归一化名包含关系（事件/入场时的事件）视为一致
            nested = any(a != b and (a in b or b in a)
                         for a in norms for b in norms)
            drifted = len(norms) > 1 and best_common < 3 and not nested
            if drifted:
                for eid in [it.id for it in items
                            if (it.masked_src or it.src) and kata in (it.masked_src or it.src)][:3]:
                    issues_map[eid].append(
                        f"专名『{kata}』疑似多种译法: {sorted(norms)[:2]}")

        return issues_map
