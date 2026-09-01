# sr_translate — 游戏文本批量翻译/润色工具

裸调 LLM API 的批量翻译工具，用于 **RPG Maker 游戏汉化**（日→中初翻、中→中润色、QA 筛查、术语一致性控制、重点句精翻）。经 SR《Succubus Rhapsodia》与《HOLLOWWALD》实战验证：口上 16.2 万条翻译完成率 97.7%，主线全量完成，每万条约 0.7–1.2 元。

> **阅读指南**：本工具由「主控 agent」驱动（agent 负责通读原文、维护项目数据包、调用 CLI）。如果你是从零接手的新手 agent，**先读「快速上手」跑通首个任务，再按「完整工作流」进入正式翻译**；数据结构与扩展指南在文末，供深入时查阅。

---

## 一、环境与准备

| 项 | 要求 |
|---|---|
| Python | 3.11 |
| 依赖 | `requests`（翻译记忆 TM 用标准库 `sqlite3`） |
| API Key | 环境变量 `DEEPSEEK_API_KEY`，或项目根 `.env` 文件（推荐，见下） |

**配置 API Key（二选一）**：

```bash
# 方式 A：环境变量（一次性，当前终端有效）
export DEEPSEEK_API_KEY=sk-xxx

# 方式 B：项目根 .env 文件（推荐，持久化；工具启动时自动读取，不覆盖已有环境变量）
# 在 sr_translate/ 目录下创建 .env：
#   DEEPSEEK_API_KEY=sk-xxx
```

`.env` 已被 `.gitignore` 忽略，不会误提交。所有命令在 `sr_translate/` 目录下运行。

**验证连通性**：

```bash
python src/cli.py models
# 输出 {"protocol": "deepseek_chat", "models": ["deepseek-v4-flash", ...]}
```

---

## 二、快速上手（5 分钟）

假设你有一个 MTool 导出的翻译表 `ManualTransFile.json`（键=日文原文，值=现译文/原文），例如：

```json
{"こんにちは": "こんにちは", "ありがとう": "ありがとう"}
```

```bash
# 1) 一条龙：初翻 + QA 路由 + 润色/审校 + 合并
python src/cli.py pipeline --input ManualTransFile.json --out runs/r1

# 2) 查看产物
#    runs/r1/<时间戳>/batches/<stage>/  各批 input/request/response/output.json
#    runs/r1/<时间戳>/journal.jsonl     请求日志（含 token/费用）
#    runs/r1/<时间戳>/final/ManualTransFile_zh.json   合并终稿（{日文: 译文}）

# 3) 零成本 QA 筛查（不调 API，只跑规则，对终稿做风险扫描）
python src/cli.py detect --input runs/r1/<时间戳>/final/ManualTransFile_zh.json --out runs/qa1
#    输出 runs/qa1/suspects.json，按 risk_score 降序：漏翻/术语/格式/风格风险

# 4) 预算测算（翻译前跑，估算 token/费用）
python src/tools/estimate_cost.py --input ManualTransFile.json --cache-hit-rate 0.85
```

> 只翻译了 2 条？这就是一次完整的最小闭环。真实流程见下一节。
> `pipeline` 内部已含 QA 路由（低风险普通润色 / 高风险 thinking 审校）；`detect` 是对最终译文做**额外**的零成本全量风险扫描。

### GUI 与多服务商（可选）

```bash
# Windows 桌面界面（仅依赖 Python 自带 tkinter；GUI 以子进程启动 CLI，异常不影响已启动任务）
python launch_gui.py

# 换服务商：OpenAI Responses / Anthropic Messages（顶层参数在子命令前）
python src/cli.py --protocol openai_responses --model gpt-5 models --api-key sk-xxx
python src/cli.py --protocol anthropic_messages --model claude-sonnet-4-5 pipeline \
    --input ManualTransFile.json --out runs --project --api-key sk-ant-xxx
```

- GUI 的 API Key 仅传给子进程环境变量 `SR_TRANSLATE_API_KEY`，不写入项目/日志/命令行。
- 非 DeepSeek 内置模型需用 `--price-cache-hit/--price-input/--price-output` 填价格，否则费用显示"未定价"。

---

## 三、完整工作流

推荐先建**项目数据包**（把术语/世界观/重点句等结构化，质量显著更高），再翻译。

### 3.1 推荐流程：项目数据包（bundle）

