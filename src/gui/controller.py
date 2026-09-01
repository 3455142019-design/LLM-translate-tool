"""GUI-independent launch and project-display helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from project import read_project_snapshot
from thinking import ThinkingResolution, resolve_effort


@dataclass(frozen=True)
class LaunchRequest:
    protocol: str
    base_url: str
    model: str
    thinking_effort: str
    stage: str
    input_path: Path
    output_root: Path
    project_name: str = ""
    glossary_path: str = ""
    price_cache_hit: float = 0.0
    price_input: float = 0.0
    price_output: float = 0.0

    @property
    def thinking_resolution(self) -> ThinkingResolution:
        return resolve_effort(self.protocol, self.model, self.thinking_effort)


def build_cli_command(python_executable: str, cli_path: Path, request: LaunchRequest) -> List[str]:
    command = [
        python_executable,
        str(cli_path),
        "--protocol", request.protocol,
        "--model", request.model,
        "--thinking-effort", request.thinking_effort,
        "--price-cache-hit", str(request.price_cache_hit),
        "--price-input", str(request.price_input),
        "--price-output", str(request.price_output),
        request.stage,
        "--input", str(request.input_path),
        "--out", str(request.output_root),
        "--project",
    ]
    if request.base_url.strip():
        command[4:4] = ["--base-url", request.base_url.strip()]
    if request.project_name.strip():
        command.extend(["--project-name", request.project_name.strip()])
    if request.glossary_path.strip():
        command.extend(["--glossary", request.glossary_path.strip()])
    return command


def list_projects(output_root: Path) -> List[Dict[str, object]]:
    project_root = output_root / "projects"
    if not project_root.exists():
        return []
    projects: List[Dict[str, object]] = []
    for path in project_root.iterdir():
        if not path.is_dir():
            continue
        try:
            snapshot = read_project_snapshot(path)
        except (OSError, ValueError):
            snapshot = None
        if snapshot is not None:
            snapshot["path"] = str(path)
            projects.append(snapshot)
    return sorted(projects, key=lambda item: str(item.get("updated_at", "")), reverse=True)
