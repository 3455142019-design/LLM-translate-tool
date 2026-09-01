"""Local tests for provider adapters, effort mapping, and project accounting."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gui.controller import LaunchRequest, build_cli_command, list_projects
from cli import main as cli_main
from project import ProjectStore, TranslationProject, read_project_snapshot
from providers.anthropic import AnthropicMessagesClient
from providers.openai_responses import OpenAIResponsesClient
from schemas import Usage
from thinking import resolve_effort


class TestThinking(unittest.TestCase):
    def test_deepseek_maps_unavailable_effort(self):
        resolution = resolve_effort("deepseek_chat", "deepseek-v4-flash", "medium")
        self.assertEqual(resolution.actual, "low")
        self.assertTrue(resolution.mapped)

    def test_gpt5_maps_ultra_to_xhigh(self):
        resolution = resolve_effort("openai_responses", "gpt-5", "ultra")
        self.assertEqual(resolution.actual, "xhigh")


class TestProviders(unittest.TestCase):
    def test_responses_extracts_text(self):
        data = {
            "output": [{"content": [
                {"type": "reasoning", "summary": []},
                {"type": "output_text", "text": "翻译结果"},
            ]}],
        }
        self.assertEqual(OpenAIResponsesClient._extract_text(data), "翻译结果")

    def test_anthropic_extracts_text_and_thinking(self):
        data = {"content": [
            {"type": "thinking", "thinking": "分析"},
            {"type": "text", "text": "翻译"},
        ]}
        self.assertEqual(AnthropicMessagesClient._extract_content(data), ("翻译", "分析"))


class TestProjects(unittest.TestCase):
    def test_cli_dry_run_creates_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            source.write_text('{"テスト": "テスト"}', encoding="utf-8")
            result = cli_main([
                "--model", "deepseek-v4-flash",
                "translate",
                "--input", str(source),
                "--out", str(root / "out"),
                "--project",
                "--dry-run",
            ])
            self.assertEqual(result, 0)
            self.assertEqual(len(list_projects(root / "out")), 1)

    def test_project_snapshot_accumulates_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dialogue.json"
            source.write_text("{}", encoding="utf-8")
            project = TranslationProject.create_or_open(root / "out", source, "测试项目")
            store = ProjectStore(project, "run-1", "openai_responses", "gpt-5", "high", "high", {
                "in_hit": 1.0,
                "in_miss": 2.0,
                "out": 3.0,
            })
            usage = Usage(
                prompt_tokens=30,
                prompt_cache_hit_tokens=10,
                prompt_cache_miss_tokens=20,
                completion_tokens=5,
                reasoning_tokens=2,
            )
            store.record_usage(usage, 0.1234, "translate", priced=True)
            store.finish("completed")
            snapshot = read_project_snapshot(project.path)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["project_total"]["total_tokens"], 35)
            self.assertEqual(snapshot["project_total"]["cost_cny"], 0.1234)
            self.assertEqual(len(list_projects(root / "out")), 1)

    def test_workers_dry_run_parallel(self):
        """--workers 2：分片并行 translate，产物合并完整（dry-run 无 key）。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            data = {f"テストテキスト{i:03d}": f"テストテキスト{i:03d}" for i in range(40)}
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            result = cli_main(["--model", "deepseek-v4-flash", "--workers", "2",
                               "translate", "--input", str(source),
                               "--out", str(root / "out"), "--dry-run"])
            self.assertEqual(result, 0)
            finals = list((root / "out").glob("*/final/ManualTransFile_zh.json"))
            self.assertEqual(len(finals), 1, "应恰好合并出一个 final 文件")
            merged = json.loads(finals[0].read_text(encoding="utf-8"))
            self.assertEqual(len(merged), 40, "分片产物应完整合并")
            parts = list((root / "out").glob("*/parts/part_*.json"))
            self.assertGreaterEqual(len(parts), 2, "应生成 >=2 个分片文件")

    def test_workers_rejected_for_pipeline(self):
        """pipeline 不支持 --workers（多阶段依赖），应报错拒绝。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            source.write_text('{"テスト": "テスト"}', encoding="utf-8")
            result = cli_main(["--model", "deepseek-v4-flash", "--workers", "2",
                               "pipeline", "--input", str(source),
                               "--out", str(root / "out"), "--dry-run"])
            self.assertEqual(result, 1)

    def test_workers_rejected_with_project(self):
        """--workers 与 --project 互斥（多进程写项目库会冲突）。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            data = {f"テストテキスト{i:03d}": f"テストテキスト{i:03d}" for i in range(40)}
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            result = cli_main(["--model", "deepseek-v4-flash", "--workers", "2",
                               "translate", "--input", str(source),
                               "--out", str(root / "out"), "--project", "--dry-run"])
            self.assertEqual(result, 1)

    def test_workers_dry_run_polish_parallel(self):
        """--workers 2 + polish：分片并行润色，产物合并完整（dry-run 无 key）。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            data = {f"テストテキスト{i:03d}": "这是机翻测试译文。" for i in range(30)}
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            result = cli_main(["--model", "deepseek-v4-flash", "--workers", "2",
                               "polish", "--input", str(source),
                               "--out", str(root / "out"), "--source-tag", "legacy",
                               "--dry-run"])
            self.assertEqual(result, 0)
            finals = list((root / "out").glob("*/final/ManualTransFile_zh.json"))
            self.assertEqual(len(finals), 1)
            merged = json.loads(finals[0].read_text(encoding="utf-8"))
            self.assertEqual(len(merged), 30, "polish 分片产物应完整合并")

    def test_gui_command_excludes_api_key(self):
        request = LaunchRequest(
            protocol="anthropic_messages",
            base_url="https://example.test/v1",
            model="claude-test",
            thinking_effort="high",
            stage="pipeline",
            input_path=Path("input.json"),
            output_root=Path("out"),
            project_name="demo",
        )
        command = build_cli_command("python", Path("src/cli.py"), request)
        self.assertIn("--project", command)
        self.assertIn("--base-url", command)
        self.assertNotIn("--api-key", command)


if __name__ == "__main__":
    unittest.main()