```bash
# ① 创建项目包（一次；生成空 JSON 模板）
python src/cli.py new-project --name SR
#    -> projects/SR/  （含 project.json / glossary.json / world.json / chars.json /
#                      story_chain.json / policies.json / key_lines.json / talk/）

# ② 主控 agent 通读原文（MTool JSON + 口上 .rb + 事件），按 project.json 的约定填入各 JSON
#    - glossary.json   ：术语表（专名统一译法 + 禁止译法）
#    - key_lines.json  ：重点句（双关/典故/俚语，见 3.3）
#    - policies.json   ：风格策略（色气词等黑名单）
#    - world/chars/story_chain.json ：世界观/人物/剧情链路（注入 prompt 提升连贯性）

# ③ 扫描语境索引（可选但推荐：同一上下文的文本同批翻译、连贯一致）
python src/cli.py scan-talk --talk-dir E:/.../Mod_Talk --out runs/idx        # 口上 .rb
python src/cli.py scan-events --data-dir "<游戏目录>/data" --out runs/idx    # RPG Maker MZ 事件

# ④ 一条龙初翻+QA+润色+审校（加载项目包 + 语境注入）
python src/cli.py pipeline --input ManualTransFile.json --out runs/r1 \
    --bundle SR --occurrence-index runs/idx/occurrence_index.json --inject-context
#    （也可分步：先 translate 再对合并产物 detect/polish；pipeline 已内置 QA 路由）

# ⑤ 终稿零成本 QA 筛查（对 pipeline 合并终稿）
python src/cli.py detect --input runs/r1/<时间戳>/final/ManualTransFile_zh.json --out runs/qa1 --bundle SR

# ⑥ 从 suspects.json 筛出高风险条目（转成 {日文: 译文} 格式），单独润色（thinking 审校）
python src/cli.py polish --input <高风险条目.json> --out runs/polish1 \
    --bundle SR --occurrence-index runs/idx/occurrence_index.json --inject-context

# ⑦ 成本审计（真实 API usage 汇总）
python src/tools/cost_report.py runs/r1
```

**大包模式**（海量文本，正式版）：单线程顺序处理，同地图绝不拆散、跨包用上下文链衔接，`--thinking-effort high` 让初翻也开思考：

```bash
python src/cli.py --glossary-json projects/SR/glossary.json --thinking-effort high \
    --max-cost-cny 50 translate --input formal_work_ManualTransFile.json --out runs/formal_v1 \
    --occurrence-index tools/occurrence_index.json \
    --batch-max-maps 200 --batch-target-input-tokens 350000 \
    --batch-target-output-tokens 350000 --batch-hard-max-items 20000 --context-chain
```

### 3.2 标准流程（不建项目包，快速跑）

```bash
# 一条龙：初翻 + QA 路由 + 润色/审校 + 合并
python src/cli.py pipeline --input ManualTransFile.json --out runs/final

# 或分步：
python src/cli.py translate --input ManualTransFile.json --out runs/r1
#    translate 产物在各批 output.json（键为内部 id）；合并终稿用 pipeline 才会生成。
#    对已有译文做 QA 时，detect 输入需为 {日文: 译文} 格式（如原输入已含译文）。
python src/cli.py detect --input ManualTransFile.json --out runs/qa1
python src/cli.py polish --input <高风险条目.json> --out runs/polish1
```

### 3.3 重点句精翻（key_lines）

翻译中会遇到大量**双关、典故、俚语**，普通批翻容易翻错。`projects/<游戏名>/key_lines.json` 约定两条通道：

```json
{
  "原文句子1": {
    "note": "双关：XX 既指 A 又指 B，中文需保留双关",
    "mode": "agent",
    "translation": "主控 agent 通读原文后直接给出的定稿译文"
  },
  "原文句子2": {
    "note": "勇战 RPG 联动官方译名，注意人名",
    "mode": "max"
  }
}
```

- `"mode": "agent"`：主控 agent 已在**完整上下文**下把译文写好。工具翻译时命中即**直接采用，不再调模型**（省 token 且保证准确）。
- `"mode": "max"`：工具对该句所在的批**强制最高思考强度（max）翻译**，并把 `note` 作为提示注入 prompt。

### 3.3b 双关回归库（pun_manifest，2026-08-13 新增）

双关/俚语靠主控一次性通读会漏（HOLLOWWALD 木板「いた。」在体验版修过、正式版复发）。现在用**持久化回归库** `projects/<游戏名>/pun_manifest.json` 积累：

