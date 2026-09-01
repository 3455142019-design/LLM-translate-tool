# sr_translate 架构总览

> 新会话接手本项目：**先读本文件**，再看 README.md，然后按需进入具体模块。
> 更新日期：2026-08-13。修改任何模块时同步更新本文件。

## 1. 数据流（pipeline 模式）

```
projects/<游戏名>/            项目数据包（bundle.py，主控 agent 通读原文后填写）
  glossary.json / key_lines.json / policies.json / world/chars/story_chain.json
  pun_manifest.json（双关回归库，pun-add/pun-check 维护，翻译时并入 key_lines）
        │   --bundle <名> 加载：术语表合并 + 重点句 + 黑名单 + 世界设定注入
        │   角色性别映射 chars_map（说话人称谓防错，如女性不译「先生」）
        ▼
ManualTransFile.json (MTool 键值对: 日文原文 -> 中文译文)
        │
        ▼
[scan-events] RPG Maker MZ data/ 事件解析(rmmz.py) -> occurrence index
        │     或 [scan-talk] 口上 .rb 解析(context.py) -> occurrence index
        │     事件页=场景单元；apply_context 填充 scene/speaker/multi_context
        ▼
[ingest.py]   读取 + 重复键检测(object_pairs_hook) + 状态分类(7类)
        │     含日文汉字残留漏翻判定（字符重叠 -> MIXED_LANGUAGE）
        │     输出: items[] (Entry 对象)
        ▼
[protect.py]  占位符三分类遮罩 (固定/可移动变量/成对嵌套 -> 唯一 ID)
        │     输出: items[] 带 masked_text
        ▼
[engine.py]   key_line agent 预填译文直接采用（mode=agent 且有 translation，不进 API）
        │     -> translation_memory 精确匹配命中 -> 直接复用, 未命中继续
        ▼
[batcher.py]  token 预算分批 (in 8-16K / out 4-10K / soft 150-250 / hard 600)
        │     按 key 语义分组: 地图/事件/文本类型优先同批
        ▼
[engine.py]   短序号批协议: 输入 [["0", src], ...]（polish 带 cur/ctx），
        │     模型输出 {"0": 译文} 按批内顺序映射回 id；旧长 id 兼容
        │     system 完全静态 + 术语表/世界设定/重点句提示放 user（缓存友好）
        │     {project_name} 占位符按 bundle 名替换（多项目共用工具防串名）
        │     repair 复用 translate system；含 max 重点句的批强制 thinking max
        ▼
[providers/]             DeepSeek Chat、OpenAI Responses、Anthropic Messages
        │                 请求适配 + usage 解析；thinking.py 负责强度映射
        │                 浏览器 UA 伪装（Cloudflare 1010 规避）；403 风控细分可重试
        ▼
[validate.py] JSON 解析(部分恢复容错) / 键数 / 占位符校验 / 漏项局部修复 (只重发缺失条目)
        │     **错位检测**（_detect_misplaced）：译文遮罩 token 与自身原文
        │     交集不足一半、与批内另一条原文匹配 -> misplaced 送 repair 重译
        │     （2026-08-10 HOLLOWWALD 教训：8,792 条错位只能整段重翻）
        │     响应完整落盘(含 usage)；journal 每请求 api_usage 事件；cost_report.py 审计
        ▼
[qa/]         format(假名残留/标点/长度/日文汉字残留/黑名单自加词) + terminology(术语)
        │       + style(风格/片假名专名译名漂移) + refusal(成人内容拒绝/净化) → risk_score
        ▼
[engine.py]   路由: 低风险 -> polish(非思考) / 高风险 -> review(thinking)
        │     熔断器 + 预算限制 + system_fingerprint 漂移监控
        ▼
[storage.py]  runs/<run_id>/ 逐批不可变落盘 + journal + 原子合并
        │     snapshot/ 快照(prompt/术语表/配置, 恢复时校验 hash)
        │     merge_final: key 级精确合并 + 备份(final/backups/) + diff 报告
        ▼
        final/ManualTransFile_zh.json  (确定性合并, 用户确认后自行替换游戏文件)

[project.py]  可选项目模式（--project）：项目级 TM、SQLite 用量账本、live_usage.json 实时快照
              （运行时存储 runs/projects/<名>-<哈希>/，与 projects/<游戏名>/ 数据包解耦）
```

