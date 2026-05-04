# Ombre Brain — 内部开发文档 / INTERNALS

> **第一人称原则（全局）**：Ombre Brain 的使用者是**模型自己**，不是用户。所有提示词、docstring、注释、前端说明文字必须用第一人称（"我的记忆"/"我记得"/"我感受到"），禁止"用户的记忆""帮用户记住""为用户存储"等第三人称表述。本规则覆盖 server.py 工具 docstring、CLAUDE_PROMPT.md、dashboard 文案、ENV_VARS.md 描述。改任何一段面向模型的文字前先回头看这条。
>
> 本文档面向开发者和维护者。合并自原 INTERNALS.md（系统怎么运作）+ BEHAVIOR_SPEC.md（行为应该符合什么规格）。
>
> **阅读约定**：每个模块/功能块分两层。
>
> - **上层（人话）**：这一块在干什么、边界在哪、当前实现到了哪一步、关键硬编码值。
> - **下层（括号内，给改代码的人看）**：实现约束、依赖关系、改动注意事项、踩过的坑。
>
> 文档以**当前代码为准**。未实现的设想统一放在末尾「未来设想」一节，不与现状混写。

---

## 目录

0. 功能总览
1. 模块结构与依赖
2. 数据流与生命周期
3. MCP 工具规格
4. REST API 与 Dashboard
5. 衰减与评分公式
6. 桶类型矩阵
7. 配置与环境变量
8. 硬编码值清单
9. 降级行为表
10. 已修复 Bug 记录（B-01 至 B-10）
11. Debug 快速索引（症状 → 文件 + 函数）
12. 已知用户向反逻辑点
13. 未来设想（依赖上游 hook 才能落地）

---

## 0. 功能总览

Ombre Brain 是一套给 LLM 用的长期情绪记忆系统。它的边界是「时间里发生的事」，不是「你是谁」（身份层交给官方记忆）。每条记忆 = 一个 Markdown 文件（YAML frontmatter + 正文），原生兼容 Obsidian 浏览/编辑。

记忆按桶类型分目录存放：`dynamic/`（普通，会衰减）、`permanent/`（钉选/固化，importance=10、不衰减）、`feel/`（模型自省，固定分 50，永不浮现到普通 breath）、`plans/active/`（待办，固定分 50，不衰减不浮现）、`letters/history/`（信件，原文永久保留，不参与压缩/合并/衰减）、`archive/`（已淘汰）。

检索三通道并联：rapidfuzz 模糊匹配（关键词层）+ 余弦相似度（向量层）+ 衰减分排序（浮现层）。情感坐标用 Russell 环形模型的 `valence`/`arousal` 双连续维度，不用离散标签。

> **三通道职责澄清（refactor-2.0 后）**：
> - **召回阶段**：rapidfuzz 关键词命中 + 元数据过滤（domain/tags/importance_min）共同决定候选池；
> - **打分阶段**：embedding 余弦相似度只作为**得分维度之一**乘进 `bucket_manager._score_bucket()`，不会单独触发召回；
> - **排序阶段**：`decay_engine.calculate_score()` 给出最终衰减分，与上面两个分数加权汇总后排序。
> 也就是说"并联"指的是「三种信号同时进入打分」，不是「三个独立的搜索引擎」。embedding 关闭时仅打分缺一维，召回不受影响。

(开发者侧：所有桶都通过 `bucket_manager.list_all()` 递归遍历目录加载；没有数据库索引，全靠目录扫描。规模 < 几千桶时 OK，再大需要重新设计。)

---

## 1. 模块结构与依赖

### 1.0 仓库布局（重构后）

```
Ombre-Brain/
├── src/                # 所有运行期 Python 源码（server.py / bucket_manager / dehydrator / ...）
├── tools/              # CLI 一次性脚本：backfill / migrate / reclassify / check_*
├── tests/              # pytest 测试套件（unit / integration / regression）
├── docs/               # INTERNALS / BEHAVIOR_SPEC / ENV_VARS / CLAUDE_PROMPT
├── frontend/           # dashboard.html
├── deploy/             # docker-compose.yml / docker-compose.user.yml
├── Dockerfile          # 根目录保留（平台自动识别）
├── render.yaml         # 根目录保留（Render 自动识别）
├── zbpack.json         # 根目录保留（Zeabur 自动识别）
├── requirements.txt    # 根目录保留（pip 标准位置）
├── config.example.yaml / config.yaml
├── README.md / LICENSE / rule.md
└── .env                # 不进 git
```

入口固定为 `python src/server.py`。`utils.load_config()` 自动按
`$OMBRE_CONFIG_PATH` → `cwd/config.yaml` → `<repo_root>/config.yaml` 的顺序查找配置。

```
                    ┌──────────────┐
                    │  src/server.py │  MCP 入口（薄封装）+ Dashboard HTTP 路由 + 起服装配
                    └─────┬───────┘
                          │  注入 _runtime
                          ▼
                  ┌─────────────────────────────────────────────┐
                  │ src/tools/ 业务包（一个工具一个子包，多分支拆文件） │
                  │   breath/ · hold/ · grow/ · dream/                    │
                  │   trace/ · anchor/ · plan/                              │
                  │   _runtime.py · _common.py                              │
                  └───────────┬───────────────────────────────┘
           ┌───────────────┼───────────────┬────────────────────┐
           ▼               ▼               ▼                    ▼
   bucket_manager   decay_engine    dehydrator         embedding_engine
   桶 CRUD+搜索     遗忘曲线         脱水/打标/合并    向量化+余弦检索
           │               │               │                    │
           └───────┬───────┴───────────────┴────────────────────┘
                   ▼
              utils.py    (config / 日志 / ID / 路径安全 / token 估算)

   import_memory.py  历史对话导入引擎，独立模块
```

### 模块职责一览

每个模块「干什么、边界在哪、依赖谁」：

