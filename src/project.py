"""Project-scoped storage and live token/cost snapshots."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from schemas import Usage


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_json_atomic(path: Path, value: Dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized[:48] or "translation"


@dataclass(frozen=True)
class TranslationProject:
    path: Path
    name: str
    source: Path

    @property
    def tm_path(self) -> Path:
        return self.path / "tm.db"

    @classmethod
    def create_or_open(cls, output_root: Path, source: Path, name: str = "") -> "TranslationProject":
        source = source.resolve()
        digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:10]
        display_name = name.strip() or source.stem
        path = output_root / "projects" / f"{_slug(display_name)}-{digest}"
        path.mkdir(parents=True, exist_ok=True)
        metadata_path = path / "project.json"
        if not metadata_path.exists():
            _write_json_atomic(metadata_path, {
                "name": display_name,
                "source": str(source),
                "created_at": _now(),
            })
        return cls(path=path, name=display_name, source=source)


class ProjectStore:
    def __init__(
        self,
        project: TranslationProject,
        run_id: str,
        provider: str,
        model: str,
        requested_effort: str,
        resolved_effort: str,
        pricing: Optional[Dict[str, float]],
    ):
        self.project = project
        self.run_id = run_id
        self.provider = provider
        self.model = model
        self.requested_effort = requested_effort
        self.resolved_effort = resolved_effort
        self.pricing = pricing
        self.database_path = project.path / "project.sqlite"
        self.snapshot_path = project.path / "live_usage.json"
        self._initialize()
        self._start_run()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    requested_effort TEXT NOT NULL,
                    resolved_effort TEXT NOT NULL,
                    pricing_json TEXT
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    cache_hit_tokens INTEGER NOT NULL,
                    cache_miss_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    reasoning_tokens INTEGER NOT NULL,
                    cost_cny REAL NOT NULL,
                    priced INTEGER NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )"""
            )

    def _start_run(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO runs
                (run_id, started_at, status, provider, model, requested_effort, resolved_effort, pricing_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.run_id,
                    _now(),
                    "running",
                    self.provider,
                    self.model,
                    self.requested_effort,
                    self.resolved_effort,
                    json.dumps(self.pricing, ensure_ascii=False) if self.pricing else None,
                ),
            )
        self.write_snapshot("running")

    def record_usage(self, usage: Usage, cost_cny: float, stage: str, priced: bool) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO usage_events
                (run_id, recorded_at, stage, prompt_tokens, cache_hit_tokens, cache_miss_tokens,
                 completion_tokens, reasoning_tokens, cost_cny, priced)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.run_id,
                    _now(),
                    stage,
                    usage.prompt_tokens,
                    usage.prompt_cache_hit_tokens,
                    usage.prompt_cache_miss_tokens,
                    usage.completion_tokens,
                    usage.reasoning_tokens,
                    cost_cny,
                    int(priced),
                ),
            )
        self.write_snapshot("running")

    def finish(self, status: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, _now(), self.run_id),
            )
        self.write_snapshot(status)

    def _totals(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        where = "WHERE run_id = ?" if run_id else ""
        values = (run_id,) if run_id else ()
        with self._connection() as connection:
            row = connection.execute(
                f"""SELECT
                    COUNT(*) AS requests,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens,
                    COALESCE(SUM(cache_miss_tokens), 0) AS cache_miss_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                    COALESCE(SUM(cost_cny), 0.0) AS cost_cny,
                    MIN(priced) AS priced
                    FROM usage_events {where}""",
                values,
            ).fetchone()
        total = dict(row)
        total["total_tokens"] = total["prompt_tokens"] + total["completion_tokens"]
        total["priced"] = bool(total["priced"]) if total["requests"] else bool(self.pricing)
        total["cost_cny"] = round(float(total["cost_cny"]), 6)
        return total

    def write_snapshot(self, status: str) -> Dict[str, Any]:
        snapshot = {
            "updated_at": _now(),
            "status": status,
            "project": self.project.name,
            "source": str(self.project.source),
            "provider": self.provider,
            "model": self.model,
            "requested_effort": self.requested_effort,
            "resolved_effort": self.resolved_effort,
            "run": self._totals(self.run_id),
            "project_total": self._totals(),
        }
        _write_json_atomic(self.snapshot_path, snapshot)
        return snapshot


def read_project_snapshot(project_path: Path) -> Optional[Dict[str, Any]]:
    path = project_path / "live_usage.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
