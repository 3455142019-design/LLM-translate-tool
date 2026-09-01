# -*- coding: utf-8 -*-
"""qa/full_qa.py — 合并前的全量产物验证器（质量闸门）。

背景（2026-08-10/11 HOLLOWWALD 正式版教训）：合并后发现错位 8,792 条、
漏译 1,241 条、`\\\\n` 双反斜杠 201 条、繁体残留、术语变体漂移
（桑吉斯/桑德司、伊玛里露/伊玛利尔…）、引擎枚举值误译，最终只能整段重翻。
结论：合并前必须有一道全量自动闸门，问题率超阈值直接阻止合并。

检查项（每条 key → value）：
1. untranslated    漏译（值==键且为真文本）
2. kana_residue    译文残留日文假名
3. kanji_residue   日文汉字残留（无假名但与键重叠率高，值!=键）
4. double_slash_n  值含字面 `\\\\n`（双反斜杠，转义损坏）
5. traditional     繁体残留
6. term_variant    术语变体（glossary target 的音近/形近变体，或 source
                   命中键但译文未用 target）
7. dup_adjacent    相邻条目值完全相同（同值式错位特征）
8. multiline_concat 值内多行而键单行（滚动合并式拼接特征）
9. engine_key      引擎/枚举键被误译（键为纯 ASCII/数字/符号，值被改成中文）
10. forbidden_word 黑名单词出现且原文无对应

输出：issue JSON（每类单独数组）+ 统计；--max-issue-rate 超限时退出码 1
（阻止合并），供主控 agent 在合并前调用。

依赖：glossary（可选）；被 cli 引用。
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from glossary import Glossary

_HIRAGANA_RE = re.compile(r"[ぁ-ん]")
_KATAKANA_RE = re.compile(r"[ァ-ヶ]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
# 双反斜杠检测：JSON 里 "\\n"（两个反斜杠+n 的转义）读入后是「反斜杠+n」
# 两字符序列（正常换行是 \n -> 单个换行符）。游戏内会显示字面 \n。
# 排除合法控制码前缀：\name[...]（名字命令）、\n[actor]（VX 名字码）、
# \next（自定义翻页码）——它们以 \n 开头但不是换行转义（2026-08-18 修复）。
_DOUBLE_SLASH_N_RE = re.compile(r"\\n(?!ame|\[|ext)")
_ASCII_KEY_RE = re.compile(r"^[\x20-\x7e\s]*$")
# 控制码/引擎指令形 token（占位符遮罩产物、HM 指令等）：值为此类时豁免全部文本类检查
_CONTROL_ONLY_RE = re.compile(r"^[\s\d_.:/\\<>\[\]{}()\-+='\",;!?*&^%$#@~|`]*$")

# 常见繁体字集合（覆盖实测残留案例「那種/何處/過踰」等；简体正文里出现即报）
_TRADITIONAL_CHARS = set(
    "與為說這對嗎們會個來麼讓後髮隻愛開關門間問題讀誰國過還當從經"
    "實際現體點動樣種變長裡滿業樂熱無見覺觀記請謝話語認識該買賣貴"
    "質轉車輕農辦進遠運連週遊鄉銀鋼鐵閉陰陽隨險難雜離電靈頭風飛飲"
    "飽馬魚鳥龍龜樹葉夢圖畫寫聽聲東車馬門鬥頁風飛飲館餡餅齊齒齡靈"
    "驗顯須頭額顏顧願類鬚鬥鬱龜斷專導層屆屬幹廢廣廳憶戀惡應懷戀懸"
    "戀戀戀擁擊據擔擇據擋擊擠檢權歡歷殘殺殼氣測濟灣災烏煉燈營狀獲"
    "環產異發監眾穀窮築簡糧紐約紅級紀紙納純紛絡統絲縫縣總縱繼續罰"
    "聽職聖聯聲肅脅腦腳腸膽臉與舉舊臺艦艙艱藝藥蘭處號蟲術衛補裝複"
    "覽觀覺觸訂訓討託記許診詞訴試詩話詳誤說請課調談論諾證譯護豐貝"
    "負責貫貨貪貫賓賞賴賺購賠賣質賽贊趕跡軍軌車轉輪輕輸辭農逃逆透"
    "途這連進週遊運過達違遙遞適遭遲選遷遺避郵鄰鄉鄭醫釋錯錢鍾鎖鎮"
    "鏈鏡鐘鐵鑼陸隊隨險隱離雜雞雙雲靈靜韓頁順須預頓領頭頻題額顏願"
    "類顧風飛飯飲飽養餘馬駐駕驗驚體髮鬥魚鳥鹼麥黃點黨齊齡龍"
)


def _char_overlap_ratio(a: str, b: str) -> float:
    set_a = set(a)
    if not set_a:
        return 0.0
    return len(set_a & set(b)) / len(set_a)


def _is_real_text(key: str) -> bool:
    """键是真文本（含假名或汉字，非引擎/数字键）。"""
    return bool(_HIRAGANA_RE.search(key) or _KATAKANA_RE.search(key)
                or _HAN_RE.search(key))


def _norm_equal_text(s: str) -> str:
    """归一化文本：去空白/标点差异 + 全角转半角。

    用于漏译/截断判定：键「レベル調整（50）」值「レベル調整(50)」（仅
    全半角括号差异）应判为漏译，而不是落入 kanji_residue 误报。
    """
    out = []
    for c in s:
        o = ord(c)
        # 全角 ASCII（0xFF01-0xFF5E）转半角
        if 0xFF01 <= o <= 0xFF5E:
            c = chr(o - 0xFEE0)
        elif c == "\u3000":  # 全角空格
            c = " "
        if c.isspace():
            continue
        if c in "。．、，．！？：；「」『』（）【】・…—～":
            continue
        out.append(c)
    return "".join(out)


def _norm_equal(a: str, b: str) -> bool:
    """归一化相等：去空白/标点差异 + 全角转半角后相同（漏译判定用）。"""
    return bool(a and b) and _norm_equal_text(a) == _norm_equal_text(b)


def _extract_candidates(value: str, target: str) -> List[str]:
    """从值中提取与 target 近似的候选词（音近/形近变体检测）。

    滑窗扫描：值内所有长度 len(target)±1 的连续候选字符窗口与 target
    比较。音译名变体（桑吉斯→桑德司、伊玛里露→伊玛利尔）中间字漂移，
    用「首字相同 + 长度差 <= 1」兜底（3 字及以上才用；2 字词只用形近
    路径，防「面包/面条」类误报）。
    """
    found: List[str] = []
    t = target.replace(" ", "")
    if not t or len(t) < 2:
        return found
    seen: set = set()
    for i in range(len(value)):
        for w in (len(t) - 1, len(t), len(t) + 1):
            if w < 2:
                continue
            seg = value[i:i + w]
            if len(seg) < w or not _CAND_CHAR_RE.fullmatch(seg):
                continue
            if seg == t:
                continue
            # 截断/包含关系不是变体（「桑吉」是「桑吉斯」的前缀，正常截断）
            if seg in t or t in seg:
                continue
            # 形近：编辑距离/相似度高，且首尾至少一侧与 target 对齐（防
            # 「去桑吉」这类跨界窗口误报）
            if difflib.SequenceMatcher(None, seg, t).ratio() >= 0.6 \
                    and (seg[0] == t[0] or seg[-1] == t[-1]):
                if seg not in seen:
                    found.append(seg)
                    seen.add(seg)
                continue
            # 音近兜底（音译名中间字漂移）：3 字及以上、首字相同、长度差 <= 1
            if len(t) >= 3 and abs(len(seg) - len(t)) <= 1 and seg[0] == t[0]:
                if seg not in seen:
                    found.append(seg)
                    seen.add(seg)
    return found


_CAND_CHAR_RE = re.compile(r"[\u4e00-\u9fffァ-ヶー]+")
_KANA_CHAR_RE = re.compile(r"[ぁ-んァ-ヶー]+")


class FullQaReport:
    """全量验证报告。"""

    def __init__(self) -> None:
        self.issues: Dict[str, List[Dict[str, str]]] = {}
        self.total_checked = 0
        self.total_issues = 0

    def add(self, issue_type: str, key: str, detail: str,
            value: str = "") -> None:
        self.issues.setdefault(issue_type, []).append(
            {"key": key, "value": value, "detail": detail})
        self.total_issues += 1

    @property
    def issue_rate(self) -> float:
        return self.total_issues / self.total_checked if self.total_checked else 0.0


def run_full_qa(data: Dict[str, str], glossary: Optional[Glossary] = None,
                forbidden_words: Optional[List[str]] = None,
                check_traditional: bool = False,
                ) -> FullQaReport:
    """对全量产物运行全部检查。data = MTool 键值对（key=日文原文, value=译文）。

    glossary 提供术语表（术语变体/术语缺失检测）；forbidden_words 为项目
    黑名单词。check_traditional：繁体检测默认关闭（简繁差异不影响多数
    中文用户理解，硬编码字表方案不理想，2026-08 暂停迭代；未来有可靠
    方案再启用）。返回 FullQaReport。
    """
    report = FullQaReport()
    report.total_checked = len(data)

    items = list(data.items())
    prev_key, prev_val = None, None
    for key, value in items:
        zh = value or ""
        is_control = bool(_CONTROL_ONLY_RE.match(zh))
        real_text = _is_real_text(key)

        # 1. 漏译：值==原文（含全半角/标点差异）且为真文本
        if real_text and (zh == key or _norm_equal(zh, key)):
            report.add("untranslated", key, "漏译: 值==原文", zh)
            prev_key, prev_val = key, zh
            continue

        # 2. 引擎/枚举键误译：键为纯 ASCII/符号，值被改成中文
        if _ASCII_KEY_RE.match(key) and key.strip() and _HAN_RE.search(zh):
            report.add("engine_key", key, "疑似引擎/枚举键被误译（原文为纯 ASCII）", zh)

        # 控制码值豁免文本类检查（占位符遮罩产物/引擎指令不是译文）
        if is_control:
            prev_key, prev_val = key, zh
            continue

        # 3. 假名残留
        hira = _HIRAGANA_RE.search(zh)
        kata = _KATAKANA_RE.search(zh)
        if hira or kata:
            report.add("kana_residue", key,
                       f"译文残留日文假名: {(hira or kata).group(0)}", zh)
        # 4. 日文汉字残留/截断（无假名、值!=键）。字符重叠/LCS 检测会把正常
        #    翻译（レベル調整→等级调整、灯り→灯火）误报为残留，只保留最强
        #    信号：值是键的归一化子串（截断残留，如「準備完了、出発！」只译
        #    出「準備完了」）。其余汉字残留（值含假名）由 kana_residue 覆盖。
        if not (hira or kata):
            norm_zh, norm_key = _norm_equal_text(zh), _norm_equal_text(key)
            if real_text and _HAN_RE.search(zh) and _HAN_RE.search(key) \
                    and norm_zh and norm_zh in norm_key:
                report.add("kanji_residue", key,
                           f"疑似截断残留: 译文是原文的连续片段", zh)

        # 5. 双反斜杠（转义损坏：值里出现字面 \\n 而非换行）
        if _DOUBLE_SLASH_N_RE.search(zh):
            report.add("double_slash_n", key, "值含字面 \\\\n（双反斜杠，转义损坏）", zh)

        # 6. 繁体残留（默认关闭：简繁差异不影响多数中文用户理解，
        #    硬编码字表方案不理想，暂停迭代；--with-traditional 可开）
        if check_traditional:
            trad = sorted({c for c in zh if c in _TRADITIONAL_CHARS})
            if trad:
                report.add("traditional", key,
                           f"译文含繁体字: {''.join(trad[:8])}", zh)

        # 7. 术语变体 / 术语缺失
        if glossary is not None:
            for e in glossary._entries:
                # 短假名术语（<=2 字，如「サン」）子串命中长词（サンクタム）
                # 误报率高，自动检查跳过（由术语变体人工审查覆盖）
                if len(e.source) <= 2 and _KANA_CHAR_RE.fullmatch(e.source):
                    continue
                if e.source not in key:
                    continue
                if e.target not in zh:
                    report.add("term_variant", key,
                               f"术语 [{e.source}→{e.target}] 在原文出现但译文未使用",
                               zh)
                    # 译文未使用正译时，找音近/形近变体（可能用了错误译法）。
                    # 同一术语只报一条：优先长度最接近 target 的候选
                    # （完整音译词信息量最大），再按相似度兜底
                    cands = _extract_candidates(zh, e.target)
                    if cands:
                        best = min(cands, key=lambda c: (
                            abs(len(c) - len(e.target)),
                            -difflib.SequenceMatcher(None, c, e.target).ratio()))
                        report.add("term_variant", key,
                                   f"术语变体: 使用「{best}」应为「{e.target}」", zh)

        # 8. 相邻同值（同值式错位特征：连续条目译文完全相同且非空）
        if prev_val is not None and zh == prev_val and key != prev_key \
                and len(zh) >= 2:
            report.add("dup_adjacent", key,
                       f"与前一条目译文完全相同（疑似同值式错位）: {prev_key[:30]}",
                       zh)

        # 9. 换行拼接（滚动合并式：值多行而键单行）
        # 键内字面 \n（反斜杠+n，RPG Maker 换行控制码）同样算换行——
        # 此前只查真实换行，把"键含字面 \n、值含真实换行"的正常镜像
        # 误报为拼接（2026-08-18 修复）。
        key_has_newline = "\n" in key or "\\n" in key
        if zh.count("\n") >= 1 and not key_has_newline:
            report.add("multiline_concat", key,
                       f"值 {zh.count(chr(10)) + 1} 行但原文单行（疑似滚动合并式拼接）",
                       zh)

        # 10. 黑名单词（译文出现且原文无对应日文）
        for word in (forbidden_words or []):
            if word in zh and word not in key:
                report.add("forbidden_word", key,
                           f"疑似自加词(黑名单): {word}", zh)

        prev_key, prev_val = key, zh

    return report


def write_report(report: FullQaReport, out_path: Path) -> Path:
    """写 issue JSON（按类型分组 + 汇总统计）。"""
    payload = {
        "summary": {
            "total_checked": report.total_checked,
            "total_issues": report.total_issues,
            "issue_rate": round(report.issue_rate, 4),
            "by_type": {k: len(v) for k, v in sorted(report.issues.items())},
        },
        "issues": report.issues,
    }
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json_dumps(payload), encoding="utf-8")
    tmp.replace(out_path)
    return out_path


def json_dumps(obj: object) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=1)


def print_summary(report: FullQaReport, max_issue_rate: float) -> int:
    """打印摘要，问题率超阈值返回退出码 1（阻止合并）。"""
    print(f"全量验证: 检查 {report.total_checked} 条 | 问题 {report.total_issues} 条 "
          f"| 问题率 {report.issue_rate:.2%}（阈值 {max_issue_rate:.1%}）")
    for issue_type, lst in sorted(report.issues.items(),
                                  key=lambda kv: -len(kv[1])):
        print(f"  {issue_type}: {len(lst)} 条")
        for it in lst[:3]:
            print(f"    - {it['key'][:36]} → {it['detail'][:60]}")
        if len(lst) > 3:
            print(f"    … 共 {len(lst)} 条")
    if report.issue_rate > max_issue_rate:
        print(f"❌ 问题率 {report.issue_rate:.2%} 超过阈值 {max_issue_rate:.1%}，禁止合并")
        return 1
    print("✅ 问题率在阈值内，可以合并")
    return 0
