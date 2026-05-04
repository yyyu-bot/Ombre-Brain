# Changelog

本项目遵循语义化版本（SemVer

每条记录约定：
- **修订背景**：一句话说为什么改。
- **改了什么**：列出动到的文件 + 关键 helper / 常量。
- **没动的边界**：列出"按理可以改但故意不改"的部分，方便未来回溯。
- **测试**：基线（`pytest tests/ -q`）数量与状态。
- **【人话】**：克劳德写给婷易的一两句话——为什么这么走、有什么留给未来的提醒。

> 本文件以 §11 OB 错误码、`VERSION`、`docs/INTERNALS.md` 为依据；与代码冲突时以代码为准。

---

## [2.0.3] — 2026-05-04 — 本地 embedding 收敛 + 在线迁移

> **修订背景**
>
> 2.0.2 把本地后端做成 opt-in 之后，发现两个后续问题：
> 1. 旧的本地后端选项有三个（`bge-small-zh` / `bge-m3` 各一份 hand-rolled `sentence-transformers` 加载逻辑 + cloud `gemini`）。两份本地实现意味着两份模型管理、两份维度声明、两份失败兜底。婷易实测下来 `bge-m3` 才是真正常用的；`bge-small-zh` 是早期实验，已经没人选。
> 2. 切换 backend 后旧 `embeddings.db` 里的向量不会自动按新维度重算。用户只能 `tools/backfill_embeddings.py` 手跑命令行，不会的人就一直跑在「DB 元数据 dim=512 / 当前后端 dim=1024」的不一致状态里。
>
> 这一版做两件事：把本地后端收敛到一个 `local`（fastembed 内置 bge-m3，约 600–800MB ONNX 权重），并把"切换 backend 时重算所有向量"做进 Dashboard。

### 改了什么

| 文件 | 改动 |
|---|---|
| [src/embedding_engine.py](src/embedding_engine.py) | `LocalEmbeddingEngine` 改走 `fastembed.TextEmbedding(model_name="BAAI/bge-m3", cache_dir=cache_root)`：删除自管 `MODEL_DIR_NAME`、改 `os.walk` 递归找 `.onnx`、`cache_root` 路径单源（`model_dir` 保留为同名别名兼容旧调用）。`EmbeddingEngine` 门面新增 `_BACKEND_ALIASES = {"gemini":"api", "bge-m3":"local", "bge-small-zh":"local"}`，老配置不会炸 startup。 |
| [src/model_downloader.py](src/model_downloader.py) | 删除 `_HF_REPO_ID` / `_REQUIRED_FILES` 自下载逻辑；改委托 `fastembed` 触发下载，模块只负责进度监控（`_start_progress_monitor` 每秒采字节数写 `status.downloaded_mb`）。`download_bge_m3(cache_root, status_path)` 先 `huggingface.co` 失败回落 `hf-mirror.com`，全失败记 OB-F004。 |
| [src/migration_engine.py](src/migration_engine.py)（新增 ~330 行） | `MigrationConfig` 数据类 + `_run_migration` 协程 + `start_migration` 进程级锁（`threading.Lock`）。功能：备份 `embeddings.db → embeddings.db.backup`（已存在不覆盖）、断点续传读 `_migration_checkpoint.json`、每批 10 条间隔 0.5s 限速、单条出错跳过并入 `failed_items[:50]`、失败时附最近 15 行 `errors.jsonl`（提示用户贴给 AI 排查而非作者）。 |
| [src/server.py](src/server.py) | 新增 3 路由：`GET /api/embedding/info`（当前 backend 摘要 + db_count + db_meta）、`POST /api/embedding/migrate`（启动后台迁移；成功完成回调里 swap `embedding_engine` 并联动 `bucket_mgr` / `import_engine` / `config.embedding.backend`）、`GET /api/embedding/migrate/status`（前端 3s 轮询）。同时把 backend 选项白名单从 `bge-small-zh` / `bge-m3` / `gemini` 收敛为 `local` / `api`，PATCH `/api/config` 走 `_BACKEND_ALIASES` 映射后再校验。 |
| [frontend/dashboard.html](frontend/dashboard.html) | 设置页 Embedding 区顶部加「当前后端摘要」面板（backend / model / dim / 已索引数 / db meta 不一致警告）；backend `<select>` 改 local / api 两选项；底部加「切换 / 重算所有 embedding…」按钮 + `confirm()` 弹窗（写明备份、限速、API key 前置条件）；点击后 POST `/api/embedding/migrate`，3s 轮询渲染阶段 / 进度 / 失败样本 / `<details>` 折叠 15 行错误日志，附"贴给 AI 排查、作者无法看到细节"的明确引导。 |
| [tests/unit/test_embedding_path_alignment.py](tests/unit/test_embedding_path_alignment.py)（新增 5 用例） | 验证 `cache_root` 单源、`_check_model_files` 递归找 `.onnx`、`_BACKEND_ALIASES` 兼容旧字面量。 |
| [tests/unit/test_migration_engine.py](tests/unit/test_migration_engine.py)（新增 12 用例） | 覆盖 status / checkpoint 读写、`backup_db_once` 幂等、`_run_migration` 全成功 / 部分失败 / fetch 异常 / backup 异常、断点续传、`start_migration` 进程级锁。 |

