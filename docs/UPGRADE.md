# 升级指南 / Upgrade Guide

> 当老用户更新到 1.7+ 后，命令和路径变了，但**数据 0 丢失**。本文档说明所有变化，以及如果踩到老命令的报错，怎么自适应。

## 1.6.x → 1.7.x

### TL;DR

| 老的命令 / 路径 | 新的命令 / 路径 |
|----------------|----------------|
| `python server.py` | `python src/server.py` |
| `python backfill_embeddings.py` | `python tools/backfill_embeddings.py` |
| `docker compose -f docker-compose.yml up` | `docker compose -f deploy/docker-compose.yml up` |
| `INTERNALS.md` | `docs/INTERNALS.md` |
| `dashboard.html`（根目录） | `frontend/dashboard.html`（服务自动找到，无需手动） |

### 数据安全

- `buckets/` 目录默认值仍是 `<repo_root>/buckets/`，**没有移动**。如果你之前用默认配置，升级后 `git pull` 不会动你的数据。
- Docker volume 也仍然落在 `<repo_root>/buckets`（compose 文件位置变了，但 `../buckets:/data` 解析路径与之前等价）。
- 如果你设置了环境变量 `OMBRE_BUCKETS_DIR=/some/path`，照旧生效。
- 如果你设置了环境变量 `OMBRE_CONFIG_PATH=/some/file.yaml`（1.7 新增），优先于 cwd 和 repo 根目录的 `config.yaml`。

启动时如果检测到 `<buckets_dir>` 里已有 `.md` 文件，日志会打 `[migration] existing buckets detected at ... — zero data loss expected.`，可以放心继续。

### 升级步骤（pip 用户）

```bash
git pull
pip install -r requirements.txt
python src/server.py        # ← 命令变了
```

### 升级步骤（Docker 用户）

```bash
git pull
docker compose -f deploy/docker-compose.yml down
docker compose -f deploy/docker-compose.yml build --no-cache
docker compose -f deploy/docker-compose.yml up -d
```

> 注意：`docker-compose.yml` 现在在 `deploy/` 目录。老命令 `docker compose up` 在仓库根目录会报「no configuration file」。

### 客户端 / Claude.ai 配置不需要改

MCP 服务的 URL、端口、传输协议、API key 都没动。客户端那边照旧。

### Dashboard 不需要重新登录

Dashboard 密码、cookie、登录状态都保留。

## 1.9.x → 2.0.x

### TL;DR

| 关心的事 | 答案 |
|---|---|
| 数据要迁移吗 | **不需要**。2.0 引入 `anchor: bool` 字段，老桶没写 = 默认不是 anchor，行为完全不变。 |
| 命令变了吗 | 没变。新增两个 MCP tool：`anchor(bucket_id)` 和 `release(bucket_id)`。 |
| 老的 `pinned` 还能用吗 | 完全照旧。pinned 和 anchor 是两个独立轴。 |
| 我现在该做什么 | （可选）打开 dashboard 的 Anchor tab，扫一眼自己的 pinned 列表，挑出几条真正是「定义我们是谁 / 关系基石」的 anchor 起来。pinned 减负，breath 上下文也更干净。 |

### 新增字段

`anchor: true` —— 标记某个桶为「坐标系」。

### 行为变化（重要）

**默认 `breath()`（不传 query/domain/emotion）现在会跳过 anchor 桶。**

- 这是有意的。anchor = 地基，地基不该天天出现在你眼前。
- 想找 anchor 信息？用 `breath(query="...")` / `breath(domain=[...])` / `breath(importance_min=N)`。这些路径下 anchor 仍然会浮现。
- 如果你之前依赖某条 pinned 在每次对话开头自动出现，**不要把它 anchor**——保持 pinned 即可。

### 24 硬上限

`anchor()` MCP 工具 / `POST /api/bucket/{id}/anchor` 都会在已有 24 条 anchor 时拒绝。
满了之后想 anchor 新的，必须先 `release(...)` 一条旧的。这是设计——稀缺即结构。

### Dashboard

- 顶部多了一个 ⚓ 计数器，显示当前 `count/24`。
- 多了「Anchor」tab，可以看所有 anchor 槽并 release。
- 桶详情页 pin 按钮旁多了 anchor 按钮。

### 端点

- `GET /api/anchors`
- `POST /api/bucket/{id}/anchor`（toggle，409 表示满了）

## 1.8.x → 1.9.x

### TL;DR

| 关心的事 | 答案 |
|---|---|
| 数据要迁移吗 | **不需要**。1.9 全部是 dashboard 体验 + 后端补全，frontmatter 字段不变。 |
| 命令变了吗 | 没变。 |
| 配置文件要改吗 | 不强制。新增 `bucket_type_defaults` 段；不写就保持 1.8 行为。 |
| 环境变量改了吗 | 新增 `OMBRE_VAULT_DIR` 推荐用法，旧 `OMBRE_BUCKETS_DIR` 仍兼容（优先级见 ENV_VARS.md）。 |
| 前端要重新登录吗 | 不需要。 |

### 1.9 主要变化

