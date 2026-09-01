# -*- coding: utf-8 -*-
"""seq_align.py — 游戏事件序列与 MTool 翻译表对齐审查器（HOLLOWWALD 接手审计）。

背景（2026-08-16 接手审计发现）：
- 翻译表（ManualTransFile.json）以"显示文本"为键；游戏 data/*.json 的事件
  code 401 文本是显示的真正来源。
- MTool 运行时收集的键可能缺漏（带 \\C[n] 颜色码的句子、脚本内嵌文本等），
  导致游戏内直接显示日文原文；同时键值合并过程可能产生错位（相邻键的译文
  整体平移一行）。
- 本工具把事件页的连续文本序列与翻译表逐条对齐，产出 {file, ev, page, lines}
  结构，cn 为 '<缺>' 表示对齐失败（疑似游戏内漏译）。

对齐策略（从严格到宽松，逐级尝试）：
1. 原样键
2. 去 \\C[n] 颜色控制码后的键
3. 去全部 MZ 控制码后的键（\\C \\V \\N \\P \\G \\. \\| \\! \\> \\< \\^ \\$）
4. 去控制码 + 去首尾空白
5. 仍失败 -> cn = '<缺>'，并在 --report 中列出（供补译）

用法：
    python tools/seq_align.py --data-dir "<游戏目录>/data" \
        --trans "<翻译表 ManualTransFile.json>" \
        --out runs/seq_full --report runs/seq_full_report.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 引入 src 包（rmmz 需要 schemas/context）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rmmz import _iter_event_pages  # noqa: E402

# RPG Maker MZ 文本控制码（显示时被引擎替换/隐藏，MTool 键可能保留也可能剥掉）
_CCTRL_RE = re.compile(r"\\[CVNPG]\[\d+\]")
_ALLCTRL_RE = re.compile(r"\\(?:C|V|N|P|G)\[\d+\]|\\[\.\|\!\>\<\^\$]")
_WS_RE = re.compile(r"\s+")
# 彩色段：MTool 按 \\C[n]...\\C[0] 切分，内层文本独立成键，剩余文本另行拼接
_CSEG_RE = re.compile(r"(\\C\[\d+\][\s\S]*?\\C\[0\])")
# 名字/变量/图标/等待码：MTool 会切分或剥除，逐段分别查表
_NVAR_RE = re.compile(r"\\[NVP]\[\d+\]")
_BANG_RE = re.compile(r"\\[\.\|\!\>\<\^\$]")
_ICON_RE = re.compile(r"\\I\[\d+\]")


def norm_variants(jp: str) -> list[str]:
    """生成同一文本的候选键（严格 -> 宽松），用于翻译表查询。"""
    out: list[str] = []
    seen: set[str] = set()
    for v in (jp,
              _CCTRL_RE.sub("", jp),
              _ALLCTRL_RE.sub("", jp),
              _WS_RE.sub(" ", _ALLCTRL_RE.sub("", jp)).strip()):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _strip_codes(jp: str) -> str:
    """剥除 MTool 不保留的控制码（名字/变量/等待/图标/颜色/自定义），返回干净文本。"""
    s = _NVAR_RE.sub("", jp)
    s = _BANG_RE.sub("", s)
    s = _ICON_RE.sub("", s)
    s = re.sub(r"\\ac", "", s)
    s = re.sub(r"\\\}", "", s)
    return s.strip()


def is_covered(jp: str, trans: dict) -> bool:
    """判定单条 401 文本在翻译表中是否已有覆盖。

    规则（2026-08-16 从 MTool 导出表实测归纳）：
    1. 原样键命中
    2. 剥除控制码后的整体键命中
    3. 按 \\C[n]...\\C[0] 彩色段切分：内层文本独立成键，
       非彩色文本剥码后拼接（"" 与 "\\n" 两种连接）成键，
       或剩余各段分别命中
    4. 按 \\N/\\V/\\P/\\!/\\I 切分后逐段命中（MTool 对变量/等待码按段匹配）
    """
    if jp in trans:
        return True
    clean = _strip_codes(jp)
    if clean in trans:
        return True

    # 3. 彩色段切分
    parts = re.split(_CSEG_RE, jp)
    remain: list[str] = []
    all_hit = True
    for part in parts:
        if not part:
            continue
        if _CSEG_RE.fullmatch(part):
            inner = _strip_codes(part[part.find("]") + 1:part.rfind("\\C[0]")])
            if inner and inner not in trans:
                if not ("\n" in inner and all(s in trans for s in inner.split("\n") if s)):
                    all_hit = False
        else:
            s = _strip_codes(part)
            if s:
                remain.append(s)
    if all_hit and not remain:
        return True
    if all_hit and ("".join(remain) in trans or "\n".join(remain) in trans):
        return True
    if all_hit and remain and all(r in trans for r in remain):
        return True

    # 4. 变量/等待/图标码切分逐段
    segs = [s for s in re.split(r"\\[NVP]\[\d+\]|\\[\.\|\!\>\<\^\$]|\\I\[\d+\]", jp)
            if s.strip()]
    segs = [s.strip() for s in segs]
    if segs and all(s in trans for s in segs):
        return True
    return False


def _lookup(jp: str, trans: dict) -> tuple[str, bool]:
    """返回 (译文, 是否匹配)。匹配规则见 is_covered；
    逐段命中时把各段译文按原顺序拼接返回。"""
    if jp in trans:
        return trans[jp], True
    clean = _strip_codes(jp)
    if clean in trans:
        return trans[clean], True

    # 彩色段切分：逐段翻译后拼接
    parts = re.split(_CSEG_RE, jp)
    remain: list[str] = []
    segs_cn: list[str] = []
    ok = True
    for part in parts:
        if not part:
            continue
        if _CSEG_RE.fullmatch(part):
            inner = _strip_codes(part[part.find("]") + 1:part.rfind("\\C[0]")])
            if inner:
                if inner in trans:
                    segs_cn.append(trans[inner])
                elif "\n" in inner and all(s in trans for s in inner.split("\n") if s):
                    # 跨行彩色段：拼接键缺失但逐行键都在（MTool 逐段匹配）
                    segs_cn.append("\n".join(trans[s] for s in inner.split("\n") if s))
                else:
                    ok = False
        else:
            s = _strip_codes(part)
            if s:
                remain.append(s)
    if ok and not remain:
        return "".join(segs_cn), True
    if ok and ("".join(remain) in trans):
        return segs_cn[0] if not segs_cn else "".join(segs_cn) + trans["".join(remain)], True
    if ok and "\n".join(remain) in trans:
        return trans["\n".join(remain)], True
    if ok and remain and all(r in trans for r in remain):
        return "".join(segs_cn) + "".join(trans[r] for r in remain), True

    # 变量/等待/图标码切分逐段
    segs = [s.strip() for s in re.split(r"\\[NVP]\[\d+\]|\\[\.\|\!\>\<\^\$]|\\I\[\d+\]", jp)
            if s.strip()]
    if segs and all(s in trans for s in segs):
        return "".join(trans[s] for s in segs), True
    return "<缺>", False


def _iter_pages_with_index(data_dir: Path):
    """遍历事件页，补充页码（rmmz 迭代器未带页码，这里直接从 data 读取）。"""
    map_names = {}
    infos = data_dir / "MapInfos.json"
    if infos.exists():
        try:
            raw = json.loads(infos.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for mid, info in enumerate(raw):
                    if isinstance(info, dict) and info.get("name"):
                        map_names[mid] = info["name"]
        except (OSError, json.JSONDecodeError):
            pass
    for fp in sorted(data_dir.glob("Map*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for ev in data.get("events") or []:
            if not isinstance(ev, dict) or not ev.get("id"):
                continue
            for pidx, page in enumerate(ev.get("pages") or []):
                if isinstance(page, dict) and isinstance(page.get("list"), list):
                    yield fp.name, ev["id"], str(ev.get("name") or ""), pidx, page["list"]
    ce = data_dir / "CommonEvents.json"
    if ce.exists():
        try:
            data = json.loads(ce.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, list):
            for ev in data:
                if not isinstance(ev, dict) or not ev.get("id"):
                    continue
                for pidx, page in enumerate(ev.get("pages") or []):
                    if isinstance(page, dict) and isinstance(page.get("list"), list):
                        yield "CommonEvents.json", ev["id"], str(ev.get("name") or ""), \
                            pidx, page["list"]


def run(data_dir: str, trans_path: str, out: str, report: str | None = None) -> int:
    """程序化入口（供 CLI/GUI 调用）。"""
    trans = json.loads(Path(trans_path).read_text(encoding="utf-8"))
    blocks: list[dict] = []
    missing: list[dict] = []
    n_lines = n_miss = 0
    for fname, evid, evname, pidx, lst in _iter_pages_with_index(Path(data_dir)):
        lines = []
        groups: list[list[dict]] = []  # 连续 401 组（跨行彩色段按组拼接键）
        cur: list[dict] = []
        for cmd in lst:
            if not isinstance(cmd, dict) or cmd.get("code") != 401:
                if cur:
                    groups.append(cur)
                    cur = []
                continue
            params = cmd.get("parameters")
            if not isinstance(params, list) or not params:
                if cur:
                    groups.append(cur)
                    cur = []
                continue
            jp = params[0]
            if not isinstance(jp, str) or not jp:
                if cur:
                    groups.append(cur)
                    cur = []
                continue
            n_lines += 1
            cn, matched = _lookup(jp, trans)
            ln = {"jp": jp, "cn": cn, "matched": matched}
            lines.append(ln)
            cur.append(ln)
        if cur:
            groups.append(cur)
        # 块级兜底：同一连续 401 组的拼接块（彩色段跨行时 MTool 以拼接键存储）
        for grp in groups:
            if not any(not ln["matched"] for ln in grp):
                continue
            block = "\n".join(ln["jp"] for ln in grp)
            block_cn, block_ok = _lookup(block, trans)
            if block_ok and block_cn != "<缺>":
                for ln in grp:
                    if not ln["matched"]:
                        ln["cn"] = block_cn
                        ln["matched"] = True
                        ln["from_block"] = True
        for ln in lines:
            if not ln["matched"]:
                n_miss += 1
                missing.append({"file": fname, "ev": evid, "ev_name": evname,
                                "page": pidx, "jp": ln["jp"]})
        if lines:
            blocks.append({"file": fname, "ev": evid, "ev_name": evname,
                           "page": pidx, "lines": lines})

    out_p = Path(out)
    out_p.mkdir(parents=True, exist_ok=True)
    # 分片落盘（每文件最多 400 事件块，防单文件过大）
    CHUNK = 400
    for i in range(0, len(blocks), CHUNK):
        part = out_p / f"seq_{i // CHUNK:03d}.json"
        part.write_text(json.dumps(blocks[i:i + CHUNK], ensure_ascii=False, indent=1),
                        encoding="utf-8")
    (out_p / "seq_last.json").write_text(
        json.dumps(blocks[max(0, len(blocks) - CHUNK):], ensure_ascii=False, indent=1),
        encoding="utf-8")
    report_data = {"total_blocks": len(blocks), "total_lines": n_lines,
                   "missing_lines": n_miss, "missing": missing}
    rp = Path(report) if report else out_p / "report.json"
    rp.write_text(json.dumps(report_data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"块数 {len(blocks)} 行数 {n_lines} 缺译 {n_miss}（{n_miss / max(n_lines, 1):.1%}）")
    print(f"产物: {out_p} 报告: {rp}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--trans", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    return run(args.data_dir, args.trans, args.out, args.report)


if __name__ == "__main__":
    sys.exit(main())