```bash
# 用高思考模型批量筛查原文中的双关/俚语候选（输出清单供人工确认）
python src/cli.py scan-puns --input ManualTransFile.json \
    --occurrence-index runs/events/occurrence_index.json --out runs/puns

# 人工确认后逐条入库（confirmed 的译法会被翻译时直接复用，防复发）
python src/cli.py pun-add --bundle SR --key "いた。" --type pun \
    --note "板/居た双关，画面是两块木板" --translation "木板。" \
    --status confirmed --source game_test

# 翻译产物交付前对照回归库（已确认译法被改/复发 -> 报告）
python src/cli.py pun-check --bundle SR --input final/ManualTransFile_zh.json
python src/cli.py pun-list --bundle SR    # 列出全部条目
```

- `--status confirmed`：译法已定稿，翻译时并入 key_lines（mode=agent）直接复用
- `--status pending`：仅注入翻译要点提示，待人工确认
- `--source`：demo / full / proofread / game_test（记录发现来源）

### 3.4 译名统一（apply-rename）

历史遗留错译（如同一角色 6 种译法）需要批量修正。工具按项目包 glossary 的 `forbidden_variants`（禁止译法）批量替换为正确 `target`，**先备份**：

```bash
# 先 dry-run 预览（不写文件，看会改哪些）
python src/cli.py apply-rename --input SR1028.json \
    --glossary-json projects/SR/glossary.json --dry-run

# 确认后执行（自动备份到 runs/rename_backup_<时间>/）
python src/cli.py apply-rename --input SR1028.json --glossary-json projects/SR/glossary.json
# 也可处理口上目录：
python src/cli.py apply-rename --talk-dir E:/.../Mod_Talk --glossary-json projects/SR/glossary.json
# 或直接给映射（不用 glossary）：--mapping "基尔冈:吉尔贡,吉尔刚:吉尔贡"
```

### 3.5 合并前质量闸门（2026-08-13 新增，强烈建议执行）

HOLLOWWALD 正式版教训：合并后才发现错位 8,792 条、漏译 1,241 条、旧文本
残留反复出现，最终只能整段重翻。现要求**替换游戏文件前依次跑**：

```bash
# ① 全量自动验证（零成本、秒级）：漏译/假名残留/双反斜杠/术语变体/
#    相邻同值串位/换行拼接/引擎键误译/黑名单。问题率超阈值退出码 1 阻止合并
python src/cli.py full-qa --input final/ManualTransFile_zh.json \
    --bundle SR --max-issue-rate 0.03 --out runs/qa_final

# ② 合并产物 vs 基准逐键校验：旧文本残留（新值包含旧值全文）/
#    换行拼接/删除键异常
python src/cli.py verify-merge --input final/ManualTransFile_zh.json \
    --base 旧版本文件.json --out runs/vm

# ③ 双关回归库对照（见 3.3b）
python src/cli.py pun-check --bundle SR --input final/ManualTransFile_zh.json

# ④ 模型全量审查 / 随机抽查（问题率 <1% 为通过线）
python src/cli.py proofread --input final/ManualTransFile_zh.json \
    --bundle SR --out runs/pr --shard 0/6   # 子代理并行各跑一片
python src/cli.py sample-check --input final/ManualTransFile_zh.json \
    --bundle SR --n 500 --seed 42 --out runs/sc
```

### 3.5 口上漏翻审计

口上 `.rb` 文件若出现日文残留（汉化不完整），扫描时顺带审计：

```bash
python src/cli.py scan-talk --talk-dir E:/.../Mod_Talk --check-missing --out runs/qa
# 输出 runs/qa/missing_talk.json：仍含日文假名的文本 + 位置（文件/行/方法/说话人）
```

> 局限：纯日文汉字句（无假名、与中文同形）无法从文本本身区分，不在本审计内，需人工/项目包语境核查。

---

## 四、子命令参考