## 1b. 合并前质量闸门（2026-08-13 新增，教训驱动）

HOLLOWWALD 正式版教训：合并后才发现错位/漏译/残留，只能整段重翻。
现要求**合并前必须跑两道闸门**（零成本、秒级）：

```
final/ManualTransFile_zh.json
        │
[full-qa]     全量验证器（qa/full_qa.py）：漏译 / 假名残留 / 汉字截断残留 /
        │     双反斜杠 / 术语缺失+音近变体 / 相邻同值串位 / 换行拼接 /
        │     引擎键误译 / 黑名单词；--max-issue-rate 超限退出码 1 阻止合并
        ▼
[verify-merge] 合并产物 vs 基准逐键校验（verify_merge.py）：
        │     旧文本残留（新值包含旧值全文）/ 换行结构突变 / 删除键异常
        ▼
[pun-check]   双关回归库对照：已确认译法被改/复发 -> 报告
        ▼
[proofread / sample-check]  模型全量审查 / 随机抽查（问题率 <1% 为通过线，
        │     2026-08-11 用户约定）；只写 issues 报告，绝不改输入文件
        ▼
     全部通过 -> 替换游戏文件
```

## 2. 模块依赖关系（只允许向下依赖）

```
cli.py
  │  --workers N 分片并行: 条目轮转分片 -> 子进程 worker(独立 run 目录)
  │  -> 合并 final + journal api_usage 成本汇总（pipeline 不支持）
  │  新命令实现: verify_merge.py / pun.py / scan_puns.py / proofread.py / qa/full_qa.py
  └─ engine.py
       ├─ bundle.py ────────────────→ glossary.py (项目数据包加载/合并)
       │    ├─ pun.py（pun_manifest 回归库 -> merged_key_lines）
       │    └─ chars_map（角色性别映射，engine 注入说话人语境）
       ├─ providers/ (DeepSeek Chat / OpenAI Responses / Anthropic Messages)
       ├─ thinking.py
       ├─ project.py
       ├─ batcher.py ──────────────→ tokenizers (Rust)
       ├─ protect.py
       ├─ rmmz.py ─────────────────→ context.py (OccurrenceIndex 复用)
       ├─ tm.py ───────────────────→ sqlite3 (标准库)
       ├─ validate.py（含批内错位检测）
       ├─ qa/ (format/terminology/style/refusal/full_qa)
       ├─ storage.py ──────────────→ snapshot 快照
       └─ config.py / schemas.py ←─ 全局基础(所有模块可引用)
```

**规则**：`schemas.py` 和 `config.py` 是地基，任何模块可引用；其余模块只允许引用地基和它上面的模块，禁止循环引用。

## 3. 核心数据结构（schemas.py）

- `Entry`：单条文本。字段：`id`(批次内唯一) / `key`(MTool 原始键=日文原文) / `src` / `cur`(现译文, 润色用) / `masked_src` / `masked_cur` / `status` / `speaker` / `scene` / `occurrences` / `risk_score` / `tm_status`
- `Batch`：一批 Entry + 批号 + 预算记录
- `Usage`：一次 API 调用的 token 明细（prompt_hit/miss/completion/reasoning）
- `RunMeta`：run 级元数据（模型/模式/快照 hash/指纹）

## 4. 状态枚举（关键值）

- 条目状态（ingest 分类）：`untranslated / mixed_language / human_translation / machine_translation / do_not_translate / script_or_control_data / empty`
- TM 状态（tm.py）：`human_approved / qa_passed / machine_unreviewed / machine_legacy / existing_unknown / rejected`
- 占位符类别（protect.py）：`fixed / movable / paired`
- finish_reason 分支（engine.py）：`stop / length(二分) / content_filter(二分定位+blocked) / insufficient_system_resource(退避重试)`
- API 错误分类（providers/deepseek.py ApiErrorKind）：`bad_request / auth / quota / rate_limit / server / network_unsent / network_uncertain / timeout / empty_response / json_invalid / cloudflare_block`（403+Cloudflare 1010 判为可重试风控，非 AUTH 终止）

## 5. 关键配置（config.py）

