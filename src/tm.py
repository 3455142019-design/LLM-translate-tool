# -*- coding: utf-8 -*-
"""tm.py — 翻译记忆库（SQLite 双表）。

设计背景（v3 评审结论）：
- 旧机翻/未审校译文不能自动复用（会污染新译文），因此拆两张表：
  * translation_corpus : 全量历史语料（34.3 万条），只读审计/对比用途
  * translation_memory : 仅质量可确认的条目，自动复用查询走这张表
- 状态机见 schemas.TmStatus；自动复用策略：
  human_approved / qa_passed -> 精确匹配可复用
  machine_unreviewed        -> 仅作参考（不自动覆盖）
  machine_legacy / existing_unknown / rejected -> 禁止复用

用法：
    tm = TranslationMemory(db_path)
    tm.import_corpus(items, default_status=TmStatus.EXISTING_UNKNOWN)
    hit = tm.lookup(src, min_status=TmStatus.QA_PASSED)   # 精确匹配
    tm.add(src, zh, status=...)                            # 新增/更新
依赖：schemas；被 engine/ingest/cli 引用。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

from schemas import TmStatus


class TranslationMemory:
    """SQLite 翻译记忆。线程单连接使用（工具为单进程顺序执行）。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        try:
            self._init_schema()
        except Exception:
            self._conn.close()  # 初始化失败也必须释放文件锁
            raise

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS translation_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,          -- 日文原文（键）
                translation TEXT NOT NULL,     -- 译文
                status TEXT NOT NULL,          -- TmStatus 值
                speaker TEXT,                  -- 说话人（可选约束）
                scene_type TEXT,               -- 文本类型（菜单/剧情/战斗...）
                glossary_hash TEXT,            -- 术语表版本（防术语变更后误复用）
                source_batch TEXT,             -- 来源批次/文件
                updated_at TEXT DEFAULT (datetime('now','localtime')),
                -- SQLite UNIQUE 中 NULL 互不冲突：speaker/scene_type 为 NULL 时
                -- 表示"不区分"，条目按 (source, speaker, scene_type) 独立共存
                UNIQUE(source, speaker, scene_type)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS translation_corpus (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                translation TEXT NOT NULL,
                status TEXT NOT NULL,
                source_file TEXT,
                UNIQUE(source)
            )""")
        self._conn.commit()

    # ---- 翻译记忆（可复用表） ----
    def lookup(self, source: str, min_status: TmStatus = TmStatus.QA_PASSED,
               speaker: Optional[str] = None) -> Optional[Tuple[str, TmStatus]]:
        """精确匹配查询。只返回状态 >= min_status 的条目（按状态优先级比较）。

        优先级：human_approved > qa_passed > machine_unreviewed > machine_legacy
              > existing_unknown > rejected

        SQL 层面直接过滤掉低于 min_status 的状态，避免"最新 N 条全是低状态
        条目遮蔽老的高状态条目"的查询盲区（review 实证发现）。
        """
        rank = {s.value: i for i, s in enumerate(
            [TmStatus.REJECTED, TmStatus.EXISTING_UNKNOWN, TmStatus.MACHINE_LEGACY,
             TmStatus.MACHINE_UNREVIEWED, TmStatus.QA_PASSED, TmStatus.HUMAN_APPROVED])}
        min_rank = rank[min_status.value]
        allowed = [s.value for s in TmStatus if rank[s.value] >= min_rank
                   and s != TmStatus.REJECTED]
        if not allowed:
            return None
        placeholders = ",".join("?" * len(allowed))
        cur = self._conn.cursor()
        if speaker:
            cur.execute(
                f"SELECT translation, status FROM translation_memory "
                f"WHERE source=? AND speaker=? AND status IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT 1", (source, speaker, *allowed))
        else:
            cur.execute(
                f"SELECT translation, status FROM translation_memory "
                f"WHERE source=? AND status IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT 1", (source, *allowed))
        row = cur.fetchone()
        if row is None:
            return None
        return row[0], TmStatus(row[1])

    def add(self, source: str, translation: str, status: TmStatus,
            speaker: Optional[str] = None, scene_type: Optional[str] = None,
            glossary_hash: str = "", source_batch: str = "") -> None:
        """新增/覆盖一条可复用记忆（同 source+speaker+scene_type 视为同一条）。

        SQLite 的 ON CONFLICT 对 NULL 列不生效（NULL 永不冲突），因此用
        UPDATE 先行匹配（COALESCE 归一并列），无更新再 INSERT。
        """
        cur = self._conn.cursor()
        cur.execute("""
            UPDATE translation_memory SET translation=?, status=?, glossary_hash=?,
                   source_batch=?, updated_at=datetime('now','localtime')
            WHERE source=? AND COALESCE(speaker,'')=COALESCE(?,'')
              AND COALESCE(scene_type,'')=COALESCE(?,'')
        """, (translation, status.value, glossary_hash, source_batch,
              source, speaker, scene_type))
        if cur.rowcount == 0:
            self._conn.execute("""
                INSERT INTO translation_memory
                    (source, translation, status, speaker, scene_type, glossary_hash, source_batch)
                VALUES (?,?,?,?,?,?,?)
            """, (source, translation, status.value, speaker, scene_type,
                  glossary_hash, source_batch))
        self._conn.commit()

    def count_memory(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM translation_memory").fetchone()[0]

    # ---- 历史语料（审计表） ----
    def import_corpus(self, items: List[Tuple[str, str]],
                      default_status: TmStatus = TmStatus.EXISTING_UNKNOWN,
                      source_file: str = "") -> int:
        """批量导入历史译文（如 34.3 万条旧数据）。同 source 覆盖。"""
        cur = self._conn.cursor()
        n = 0
        for src, zh in items:
            cur.execute("""
                INSERT INTO translation_corpus (source, translation, status, source_file)
                VALUES (?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET translation=excluded.translation,
                    status=excluded.status, source_file=excluded.source_file
            """, (src, zh, default_status.value, source_file))
            n += 1
            if n % 50_000 == 0:
                self._conn.commit()
        self._conn.commit()
        return n

    def corpus_stats(self) -> dict:
        cur = self._conn.cursor()
        total = cur.execute("SELECT COUNT(*) FROM translation_corpus").fetchone()[0]
        by_status = dict(cur.execute(
            "SELECT status, COUNT(*) FROM translation_corpus GROUP BY status").fetchall())
        return {"total": total, "by_status": by_status}

    def close(self) -> None:
        self._conn.close()