- **dashboard 加权采样面板**：在「设置」tab 新增控制 `breath` 加权采样的开关 + top_k / sample_k / temperature。`POST /api/settings/sampling` 热更新。
- **批量主动遗忘**：`POST /api/buckets/forget` 接收 `{ids:[...], dont_surface: bool}`。dashboard 列表底部新增「已主动遗忘」折叠区，可单条/全部恢复。
- **触发反向链**：`/api/bucket/{id}` 现在返回 `triggered_feels: [{id,name,created}]` —— dashboard 详情页能看到「这条触发了 N 条 feel」并可跳转。
- **承诺重量语义档位**：plan 桶 weight 显示「轻 25% · 中 50% · 重 75% · 必须 100%」，编辑表单滑块带锚点。
- **`bucket_type_defaults`**：在 config.yaml 写 `bucket_type_defaults.letter.weight: 1.0` 之类，create 时自动应用（仅当调用方未显式传该字段）。
- **采样回退日志**：当 sampling 启用但候选池太小自动退回固定排序时，会打 INFO 日志，告诉你「不是没生效，是池太小」。
- **`OMBRE_VAULT_DIR`**：统一推荐的环境变量名。优先级 `OMBRE_BUCKETS_DIR` > `OMBRE_VAULT_DIR` > `config.yaml.buckets_dir`，老用户继续沿用 `OMBRE_BUCKETS_DIR` 不会有任何影响。

### 设计原则

1. 1.9 不动 frontmatter schema，所以无需迁移脚本。
2. 所有 dashboard 新功能都对应一个公开端点，方便脚本同样调用。
3. 反向链查询是单次 list 扫描，feel 数量小不至于成为瓶颈；将来真要优化可改为索引。
4. `bucket_type_defaults` 只补 caller 没传的字段，**绝不覆盖显式值**。

## 1.7.x → 1.8.x

### TL;DR

| 关心的事 | 答案 |
|---|---|
| 数据要迁移吗 | **可选**。所有 1.8 新字段都是 optional，老桶不补也能跑。 |
| 命令变了吗 | 没变。`python src/server.py` 仍然有效。 |
| 配置文件要改吗 | 不强制。新增 `breath.sampling.*` 子段；不写就走默认（已禁用，行为同 1.7）。 |
| 前端要重新登录吗 | 不需要。 |

### 一行话回填脚本（推荐但非必须）

```bash
python tools/migrate_v17_to_v18.py            # 给所有桶补 1.8 默认字段
python tools/migrate_v17_to_v18.py --dry-run  # 只列出会被改的桶，不写盘
```

幂等：跑两次结果一样；不会覆盖你已经手填的字段。

### 1.8 新增了什么

- **`why_remembered`**：每条桶可选「为什么我把它记下来」一句话。dashboard 详情顶部以朱砂引文渲染。
- **`dont_surface`（主动遗忘）**：把某条桶静音，无参 `breath()` 不再主动浮现，但搜索仍能找到。
  比 `delete` 温柔，比 `resolve` 安静。
- **`first_of_kind`**：写入新桶时，若 tag 与全库已有 tag 完全无交集，自动标 ✨。
- **`weight`**（plan 桶专有，0–1）：承诺重量。不参与评分，仅用于看板排序。
- **`triggered_by`**（feel 桶可选）：feel 的因果链入口，记下被哪条原始记忆触发。
- **`/api/bucket/{id}/forget`**：dashboard 新增「主动遗忘」按钮的后端。
- **加权采样 breath**（默认关闭）：`config.surfacing.sampling_enabled=true` 后，无参 `breath` 从 top-K 候选里按 score 加权抽样，给「忽然想起」留一点温度。开关关上时行为与 1.7 完全一致。

### 设计原则（如果你打算继续魔改）

1. 不强制：所有新字段 optional，老桶不会崩。
2. 不算分：1.8 字段全部不进 `decay_engine.calculate_score`。
3. 采样不排序：breath 改加权采样，但 `enabled=False` 时回退到 1.7 的确定性排序，所以测试可重现。
4. 可遗忘 ≠ 删除：`dont_surface=True` 不动磁盘，搜索仍可达。
5. 第一次会被记得：`first_of_kind` 自动检测，给「再也没有第二次的事」留位置。

## 新功能（1.7）

- **记忆网络**：现在按 `[[wikilink]]` 双链构建（之前是 embedding 相似度）。如果你之前依赖相似度图，加 `?mode=embedding` 仍可用。
- **计划视图**：新增独立 Plan 看板（active / resolved / abandoned），支持勾选/打叉/编辑/查看变更历史。
- **作者有话说**：dashboard 末尾新增「关于」入口。
- **图标**：dashboard 改用 Lucide 矢量图标，加了 OB 应用图标，「添加到桌面 / 添加到 Dock」时不再是空白。
- **版本号统一**：根目录 `VERSION` 文件作为唯一来源，`/api/version` 暴露给前端。

## 遇到问题？

1. 先看终端 stderr，第一行会打 `Ombre Brain v<version>`。
2. 数据没了？检查 `<repo_root>/buckets/` 是不是还在；99% 的情况是命令路径错了，不是数据丢了。
3. 仍然有问题，提 issue。
