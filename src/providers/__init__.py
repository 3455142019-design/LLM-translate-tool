# -*- coding: utf-8 -*-
"""providers 包 — LLM API 提供者。

注意：本包名与 Hermes 内置 providers 冲突，必须保留 __init__.py
使其成为 regular package（namespace package 会被 Hermes 的 regular
package 遮蔽）。调用方需确保 src 目录位于 sys.path 最前。
"""