- **server.py**（约 3441 行）— MCP 服务入口。创建所有组件后调 `tools._runtime.init(...)` 注入依赖；以 `@mcp.tool()` 注册 11 个薄封装（每个 ≤ 10 行，只转发到 `tools/<名字>/`）；还负责所有 Dashboard HTTP 路由（`@mcp.custom_route`）、cookie/CSRF/限流/Webhook/SSE/heartbeat 这类走 HTTP 的事。不写业务逻辑。
- **tools/**（拆分后的应用层）— 详见下面「1.x tools/ 包结构」。
- **bucket_manager.py** — 桶 CRUD + 多维加权搜索 + `touch()` 激活刷新 + `_time_ripple()` 时间涟漪 + 文件搬运（archive/permanent 之间）。
- **decay_engine.py** — `calculate_score(metadata)` 单桶活跃度评分；`run_decay_cycle()` 周期扫描 → auto-resolve / archive；后台 asyncio 循环。
- **dehydrator.py** — 通过 OpenAI 兼容 LLM API 做四件事：`analyze()` 自动打标、`merge()` 内容融合、`digest()` 日记拆分、`dehydrate()` 摘要压缩；外加 `judge_plan_resolution()` 给 plan 自动结案做 LLM 双判。带 SQLite 缓存避免重复 API 调用。
- **embedding_engine.py** — 三后端（gemini / bge-small-zh / bge-m3）向量化，SQLite 存储，余弦相似度搜索；本地后端用 sentence-transformers 懒加载。
- **import_memory.py** — Claude JSON / ChatGPT / DeepSeek / Markdown / 纯文本五种格式的历史对话导入，分块处理 + 断点续传 + 词频规律检测。

> **第一人称豁免**：`import_memory.py` 在喂给 LLM 的 prompt 里把对话格式化成 `[用户] ... [AI] ...` 文本块（[src/import_memory.py](src/import_memory.py) `_chunk_turns` 第 291 行），这是给 LLM 看的「对话块」标签，不是写入桶 frontmatter 或返回给模型的 docstring，因此不违反 §2.9 第一人称原则。修改这段时勿误删。
> **导入阈值**：`_PATTERN_MIN_DYNAMIC_BUCKETS = 5` / `_PATTERN_PIN_SUGGEST_THRESHOLD = 5`，详见 rule.md §6 备注。
- **utils.py** — 配置加载（env > yaml > defaults 三级优先级）、日志、12 位 hex 短 ID 生成、`safe_path()` 路径遍历防护、`count_tokens_approx()` 中英混排 token 估算。

(改动约束：`bucket_manager` 不能直接调 `decay_engine`，避免循环依赖；`embedding_engine` 在 `BucketManager` 构造时通过参数注入，不能反向引用。`tools/*` 只能通过 `tools._runtime` 拿到依赖，不可反向 `import server`（否则循环）。新增模块时遵循「server.py 是唯一可以引用所有模块的中枢」原则。)

### 1.x tools/ 包结构（2.0 拆分后）

2.0 把 server.py 里原本「11 个肥大入口 + 一堆内部 helper」按路径拆到 `src/tools/<工具>/<分支>.py`，薄封装留在 server.py，真逻辑进子包。

```
src/tools/
├── _runtime.py    # 依赖注入容器：config / bucket_mgr / dehydrator / decay_engine /
│                #   embedding_engine / import_engine / logger / fire_webhook / mark_op
├── _common.py     # 多个工具共享的 helper：内容限额/pinned 配额/check_duplicate_for/
│                #   check_plan_resolution/merge_or_create
├── breath/        # feel/importance/surface/search 四分支，__init__.py 转发
├── hold/          # core/feel/pinned 三分支，__init__.py 统一入口与参数校验
├── grow/          # core/shortpath（短内容快路径在 shortpath，raw_merge=True）
├── dream/         # candidates/hints/output 三阶段 + __init__ 编排
├── trace/         # core（metadata/resolved/pinned/delete/content 替换/计划状态等全在这）
├── anchor/        # core：anchor_set / anchor_release / pulse
└── plan/          # core：plan_create / letter_write / letter_read
```

路线：`server.X(...)` → `tools.X.dispatch(...)`（`__init__.py`）→ 分支函数。所有分支只通过 `from .. import _runtime as rt` 读依赖，不能 `import server`。`server.py` 保留了 `_check_content_size / _check_pinned_quota / _max_bucket_bytes / _max_pinned / _merge_or_create / _check_duplicate_for / _check_plan_resolution` 这几个别名，让 Dashboard HTTP 路由原有调用点不需要改。

### 辅助脚本

`tools/backfill_embeddings.py`（为存量桶补 embedding）、`src/write_memory.py`（CLI 直写记忆，绕过 MCP）、`tools/reclassify_domains.py` / `src/reclassify_api.py`（重新打标）、`tools/check_buckets.py`（数据完整性检查）、`tools/check_icloud_conflicts.py`（iCloud 同步冲突文件清理）。

---

## 2. 数据流与生命周期

### 2.1 一条记忆的完整生命周期

```
用户内容
  │
  ▼
hold / grow（Claude 决策）
  │
  ├─ grow ─→ dehydrator.digest()  → 拆为 2~6 条 → 每条独立走 hold
  │
  └─ hold ─→ dehydrator.analyze()  → {domain, valence, arousal, tags, name}
              │
              ▼
       _merge_or_create()
              │
       bucket_mgr.search(content, limit=1, domain_filter)
              │
       score > merge_threshold(75)?
        ├─ 是 → dehydrator.merge() → bucket_mgr.update() → 更新 embedding
        └─ 否 → bucket_mgr.create() → embedding_engine.generate_and_store()
              │
              ▼
       写入 buckets/dynamic/{domain}/{name}_{id}.md
       activation_count = 0   ← 关键：创建时为 0，touch() 才会变 1+
              │
              ▼
       存活期：每次 breath(query) 命中 → bucket_mgr.touch()
                                           ├─ last_active = now
                                           ├─ activation_count += 1
                                           └─ _time_ripple()  ±48h 邻近桶 +0.3
              │
              ▼
       decay_engine 后台循环（每 24h）→ run_decay_cycle()
              │
       score < threshold(0.3)？
        ├─ 是 → bucket_mgr.archive() → 移入 archive/{domain}/，type="archived"
        └─ 否 → 继续存活
```

(数据流约束：`touch()` 只在**检索命中**时调用，**浮现模式不调用**——这是为了不让 `breath()` 自动浮现重置衰减计时器，否则高活跃桶会永远霸占浮现位。)

### 2.2 对话启动序列（CLAUDE_PROMPT.md 规定的 Claude 端行为）

```
1. breath()                — 必须。浮现未解决记忆
2. dream()                 — 可选。你或用户觉得需要消化时再调
3. breath(domain="feel")   — 可选。想读 feel 时再调
4. 开始和用户说话
```

dream 不是 hook，不是对话启动义务流程。它是你和用户一起决定要不要做的事，没有消化的必要就不做。

### 2.3 feel 桶的特殊生命周期

```
hold(feel=True, source_bucket="xxx", valence=0.45)
  │
  ├─ 跳过 analyze() 和 _merge_or_create()
  ├─ 自动注入 __feel__ 系统标签
  ├─ 写入 buckets/feel/沉淀物/
  ├─ embedding_engine.generate_and_store() （供 dream 结晶检测使用）
  └─ 若 source_bucket 提供 → bucket_mgr.update(source, digested=True, model_valence=0.45)
                              源桶 resolved_factor → 0.02（加速淡化）

feel 桶自身：
  - calculate_score() 固定返回 50.0，永不归档
  - 普通 breath 不浮现（被 type 过滤）
  - 只通过 breath(domain="feel") 或 breath(tags="feel"/"__feel__") 读取
  - 仍参与 dream 的结晶化检测（>0.7 相似度且 ≥3 条 → 提示升级为 pinned）
```

---

## 3. MCP 工具规格（共 11 个）

### 3.1 `breath` — 检索/浮现

签名：`breath(query="", max_tokens=10000, domain="", valence=-1, arousal=-1, max_results=20, importance_min=-1, tags="")`

四种模式（按代码内判定顺序）：

1. **Feel 通道**（`domain="feel"` 或 `tags` 含 `"feel"`/`"__feel__"`）：直接拉所有 `type==feel` 桶，按 `created` 倒序展示原文，按 `surfacing.feel_max_tokens`（默认 6000）做 token 预算；**超出预算的旧 feel 折叠为 60 字符单行摘要**，并在末尾追加 `更早的 feel 摘要（N 条，已折叠）` 段。**不排除 anchor 桶**（设计：feel 通道只看 type=feel）。
2. **重要度批量模式**（`importance_min >= 1`）：跳过语义搜索，按 importance 降序返回 ≤20 条；过滤 `feel/plan/letter` 与 `dont_surface=True`；**不过滤 anchor、不过滤 pinned**（设计：主动按 importance 检索时希望能找到所有重要桶）。
3. **浮现模式**（无 `query`）：钉选桶始终展示为「核心准则」+ 未解决桶按衰减分排序，**冷启动**（`activation_count==0 && importance>=8`）的桶最多 2 个插到最前；后续排序**有两条互斥路径**：当 `surfacing.sampling.enabled=true` 时走加权无放回采样（`top_k` / `sample_k` / `temperature` 控制；详见 §7.1），否则走原 Top-1 固定 + Top-2~20 随机洗牌；按 `max_results` 硬截断。**排除 anchor 桶**（设计：anchor 是坐标系，不该随机冒泡干扰日常浮现；这是浮现模式独有的过滤）。浮现**不调用** `touch()`。**末尾追加 `=== 久未浮现 ===`** 段（iter 1.6 §7 被动联想）：从 `activation_count==0 && importance>=8` 或 `importance>=9 && 距 last_active>7天` 的桶里随机抽 1~2 条，模拟「突然想起来」。
4. **检索模式**（有 `query`）：四维加权评分 → 过滤 `feel/plan/letter`，**pinned/permanent 仍可被检索命中（不过滤），命中后加 📌 前缀** → 向量补充通道（相似度 > 0.5 标 `[语义关联]`）→ 情绪重构（valence 微调 ±0.1）→ 命中时 `touch()` → 结果不足 3 条时 40% 概率随机漂浮 1~3 条低权重旧桶。**不过滤 anchor**（设计：主动检索时希望能找到坐标系桶）。

(实现注意：`tags="feel"` 在第一个分支被映射为 `domain="feel"` 后清出 tag_filter；其它 tag 走 AND 过滤；`max_tokens` 上限 20000，`max_results` 上限 50；`importance_min` 模式下硬上限 20 条不可调；浮现模式中钉选桶**不计入** `max_results` 上限。)

### 3.2 `hold` — 存储单条记忆

签名：`hold(content, tags="", importance=5, pinned=False, feel=False, source_bucket="", valence=-1, arousal=-1, why_remembered="")`

两种路径：

- **Feel 模式** (`feel=True`)：跳过 LLM 分析，自动注入 `__feel__` 标签，写入 `feel/沉淀物/`。`source_bucket` 提供时把源桶标记为 `digested=True` 并写 `model_valence`。返回 `🫧feel→{id}`。
- **普通模式**：`analyze()` → 用户传入的 `valence`/`arousal` 优先于 LLM 结果（B-09 修复）→ `_merge_or_create()`（相似度 > `merge_threshold` 合并，否则新建）→ 写 embedding → 异步触发 `_check_plan_resolution()` 扫 active plans。返回 `合并→{name}` 或 `新建→{name}`。

(改动注意：`pinned=True` 走单独分支直接创建到 `permanent/`，importance 强制锁 10，不走合并；用户显式传 valence/arousal=0.0 也算「有效」，必须走 `0 <= v <= 1` 判定，不能用 `if valence` 否则 0.0 会被忽略——这就是 B-09。)

### 3.3 `grow` — 日记拆分归档

签名：`grow(content)`

- 短内容（< 30 字符）走快速路径：`analyze()` + `_merge_or_create()`，跳过 `digest()` 节省一次 API。
- 正常路径：`dehydrator.digest()` 拆为 2~6 条 → 每条独立走 `_merge_or_create()`，单条失败 try/except 隔离，标 `⚠️条目名`。
- 末尾异步触发 `_check_plan_resolution()`。

返回示例：`3条|新2合1\n📝体检结果\n📌朋友聚餐\n📎近期焦虑情绪`。

### 3.4 `trace` — 修改/删除

签名：`trace(bucket_id, name="", domain="", valence=-1, arousal=-1, importance=-1, tags="", resolved=-1, pinned=-1, digested=-1, content="", delete=False, status="", weight=-1, dont_surface=-1, why_remembered="")`

- `delete=True` → `bucket_mgr.delete()` + `embedding_engine.delete_embedding()`。
- 其它字段：仅收集传入的（用 `-1`/空串作为「未传」哨兵）批量更新 frontmatter。
- `pinned=1` 自动锁 importance=10 + 触发 `_move_bucket(permanent_dir)`。
- `resolved=1` **不**自动归档（B-01 修复）；只更新 frontmatter，由 decay 引擎自然衰减。
- `status` 仅接受 `active`/`resolved`/`abandoned`，主要用于 plan 桶。
- `content="..."` 替换正文并重新生成 embedding。
- `weight` 仅对 plan 桶有意义；`dont_surface` 切换主动遗忘标记；`why_remembered` 写「为什么留着这条」自由文本。
- **不暴露 `anchor` 字段**：anchor 切换必须走 `anchor()` / `release()` 工具（受 24 上限保护）。

(返回时会按 `resolved`/`digested` 状态变化追加人话提示，如「→ 已沉底，只在关键词触发时重新浮现」。)

### 3.5 `pulse` — 系统状态 + 桶列表

签名：`pulse(include_archive=False)`

返回：固化/动态/归档桶数、总 KB、衰减引擎状态、所有桶（带图标）的元数据摘要行。

(已知局限：返回头部的统计行**不显示** `feel_count` / `plan_count` / `letter_count`，但底下的列表会列出这些桶——会让用户感到「数字对不上数量」。详见 §12 反逻辑点 1。)

### 3.6 `dream` — 做梦自省

签名：`dream(window_hours=48)`（默认 48h 窗口；clamp 到 1~336h）

- 默认取过去 48 小时内 `created` 或 `last_active` 任一在窗口内的桶（排除 permanent/feel/pinned/protected/plan/letter）
- 排序：先按 `last_active` 倒序；候选超过 **40 个**时改按 `decay_engine.calculate_score()` 降序截断到前 40，避免一次涌进来太多撑爆上下文
- 拼接桶摘要（完整正文，不截断）+ 自省引导 header
- embedding 启用时附加：连接提示（最相似对，`>0.5`）+ feel 结晶提示（一条 feel 与 ≥2 条其它 feel 相似度 `>0.7` → 建议升级为 pinned）
- 末尾追加 `=== 你的 active plans ===` 全量列表
- 末尾追加 `=== 你的 feel 历史（全量，旧 feel 按 token 预算折叠）===`：按 `surfacing.feel_max_tokens`（默认 6000）做预算，超出的老 feel 折叠为 60 字符单行摘要

(实现细节：用户可手动传更大的 `window_hours`，但软上限 40 仍生效。plan 历史不参与 token 预算全量返回；feel 历史走 token 预算折叠。)

### 3.7 `plan` — 登记待办

签名：`plan(content, status="active", related_bucket="", weight=0.5, why_remembered="")`

写入 `plans/active/`，自动打 `__plan__` 标签，**硬编码** `importance=7` / `domain=["plan"]` / `valence=0.5` / `arousal=0.4`（设计：plan 不开放给用户调情感坐标）。`status` 仅接受 `active`/`resolved`/`abandoned`，其它静默回退为 `active`。`weight` 是「承诺重量」（0~1，dashboard 看板按此倒序）。`why_remembered` 写自由文本说明为什么登记这条。

**严格字符串去重**：登记前扫描所有 `status="active"` 的 plan 桶，若存在 `content` 与新内容**完全字符串相等**的桶，直接返回原 ID 不重复创建（避免重复 `plan("还没回邮件")` 刷屏）。

**自动结案机制**：每次 `hold()` 或 `grow()` 末尾 `asyncio.create_task(_check_plan_resolution())` —— 向量预筛（>0.7）→ LLM 双判 (`resolved && confidence >= 0.7`) → 写 `status="resolved"` + `resolution_reason` + `resolved_by`。任何异常都吞掉，不影响主流程。无 embedding 时整个机制跳过（保守，宁漏报不误报）。

### 3.8 `letter_write` / `letter_read` — 信件

`letter_write(author, content, user_name="", title="", date="")` —— `author` 必填且仅 `user`/`claude`；写入 `letters/history/`，**硬编码** `importance=10` / `valence=0.5` / `arousal=0.3`（设计：信件不开放给用户调这三项），原文永久保留。**不接受 `why_remembered`**——信件本身就是「为什么记得」的载体。

`letter_read(query="", limit=10, author="", date_from="", date_to="")` —— 无 query 时按 `letter_date` 或 `created` 倒序；有 query 且 embedding 启用时用向量相似度排序。

信件特性：永不衰减（`calculate_score` 固定 50）、永不合并、不参与压缩；普通 `breath` 不浮现（被 `feel/plan/letter` 过滤）；`/breath-hook`（SessionStart）末尾追加双方各最新一封。

### 3.9 `anchor` — 标记坐标系桶（iter 2.0）

签名：`anchor(bucket_id)`

把指定桶的 `anchor` frontmatter 字段置为 `True`。**硬上限 24**（`BucketManager.ANCHOR_LIMIT`），由 `set_anchor()` 入口校验；`update()` 透传路径也补了同样校验（False→True 切换时计数，已是 anchor 的重复设置幂等）。超过上限返回 `{ok:False, error:"anchor 已达上限 24"}`，REST 端点 `/api/bucket/{id}/anchor` 返回 **409**。

语义：anchor 是「坐标系」——告诉模型「这是定位用的参照点，不是日常需要冒出来的内容」。anchor 桶**不参与无参 `breath()` 浮现**，但 `query` / `domain` / `importance_min` 等显式检索仍可命中。**与 pinned / dont_surface / weight 完全独立**，不参与 `calculate_score()`。

### 3.10 `release` — 释放坐标系标记（iter 2.0）

签名：`release(bucket_id)`

把指定桶的 `anchor` 字段从 `True` 改回未设置（`update(anchor=False)` 路径直接删除该 frontmatter 键，保持文件干净）。释放后该桶恢复正常浮现资格。无副作用，幂等。

---

## 4. REST API 与 Dashboard

### 4.1 端点完整列表

| 端点 | 方法 | 鉴权 | 用途 |
|---|---|---|---|
| `/` | GET | 公开 | 重定向到 `/dashboard` |
| `/health` | GET | 公开 | 健康检查（桶数 + 衰减引擎状态） |
| `/breath-hook` | GET | 公开 | SessionStart 钩子（HTTP 模式才生效） |
| `/dream-hook` | GET | 公开 | Dream 钩子 |
| `/dashboard` | GET | 公开（页面），AJAX 走 cookie | Dashboard HTML |
| `/letters` | GET | 公开 | 301 → `/#letters`（已合并进 dashboard 的「信」分页，老书签兼容） |
| `/auth/status` | GET | 公开 | 是否已登录 / 是否需要初始化密码 |
| `/auth/setup` | POST | 公开（仅未配置密码时） | 首次设置密码 |
| `/auth/login` | POST | 公开 | 密码登录，颁发 cookie（7 天） |
| `/auth/logout` | POST | 公开 | 注销 |
| `/auth/change-password` | POST | 🔒 | 修改密码（环境变量密码模式下禁用） |
| `/api/buckets` | GET | 🔒 | 桶列表（带评分、不带正文，仅预览） |
| `/api/bucket/{id}` | GET | 🔒 | 桶详情（含正文）。iter 1.9 起额外返回 `triggered_feels: [{id,name,created}]` —— 反向链：哪些 feel 桶把这条作为 `triggered_by` |
| `/api/bucket/{id}/pin` | POST | 🔒 | 切换 pinned（自动同步 type permanent⇄dynamic） |
| `/api/bucket/{id}/resolve` | POST | 🔒 | 切换 resolved |
| `/api/bucket/{id}/archive` | POST | 🔒 | 软删除（移入 archive/） |
| `/api/bucket/{id}/forget` | POST | 🔒 | iter 1.8：切换 `dont_surface`。桶仍在磁盘，只是不再被无参 `breath()` 主动浮现，关键词搜索仍可达 |
| `/api/buckets/forget` | POST | 🔒 | iter 1.9：批量设置 `dont_surface`。Body `{ids:[...], dont_surface: bool}`。返回 `{ok, updated:[], missing:[], errors:[]}` |
| `/api/settings/sampling` | GET / POST | 🔒 | iter 1.9：dashboard 的加权采样面板。GET 返回当前 `surfacing.sampling.{enabled,top_k,sample_k,temperature}`；POST 校验范围后热更新到内存 config（不写回 yaml） |
| `/api/anchors` | GET | 🔒 | iter 2.0：列出所有 anchor 桶（按 `created` 升序），返回 `{ok, count, limit, anchors:[...]}` |
| `/api/bucket/{id}/anchor` | POST | 🔒 | iter 2.0：toggle anchor 标记。Body 可传 `{value: bool}` 强制设置；不传则切换。已满 24 时返回 **409** + `{error, count, limit}` |
| `/api/bucket/{id}` | DELETE | 🔒 | 硬删除，需 `?confirm=true` |
| `/api/letters` | GET | 🔒 | 信件列表，支持 `?author=user\|claude` |
| `/api/letter` | POST | 🔒 | Dashboard 写信入口 |
| `/api/search?q=` | GET | 🔒 | 搜索 |
| `/api/network` | GET | 🔒 | iter 1.7：默认按 `[[wikilink]]` 引用建图；`?mode=embedding` 走相似度兜底 |
| `/api/plans` | GET | 🔒 | iter 1.7 §G：返回 active / resolved / abandoned 三组，含 change_log |
| `/api/plans/{id}/action` | POST | 🔒 | iter 1.7 §G：看板操作（resolve / abandon / reopen / edit），自动追加 change_log |
| `/api/version` | GET | 公开 | iter 1.7 §B：项目版本号（读 `<repo_root>/VERSION`） |
| `/api/author` | GET | 公开 | iter 1.7 §H：静态作者note + 爱发电链接 |
| `/static/{name}` | GET | 公开 | iter 1.7 §C：白名单静态资源（icon.svg / favicon.svg / manifest.json） |
| `/favicon.ico` | GET | 公开 | iter 1.7 §C：301 → /static/favicon.svg |
| `/api/duplicates` | GET | 🔒 | 列出疑似重复桶对（iter 1.6 §4，sim>0.95，由 hold/grow 后台扫出） |
| `/api/breath-debug?q=&valence=&arousal=` | GET | 🔒 | 评分调试（每桶四维分解） |
| `/api/config` | GET | 🔒 | 配置查看（API key 脱敏） |
| `/api/config` | POST | 🔒 | 热更新配置（dehydration / embedding / merge_threshold；可选持久化到 yaml） |
| `/api/host-vault` | GET | 🔒 | 读 `OMBRE_HOST_VAULT_DIR`（process env → .env 文件 fallback） |
| `/api/host-vault` | POST | 🔒 | 写入项目根目录的 `.env`，需重启 docker compose 生效 |
| `/api/status` | GET | 🔒 | Dashboard 设置页用：版本号 + 桶数 + embedding/decay 状态 + 是否环境变量密码 |
| `/api/import/upload` | POST | 🔒 | 上传对话历史并启动导入 |
| `/api/import/status` | GET | 🔒 | 导入进度 |
| `/api/import/pause` | POST | 🔒 | 暂停/继续 |
| `/api/import/patterns` | GET | 🔒 | 词频规律检测 |
| `/api/import/results` | GET | 🔒 | 已导入桶列表（含正文 300 字预览） |
| `/api/import/review` | POST | 🔒 | 批量审阅（important / pin / noise / delete） |
| `/api/bucket/{id}/edit` | PATCH/POST | 🔒 | iter 1.6 §6：Dashboard 编辑桶元数据（name/tags/domain/importance/resolved/pinned/digested/content）；走 §5 大小+pinned 配额 |
| `/api/export` | GET | 🔒 | iter 1.6 §2：流式返回 zip（buckets/*.md + embeddings.db + 脱敏 config.snapshot.yaml + export_meta.json） |
| `/api/heartbeat` | GET | 🔒 | iter 1.6 §3：心跳（uptime / last_op_ts / decay 状态），Dashboard 右上角灯轮询 |
| `/api/logs` | GET | 🔒 | iter 1.6 §3：读 `OMBRE_LOG_FILE`（RotatingFileHandler 写的 server.log）末尾若干行，支持 `?level=ERROR\|WARNING\|INFO\|ALL&limit=200` |
| `/api/onboarding/status` | GET | 公开 | iter 1.6 §8：判断"全新启动"。env+config 同时缺 dashboard_password 与 gemini api_key 时 `first_run=true`。**不要求登录**——首次访问连密码都还没设。不返回任何密钥值，仅布尔/来源标识 |
| `/api/errors/recent` | GET | 🔒 | 读 `<vault>/errors.jsonl` 最近 N 条（任务A 结构化日志后端） |
| `/api/errors/clear` | POST | 🔒 | 清空 `errors.jsonl` |
| `/api/embedding/model/status` | GET | 🔒 | 本地 bge-m3 权重下载进度（首次启动看这条） |
| `/api/embedding/info` | GET | 🔒 | 当前 embedding 后端 / 模型 / 维度 / 已索引向量数 |
| `/api/embedding/migrate` | POST | 🔒 | 触发后端切换 + 全量重算 embeddings（异步） |
| `/api/embedding/migrate/status` | GET | 🔒 | 重算进度（done/total） |
| `/api/settings/human` | GET / POST | 🔒 | 系统通知称呼（`OMBRE_HUMAN_NAME`），dashboard「① 我」面板 |
| `/api/buckets/purge` | POST | 🔒 | 危险区批量物理删除（不可恢复，仅 dashboard 进入「清理模式」后可调） |
| `/api/letter/{letter_id}` | PATCH | 🔒 | 改信件元数据（read_at 等） |
| `/api/letter/{letter_id}` | DELETE | 🔒 | 删信件（移入 archive） |
| `/api/env-vars` | GET | 🔒 | dashboard 设置页「⑤ 环境变量」只读区：当前进程读到的所有 `OMBRE_*`，敏感字段脱敏 |
| `/api/env-config` | GET | 🔒 | 可写 6 字段的当前值（脱敏） |
| `/api/env-config` | POST | 🔒 | 热更新 6 字段并写回 `.env`（重启仍有效） |
| `/mcp/*` | — | 公开 | FastMCP 协议端点 |

🔒 = 需要 cookie 认证，未认证返回 `JSON {error, setup_needed}` 状态码 401。

(实现注意：所有 `/api/*` 路由在函数体首行调用 `_require_auth(request)`；新增端点必须沿用此模式。`/mcp` 不受保护——MCP 协议自身没有认证层，靠传输层（cloudflared、ngrok）做边界。)

### 4.2 Dashboard 认证

- 密码存储：SHA-256 + 16 字节随机 salt，文件 `{buckets_dir}/.dashboard_auth.json`，格式 `{"password_hash": "salt:hash"}`
- 环境变量 `OMBRE_DASHBOARD_PASSWORD` 优先于文件密码；设置后修改密码功能在 UI 中禁用
- Session：内存字典（服务重启失效），cookie `ombre_session`（HttpOnly, SameSite=Lax, 7 天）
- 密码长度 ≥ 6 位

### 4.3 Webhook 推送

设置 `OMBRE_HOOK_URL` 后，下面四个事件 fire-and-forget POST JSON（5 秒超时，失败仅 WARNING 日志）：

| event | 触发 | payload |
|---|---|---|
| `breath` | MCP `breath()` 返回时 | `mode`, `matches`, `chars` |
| `dream` | MCP `dream()` 返回时 | `recent`, `chars` |
| `breath_hook` | `/breath-hook` 命中 | `surfaced`, `chars` |
| `dream_hook` | `/dream-hook` 命中 | `surfaced`, `chars` |

`OMBRE_HOOK_SKIP=1` 全局跳过推送。

### 4.4 Dashboard 页面（侘寂风）

调色板：米白 `#FAF8F3` / 墨黑 `#2C2A26` / 淡灰线 `#D9D5CB` / 朱砂 `#B85C3C`；字体 Noto Serif SC；border-radius 收敛到 2px。Tab 包括：记忆桶列表、Breath 模拟、记忆网络、Plan 看板（iter 1.7）、Anchor 面板（iter 2.0）、配置、导入、设置、Letters 入口。

### 4.5 iter 1.8 — 桶 frontmatter 新增字段

| 字段 | 类型 | 默认 | 含义 / 写入路径 | 是否参与评分 |
|---|---|---|---|---|
| `why_remembered` | str (≤500 char) | 不写 | 「这条为什么值得留下」自由文本。`hold/grow/feel/letter(why_remembered=...)` 或 `trace(why_remembered=...)` 写入。dashboard 桶详情顶部以朱砂斜体引文渲染。 | ❌ |
| `dont_surface` | bool | False | 主动遗忘：True 时无参 `breath()` 跳过该桶；带 `query`/`domain` 的 breath、`/api/buckets`、关键词搜索仍可达。`/api/bucket/{id}/forget` 切换 / `trace(dont_surface=1\|0)`。 | ❌ |
| `first_of_kind` | bool | False | 自动检测：写入新桶时若其 `tags` 与全库已有 `tags` **完全无交集**则置 True。仅展示用，dashboard 旁亮 ✨。失败不阻塞写入。 | ❌ |
| `weight` | float ∈ [0,1] | None（仅 plan 写） | plan 桶专有「承诺重量」。由 `plan(content, weight=0.7, ...)` 写入（hold 没有 `domain` 参数，不能用 `hold(domain=["plan"], ...)` 创建 plan）；或事后 `trace(weight=0.7)` 调整。dashboard 计划看板按 weight 倒序排 active 列。**与 importance 是两个轴**：importance 是事的客观重要度，weight 是这件事压在心头的主观重量。 | ❌ |
| `triggered_by` | str (bucket_id) | 不写 | feel/衍生桶的因果链入口：记下「我这条感受是被哪条记忆触发的」。1.9 会做 UI 联动。 | ❌ |
| `anchor` | bool | 不写 (False) | **iter 2.0**：坐标系标记。True 时该桶**不参与**无参 `breath()` 浮现池——即使 pinned 也不浮现。但 `query` / `domain` / `importance_min` 命中时仍返回（检索 / 重要度模式不过滤 anchor；Feel 通道只看 type=feel，也不过滤）。硬上限 24（`BucketManager.ANCHOR_LIMIT`）：`set_anchor()` 入口与 `update(anchor=True)` 透传路径都会校验（False→True 切换计数，幂等重复设置不计），超过返回 `{ok:False, error}` / 端点返回 409。通过 `anchor()` MCP tool / `release()` MCP tool / `POST /api/bucket/{id}/anchor` 切换；**`trace` 不暴露该字段**。**不参与评分；与 pinned/dont_surface/weight 完全独立**。 | ❌ |
| `source_tool` | str (`hold`/`grow`) | 不写 | **iter 2.0**：记录「这条桶是哪个工具创建的」。`hold` 路径（含 `feel=True` 子分支）写 `hold`；`grow`（含短路径与 digest 拆出来的每条）写 `grow`。**合并不会改这个字段**——保留原桶最初来源；合并触发方写到下面的 `last_merged_by`。dashboard 桶详情可按 source 筛选。letters/plans/anchor 等不写此字段（它们的 `type` 已经表明出处）。 | ❌ |
| `grow_batch_id` | str (`g_<12hex>`) | 不写 | **iter 2.0**：仅 `grow` 创建的桶有此字段，同一次 `grow` 调用里所有新建桶共享同一个 batch_id（包括短路径，即使只产出一条）。dashboard 可按 batch 聚合「这次日记一共归档了哪些事件」。合并不写此字段（合并到的老桶可能来自完全不同的批次/工具，硬覆盖会丢失原始批次信息）。 | ❌ |
| `last_merged_by` | str (`hold`/`grow`) | 不写 | **iter 2.0**：仅在桶被合并时由 `_common.merge_or_create` 写入，记录「最近一次合并是被哪个工具触发的」。原桶最初来源仍由 `source_tool` 表达。 | ❌ |

**关键设计决定**：所有 1.8 新字段都不参与 `decay_engine.calculate_score`。它们是「为什么 / 怎么对待」的元数据，不是「多重要」的算分输入——避免把记忆变成可被优化的目标函数。

老桶（无这些字段）读出时全部走默认值，不会崩；可选的一次性回填脚本：

```bash
python tools/migrate_v17_to_v18.py            # 默认补默认值
python tools/migrate_v17_to_v18.py --dry-run  # 只看会改哪些桶
```

### 4.6 iter 2.0 — feel 桶可读命名

feel 桶的 `bucket_id`（同时也是文件名 stem）从 12 位 UUID hex 改为人类可读的
`feel_YYYYMMDDHHMM_V<valence*100>` 形式（例：`feel_202605011423_V085.md`）。
分钟精度 + valence 后缀让 dashboard 列表「看名字就能猜出是哪条 feel」。冲突时
`bucket_manager.create()` 自动追加秒级或 2 位 hex 后缀。embeddings.db 里
`bucket_id` 字段同步使用新可读 id。其它类型（dynamic/permanent/plan/letter/anchor）
命名规则不变，仍是 12 位 UUID hex。

历史 feel 桶迁移：

```bash
docker compose -f deploy/docker-compose.yml stop  # 必须停服务避免并发写入
python tools/migrate_v19_to_v20.py --dry-run     # 干跑：只看会改什么
python tools/migrate_v19_to_v20.py               # 真跑：重命名 + 同步 embeddings + 补 source_tool
docker compose -f deploy/docker-compose.yml up -d
```

迁移脚本同时补齐 `source_tool`：feel 桶补 `hold`，其它历史桶默认补 `hold`
（用 `--no-default-source-tool` 关闭这个默认补齐）。

---

## 5. 衰减与评分公式

### 5.1 衰减分（decay_engine.calculate_score）

```
final_score = importance × activation_count^0.3
              × e^(-λ × days_since)
              × combined_weight
              × resolved_factor
              × urgency_boost
```

**权重分段（关键设计）**：

- 短期（`days_since ≤ 3`）：`combined_weight = time_weight × 0.7 + emotion_weight × 0.3`（时间主导）
- 长期（`days_since > 3`）：`combined_weight = emotion_weight × 0.7 + time_weight × 0.3`（情感主导）

**子权重**：

- `time_weight = 1.0 + e^(-hours/36)` —— t=0→×2.0，~36h 半衰，72h 后 ≈×1.14，∞→×1.0
- `emotion_weight = base(1.0) + arousal × arousal_boost(0.8)` —— arousal=0 → 1.0；arousal=1 → 1.8

**修正因子**：

| 状态 | 因子 |
|---|---|
| 未解决 | `resolved_factor = 1.0` |
| `resolved=True` | `resolved_factor = 0.05` |
| `resolved=True && digested=True` | `resolved_factor = 0.02` |
| `arousal > 0.7 && !resolved` | `urgency_boost = 1.5` |

**短路返回**（不走公式）：

| 条件 | 返回值 |
|---|---|
| `pinned` 或 `protected` 或 `type=="permanent"` | 999.0 |
| `type` 在 `("feel", "plan", "letter")` | 50.0 |

(改动注意：activation_count 必须 `float()` 而非 `int()`，否则 `_time_ripple` 写入的 0.3 增量会被截断——B-03。)

### 5.2 自动结案（auto-resolve）

每个 `run_decay_cycle()` 中：

```
if not resolved && importance ≤ 4 && days_since > 30:
    bucket_mgr.update(bucket_id, resolved=True)
    meta["resolved"] = True   # ← 关键：本地 meta 同步刷新，下面 calculate_score 立即生效（B-08）
```

(改动注意：必须立即更新本地 `meta` dict，否则该桶在本轮 cycle 仍按未结案分计算，archive 判定要等下一轮。)

### 5.3 自动归档

`score < threshold(0.3)` → `bucket_mgr.archive()`：读 frontmatter 改 `type="archived"` → 写回 → `shutil.move()` 到 `archive/{primary_domain}/`。

### 5.4 搜索评分（bucket_manager.search）

```
total = topic × w_topic(4.0)
      + emotion × w_emotion(2.0)
      + time × w_time(1.5)
      + importance × w_importance(1.0)
normalized = total / w_sum × 100   # 归一化到 0~100
```

**子分**：

- `topic_score = (name×3 + domain×2.5 + tag×2 + body×content_weight(1.0)) / 100×(3+2.5+2+content_weight)` —— 全部用 `rapidfuzz.fuzz.partial_ratio()`；正文截前 1000 字
- `emotion_score = max(0, 1 - dist/√2)`，欧氏距离基于 (valence, arousal)；query 不带情感时返回 0.5
- `time_score = e^(-0.02 × days)` —— 30 天后 ≈ 0.55（B-05 修复值，曾经是 0.1 太快）
- `importance_score = importance / 10`

**阈值与降权**：

- `normalized ≥ fuzzy_threshold(50)` 才进入候选
- `resolved=True` 桶通过阈值后，排序分 `× 0.3`（不影响是否被检出，只影响排名）

**多层流程**：

1. domain 预筛（domain_filter 命中的桶；空集合时回退全量）
2. embedding 评分（如果 `embedding_engine.enabled`，取 top 50 向量近邻；分数注入 Layer 2 的 `semantic` 维度）—— **不再窄化候选集**
3. 多维加权精排（topic / emotion / time / importance / touch [+ semantic]）
4. 截断到 `limit`

(改动注意：iter 2.1+ 起 embedding 不再用作候选预筛。历史实现把候选集替换成「在 embeddings.db 里的桶」，导致缺失向量的桶在 breath 检索里整体消失，pulse 总数与 breath 命中数对不上。修复后没向量的桶 `semantic_score=0`，仍可凭 topic/emotion/time/importance 命中。索引一致性由 `bucket_manager.create()/update(content=...)` 自动 `_sync_embedding()` 维持；pulse 也会附带「索引漂移」告警。)

---

## 6. 桶类型矩阵

| 类型 (`type`) | 目录 | importance | 衰减分 | 普通 breath 浮现 | 参与合并 | 参与 dream | 自动归档 |
|---|---|---|---|---|---|---|---|
| `dynamic` | `dynamic/{domain}/` | 1~10 | 公式计算 | ✅ | ✅ | ✅ | ✅ |
| `permanent`（含 `pinned`） | `permanent/{domain}/` | 锁 10 | 999 | 作为「核心准则」始终展示 | ❌ | ❌ | ❌ |
| `feel` | `feel/沉淀物/` | 5 | 50 | ❌（仅 `domain="feel"`） | ❌ | 仅参与结晶检测 | ❌ |
| `plan` | `plans/active/` | 7 | 50 | ❌（仅 dream 末尾 active 段） | ❌ | dream 列出 | ❌ |
| `letter` | `letters/history/` | 10 | 50 | ❌（仅 `/breath-hook` 末尾各最新一封） | ❌ | ❌ | ❌ |
| `archived` | `archive/{domain}/` | — | — | ❌ | ❌ | ❌ | — |

**新建时初始字段**：`activation_count = 0`（B-04 修复值；曾经是 1 导致冷启动检测失效）；`resolved/pinned/digested` 不显式写入，仅在变更时才出现在 frontmatter 中。

**permanent 与 pinned 的配额关系**：`_count_pinned()` 同时数 `pinned=True` 与 `type=="permanent"` 两类，二者合并受 `limits.max_pinned`（默认 20）约束。手工把文件才进 `permanent/{domain}/` 目录的老桶也计入。`feel` / `plan` / `letter` 均不占该配额。

(实现注意：`pinned` 和 `protected` 在代码里几乎等价处理，但 `protected` 是历史遗留字段，新桶不应再写；UI 只暴露 pinned。)

---

## 7. 配置与环境变量

### 7.1 config.yaml 完整键

| 键 | 默认 | 说明 |
|---|---|---|
| `transport` | `stdio` | `stdio` / `sse` / `streamable-http` |
| `log_level` | `INFO` | 日志级别 |
| `buckets_dir` | `./buckets` | 记忆桶目录 |
| `merge_threshold` | `75` | 合并相似度阈值 (0~100) |
| `dehydration.model` | `deepseek-chat` | LLM 模型名 |
| `dehydration.base_url` | `https://api.deepseek.com/v1` | OpenAI 兼容 endpoint |
| `dehydration.api_key` | `""` | 推荐用环境变量传入，不要写文件 |
| `dehydration.max_tokens` | `1024` | 单次生成上限 |
| `dehydration.temperature` | `0.1` | 采样温度 |
| `embedding.enabled` | `true` | 启用向量检索 |
| `embedding.backend` | `gemini` | `gemini` / `bge-small-zh` / `bge-m3` |
| `embedding.model` | `gemini-embedding-001` | （仅 gemini 后端使用） |
| `embedding.base_url` | （继承 dehydration） | 可独立配置 |
| `embedding.api_key` | （继承 dehydration） | 可独立配置 |
| `decay.lambda` | `0.05` | 衰减速率 λ |
| `decay.threshold` | `0.3` | 归档分阈值 |
| `decay.check_interval_hours` | `24` | 后台扫描间隔 |
| `decay.emotion_weights.base` | `1.0` | 情感权重基值 |
| `decay.emotion_weights.arousal_boost` | `0.8` | arousal 加成系数 |
| `matching.fuzzy_threshold` | `50` | 搜索分下限 |
| `matching.max_results` | `5` | search() 默认上限（被 breath 覆盖为 20） |
| `scoring_weights.topic_relevance` | `4.0` | topic 权重 |
| `scoring_weights.emotion_resonance` | `2.0` | emotion 权重 |
| `scoring_weights.time_proximity` | `1.5` | time 权重（B-06 修复值） |
| `scoring_weights.importance` | `1.0` | importance 权重 |
| `scoring_weights.content_weight` | `1.0` | 正文权重（B-07 修复值） |
| `limits.max_bucket_bytes` | `51200` (50KB) | 单桶内容字节上限（iter 1.6 §5）；0 禁用 |
| `limits.max_pinned` | `20` | pinned 桶数量上限（iter 1.6 §5）；permanent 桶同计；0 禁用 |
| `bucket_type_defaults.{type}.{field}` | （空） | iter 1.9：按桶类型覆盖 importance/valence/arousal 默认值。例：`bucket_type_defaults.feel.importance: 5`。`bucket_manager.create()` 在不传入该字段时查此表 |
| `surfacing.breath_max_tokens` | `10000` | 覆盖 `breath` 默认 max_tokens |
| `surfacing.breath_max_results` | `20` | 覆盖 `breath` 默认 max_results |
| `surfacing.feel_max_tokens` | `6000` | Feel 通道 与 dream feel 历史段的 token 预算，超出折叠为 60 字摘要 |
| `surfacing.sampling.enabled` | `false` | 浮现模式加权采样总开关；false 走原 Top-1 + shuffle |
| `surfacing.sampling.top_k` | `5` | 候选池大小（按衰减分取前 k） |
| `surfacing.sampling.sample_k` | `2` | 从池里无放回抽 k 条返回 |
| `surfacing.sampling.temperature` | `0.7` | 权重 = score^(1/temperature)；>1 更均匀，<1 更偏向高分桶 |
| `wikilink.*` | （已废弃） | wikilink 自动注入已禁用，由 LLM prompt 直接生成 `[[]]` |

### 7.2 环境变量

| 变量 | 默认 | 用途 |
|---|---|---|
| `OMBRE_COMPRESS_API_KEY` | — | 压缩/打标/合并/拆分（dehydration）的 LLM API Key |
| `OMBRE_COMPRESS_BASE_URL` | `https://api.deepseek.com/v1` | 覆盖 `dehydration.base_url` |
| `OMBRE_COMPRESS_MODEL` | `deepseek-chat` | 覆盖 `dehydration.model` |
| `OMBRE_EMBED_API_KEY` | — | 向量化（embedding）的 API Key；不设则语义检索不可用，桶仍可写入 |
| `OMBRE_EMBED_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` | 覆盖 `embedding.base_url` |
| `OMBRE_EMBED_MODEL` | `gemini-embedding-001` | 覆盖 `embedding.model` |
| `OMBRE_EMBED_BACKEND` | `gemini` | 向量后端：`gemini` / `bge-small-zh`（本地 100MB）/ `bge-m3`（本地 2.2GB）；本地模式无需 EMBED_API_KEY |
| `OMBRE_TRANSPORT` | `stdio` | 覆盖 `transport` |
| `OMBRE_PORT` | `8000` | HTTP/SSE 监听端口 |
| `OMBRE_BUCKETS_DIR` | `./buckets` | 覆盖 `buckets_dir`（Docker volume 必设） |
| `OMBRE_VAULT_DIR` | — | `OMBRE_BUCKETS_DIR` 未设时的 fallback（二者同义，`OMBRE_BUCKETS_DIR` 优先） |
| `OMBRE_HOOK_URL` | — | Webhook 推送地址；空则不推送 |
| `OMBRE_HOOK_SKIP` | `false` | `1`/`true`/`yes` 跳过推送 |
| `OMBRE_DASHBOARD_PASSWORD` | — | 预设 Dashboard 密码（覆盖文件密码，UI 改密码功能禁用） |
| `OMBRE_HOST_VAULT_DIR` | — | docker-compose 用：宿主机 vault 路径，写入项目根 `.env` 后 `docker compose down/up` 生效 |

优先级：**环境变量 > config.yaml > 内置默认值**。读取入口都在 `utils.load_config()`（`OMBRE_EMBED_BACKEND` 例外，直接在 `embedding_engine.py` 读取）。新增 env 变量必须在那里注入到 config dict。

---

## 8. 硬编码值清单（按位置归类）

### 8.1 decay_engine.py

| 值 | 位置 | 用途 |
|---|---|---|
| `999.0` | `calculate_score` | pinned/protected/permanent 桶分数 |
| `50.0` | `calculate_score` | feel/plan/letter 桶固定分 |
| `0.3` (指数) | `calculate_score` | `activation_count^0.3` 巩固指数 |
| `3.0` (天) | `calculate_score` | 短期/长期切换阈值 |
| `0.7 / 0.3` | `calculate_score` | 短/长期权重分配 |
| `36.0` (小时) | `_calc_time_weight` | 新鲜度半衰期 |
| `0.7` | `calculate_score` | urgency 触发 arousal 阈值 |
| `1.5` | `calculate_score` | urgency_boost 倍数 |
| `0.05 / 0.02` | `calculate_score` | resolved / resolved+digested 因子 |
| `4` / `30 天` | `run_decay_cycle` | auto-resolve 阈值 |

### 8.2 bucket_manager.py

| 值 | 位置 | 用途 |
|---|---|---|
| `×3 / ×2.5 / ×2 / ×1` | `_calc_topic_score` | name / domain / tag / body 权重 |
| `1000` 字符 | `_calc_topic_score` | 正文截取长度 |
| `0.02` | `_calc_time_score` | `e^(-0.02×days)`（B-05） |
| `0.3` | `search` | resolved 桶排序降权 |
| `48.0h` | `_time_ripple` | 时间涟漪窗口 |
| `+0.3` | `_time_ripple` | 邻近桶 activation_count 增量 |
| `5` | `_time_ripple` | 单次涟漪最大桶数 |

### 8.3 server.py

| 值 | 位置 | 用途 |
|---|---|---|
| `10000` / `20000` | `breath` | max_tokens 默认 / 上限 |
| `20` / `50` | `breath` | max_results 默认 / 上限 |
| `2` | `breath` 浮现 | 冷启动桶数上限 |
| `8` | 冷启动 | importance >= 8 才进入冷启动 |
| `20` | `breath` 浮现 | top-1 固定 + top-2~20 随机 |
| `0.5` | `breath` 检索 | 向量补充通道相似度下限 |
| `0.2` | `breath` 检索 | 情感重构系数 `(q_v - 0.5) × 0.2`，最大 ±0.1 |
| `3` / `0.4` / `2.0` / `1~3` | `breath` 检索 | 随机漂浮触发条件 / 概率 / 池阈值 / 数量 |
| `30` 字符 | `grow` | 短内容快速路径阈值 |
| `0.7` | `_check_plan_resolution` | plan 自动结案向量预筛 |
| `0.7` | dream | feel 结晶相似度阈值 |
| `0.5` | dream | 连接提示相似度阈值 |
| `10` | dream | 取最近 N 条 |
| `60s` | keepalive | `/health` 自 ping 间隔 |
| `86400 × 7` | session | cookie 有效期 7 天 |

### 8.4 dehydrator.py / embedding_engine.py / utils.py

| 值 | 位置 | 用途 |
|---|---|---|
| `60.0s` / `30.0s` | OpenAI 客户端 | dehydrator / embedding 超时 |
| `3000` / `2000` / `5000` 字符 | `dehydrate` / `merge` / `digest` | API 输入截断 |
| `100` token | `dehydrate` | 阈下不压缩直接返回 |
| `2000` 字符 | `embedding._generate_embedding` | embedding 文本截断 |
| `12` | `gen_id` | UUID hex 取前 12 位 |
| `80` 字符 | `sanitize_name` | 桶名最大长度 |
| `1.5` / `1.3` | `count_tokens_approx` | 中文 / 英文系数 |

---

## 9. 降级行为表

| 场景 | 异常 | 行为 |
|---|---|---|
| `breath` 浮现 | 桶目录空 | 返回「权重池平静，没有需要处理的记忆。」 |
| `breath` 浮现 | `list_all` 异常 | 返回「记忆系统暂时无法访问。」 |
| `breath` 检索 | `search` 异常 | 返回「检索过程出错，请稍后重试。」 |
| `breath` 检索 | embedding 不可用 | WARNING 日志，跳过向量通道，仅 keyword |
| `breath` 检索 | 结果 < 3 | 40% 概率随机漂浮 1~3 条低权重旧桶 |
| `hold` `analyze` 失败 | API 异常 | **直接 RuntimeError**，不创建桶，返回「API key 未配置或调用失败，打标无法完成，桶未创建。请检查 OMBRE_COMPRESS_API_KEY。」 |
| `hold` 合并搜索失败 | search 异常 | 直接走新建路径 |
| `hold` 合并融合失败 | merge 异常 | 直接走新建路径 |
| `hold` embedding | API 异常 / 未配置 | 桶仍创建成功，返回值追加「向量化失败，该桶不参与语义检索，仅支持关键词匹配。请检查 OMBRE_EMBED_API_KEY。」 |
| `grow` digest 失败 | API 异常 | **直接 RuntimeError**，不创建任何桶，返回「API key 未配置或调用失败，日记拆分无法完成，桶未创建。请检查 OMBRE_COMPRESS_API_KEY。」 |
| `grow` 单条失败 | 单 item 异常 | 标 `⚠️条目名`，其它继续 |
| `grow` 短内容 (<30 字) | — | 跳过 digest 走 hold 单条 |
| `trace` 桶不存在 | get None | 返回「未找到记忆桶: {id}」 |
| `trace` 无字段变更 | — | 返回「没有任何字段需要修改。」 |
| `dehydrator.dehydrate` API 不可用 | `api_available=False` | **直接 RuntimeError，不静默降级** |
| `embedding.search_similar` 未启用 | enabled=False | 返回 `[]`，调用方 fallback |
| `_check_plan_resolution` 无 embedding | — | 整体跳过（保守，不误报） |
| `decay_cycle` list_all 失败 | 异常 | 返回 `{checked:0, error:str}`，不终止后台循环 |
| `decay_cycle` 单桶评分失败 | 异常 | WARNING 日志，跳过该桶 |

**核心设计决策（不要轻改）**：脱水/打标 API 不可用时**直接报错**而非本地降级。理由：本地关键词提取的语义质量不足以替代 LLM 打标，静默降级会产生错误分类的记忆，比直接报错更危险。

---

## 10. 已修复 Bug 记录（B-01 至 B-10）

> 所有 bug 已在当前代码修复并有回归测试。保留此表用于回查历史决策。

| ID | 严重度 | 文件 | 函数 | 一句话 | 测试 |
|---|---|---|---|---|---|
| B-01 | 🔴 高 | `bucket_manager.py` | `update()` | resolved 桶不再立即移入 archive/，由 decay 自然衰减 | `tests/regression/test_issue_B01.py` |
| B-03 | 🔴 高 | `decay_engine.py` | `calculate_score()` | activation_count 用 float 不被 int() 截断浮点涟漪增量 | `tests/regression/test_issue_B03.py` |
| B-04 | 🟠 中 | `bucket_manager.py` | `create()` | 初始 activation_count=0 而非 1，冷启动检测才能生效 | `tests/regression/test_issue_B04.py` |
| B-05 | 🟠 中 | `bucket_manager.py` | `_calc_time_score()` | 时间衰减系数 0.02 而非 0.1（旧值衰减过快） | `tests/regression/test_issue_B05.py` |
| B-06 | 🟠 中 | `bucket_manager.py` | 评分权重 | `w_time` 默认 1.5（原 2.5 过偏近期） | `tests/regression/test_issue_B06.py` |
| B-07 | 🟠 中 | `bucket_manager.py` | `_calc_topic_score()` | `content_weight` 默认 1.0（原 3.0 让正文堆砌打败精确名匹配） | `tests/regression/test_issue_B07.py` |
| B-08 | 🟡 低 | `decay_engine.py` | `run_decay_cycle()` | auto-resolve 后立即 `meta["resolved"]=True` 同轮降权生效 | `tests/regression/test_issue_B08.py` |
| B-09 | 🟡 低 | `server.py` | `hold()` | 用户传入 valence/arousal=0.0 也算有效，优先于 analyze 结果 | `tests/regression/test_issue_B09.py` |
| B-10 | 🟡 低 | `bucket_manager.py` | `create()` | feel 桶 domain=[] 不被填充为 `["未分类"]` | `tests/regression/test_issue_B10.py` |

(B-02 在审查中并入了 B-01，故缺号，不是遗失。)

---

## 11. Debug 快速索引（症状 → 文件 + 函数）

> 出现这些症状先去这里查。每条按「**用户/Claude 看到什么** → 去看哪个函数」组织。

### 11.1 浮现 / 检索类

| 症状 | 文件 | 函数 |
|---|---|---|
| `breath()` 无参返回「权重池平静」但桶其实存在 | `server.py` | `breath` 浮现分支；检查 `bucket_mgr.list_all()` 是否漏遍历某子目录 |
| 应该浮现的钉选桶没出现 | `server.py` | `breath` 浮现分支的 pinned 过滤；`bucket_mgr.create` 是否写入 `pinned: True` |
| 钉选桶 importance 不是 10 | `bucket_manager.py` | `create()`（pinned 锁 10）+ `update()`（pinned 重新锁 10） |
| 检索结果排序看着不对 | `bucket_manager.py` | `search()` Layer 2 + `_calc_topic_score / _calc_emotion_score / _calc_time_score` |
| 关键词明明在桶名里却没命中 | `bucket_manager.py` | `_calc_topic_score`（rapidfuzz partial_ratio 阈值）+ `fuzzy_threshold` 配置 |
| resolved 桶完全搜不到 | `bucket_manager.py` | `search()` 阈值检查应该用 normalized 原始值，× 0.3 只在通过阈值后；旧版 B-01 行为 |
| 向量搜索没生效 | `embedding_engine.py` | `enabled` 是否为 True；`search_similar` 是否抛异常被 server.py 捕获 |
| 向量后端切换不生效 | `server.py` | `/api/config` POST 中 embedding.backend 分支必须 `EmbeddingEngine(config)` 完整重建 |
| `breath(domain="feel")` 返回空但有 feel 桶 | `bucket_manager.py` | `list_all()` `dirs` 列表必须含 `self.feel_dir` |
| Top-1 永远是同一个桶 | `server.py` | `breath` 浮现分支 `top1` 固定逻辑；想加多样性需改成 sampling |

### 11.2 存储 / 合并类

| 症状 | 文件 | 函数 |
|---|---|---|
| `hold` 应合并却新建了 | `server.py` | `_merge_or_create`；检查 `merge_threshold` + `bucket_mgr.search(content, limit=1)` 返回的 score |
| `hold` 应新建却合并到无关桶 | `bucket_manager.py` | `_calc_topic_score` content_weight 是否被改回 3.0；query 用了 content 全文导致正文相似度爆表 |
| 用户传入 valence=0.0 被忽略 | `server.py` | `hold` 中必须用 `0 <= valence <= 1` 判定，不能 `if valence`（B-09） |
| `grow` 短内容报「digest 失败」 | `server.py` | 短内容 `< 30` 字应走快速路径；检查长度判断 |
| 桶名乱码 / 文件名错误 | `utils.py` | `sanitize_name`；检查正则 `[^\w\s\u4e00-\u9fff-]` |
| feel 桶 domain 莫名变成「未分类」 | `bucket_manager.py` | `create()` 必须对 `bucket_type=="feel"` 单独处理（B-10） |
| `hold(feel=True)` 没自动打 `__feel__` | `server.py` | `hold` feel 分支 `feel_tags = ["__feel__"] + extra_tags` |
| source_bucket 没被标 digested | `server.py` | `hold` feel 分支末尾 `bucket_mgr.update(source_bucket, digested=True, model_valence=...)` |

### 11.3 衰减 / 归档类

| 症状 | 文件 | 函数 |
|---|---|---|
| 桶不该归档却被归档了 | `decay_engine.py` | `calculate_score`；检查是否漏 pinned/protected/permanent/feel 短路 |
| auto-resolve 后桶分数没降 | `decay_engine.py` | `run_decay_cycle` 中 `meta["resolved"] = True` 必须在 `update` 后立即执行（B-08） |
| 时间涟漪不生效 | `bucket_manager.py` | `_time_ripple` 写入 `+0.3` 后 `calculate_score` 必须用 `float()` 而非 `int()`（B-03） |
| 新建重要桶没被冷启动浮现 | `bucket_manager.py` | `create()` 初始 `activation_count=0`（B-04）；`server.py:breath` 冷启动条件 `==0` |
| 30 天前的高情感桶被归档了 | `decay_engine.py` | 长期分支 `emotion×0.7` 检查 arousal 字段；`urgency_boost` 触发条件 |

### 11.4 系统 / 部署类

| 症状 | 文件 | 函数 |
|---|---|---|
| Dashboard 401 | `server.py` | `_require_auth`；检查 cookie `ombre_session`；`OMBRE_DASHBOARD_PASSWORD` 是否正确 |
| 改密码报「环境变量密码」错误 | `server.py` | `auth_change_password` 检测 `OMBRE_DASHBOARD_PASSWORD` 设置时禁用 |
| HTTP 模式下 Claude.ai 连不上 | `server.py` | `__main__` CORS 中间件；`_app = mcp.streamable_http_app()`；URL 末尾必须 `/mcp` |
| docker compose 重启后桶丢失 | — | volume 必须挂载到 `OMBRE_BUCKETS_DIR`（默认 `/data` 或 `/app/buckets`） |
| Dashboard 改 host vault 不生效 | `server.py` | `_write_env_var`；写入 `.env` 后必须 `docker compose down/up` 重新挂载 |
| keepalive 失败 | `server.py` | `_keepalive_loop`；检查 `OMBRE_PORT` 实际监听端口 |
| Webhook 不推送 | `server.py` | `_fire_webhook`；检查 `OMBRE_HOOK_URL` 和 `OMBRE_HOOK_SKIP` |
| 配置热更新 dehydrator 没生效 | `server.py` | `api_config_update` 中 dehydrator 字段直接赋值 + 重建 `AsyncOpenAI` 客户端 |

### 11.5 import / 历史导入类

| 症状 | 文件 | 函数 |
|---|---|---|
| 导入卡住 | `import_memory.py` | `ImportEngine.start`；`is_running` 状态；`pause()` 是否被误触发 |
| 导入识别不出格式 | `import_memory.py` | 格式 sniff 逻辑；支持 Claude JSON / ChatGPT / DeepSeek / Markdown / 纯文本 |
| 导入完成但桶很少 | `import_memory.py` | 分块大小 + dehydrator merge 阈值；可能被合并到现有桶 |

---

## 12. 已知用户向反逻辑点

> 这些点不是 bug，但用户/Claude 用起来会困惑。修复需要权衡，先记录。

1. **`pulse` 顶部统计行不显示 plan/letter/feel 数**。底下列表会列出这些桶，导致「数字对不上数量」。改动点：`server.py:pulse` 拼字符串处增加 `feel_count / plan_count / letter_count` 字段（`bucket_mgr.get_stats()` 已经返回，缺的只是显示）。

2. **README 与代码降级行为已对齐**（iter 2.0 doc-fix 闭合）。README 第三步与「常见问题」均改口为「无 key 时 hold/grow 仍能保存桶（自动兜底为「未分类」域，无打标、无向量），但 breath 浮现/检索阶段一旦触发脱水就会报错」。原冲突源自旧版 README 措辞「没有 API key 也能跑，只是脱水压缩功能不可用」与代码 `dehydrator.dehydrate()` 在 `api_available=False` 时直接 `RuntimeError` 的实情不符；现以代码实情为准。

3. **`breath(domain="feel")` 文档说支持，但很多用户没意识到 `tags="feel"` 等价**。两条路径在 server.py:`breath` 顶部统一映射，已加在工具 docstring 里，但 dashboard 没暴露 feel 通道入口。

4. **`grow` 短内容 < 30 字被静默走 hold 路径**，没有任何提示。用户传一句日记发现没被拆分会困惑。修复方向：返回串前加「（短内容，已直接保存为单桶）」标注。

5. **dream feel 历史折叠已实现**。iter 2.0 后 dream 末尾的 feel 历史段按 `surfacing.feel_max_tokens`（默认 6000）做 token 预算，超出的老 feel 折叠为 60 字符单行摘要。原记录「dream 全量返回 feel 历史不限数量」问题已闭合。

6. **`OMBRE_HOST_VAULT_DIR` 写入 .env 后无任何「需要重启」提示**。Dashboard 仅在 POST 响应里写了 note，但用户如果只看 UI 状态可能不会看到那段 JSON。修复方向：UI 上加 toast 提示。

7. **wikilink 配置项已废弃但仍在 `config.example.yaml`**。新用户会以为配 `wikilink.auto_top_k` 等参数有用，实际全被忽略。修复方向：从 example 删除整个 `wikilink:` 段，或注释掉并标 deprecated。

8. **`trace(resolved=1)` 与 `/api/bucket/{id}/resolve` 行为一致但提示不同**。CLI 端有「→ 已沉底，只在关键词触发时重新浮现」人话说明，REST 端只返回 `{ok: true}`。Dashboard 应自行渲染同样的提示。

9. **`/api/bucket/{id}` DELETE 需要 `?confirm=true`，但 archive POST 不需要**。同样是「移除一个桶」，DELETE 是硬删（不可逆）archive 是软删（可在文件系统手动恢复），semantics 没问题但用户容易混淆。Dashboard UI 应明确区分两个按钮。

10. **冷启动检测最多 2 个**。`importance >= 8` 的新桶超过 2 个时，第 3 个开始按普通衰减分排队，可能被压在 top-20 后随机洗牌。如果用户一次性钉选 5 条核心准则后又新建 3 个 importance=10 的事件桶，会感到「我刚建的核心事件没浮现」。

11. **Letter 不参与压缩但仍生成 embedding**。原文如果非常长（>2000 字符）embedding 只看前 2000 字符——长信件的语义检索会偏向开头。这是已知 trade-off，未来若需要可改为分段 embedding。

---

## 13. 未来设想（依赖上游 hook 才能落地）

### 13.1 自动上下文注入 (auto-context injection)

让模型在回复用户当前消息**前**自动获得相关历史记忆，无需主动 `breath()`。当前 MCP 协议只有 `SessionStart` hook 在会话开始触发一次，无法对每一轮 user turn 介入。

设计草案：新增 `pre_user_turn` hook → server.py 增 `/turn-hook` 端点 → embedding 取相似度 > 0.6 的 8 条 + decay 取 top 5 高活跃未解决 → 压缩到 ≤120 token 合并为系统提示注入下一轮 → token_budget = `min(2000, 0.1 × context_window)`。

### 13.2 跨会话连续性 token

服务端在 SessionStart 下发 `continuity_token`（上一会话末态摘要 + 未解决议题 ID 列表），客户端 dream 后回写更新。同样依赖 hook 双向通道。

### 13.3 分段 letter embedding

长信件按段落生成多 embedding，检索时合并最高相似度段。需要 SQLite schema 改为支持一对多。

---

*本文档基于代码直接推导，每条断言都可对照源文件函数名验证。代码更新时请同步修订。*