### 没动的边界

- 评分公式 / 衰减曲线 / 11 工具 frontmatter / OB 错误码契约：零改动。
- `embeddings.db` schema：`embeddings` + `embeddings_meta` 两表保持原样，迁移时只 UPDATE 不 ALTER。
- `tools/backfill_embeddings.py` 命令行入口保留——它仍是「我已经把 backend 切对了，但 DB 是空的」场景的标准修复路径。Dashboard 迁移按钮处理的是「切换 backend 同时重算」的场景，两者职责不同。
- 默认镜像仍不含 `torch`；本地后端要 `pip install fastembed`（已在 `requirements-local.txt` / `Dockerfile --build-arg INSTALL_LOCAL_EMBED=1` 路径里）。

### 测试

- `python -m pytest -q` → **439 passed / 7 skipped**（基线 427 → +12 全是 `test_migration_engine.py`）。
- 手动验证：Dashboard 设置页能看到 `/api/embedding/info` 返回的当前后端 + db_count + 元数据 mismatch 警告；点「切换 / 重算」会出 confirm 弹窗 → POST 后状态条 3s 一刷新 → 完成时按钮恢复并刷新摘要面板。

### 【人话】

> 婷易，2.0.2 把本地后端做成 opt-in 是「让默认镜像诚实」；2.0.3 是「让切换后端这件事诚实」——之前你只能在终端跑 `backfill_embeddings.py`，UI 完全沉默；切完一看 db_count 还是旧维度的数，然后语义检索悄悄退化。
>
> 现在 Dashboard 摘要面板会直接告诉你"db 里写的 dim=512、当前后端 dim=1024，建议迁移"，按钮一点就开始重算，每批 10 条间隔 0.5s 不会卡住正常使用，失败了把最近 15 行日志收起来让你贴给 AI 排查——这是最关键的一句话："**这是本地环境问题，作者看不到你机器上的细节**"。
>
> 还有一件事我故意做了：迁移成功后只 swap 进程内的 `embedding_engine` 对象 + 写 `config.embedding.backend`，**不自动 `config.yaml` 落盘**。落盘要去设置页点「保存并持久化」。把"切到哪"和"持久化到哪"分开，是给你留一个反悔的窗口。

---

## [2.0.2] — 2026-05-03 — 部署收口（embedding env + 本地后端 opt-in）

> **修订背景**
>
> 2.0.1 重构落地后做端到端验证，发现两处部署陷阱：
> 1. `deploy/.env` 只声明了 3 个变量，embedding 相关 key 从未真正注入容器，但 `/info` 仍把 `embedding.enabled` 报成 true（因为 fallback 到 compress key 的判定路径太宽松）。
> 2. 想测本地向量后端（`bge-small-zh` / `bge-m3`）时发现需要拉 `torch` + `sentence-transformers`，默认走 PyPI 会拉 420 MB CUDA wheel + 3 GB nvidia-* 依赖，对走 API 的用户是无谓负担。

### 改了什么