- `PRICING`：价格表 + `pricing_version`；`peak_pricing=False`（未实施）
- 默认模型为 `deepseek-v4-flash`；CLI 可用 `--protocol` / `--model` / `--base-url` 选择其他服务商
- `MODE_POLICY`：translate/polish/review_ambiguous/review_hard 的 thinking+temperature
- `BATCH_LIMITS`：target_in 8-16K / target_out 4-10K / soft_max 150-250 / hard_max 600
- `HTTP_USER_AGENT`：浏览器 UA（所有 provider 会话默认携带，规避 Cloudflare 1010）
- `PROJECTS_ROOT`：项目数据包根目录（`sr_translate/projects/<游戏名>/`，bundle.py 使用）
- `load_dotenv()`：启动时读取项目根 `.env`（API key 持久化，不覆盖已有环境变量）

## 6. 运行目录约定（storage.py）

```
runs/<run_id=YYYYMMDD_HHMMSS>/
├── manifest.json          # run 元数据 + 全部批次状态
├── journal.jsonl          # 追加式事件日志(每批/每次请求一行)
├── batches/<stage>/       # stage 隔离: translate/polish/review_hard/review_ambiguous
│   └── 000001.{input,request,response.raw,output,meta}.json
├── failed/<stage>/        # 重试耗尽/被过滤的批次
├── snapshot/              # prompt/术语表/配置快照(校验 hash)
└── final/                 # 全批完成后确定性合并产物
```

项目模式使用 `runs/projects/<名称>-<输入路径哈希>/`，其中包含独立 `tm.db`、`runs/`、
`project.sqlite` 和供 GUI 轮询的 `live_usage.json`。GUI 只负责启动 CLI 子进程和读取快照，
因此 GUI 进程异常不会影响正在执行的翻译任务。

**区分两个 `projects/` 概念（易混淆）**：

| 目录 | 归属 | 内容 | 谁维护 |
|---|---|---|---|
| `projects/<游戏名>/` | 项目**数据包**（bundle.py） | glossary/world/chars/story_chain/policies/key_lines/talk | 主控 agent（`new-project` 生成骨架后通读原文填写） |
| `runs/projects/<名>-<哈希>/` | 项目**运行时**（project.py） | tm.db / project.sqlite / live_usage.json / runs | 工具自动生成 |

两者解耦：数据包是输入（术语/设定），运行时是产物（账本/记忆）。`--bundle <游戏名>` 加载数据包；`--project` 启用运行时项目。

## 7. 已知限制与实测数据（2026-08-08）

- **口上 occurrence index**：扫描 Mod/Mod_Talk 全量结果 = 170,747 唯一文本，
  **47,716 个（27.9%）多语境冲突**（同一句被多角色/场景共用）——中性译法策略必要。
  索引文件 ~595MB（紧凑 JSON）；加载约 10-20 秒，仅 ingest 阶段用一次。
- **口上漏翻审计（scan-talk --check-missing）只抓含假名的日文残留**：纯日文汉字句
  （无假名、与中文同形）无法从文本本身区分，需项目包语境/人工核查。
- **片假名专名译名漂移检测是启发式**：按"同一专名各译文最长公共子串 <3"判定，
  对 2 字译名可能误报（低权重"疑似"，agent 人工核对即可）。
- **speaker 提取**：当前为行级启发式（speaker/voice/立绘调用），提取不到标 speaker_unknown。
- **tokenizer**：DeepSeek-V4 官方 tokenizer 未公开，用 V3 同系（误差 ±10% 内）。
- **providers 包名冲突**：src/providers 必须保留 __init__.py（与 Hermes 内置
  providers 同名，namespace 包会被遮蔽）。
- **Cloudflare 风控**：默认端点 opencode.ai/zen/go 走 Cloudflare，必须带浏览器 UA；
  403 + code 1010 已分类为可重试风控（否则被误判 key 失效终止 run）。

## 8. 冒烟测试验收标准（开发完成后执行）

1. 批次规模 4 组（50/100/200/400 条）对比：漏项率/空响应率/占位符破坏率/延迟/成本
2. 润色三路盲选（非思考 vs thinking low vs thinking high，100 条混合编号交人工）
3. 样本：口上 16 万条随机 600 条 + 主线剩余 364 条
4. 全部通过后才允许全量运行
