# -*- coding: utf-8 -*-
"""engine.py — 批处理主循环（翻译/润色/审校三阶段共用）。

职责：
1. 按阶段（translate/polish/review）驱动批次：mask -> TM 命中 -> 请求 -> 校验
   -> 局部修复 -> QA 路由 -> 落盘
2. 错误分流：ApiErrorKind -> 重试/终止（402 终止整个 run）
3. finish_reason 分支：length 二分 / content_filter 二分定位+blocked
4. 保护：熔断器（连续失败暂停）、预算硬限制、system_fingerprint 漂移监控
5. 成本：真实 usage 累计 + journal 记录 + 超限自动暂停
6. 断点：--resume 跳过已完成批次

pipeline = cli 串联 translate 阶段 + polish 阶段（QA 在 polish 阶段内路由）。

依赖：config/schemas/protect/tm/validate/qa/storage/providers。
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from batcher import Batcher
from glossary import Glossary
from protect import MaskRecord, Protector
from providers.deepseek import ApiError, ApiErrorKind, DeepSeekClient
from project import ProjectStore
from qa import LOW_RISK_THRESHOLD, HIGH_RISK_THRESHOLD, run_qa
from schemas import Batch, Entry, RunMeta, Usage
from storage import RunStorage
from thinking import resolve_effort
from tm import TranslationMemory
from validate import (check_template_vars, map_seq_keys, parse_model_output,
                      restore_entry_zh, validate_batch_output)

# 阶段 -> prompt 模板文件
_STAGE_PROMPT = {
    "translate": "translate.txt",
    "polish": "polish.txt",
    "review": "review.txt",
    "review_hard": "review.txt",
    "review_ambiguous": "review.txt",
    # repair 复用 translate 静态 system（同 run 内 repair 可命中 translate 的缓存前缀），
    # 修复指令内联到 user 消息开头（见 _build_messages）
    "repair": "translate.txt",
}
# 阶段 -> 默认模式策略键
_STAGE_POLICY = {
    "translate": "translate",
    "polish": "polish",
    "review_hard": "review_hard",
    "review_ambiguous": "review_ambiguous",
    "repair": "repair",
}

# 不可重试的错误 -> run 终止原因
_FATAL_KINDS = {
    ApiErrorKind.BAD_REQUEST: "参数错误（400）",
    ApiErrorKind.AUTH: "认证/权限失败",
    ApiErrorKind.QUOTA: "余额不足（402）",
}


class Engine:
    """批量执行引擎。一个 Engine 实例对应一个 run。"""

    def __init__(self, client: DeepSeekClient, storage: RunStorage,
                 glossary: Glossary, tm: TranslationMemory,
                 batcher: Optional[Batcher] = None,
                 max_cost_cny: float = config.DEFAULT_MAX_COST_CNY,
                 fingerprint_policy: str = "strict",
                 thinking_effort: str = "auto",
                 project_store: Optional[ProjectStore] = None,
                 context_chain: bool = False,
                 chain_budget: int = 800_000,
                 key_lines: Optional[Dict[str, Dict]] = None,
                 world_context: str = "",
                 project_name: str = "Succubus Rhapsodia",
                 chars_info: Optional[Dict[str, dict]] = None):
        self.client = client
        self.storage = storage
        self.glossary = glossary
        self.tm = tm
        self.batcher = batcher or Batcher()
        self.max_cost_cny = max_cost_cny
        self.fingerprint_policy = fingerprint_policy  # strict/warn/ignore
        self.thinking_effort = thinking_effort
        self.project_store = project_store
        self.context_chain = context_chain          # 跨包多轮上下文链
        self.chain_budget = chain_budget            # 链 token 预算（默认 1M×80%）
        self.key_lines = key_lines or {}            # 重点句 {原文: {note, mode, translation}}
        self.world_context = world_context          # 世界/人物/剧情设定（项目包注入 prompt）
        self.project_name = project_name            # prompt 项目名占位符（多项目共用工具时防串名）
        self.chars_info = chars_info or {}          # 角色名/别名 -> {gender, role, ...}
        self._chain: List[Dict[str, str]] = []      # 链历史 [user, assistant, ...]
        self._pending_history: Optional[List[Dict[str, str]]] = None
        self.resolved_effort = "auto"
        self._last_thinking: Optional[str] = None          # 最近一次请求实际 thinking 状态
        self._last_reasoning_effort: Optional[str] = None  # 最近一次请求实际推理强度

        self.total_usage = Usage()
        self.cost_cny = 0.0
        self.consecutive_failures = 0
        self.paused_reason: Optional[str] = None
        self.run_fingerprint: Optional[str] = None
        # 请求期间成本快照（用于预算校验）
        self._stage_cost = 0.0

    # ======================================================================
    # 公开入口
    # ======================================================================
    def run_stage(self, stage: str, entries: List[Entry], resume: bool = False,
                  max_stage_cost_cny: Optional[float] = None) -> Dict[str, int]:
        """执行一个阶段（translate/polish/review_hard/review_ambiguous）。

        返回统计 {translated, reused, failed, repaired, reviewed}。
        """
        self._stage_cost = 0.0
        self._current_stage = stage
        self.storage.set_stage(stage)
        stage_limit = max_stage_cost_cny or config.DEFAULT_MAX_STAGE_COST_CNY
        batches = self.batcher.build_batches(entries, mode=stage)
        done = self.storage.completed_batch_numbers() if resume else set()
        stats = {"translated": 0, "reused": 0, "failed": 0, "repaired": 0, "reviewed": 0}

        for batch in batches:
            if self.paused_reason:
                self.storage.log("run_paused", stage=stage, reason=self.paused_reason)
                break
            if batch.number in done:
                # 断点续跑：跳过已完成批次，但必须从磁盘恢复译文到
                # entry.extra["final_zh"]——否则 pipeline 阶段 2 QA 路由与
                # 阶段 3 merge_final 拿不到 final_zh，终稿会以原文落盘（漏译）。
                # 2026-08-18 审计发现：此前只跳过不恢复，--resume 后合并产物丢译文。
                self._restore_batch_output(batch)
                stats["translated"] += len(batch.items)
                continue
            if self._stage_cost >= stage_limit:
                self.pause(f"阶段预算超限（{self._stage_cost:.2f} 元 >= {stage_limit} 元）")
                break
            # 上下文链：主批且链内预算充足时附加历史（repair 子批自动排除）
            self._pending_history = self._resolve_chain(batch)
            try:
                self._process_batch(batch, stage, stats)
                self.consecutive_failures = 0
            except _BatchExhausted as e:
                self.consecutive_failures += 1
                stats["failed"] += len(batch.items)
                self.storage.save_failed(batch, str(e))
                self.storage.log("batch_failed", batch=batch.number, reason=str(e))
                if self.consecutive_failures >= config.CIRCUIT_BREAKER_LIMIT:
                    self.pause(f"熔断：连续 {self.consecutive_failures} 批失败")
                    break
            except _RunFatal as e:
                self.pause(str(e))
                break
        return stats

    def pause(self, reason: str) -> None:
        self.paused_reason = reason
        self.storage.log("run_paused", reason=reason)

    def _restore_batch_output(self, batch: Batch) -> None:
        """从磁盘恢复已完成批次的译文到 entry.extra["final_zh"]（--resume 用）。

        读取 batches/<stage>/NNNNNN.input.json 的 id->key 映射与
        NNNNNN.output.json 的 {id: 译文}，按 id 回填。文件缺失/损坏时
        只记日志不抛错：该批会被当作未完成重新请求（宁可重付少量费用，
        不可静默丢译文）。
        """
        stage_dir = self.storage.batches_dir
        input_path = stage_dir / f"{batch.number:06d}.input.json"
        output_path = stage_dir / f"{batch.number:06d}.output.json"
        if not input_path.exists() or not output_path.exists():
            self.storage.log("resume_restore_missing", batch=batch.number,
                             reason="input/output 文件缺失，该批将重新请求")
            return
        try:
            input_data = json.loads(input_path.read_text(encoding="utf-8"))
            output_data = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self.storage.log("resume_restore_missing", batch=batch.number,
                             reason=f"文件损坏: {e}")
            return
        id2key = {it.get("id"): it.get("key") for it in input_data
                  if isinstance(it, dict)}
        restored = 0
        for it in batch.items:
            zh = output_data.get(it.id)
            if zh is None and id2key.get(it.id) is not None:
                # 旧格式 output 可能以 key 为键（兼容兜底）
                zh = output_data.get(id2key[it.id])
            if zh:
                it.extra["final_zh"] = zh
                restored += 1
        self.storage.log("resume_restored", batch=batch.number, restored=restored)

    # ======================================================================
    # 批次处理
    # ======================================================================
    def _process_batch(self, batch: Batch, stage: str, stats: Dict[str, int]) -> None:
        """单批完整流程。任何重试耗尽抛 _BatchExhausted；致命错误抛 _RunFatal。"""
        self.storage.save_batch_input(batch)
        original_items = list(batch.items)  # 完整条目（含 TM 复用），落盘/统计用

        # 1. 遮罩 + TM 命中复用
        active: List[Entry] = []
        for it in batch.items:
            if it.masked_src is None:
                rec = Protector.mask(it.src)
                it.masked_src = rec.text
                it.extra["mask_record"] = rec
            if stage == "polish" and it.masked_cur is None and it.cur:
                rec = Protector.mask(it.cur)
                it.masked_cur = rec.text
                it.extra["mask_record_cur"] = rec
            # 重点句 agent 预填译文：主控 agent 已通读原文并给出定稿，直接采用不进 API
            key_line = self.key_lines.get(it.src)
            if key_line and key_line.get("mode") == "agent" and key_line.get("translation"):
                it.extra["final_zh"] = key_line["translation"]
                stats["reused"] += 1
                continue
            hit = self.tm.lookup(it.src)
            if hit and stage == "translate":
                zh, status = hit
                it.extra["final_zh"] = zh
                it.tm_status = status
                stats["reused"] += 1
                continue
            active.append(it)
        if not active:
            self._finish_batch(batch, stats, original_items=original_items)
            return
        batch.items = active  # 只请求未命中条目（id 保持不变，校验不受影响）

        # 2. 构造 prompt 并请求（带重试）
        policy_key = _STAGE_POLICY[stage]
        resp = self._request_with_retry(batch, policy_key)
        self._save_response(batch, resp)

        # 3. finish_reason 分支
        if resp.finish_reason == "length":
            # 输出截断：二分批次重发（与 content_filter 同策略）。文档承诺
            # "length 自动二分"，此前实现只抛异常整批 failed（2026-08-18 修复）。
            self._handle_length(batch, stage, stats, original_items)
            return
        if resp.finish_reason == "content_filter":
            self._handle_content_filter(batch, stage, stats, original_items)
            return
        if resp.finish_reason == "insufficient_system_resource":
            raise _BatchExhausted("系统资源不足")

        # 4. 校验 + 局部修复
        vresult = validate_batch_output(resp.content or "", batch)
        if vresult.parse_error:
            # 整批无法解析 -> 最多重发 2 次（模型偶发坏 JSON，如译文以全角引号结尾
            # 时缺失 ASCII 闭合引号）；仍失败才整批 failed。每次请求都落盘便于审计。
            for _attempt in (1, 2):
                self.storage.log("json_parse_failed_retry", batch=batch.number,
                                 err=vresult.parse_error[:120], attempt=_attempt)
                batch.subtag = f".retry{_attempt}"  # 与首次请求的文件名隔离
                resp = self._request_with_retry(batch, policy_key)
                self._save_response(batch, resp)
                vresult = validate_batch_output(resp.content or "", batch)
                if not vresult.parse_error:
                    break
            if vresult.parse_error:
                raise _BatchExhausted(f"JSON 解析失败(重试后): {vresult.parse_error[:120]}")
        if vresult.missing:
            self._repair_missing(batch, vresult.missing, stats)
        if vresult.misplaced:
            # 疑似错位条目走 repair 重译（译文与自身原文无关、与批内其他
            # 条目匹配）。2026-08-10 HOLLOWWALD 教训：错位必须当场拦截，
            # 一旦进入 final 合并再发现只能整段重翻。
            self.storage.log("misplaced_detected", batch=batch.number,
                             ids=vresult.misplaced)
            self._repair_missing(batch, vresult.misplaced, stats)
        if vresult.suspicious:
            # 弱信号：仅记日志，由 full-qa/审查阶段人工确认，不自动重译
            self.storage.log("suspicious_misplace", batch=batch.number,
                             ids=vresult.suspicious)
        for eid, issues in vresult.placeholder_issues.items():
            self.storage.log("placeholder_issue", id=eid, issues=issues[:3])
        # 写入译文（含 TM 复用 + 新译 + 修复）；misplaced 条目已被 repair
        # 重译并写入 final_zh，主批译文丢弃不覆盖
        for it in batch.items:
            if it.id in vresult.parsed and it.id not in vresult.placeholder_issues \
                    and it.id not in vresult.misplaced:
                zh = restore_entry_zh(it, vresult.parsed[it.id])
                it.extra["final_zh"] = zh
                stats["translated"] += 1
        self._finish_batch(batch, stats, original_items=original_items)

    def _finish_batch(self, batch: Batch, stats: Dict[str, int],
                      original_items: Optional[List[Entry]] = None) -> None:
        """落盘 output + meta + journal + 成本快照。

        original_items：TM 复用会缩减 batch.items，传完整列表保证
        output.json 包含全部条目（含复用条目），数据一致。
        """
        items = original_items if original_items is not None else batch.items
        output = {it.id: (it.extra.get("final_zh") or it.cur or "")
                  for it in items}
        self.storage.save_batch_output(batch, output)
        meta = {
            "model": self.client.model,
            "thinking": self._last_thinking or "disabled",
            "reasoning_effort": self._last_reasoning_effort,
            "usage": self.total_usage.__dict__,
            "cost_cny": round(self.cost_cny, 4),
            "fingerprint": self.run_fingerprint,
        }
        self.storage.save_batch_meta(batch, meta)
        self.storage.log("batch_done", batch=batch.number,
                         items=len(batch.items), translated=stats["translated"])

    # ======================================================================
    # 请求与重试
    # ======================================================================
    def _request_with_retry(self, batch: Batch, policy_key: str) -> object:
        """带重试的请求。返回 ApiResponse。

        批次文件只在首次尝试落盘（重试内容相同无需重复写）；
        调用方可通过 batch.subtag 隔离同一批号的子批（repair/二分）。
        """
        messages = self._build_messages(batch, policy_key,
                                        history=self._pending_history
                                        if (self.context_chain and policy_key != "repair") else None)
        params = dict(config.MODE_POLICY[policy_key])
        # 重点句 max 通道：批内含 mode=max 条目 -> 强制最高思考强度（双关/典故/俚语）
        effort = self.thinking_effort
        if self.key_lines and any(
                (self.key_lines.get(it.src) or {}).get("mode") == "max"
                for it in batch.items):
            effort = "max"
        if effort != "auto":
            resolution = resolve_effort(
                getattr(self.client, "protocol", "deepseek_chat"),
                self.client.model,
                effort,
            )
            self.resolved_effort = resolution.actual
            if resolution.actual == "none":
                params["thinking"] = "disabled"
                params.pop("reasoning_effort", None)
            elif resolution.actual == "auto":
                params["thinking"] = "disabled"
                params.pop("reasoning_effort", None)
            else:
                params["thinking"] = "enabled"
                params["reasoning_effort"] = resolution.actual
                params.pop("temperature", None)
        # 记录实际生效的 thinking 状态（meta.json 审计用；此前硬编码 disabled，
        # review_hard/review_ambiguous 的元数据失真，2026-08-18 修复）
        self._last_thinking = params.get("thinking", "disabled")
        self._last_reasoning_effort = params.get("reasoning_effort")
        self.storage.save_batch_request(batch, messages, params)

        max_attempts = config.RETRY_POLICY["max_attempts"]
        for attempt in range(max_attempts + 1):
            try:
                resp = self.client.chat(
                    messages,
                    thinking=params.get("thinking", "disabled"),
                    reasoning_effort=params.get("reasoning_effort"),
                    temperature=params.get("temperature"),
                    response_format={"type": "json_object"},
                )
                self._on_usage(resp.usage, resp.system_fingerprint, batch)
                batch.attempts = attempt
                # 上下文链：主批成功后把 [user, assistant] 追加（repair 子批不链）
                if self.context_chain and policy_key != "repair" and batch.subtag == "":
                    self._chain_append(messages[-1], resp.content)
                return resp
            except ApiError as e:
                if e.kind in _FATAL_KINDS:
                    raise _RunFatal(f"{_FATAL_KINDS[e.kind]}: {e}")
                if e.kind == ApiErrorKind.RATE_LIMIT:
                    wait = e.retry_after or (config.RETRY_POLICY["backoff_base"] ** attempt
                                             + random.uniform(0, config.RETRY_POLICY["backoff_jitter"]))
                    self.storage.log("rate_limited", batch=batch.number, wait=round(wait, 1))
                    time.sleep(wait)
                    continue
                if e.kind in (ApiErrorKind.SERVER, ApiErrorKind.NETWORK_UNSENT,
                              ApiErrorKind.EMPTY_RESPONSE, ApiErrorKind.CLOUDFLARE_BLOCK):
                    if attempt >= max_attempts:
                        raise _BatchExhausted(f"{e.kind} 重试耗尽: {e}")
                    wait = config.RETRY_POLICY["backoff_base"] ** attempt + random.uniform(
                        0, config.RETRY_POLICY["backoff_jitter"])
                    self.storage.log("retry", batch=batch.number, kind=e.kind, wait=round(wait, 1))
                    time.sleep(wait)
                    continue
                if e.kind in (ApiErrorKind.TIMEOUT, ApiErrorKind.NETWORK_UNCERTAIN):
                    # 计费状态未知：记录 uncertain，不重试该批（防重复计费）
                    self.storage.log("uncertain_attempt", batch=batch.number, kind=e.kind)
                    raise _BatchExhausted(f"{e.kind}（计费状态未知，标记 uncertain，不重复提交）")
                raise _BatchExhausted(f"未分类错误 {e.kind}: {e}")
        raise _BatchExhausted("重试次数耗尽")

    def _save_response(self, batch: Batch, resp) -> None:
        """落盘完整响应（含 usage 明细/思考链/finish_reason/指纹），供事后成本审计。"""
        self.storage.save_batch_response_raw(batch, {
            "content": resp.content,
            "reasoning_content": resp.reasoning_content or None,
            "usage": resp.usage.__dict__,
            "finish_reason": resp.finish_reason,
            "model": resp.usage.model or self.client.model,
            "system_fingerprint": resp.system_fingerprint,
        })

    def _on_usage(self, usage: Usage, fingerprint: Optional[str],
                  batch: Optional[Batch] = None) -> None:
        """累计成本 + 指纹漂移监控 + 预算校验。"""
        pricing = getattr(self.client, "pricing", None)
        priced = pricing is not None
        if pricing is None:
            try:
                pricing = config.get_pricing(self.client.model)
                priced = True
            except ValueError:
                pricing = {"in_hit": 0.0, "in_miss": 0.0, "out": 0.0}
        self.total_usage.prompt_tokens += usage.prompt_tokens
        self.total_usage.prompt_cache_hit_tokens += usage.prompt_cache_hit_tokens
        self.total_usage.prompt_cache_miss_tokens += usage.prompt_cache_miss_tokens
        self.total_usage.completion_tokens += usage.completion_tokens
        self.total_usage.reasoning_tokens += usage.reasoning_tokens
        request_cost = usage.cost_cny(pricing)
        self.cost_cny += request_cost
        self._stage_cost += request_cost
        # 每请求真实用量事件（审计/对账：缓存命中率、思考占比、单价构成）
        self.storage.log("api_usage",
                         batch=batch.number if batch else None,
                         subtag=batch.subtag if batch and batch.subtag else None,
                         prompt=usage.prompt_tokens,
                         hit=usage.prompt_cache_hit_tokens,
                         miss=usage.prompt_cache_miss_tokens,
                         completion=usage.completion_tokens,
                         reasoning=usage.reasoning_tokens,
                         cost=round(request_cost, 6))
        if self.project_store is not None:
            self.project_store.record_usage(
                usage,
                request_cost,
                getattr(self, "_current_stage", "common"),
                priced,
            )

        # 指纹漂移
        if fingerprint:
            if self.run_fingerprint is None:
                self.run_fingerprint = fingerprint
            elif fingerprint != self.run_fingerprint and self.fingerprint_policy == "strict":
                self.pause(f"后端指纹变化: {self.run_fingerprint} -> {fingerprint}")
            elif fingerprint != self.run_fingerprint and self.fingerprint_policy == "warn":
                self.storage.log("fingerprint_changed", old=self.run_fingerprint, new=fingerprint)

        # 预算
        if self.cost_cny >= self.max_cost_cny:
            self.pause(f"总预算超限（{self.cost_cny:.2f} 元 >= {self.max_cost_cny} 元）")
        # 缓存异常保护
        if usage.prompt_tokens > 0:
            miss_rate = usage.prompt_cache_miss_tokens / usage.prompt_tokens
            if miss_rate > config.STOP_ON_CACHE_MISS_RATE:
                self.storage.log("cache_miss_high", batch_rate=round(miss_rate, 3))
        # 思考膨胀保护
        if usage.completion_tokens > 0:
            ratio = usage.reasoning_tokens / usage.completion_tokens
            if ratio > config.MAX_REASONING_RATIO:
                self.storage.log("reasoning_high", ratio=round(ratio, 3))

    # ======================================================================
    # 局部修复 / content_filter 处理
    # ======================================================================
    def _repair_missing(self, batch: Batch, missing_ids: List[str], stats: Dict[str, int]) -> None:
        """缺失条目局部补译（只重发缺失条目）。

        补译失败只记录日志，不向上抛（主批已成功，不能因 repair 失败
        导致整批被误标 failed / 重复落盘）。
        """
        missing_items = [it for it in batch.items if it.id in missing_ids]
        if not missing_items:
            return
        repair_batch = Batch(number=batch.number, items=missing_items,
                             subtag=".repair")  # 文件名隔离，避免与主批冲突
        try:
            resp = self._request_with_retry(repair_batch, "repair")
        except (_BatchExhausted, _RunFatal) as e:
            self.storage.log("repair_failed", batch=batch.number,
                             missing=missing_ids, reason=str(e)[:120])
            self._mark_repair_failed(missing_items, stats)
            return
        self._save_response(repair_batch, resp)  # 修复响应也落盘（审计完整性）
        parsed, err = parse_model_output(resp.content or "")
        if parsed is None:
            self.storage.log("repair_failed", batch=batch.number,
                             missing=missing_ids, reason=err)
            self._mark_repair_failed(missing_items, stats)
            return
        # repair 输出键为短序号（与主批同协议），映射回条目 id 再匹配
        parsed = map_seq_keys(parsed, missing_items)
        rejected = []
        repaired_ids = set()
        for it in missing_items:
            if it.id not in parsed:
                continue
            zh = parsed[it.id]
            # 与主批相同的占位符/模板变量校验（防 repair 译文丢控制码污染 final）
            rec = it.extra.get("mask_record")
            ok, issues = (Protector.verify(zh, rec)
                          if isinstance(rec, MaskRecord) else (True, []))
            missing_vars = check_template_vars(it.src, zh)
            if not ok or missing_vars:
                reasons = list(issues)
                if missing_vars:
                    reasons.append(f"模板变量缺失: {missing_vars}")
                rejected.append((it.id, reasons))
                continue
            zh = restore_entry_zh(it, zh)
            it.extra["final_zh"] = zh
            repaired_ids.add(it.id)
            stats["repaired"] += 1
        if rejected:
            self.storage.log("repair_failed", batch=batch.number,
                             missing=[i for i, _ in rejected],
                             reason=f"占位符校验失败: {rejected[0][1][:120]}")
        # 未修复成功的条目（模型漏输出/校验拒绝）：保留原文/旧译文并计入
        # failed，防止 merge_final 静默丢 key（2026-08-18 审计修复）。
        unrepaired = [it for it in missing_items if it.id not in repaired_ids]
        if unrepaired:
            self._mark_repair_failed(unrepaired, stats)
        # 修复结果并入原批的 parsed（_process_batch 使用 vresult.parsed 前已判定，这里直接写 extra）
        self.storage.log("repair_done", batch=batch.number, repaired=stats["repaired"])

    def _mark_repair_failed(self, items: List[Entry], stats: Dict[str, int]) -> None:
        """repair 失败的条目：保留原文/旧译文占位并计入 failed 统计。

        绝不写空值——translate 阶段 cur 为 None 时回退到原文（日文），
        保证 final 合并不丢 key，后续 detect/full-qa 能再次发现漏译。
        """
        for it in items:
            if not it.extra.get("final_zh"):
                it.extra["final_zh"] = it.cur or it.src
            stats["failed"] = stats.get("failed", 0) + 1

    def _handle_length(self, batch: Batch, stage: str, stats: Dict[str, int],
                       original_items: Optional[List[Entry]] = None) -> None:
        """finish_reason=length：输出被 max_tokens 截断，二分批次重发。

        与 content_filter 同策略：n<=2 无法再二分时整批 failed（保留原文，
        绝不写空值）；否则左右两半递归处理，子批 subtag 隔离文件名。
        """
        self.storage.log("length_bisect", batch=batch.number, action="bisect")
        n = len(batch.items)
        if n <= 2:
            for it in batch.items:
                it.extra["final_zh"] = it.cur or it.src  # 保留原文/旧译文，绝不写空
                stats["failed"] += 1
            self._finish_batch(batch, stats, original_items=original_items)
            return
        half = n // 2
        self._process_batch(Batch(number=batch.number, items=batch.items[:half],
                                  subtag=batch.subtag + ".l1"), stage, stats)
        self._process_batch(Batch(number=batch.number, items=batch.items[half:],
                                  subtag=batch.subtag + ".l2"), stage, stats)

    def _handle_content_filter(self, batch: Batch, stage: str, stats: Dict[str, int],
                               original_items: Optional[List[Entry]] = None) -> None:
        """content_filter：不整批重试，二分定位被过滤条目 -> blocked 标记。"""
        self.storage.log("content_filter", batch=batch.number, action="bisect")
        n = len(batch.items)
        if n <= 2:
            for it in batch.items:
                it.blocked = True
                it.extra["final_zh"] = it.cur or ""  # 保留旧译文/原文，绝不写空
                stats["failed"] += 1
            self._finish_batch(batch, stats, original_items=original_items)
            return
        half = n // 2
        self._process_batch(Batch(number=batch.number, items=batch.items[:half],
                                  subtag=batch.subtag + ".b1"), stage, stats)
        self._process_batch(Batch(number=batch.number, items=batch.items[half:],
                                  subtag=batch.subtag + ".b2"), stage, stats)

    # ======================================================================
    # Prompt 构造
    # ======================================================================
    def _build_messages(self, batch: Batch, policy_key: str,
                        history: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, str]]:
        """构造 system + user 消息。公共前缀（system）跨批保持一致以命中缓存。
        history：上下文链的 [user, assistant, ...] 历史（插在 system 后）。"""
        prompt_file = _STAGE_PROMPT[policy_key]
        prompt_path = Path(__file__).resolve().parent / "prompts" / prompt_file
        system = prompt_path.read_text(encoding="utf-8")  # 完全静态，跨批/跨 run 稳定命中缓存
        system = system.replace("{project_name}", self.project_name)

        # 术语表按批注入：放 user 消息开头（动态内容不进入 system，保持前缀稳定）
        texts = [it.masked_src or it.src for it in batch.items]
        glossary_block = self.glossary.to_prompt_block(texts)
        # 项目包世界/人物/剧情设定（放 user 保持 system 前缀稳定，repair 子批不重复注入）
        if self.world_context and policy_key != "repair":
            glossary_block += "\n【世界/人物/剧情设定】\n" + self.world_context
        # 重点句提示（双关/典故/俚语翻译要点）
        key_line_block = self._key_line_block(batch.items)
        if key_line_block:
            glossary_block += "\n" + key_line_block

        # 条目 payload：短序号数组对（序号 = 批内顺序，0 起），省 id 键名 token。
        # translate/repair: [["0", src, ctx?], ...]；polish/review: [["0", src, cur, ctx?], ...]
        # 模型输出 JSON 键必须使用对应短序号（validate 按顺序映射回 Entry.id）。
        # ctx（第 3 项）由 occurrence index 提供（--occurrence-index + --inject-context），
        # translate 初翻也注入前后句/场景上下文，提升连贯性（2026-08-09 修复：原仅 polish/review 注入）。
        if policy_key in ("translate", "repair"):
            payload = []
            for i, it in enumerate(batch.items):
                item = [str(i), it.masked_src]
                ctx = self._entry_context(it)
                if ctx:
                    item.append(ctx)
                payload.append(item)
        else:
            payload = []
            for i, it in enumerate(batch.items):
                item = [str(i), it.masked_src,
                        it.masked_cur if it.masked_cur is not None else it.cur]
                # 语境注入：occurrence index 提供的信息（speaker/scene/多语境）
                ctx = self._entry_context(it)
                if ctx:
                    item.append(ctx)
                payload.append(item)
        user = json.dumps(payload, ensure_ascii=False)
        if policy_key == "repair":
            user = "【修复任务】以下条目是上一轮翻译中缺失或校验失败的，请只翻译这些条目。\n" + user
        if glossary_block:
            user = glossary_block + "\n" + user
        return [{"role": "system", "content": system}] + self._with_history(user, history)

    def _with_history(self, user: str, history: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
        """构造 user 消息；上下文链模式下附加历史（system 后、当前 user 前）。"""
        msgs = []
        if history:
            msgs.extend(history)
        msgs.append({"role": "user", "content": user})
        return msgs

    # ---- 上下文链（跨包多轮：前一包的 user+assistant 附加到下一包请求） ----
    def _chain_tokens(self) -> int:
        """估算链历史的 token 占用。"""
        return sum(self.batcher.count_tokens(m["content"]) for m in self._chain)

    def _resolve_chain(self, batch: Batch) -> Optional[List[Dict[str, str]]]:
        """主批且链内预算充足时返回链历史；否则断链（清空）并返回 None。

        预算只算**输入侧**（历史 + 本包输入）：输出由 API 硬限（384K）兜底，
        350K 输入包输出 ≈245K 远低于硬限；链 2 包请求输入 ≈945K（350K 包）。
        """
        if not self.context_chain or batch.subtag != "" or not self._chain:
            return None
        in_tok = sum(self.batcher.count_tokens(it.masked_src or it.src)
                     for it in batch.items) + 6 * len(batch.items)
        if self._chain_tokens() + in_tok > self.chain_budget:
            self.storage.log("chain_break", batch=batch.number,
                             reason=f"链预算 {self._chain_tokens()}+{in_tok}>{self.chain_budget}")
            self._chain = []
            return None
        return list(self._chain)

    def _chain_append(self, user_msg: Dict[str, str], content: str) -> None:
        """主批请求成功后把 [user, assistant] 追加到链（超预算则断链）。"""
        if not self.context_chain:
            return
        self._chain.append(user_msg)
        self._chain.append({"role": "assistant", "content": content or ""})
        if self._chain_tokens() > self.chain_budget:
            self._chain = []

    def _key_line_block(self, items: List[Entry]) -> str:
        """批次内重点句的提示块（双关/典故/俚语翻译要点）。无则空串。"""
        if not self.key_lines:
            return ""
        notes = []
        for it in items:
            key_line = self.key_lines.get(it.src)
            if key_line and key_line.get("note"):
                notes.append(f"- 「{(it.masked_src or it.src)[:40]}」：{key_line['note']}")
        if not notes:
            return ""
        return "【重点句（含双关/典故/俚语，翻译时注意要点）】\n" + "\n".join(notes[:30])

    def _entry_context(self, entry: Entry) -> str:
        """生成条目的语境描述（有信息才注入，省 token）。

        来源：context.apply_context 填充的 speaker/scene/multi_context，
        或 ingest 阶段预填的 extra["context_desc"]。说话人在项目包人物表
        中有登记时追加性别/身份信息（防「ウィーウ(女)译成先生」类错误）。
        """
        pre = entry.extra.get("context_desc")
        if pre:
            return pre
        parts = []
        if entry.scene:
            parts.append(f"文件:{entry.scene}")
        if entry.speaker:
            speaker_note = f"说话人:{entry.speaker}"
            info = self.chars_info.get(entry.speaker)
            if info:
                extras = []
                if info.get("gender") and info["gender"] != "unknown":
                    extras.append(f"性别:{info['gender']}")
                if info.get("role"):
                    extras.append(f"身份:{info['role']}")
                if extras:
                    speaker_note += "(" + "/".join(extras) + ")"
            parts.append(speaker_note)
        if entry.multi_context:
            parts.append("多语境共用句，请用各场景均成立的中性译法")
        return "；".join(parts)


class _BatchExhausted(Exception):
    """批次重试耗尽/不可处理：整批进 failed/。"""


class _RunFatal(Exception):
    """致命错误：整个 run 暂停。"""