| 命令 | 用途 | 核心参数 |
|---|---|---|
| `translate` | 日→中初翻（非思考 t0.25） | `--input` `--out` `--bundle` |
| `polish` | 中→中润色（QA 路由，高风险 thinking） | `--input` `--out` `--bundle` |
| `pipeline` | 初翻+QA+润色+审校一条龙 | `--input` `--out` `--bundle` |
| `detect` | 零成本 QA 筛查 → `suspects.json` | `--input` `--out` `--bundle` |
| `import-corpus` | 历史语料导入 TM | `--legacy` `--out` |
| `scan-talk` | 口上 .rb 扫描建索引（+`--check-missing` 漏翻审计） | `--talk-dir` `--out` |
| `scan-events` | RPG Maker MZ 事件扫描建索引 | `--data-dir` `--out` |
| `seq-align` | 事件文本与翻译表对齐（模拟 MTool 键规则，查漏译） | `--input` `--data-dir` `--out` |
| `misalign` | 键值错位检测（角色名互斥+场景复核块） | `--input` `--glossary-json` `--occurrence-index` |
| `apply-rename` | 按术语表禁止译法批量统一译名（先备份） | `--input`/`--talk-dir` `--glossary-json` |
| `new-project` | 创建项目数据包骨架（空 JSON 模板） | `--name` |
| `smoke` | 冒烟测试（批次规模对比，`--dry-run` 无 key） | `--sample` `--out` |
| `models` | 拉取服务商可用模型 | `--api-key` |

**顶层参数**（写在子命令**之前**）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--protocol` | `deepseek_chat` | `deepseek_chat` / `openai_responses` / `anthropic_messages` |
| `--model` | `deepseek-v4-flash` | 白名单仅 flash（pro 预览版代码层禁止） |
| `--thinking-effort` | `auto` | `none/minimal/low/medium/high/xhigh/max/ultra`；`high`=全量剧情开思考 |
| `--max-cost-cny` | 80 | 单 run 预算硬上限，超限自动暂停 |
| `--max-stage-cost` | 25 | 单阶段（初翻/润色）预算上限 |
| `--workers N` | 1 | 分片并行（仅 translate/polish；与 `--project`/`--resume`/pipeline 冲突） |
| `--price-cache-hit`/`--price-input`/`--price-output` | 0 | 自定义价格（元/百万 token），非内置模型必填否则费用显示"未定价" |
| `--base-url` | 空 | 其他兼容服务商 |

**子命令级参数**：`--input`/`--out`/`--bundle`/`--glossary`/`--glossary-json`/`--resume`/`--source-tag`/`--occurrence-index`/`--inject-context`/`--batch-*`/`--context-chain`/`--dry-run` 等（放在子命令后）。

---

## 五、项目数据包（bundle）详解

### 5.1 目录结构

```
projects/<游戏名>/            # 每个游戏一个独立数据包（主控 agent 维护）
├── project.json              # 元数据 + 各文件结构约定（new-project 自动生成）
├── glossary.json             # 术语表（见下）
├── world.json                # 世界观：势力/地名/系统
├── chars.json                # 人物表：说话人/称谓/身份/关系
├── story_chain.json          # 剧情链路：章节/关键事件
├── policies.json             # 风格策略（黑名单等）
├── key_lines.json            # 重点翻译条目（双关/典故/俚语）
└── talk/                     # 口上数据（结构化或源 .rb 副本）
```

`new-project` 生成的是**空骨架**。主控 agent **通读原文后自行填入**——这是刻意设计：由 agent 在完整上下文下整理数据，避免"再调一组模型去解构"造成的上下文割裂与额外 token 消耗。

### 5.2 各文件约定

**glossary.json**（术语表，数组）：
```json
[
  {"source": "ギルゴーン", "target": "吉尔贡", "priority": "high",
   "scope": "global", "notes": "勇战RPG联动官方译名", "forbidden_variants": ["吉尔冈", "基尔冈"]}
]
```
- `source` 日文原文，`target` 规范中文，`forbidden_variants` 禁止译法（`apply-rename` 按它替换、QA 检测它）。
- 工具加载时**自动附加内置常见术语**（`glossary.py` 的 `_extra_entries`）作兜底；显式 `--glossary-json` 传入时**覆盖**同 source 的项目包术语。

**key_lines.json**（重点句，见 3.3）：
```json
{"原文": {"note": "翻译要点", "mode": "agent|max", "translation": "mode=agent 时预填译文"}}
```

**policies.json**（风格策略）：
```json
{"forbidden_words": ["饥渴", "淫靡"], "style_notes": ["保持原著语气，不添加色气修饰"]}
```
- `forbidden_words`：模型自加色气词等黑名单，QA 检测译文命中即报"疑似自加词"（词条由 agent 维护，工具不内置）。

**world.json / chars.json / story_chain.json**：结构化设定，加载后拼成文本注入 prompt（user 消息，保持 system 前缀稳定以命中缓存）。

### 5.3 加载方式

```bash
python src/cli.py translate --input ... --out ... --bundle SR
# 启动时打印：项目包: SR（术语 N 条 / 重点句 N 条 / 黑名单 N 词）
```

---

## 六、质量检查器（QA）

`detect` 与 pipeline 阶段 2 会对译文跑全套检查，按风险分排序：

| 检查器 | 检测项 | 对应会话事故 |
|---|---|---|
| format | 假名残留、日文标点、长度异常、添油加醋（强程度词）、**日文汉字残留漏翻**（译文字符重叠）、**黑名单自加词** | 口上 531 条漏翻、色气词 |
| terminology | 术语 source 出现但 target 未用 / 用了 forbidden_variants | 吉尔贡 6 译法 |
| style（批次级） | 泛化译文、称谓漂移、**片假名专名译名漂移**（同一专名多译法） | 汉化名不一致 |
| refusal | 模型拒答/净化/成人内容淡化 | 成人内容被过滤 |

阈值：`risk_score >= 1.0` 进 thinking low 审校；`>= 2.5` 进 thinking high 审校（`qa/__init__.py`）。

---

## 七、容错与保护（默认内置）

- **占位符**：只遮 `\C[n]`/`%s`/`<tag>`；`{xxx}`/「」/`\H`/`\n` 不遮但做文本级校验（缺失即拦截+repair）
- **finish_reason 分支**：`length` 自动二分 / `content_filter` 二分定位+blocked / `insufficient_system_resource` 退避重试
- **错误分类**：400=参数错误(终止)、401/403=认证失败(终止，但 **403+Cloudflare 1010 风控判为可重试**)、402=余额不足(终止)、429=限流(退避)、5xx=服务端(退避)
- **Cloudflare 风控**：所有 provider 会话默认带浏览器 User-Agent，规避 1010 拦截
- **熔断器**：连续 5 批失败自动暂停；缓存未命中率 >90% 或思考占比 >60% 告警
- **TM 双表**：`translation_corpus`（全量）+ `translation_memory`（仅可复用），防旧机翻污染
- **断点续跑**：`--resume` 跳过已完成批次
- **预算硬限制**：`--max-cost-cny` 超限自动暂停

---

## 八、模块地图（开发者/深入调试）

> 每个模块顶部有职责 docstring；**依赖规则：只允许向下依赖**（schemas/config 是地基）。

```
cli.py          命令行入口：11 个子命令 + 流程编排
  └─ engine.py  批处理主循环：mask→key_line预填→TM命中→请求→校验→修复→QA路由→落盘
       ├─ bundle.py        项目数据包（new-project 骨架 / --bundle 加载 glossary/世界观/重点句/黑名单）
       ├─ providers/  DeepSeek Chat / OpenAI Responses / Anthropic Messages 适配层
       ├─ thinking.py            思考强度目录与跨模型映射
       ├─ project.py            项目级 TM、SQLite 用量账本与实时快照（运行时，与 bundle 解耦）
       ├─ batcher.py            token 预算分批 + 语义分组
       ├─ protect.py            占位符遮罩/恢复/校验
       ├─ context.py / rmmz.py  口上 .rb / RPG Maker MZ 事件扫描（occurrence index）
       ├─ tm.py                 SQLite 翻译记忆
       ├─ validate.py           输出解析/键数校验/模板变量校验
       ├─ qa/                   风险检查器：format/terminology/style/refusal
       ├─ storage.py            事务式批次存储
       └─ config.py / schemas.py ← 全局地基（价格表/MODE_POLICY/PROJECTS_ROOT/Entry/Batch）