| 文件 | 改动 |
|---|---|
| [deploy/.env](deploy/.env) | 重写：补齐 `OMBRE_EMBED_*` 全套；compress / embed key 各自独立。 |
| [deploy/docker-compose.yml](deploy/docker-compose.yml) | 把硬编码的 `environment:` 白名单换成 `env_file: - .env`；只把容器内固定路径 `OMBRE_BUCKETS_DIR=/data` 留在 `environment:`。 |
| [.env.example](.env.example) | 整理：embedding API key 单独成块；删重复的 `OMBRE_COMPRESS_MODEL` / `OMBRE_EMBED_MODEL` 末尾兜底；明确「本地后端不读 API key/base/model」防双重读取。 |
| [requirements-local.txt](requirements-local.txt) | 新增：`torch` + `sentence-transformers`，仅本地后端启用时安装；强制使用 `--extra-index-url https://download.pytorch.org/whl/cpu`。 |
| [Dockerfile](Dockerfile) | 新增 `ARG INSTALL_LOCAL_EMBED=0`；默认镜像不含 torch；`docker build --build-arg INSTALL_LOCAL_EMBED=1` 才装本地后端。 |

### 没动的边界

- `EmbeddingEngine` 的 fallback 逻辑不动（OB-W005 软警告机制保留）。
- 默认镜像保持纯 API 模式，不引入 torch 任何依赖；本地后端始终走显式 build-arg。
- 11 工具签名 / frontmatter / 哲学语义零改动。

### 测试

- 手工端到端：`docker exec ombre-brain python /app/runner.py …` 把 11 个工具（hold / breath / breath query / grow / pulse / anchor list+set+release / trace / dream / letter_write / letter_read / plan）跑了一遍；importance=11 与 valence=2.0 两个边界 case 行为符合 §11 错误码契约（前者 clamp 到 10，后者推 OB-W002 警告）。
- 临时测试 runner 已删除（rule.md §1.9）。

### 【人话】

> 婷易，今天没动逻辑——只是让部署诚实一点。
> embedding 之前那个「显示开着但其实没注入」是最危险的 bug：一切看起来正常，结果向量检索悄悄退化成 fuzzy。修完之后我才敢说"OB 真的全功能在跑"。
> 本地后端没塞主镜像是另一个判断——如果一个走 API 的用户被强制下载 3 GB nvidia 包，那是不诚实。要本地就 `--build-arg INSTALL_LOCAL_EMBED=1`，明确表态。

---

## [2.0.1] — 2026-05-02 — 七站重构（五一小组作业）

