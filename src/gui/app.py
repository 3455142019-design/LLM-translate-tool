"""Tkinter desktop interface that launches the CLI in a child process."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, List

from gui.controller import LaunchRequest, build_cli_command, list_projects
from providers.factory import PROTOCOLS, make_client
from thinking import ALL_EFFORTS, resolve_effort


class TranslateApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SR Translate")
        self.root.minsize(920, 640)
        self.repo_root = Path(__file__).resolve().parents[2]
        self.cli_path = self.repo_root / "src" / "cli.py"
        self.processes: List[subprocess.Popen] = []
        self.protocol = tk.StringVar(value="deepseek_chat")
        self.base_url = tk.StringVar()
        self.api_key = tk.StringVar()
        self.model = tk.StringVar(value="deepseek-v4-flash")
        self.thinking_effort = tk.StringVar(value="auto")
        self.stage = tk.StringVar(value="pipeline")
        self.input_path = tk.StringVar()
        self.output_root = tk.StringVar(value=str(self.repo_root / "runs"))
        self.project_name = tk.StringVar()
        self.glossary_path = tk.StringVar()
        self.price_cache_hit = tk.StringVar(value="0")
        self.price_input = tk.StringVar(value="0")
        self.price_output = tk.StringVar(value="0")
        self.status = tk.StringVar(value="就绪")
        self.effort_info = tk.StringVar()
        self._build()
        self._refresh_effort_info()
        self._refresh_projects()
        self.root.after(1000, self._poll)

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        settings = ttk.LabelFrame(root, text="连接与执行")
        settings.grid(row=0, column=0, padx=12, pady=12, sticky="nsew")
        for index in range(6):
            settings.columnconfigure(index, weight=1 if index in {1, 3, 5} else 0)

        self._add_combo(settings, "协议", self.protocol, sorted(PROTOCOLS), 0, 0)
        self._add_entry(settings, "Base URL", self.base_url, 0, 2)
        self._add_entry(settings, "API Key", self.api_key, 0, 4, show="•")
        self._add_combo(settings, "模型", self.model, [], 1, 0, editable=True)
        ttk.Button(settings, text="获取模型", command=self._fetch_models).grid(row=1, column=2, sticky="ew", padx=5, pady=5)
        self._add_combo(settings, "思考强度", self.thinking_effort, ALL_EFFORTS, 1, 3)
        ttk.Label(settings, textvariable=self.effort_info).grid(row=1, column=5, sticky="w", padx=5, pady=5)
        self._add_combo(settings, "任务", self.stage, ("translate", "polish", "pipeline"), 2, 0)
        self._add_path(settings, "输入", self.input_path, 2, 2, self._choose_input, "选择文件")
        self._add_path(settings, "输出根目录", self.output_root, 3, 0, self._choose_output, "选择目录")
        self._add_entry(settings, "项目名", self.project_name, 3, 4)
        self._add_path(settings, "术语表", self.glossary_path, 4, 0, self._choose_glossary, "选择文件")
        self._add_entry(settings, "缓存输入 ¥/M", self.price_cache_hit, 4, 3)
        self._add_entry(settings, "普通输入 ¥/M", self.price_input, 5, 0)
        self._add_entry(settings, "输出 ¥/M", self.price_output, 5, 3)
        ttk.Button(settings, text="启动项目", command=self._start).grid(row=5, column=5, sticky="ew", padx=5, pady=5)

        projects = ttk.LabelFrame(root, text="翻译项目（每秒刷新 token 与人民币费用）")
        projects.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="nsew")
        projects.columnconfigure(0, weight=1)
        projects.rowconfigure(0, weight=1)
        columns = ("name", "status", "model", "effort", "tokens", "cost", "updated")
        self.project_tree = ttk.Treeview(projects, columns=columns, show="headings", height=14)
        headings = {
            "name": "项目", "status": "状态", "model": "模型", "effort": "思考强度",
            "tokens": "Token", "cost": "费用（¥）", "updated": "更新时间",
        }
        widths = {"name": 170, "status": 90, "model": 160, "effort": 110, "tokens": 110, "cost": 100, "updated": 145}
        for column in columns:
            self.project_tree.heading(column, text=headings[column])
            self.project_tree.column(column, width=widths[column], anchor="w")
        self.project_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(projects, orient="vertical", command=self.project_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.project_tree.configure(yscrollcommand=scrollbar.set)
        ttk.Label(root, textvariable=self.status, anchor="w").grid(row=2, column=0, padx=12, pady=(0, 12), sticky="ew")

        self.protocol.trace_add("write", self._on_settings_changed)
        self.model.trace_add("write", self._on_settings_changed)
        self.thinking_effort.trace_add("write", self._on_settings_changed)

    @staticmethod
    def _add_entry(parent: ttk.Widget, label: str, variable: tk.StringVar, row: int, column: int, show: str | None = None) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=5, pady=5)
        ttk.Entry(parent, textvariable=variable, show=show).grid(row=row, column=column + 1, sticky="ew", padx=5, pady=5)

    def _add_combo(self, parent: ttk.Widget, label: str, variable: tk.StringVar, values, row: int, column: int, editable: bool = False) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=5, pady=5)
        state = "normal" if editable else "readonly"
        widget = ttk.Combobox(parent, textvariable=variable, values=values, state=state)
        widget.grid(row=row, column=column + 1, sticky="ew", padx=5, pady=5)
        if label == "模型":
            self.model_combo = widget

    def _add_path(self, parent: ttk.Widget, label: str, variable: tk.StringVar, row: int, column: int, chooser: Callable[[], None], button_label: str) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=5, pady=5)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=column + 1, columnspan=3, sticky="ew", padx=5, pady=5)
        ttk.Button(parent, text=button_label, command=chooser).grid(row=row, column=column + 4, sticky="ew", padx=5, pady=5)

    def _on_settings_changed(self, *_: object) -> None:
        self._refresh_effort_info()

    def _refresh_effort_info(self) -> None:
        resolution = resolve_effort(self.protocol.get(), self.model.get(), self.thinking_effort.get())
        supported = ", ".join(resolution.supported)
        self.effort_info.set(f"实际：{resolution.label}；可用：{supported}")

    def _choose_input(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("MTool JSON", "*.json"), ("All files", "*.*")])
        if value:
            self.input_path.set(value)

    def _choose_output(self) -> None:
        value = filedialog.askdirectory()
        if value:
            self.output_root.set(value)

    def _choose_glossary(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("Glossary", "*.md *.json"), ("All files", "*.*")])
        if value:
            self.glossary_path.set(value)

    def _fetch_models(self) -> None:
        protocol = self.protocol.get()
        key = self.api_key.get().strip()
        model = self.model.get().strip()
        base_url = self.base_url.get().strip()
        if not key:
            messagebox.showwarning("需要 API Key", "填写 API Key 后才能从服务商获取模型列表。")
            return
        self.status.set("正在获取模型列表…")

        def fetch() -> List[str]:
            client = make_client(protocol, key, model or "model-list", base_url)
            try:
                return client.list_models()
            finally:
                client.close()

        self._run_background(fetch, self._set_models)

    def _set_models(self, models: List[str]) -> None:
        self.model_combo.configure(values=models)
        if models and self.model.get() not in models:
            self.model.set(models[0])
        self.status.set(f"已获取 {len(models)} 个模型")

    def _request(self) -> LaunchRequest:
        input_path = Path(self.input_path.get().strip())
        if not input_path.is_file():
            raise ValueError("请选择存在的输入 JSON 文件。")
        output_root = Path(self.output_root.get().strip())
        if not str(output_root):
            raise ValueError("请选择输出根目录。")
        if not self.api_key.get().strip():
            raise ValueError("请填写 API Key。")
        return LaunchRequest(
            protocol=self.protocol.get(),
            base_url=self.base_url.get(),
            model=self.model.get().strip(),
            thinking_effort=self.thinking_effort.get(),
            stage=self.stage.get(),
            input_path=input_path,
            output_root=output_root,
            project_name=self.project_name.get(),
            glossary_path=self.glossary_path.get(),
            price_cache_hit=self._price(self.price_cache_hit.get(), "缓存输入"),
            price_input=self._price(self.price_input.get(), "普通输入"),
            price_output=self._price(self.price_output.get(), "输出"),
        )

    @staticmethod
    def _price(value: str, label: str) -> float:
        try:
            result = float(value or 0)
        except ValueError as error:
            raise ValueError(f"{label}价格必须是数字。") from error
        if result < 0:
            raise ValueError(f"{label}价格不能为负数。")
        return result

    def _start(self) -> None:
        try:
            request = self._request()
        except ValueError as error:
            messagebox.showerror("无法启动", str(error))
            return
        command = build_cli_command(sys.executable, self.cli_path, request)
        log_dir = request.output_root / "gui-launches"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{time.strftime('%Y%m%d_%H%M%S')}.log"
        environment = os.environ.copy()
        environment["SR_TRANSLATE_API_KEY"] = self.api_key.get().strip()
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            with log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=self.repo_root,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    creationflags=creation_flags,
                )
        except OSError as error:
            messagebox.showerror("无法启动", str(error))
            return
        self.processes.append(process)
        resolution = request.thinking_resolution
        self.status.set(f"已启动 PID {process.pid}；思考强度：{resolution.label}；日志：{log_path}")
        self._refresh_projects()

    def _run_background(self, function: Callable[[], object], success: Callable[[object], None]) -> None:
        def run() -> None:
            try:
                result = function()
            except Exception as error:
                self.root.after(0, lambda: self._background_error(error))
                return
            self.root.after(0, lambda: success(result))

        threading.Thread(target=run, daemon=True).start()

    def _background_error(self, error: Exception) -> None:
        self.status.set(f"操作失败：{error}")
        messagebox.showerror("操作失败", str(error))

    def _poll(self) -> None:
        running: List[subprocess.Popen] = []
        completed = 0
        for process in self.processes:
            if process.poll() is None:
                running.append(process)
            else:
                completed += 1
        self.processes = running
        self._refresh_projects()
        if completed:
            self.status.set(f"{completed} 个任务已结束；仍在运行：{len(running)}")
        self.root.after(1000, self._poll)

    def _refresh_projects(self) -> None:
        try:
            projects = list_projects(Path(self.output_root.get().strip()))
        except OSError as error:
            self.status.set(f"读取项目失败：{error}")
            return
        for row in self.project_tree.get_children():
            self.project_tree.delete(row)
        for project in projects:
            total = project.get("project_total", {})
            priced = bool(total.get("priced"))
            cost = f"¥ {float(total.get('cost_cny', 0.0)):.4f}" if priced else "未定价"
            self.project_tree.insert("", "end", values=(
                project.get("project", ""),
                project.get("status", ""),
                project.get("model", ""),
                f"{project.get('requested_effort', '')} → {project.get('resolved_effort', '')}",
                f"{int(total.get('total_tokens', 0)):,}",
                cost,
                project.get("updated_at", ""),
            ))


def main() -> None:
    root = tk.Tk()
    ttk.Style().theme_use("clam")
    TranslateApp(root)
    root.mainloop()