smoke.py        冒烟测试
gui/            可选 Tk 界面
tools/          cost_report.py / estimate_cost.py / send_qq_text.py
```

### 核心数据结构（schemas.py）

- `Entry`：单条文本。`key`(原文)/`cur`(现译文)/`masked_src`/`status`/`speaker`/`scene`/`risk_score`/`extra`
- `Batch`：一批 Entry + `subtag`（repair/二分/重试文件名隔离）
- `Usage`：API 用量 → `cost_cny()`
- 状态枚举：`EntryStatus`(7类) / `TmStatus`(6级) / `PlaceholderKind`(FIXED/MOVABLE/PAIRED/QUOTE)

### 数据流（Entry 生命周期，pipeline 模式）

```
输入 JSON → ingest.py（分类）
  → filter（只留该阶段对象）
  → batcher（token 预算切批）
  → engine._process_batch：
      mask（protect）
      → key_line agent 预填译文直接采用（不进 API）
      → TM lookup（命中直接复用）
      → 请求（MODE_POLICY 决定 thinking/温度；含 max 重点句的批强制 max）
      → finish_reason 分支
      → validate → missing → repair
      → restore → QA（risk_score）
      → 高风险 → review（thinking）审校
  → storage（逐批落盘）→ merge_final（原子发布 final/）