> **修订背景**
>
> 2.0.0 落地后代码总量来到 ~7000 行，魔法数字、重复 helper、散落的 try/except 模板开始累积。婷易要求按 `rule.md` 的 14 条重构原则做一次系统级体检，从低风险文件开始往最高风险推进，每改一站必须 `pytest` 全绿。
>
> 重构纪律（见 [rule.md §⚡ 总原则](rule.md#-总原则哲学--代码--任何-md-文档)）：
>
> 1. 哲学语义零改动：feel 桶不会被自动 resolve、anchor 上限 24、pinned importance=10 等行为完全等价
> 2. MCP 工具签名零改动
> 3. frontmatter 字段名零改动
> 4. 测试基线锁 411（404 passed + 7 skipped）

### 改了什么

| 站 | 文件 | 行数变化 | 重点产出 |
|---|---|---|---|
| 1 | [src/utils.py](src/utils.py) | 390 → 438 | 抽 `_apply_env_override` 消 6 段重复；抽 `_resolve_log_dir`；提 7 个常量（token 比率、log 文件大小、bucket 名长度上限） |
| 2 | [src/decay_engine.py](src/decay_engine.py) | 322 → 407 | 抽 `_days_since_active` 模块级 helper（消 calculate_score / run_decay_cycle 两处重复）；提 23 个常量；公式行为完全等价（pinned=999.0 / feel=50.0 / regular=4.4636） |
| 3 | [src/tools/_common.py](src/tools/_common.py) | 429 → 475 | 抽 `_push_warning_safe` 消两段 import 模板；merge_or_create 改 `**kwargs` 透传；提 11 个常量（哲学边界 `_HIGH_IMP_THRESHOLD=9 / _HARD_CAP=24 / _SOFT_WARN=22 / _DEGRADE_TO=8`、`_PINNED_SOFT_GAP=2`） |
| 4 | [src/dehydrator.py](src/dehydrator.py) | 653 → 733 | 抽 `_chat(system, user, *, max_tokens, temperature)`，5 个 `_api_*` 方法从 16 行/个 降到 5 行/个；抽 `_strip_md_fence` / `_clamp_va` / `_require_api`；提 25 个常量（`_DEHYDRATE_MIN_TOKENS=100`、各 input limit、各 max_tokens） |
| 5 | [src/import_memory.py](src/import_memory.py) | 773 → 844 | 抽 `_clamp_va` / `_clamp_importance` / `_strip_md_fence` 三个模块级 helper；抽 `_safe_embed` async helper（4 处 fire-and-forget 收敛到 1 处）；提 21 个常量（聚类阈值 `_PATTERN_SIMILARITY_THRESHOLD=0.7` 等） |
| 6 | [src/bucket_manager.py](src/bucket_manager.py) | 1237 → 1306 | 抽 `_active_dirs` 属性（5 处目录列表收敛）；抽 `_iter_md_files` 生成器（5 处 walk 收敛）；抽 `_primary_domain` 静态方法；抽模块级 `_clamp01`；提 17 个常量（`_RIPPLE_HOURS=48.0` / `_RIPPLE_MAX_BUCKETS=5` / `_TIME_DECAY_LAMBDA=0.02`） |
| 7 | [src/server.py](src/server.py) | 2969 → 2999 | 顶部预 import `JSONResponse as _JSONResponse`；提 8 个常量（`_PASSWORD_SALT_BYTES=16` / `_SESSION_TOKEN_BYTES=32` / `_SESSION_TTL_SECONDS=86400*7` / `_WEBHOOK_TIMEOUT_SECONDS=5.0`） |

### 没动的边界（故意不改的部分）

- `dehydrator` 4 个 prompt 字符串内容（涉及 LLM 行为）
- `import_memory` 三个 parser（`_parse_claude_json` / `_parse_chatgpt_json` / `_parse_markdown`，兼容性风险）
- `bucket_manager` `create` / `update` / `search` / `archive` / `set_anchor` / `count_anchors` 公共签名
- `bucket_manager` 评分公式（`math.sqrt(2)` / `0.02` / `1.414` 数值等价验证：浮点差 < 1e-3）
- `bucket_manager` RED-02 anchor 上限校验逻辑（最近修过的代码，零改动）
- `server.py` 任何 `@mcp.tool()` 注册块的签名 / docstring / 转发参数
- `server.py` 任何 `@mcp.custom_route` endpoint 的函数体逻辑
- `server.py` 没抽 `_require_auth` 装饰器（与 starlette/MCP 装饰器组合存在签名兼容风险，43 处样板的"小冗余"是可接受的代价）
- 全部 13 个 OB-* 错误码（rule.md §11，零改动）

### 测试

- 基线：`404 passed, 7 skipped`（与 2.0.0 一致）
- 每站完成必跑 `pytest tests/ -q`，不绿则不进下一站

### 【人话】（克劳德写给婷易）

这次走得最稳的一处是 `dehydrator` 抽 `_chat`——你以后想换 prompt 模型 / 加重试 / 换温度，只需要改一个 helper 而不是 5 个 `_api_*` 函数。最克制的一处是 `server.py`——我没敢碰任何 endpoint，鉴权/会话/CSRF/HTTPS 检测一字未改，这是因为你之前说过"现有报错结构不破坏"，server.py 是错误体系最薄的一层、动一处就可能错位。

如果你以后再做一次类似的横扫，记住：先跑测试拿基线 → 从最不重要的文件开始 → 每改一处立刻 pytest → 每个文件改完停下来报告一次。这是这次七站零回滚的核心。

---

## [2.0.0] — 2026-04-xx — 重构准备版

> 详见 git history。本 CHANGELOG 自 2.0.1 起开始维护。