```

---

## 九、设计决策记录（ADR）

| # | 决策 | 原因 |
|---|---|---|
| 1 | 占位符只遮 `\C[n]`/`%s`/`<tag>`，不遮 `{xxx}`/引号/`\H`/`\n` | 实测长随机占位符被模型当噪声丢弃（拦截率 94%）；清晰语法模型原生保留率 100% |
| 2 | 模板变量文本级校验（不遮罩但校验缺失） | `{myname}` 丢失不可接受；缺失 → 拦截+repair |
| 3 | `\H` 缺失自动补回句尾 | 口上演出标记，无参数单字符，句尾追加语义安全 |
| 4 | TM 双表 | 防旧机翻/未审校译文污染新译文；SQL 状态过滤防"新遮蔽老" |
| 5 | 批次按 token 预算而非条数 | 菜单项和剧情 token 量差 10 倍；冒烟实测 200 条/批为平衡点 |
| 6 | storage stage 隔离 + subtag | 同 run 多阶段批号冲突；subtag 隔离 repair/二分 |
| 7 | 引号「」允许译为中文“”或保留 | 模型本地化行为，只要求"存在且成对" |
| 8 | 初翻/润色非思考 + 低温度 | 思考 token 占输出 84-95%（成本 5-11 倍）；低温度防术语漂移 |
| 9 | **项目数据包由主控 agent 手工维护，而非模型批量解构** | 多模型解构上下文不互通、效果差且多耗 token；agent 通读原文后自行写入，上下文齐全不割裂 |
| 10 | **key_lines 双通道（agent 预填 / max 精翻）** | 双关/典故/俚语需完整上下文；agent 预填最准，max 通道兜底 |
| 11 | **UA 伪装 + 403 风控细分** | 默认端点走 Cloudflare，python-requests UA 被 1010 拦截误判为 key 失效 |

---

## 十、实测数据（2026-08-02，真实 API）

- 口上 162,588 条完成 158,879 条（97.7%）；主线全量完成
- 有效成本 38.15 元；每万条约 0.7–1.2 元（非思考，缓存命中后）
- 占位符重构后拦截率 94% → 2%；QA 日式标点误报 94,833 条已修正
- 交付：`runs/work/final_talk_zh.json` + `final_main_zh.json`

---

## 十一、常见问题（FAQ）

**Q：报错 `缺少 API key`**
A：设置环境变量 `DEEPSEEK_API_KEY`，或在项目根创建 `.env` 写 `DEEPSEEK_API_KEY=sk-xxx`。

**Q：报错 403**
A：403 + 响应含 `Cloudflare`/`1010` 是风控，工具已自动退避重试（UA 已伪装）；403 无这些特征才是 key 失效。确认 key 正确、额度充足。

**Q：顶层参数报错**
A：`--max-cost-cny`/`--model`/`--workers` 等顶层参数必须写在子命令**之前**；`--input`/`--bundle` 等写在子命令**之后**。

**Q：`--workers` 与 `--project`/`--resume`/`pipeline` 冲突**
A：去掉其一（worker 均新 run 目录，resume 无意义）。

**Q：非 DeepSeek 模型费用显示"未定价"**
A：用 `--price-cache-hit`/`--price-input`/`--price-output` 填价格。

**Q：中断后如何续跑**
A：同一 `--out` 加 `--resume`；每批不可变落盘，跳过已完成批次。

**Q：如何让 QA 认识新的"自加词"**
A：在项目包 `projects/<名>/policies.json` 的 `forbidden_words` 里加词（agent 维护）。

**Q：`projects/` 目录里那些 `-哈希` 后缀的目录是什么**
A：`--project` 运行时生成的存储（TM/账本/快照），与 `projects/<游戏名>/`（agent 维护的数据包）是两回事，互不影响。

---

## 目录

- `ARCHITECTURE.md` — 架构总览、数据流图、模块依赖、状态枚举、已知限制
- `README.md` — 本文件：上手指南 + 子命令参考 + 数据结构 + ADR
- `src/` — 工具源码
- `tests/` — 单元测试（`python -m unittest discover -s tests`，无需 API key）
- `runs/` — 运行产物（批次/报告/快照/交付文件）
- `projects/` — 项目数据包（每游戏一个子目录）与运行时项目存储
