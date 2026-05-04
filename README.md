# Ombre Brain

一个给 Claude（或其它 MCP 客户端）用的长期情绪记忆系统。基于 Russell 效价/唤醒度坐标打标，Obsidian 做存储层，MCP 接入，带遗忘曲线和向量语义检索。

A long-term emotional memory system for Claude (and any MCP client). Tags memories using Russell's valence/arousal coordinates, stores them as Obsidian-compatible Markdown, connects via MCP, with forgetting curve and vector semantic search.

> **开发者文档**：架构 / API / 配置细节请见 [docs/INTERNALS.md](docs/INTERNALS.md)。本 README 只关心『怎么把它跑起来用上』。
>
> **从 1.6 升级？** 项目结构在 1.7 调整了（源码进 `src/`、运维进 `deploy/`、文档进 `docs/`）。命令变了但**数据 0 丢失**，先看 [docs/UPGRADE.md](docs/UPGRADE.md)。

---

## 它是什么 / What is this

Claude 没有跨对话记忆。每次新会话开始，之前聊过的东西都消失。

Ombre Brain 给它一套持久记忆——不是冷冰冰的键值存储，而是带情感坐标、会自然衰减、像人类一样会遗忘和浮现的系统。

Claude has no cross-conversation memory. Everything from a previous chat vanishes once it ends.

Ombre Brain gives it persistent memory — not cold key-value storage, but a system with emotional coordinates, natural decay, and forgetting/surfacing mechanics that loosely mimic how human memory works.

**核心特性 / Key features**

- **情感坐标打标**：每条记忆用 Russell 环形情感模型的 valence（效价）+ arousal（唤醒度）两个连续维度标记，不是「开心/难过」这种离散标签
- **双通道检索**：rapidfuzz 关键词匹配 + cosine 向量语义并联检索，去重合并后按 token 预算截断
- **自然遗忘**：改进版艾宾浩斯遗忘曲线，不活跃的记忆自动衰减归档，高情绪强度的记忆衰减更慢
- **权重池浮现**：未解决的、情绪强烈的记忆权重更高，对话开头自动浮现
- **Obsidian 原生**：每个记忆桶 = 一个 Markdown 文件 + YAML frontmatter，可直接在 Obsidian 浏览编辑
- **历史对话导入**：批量导入 Claude / ChatGPT / DeepSeek 历史对话，分块处理带断点续传
- **每条记忆带「为什么」（1.8）**：可选的 `why_remembered` 字段，让记忆本身解释自己为什么不能掉；`first_of_kind` 自动给「第一次」留位置；`dont_surface` 主动遗忘——比删除温柔，比 resolve 安静
- **dashboard 把控制权交还给你（1.9）**：批量主动遗忘 + 已遗忘折叠区、触发反向链跳转、加权采样面板（开关 + top_k / sample_k / 温度可热调），plan 承诺重量带「轻 / 中 / 重 / 必须」语义档位
- **Anchor — 24 槽坐标系（2.0）**：拆开「不许淡」和「最重要」。`anchor` 是「定义我们是谁」的事实，不主动浮现到默认 breath，但 query/domain/emotion 命中时仍可被找到。硬上限 24，满了之后想加新的必须先 release——稀缺即结构。
- **Dashboard**：内置 Web 管理面板，密码保护，桶列表 / 检索调试 / 记忆网络 / 配置管理 / 信件入口

---

## 边界 / Design boundaries

官方记忆功能（Claude、ChatGPT 自带的 memory）已经在做身份层的事——你是谁、有什么偏好、你们的关系是什么。Ombre Brain 不重复造轮子。

Ombre Brain 的边界是**时间里发生的事**，不是**你是谁**。它记住的是：你们聊过什么、经历了什么、哪些事还悬在那里没解决。两层配合用，才是完整的。

每次新对话，Claude 从零开始——但它能从 Ombre Brain 里把跟你有关的一切找回来。不是重建，是接续。

---

## 快速开始 / Quick Start（Docker Hub 预构建镜像）

> 不需要 clone 代码，不需要 build。第一次完整跑通约 5 分钟。

### 第零步：装 Docker Desktop

打开 [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)，下载对应你系统的版本，安装后启动。Windows 用户安装时会提示启用 WSL 2，点同意。

### 第一步：打开终端

| 系统 | 怎么打开 |
|---|---|
| **Mac** | `⌘ + 空格` → 输入 `终端` → 回车 |
| **Windows** | `Win + R` → 输入 `cmd` → 回车 |
| **Linux** | `Ctrl + Alt + T` |

### 第二步：创建工作文件夹

```bash
mkdir ombre-brain && cd ombre-brain
```

### 第三步：拿一个 LLM API Key

Ombre Brain 用 LLM 做脱水压缩、自动打标、合并判定——强烈推荐配置。**没有 LLM key 时 `hold` / `grow` 会直接报错并不创建桶**（返回明确提示：「API key 未配置或调用失败，请检查 OMBRE_COMPRESS_API_KEY」）。没有向量化 key（`OMBRE_EMBED_API_KEY`）时桶仍能正常写入，但返回会追加警告：「向量化失败，该桶不参与语义检索，仅支持关键词匹配」。换句话说：LLM key 是记忆写入的必要条件，embed key 是可降级选项。

**推荐免费方案：Google AI Studio**

1. 打开 [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. 用 Google 账号登录 → 点 **Create API key** → 复制
3. 免费额度（截至 2025 年，请以官网实时信息为准）：
   - 脱水/打标模型 `gemini-2.5-flash-lite`：30 req/min
   - 向量化模型 `gemini-embedding-001`：1500 req/day，3072 维

也支持任何 OpenAI 兼容接口：DeepSeek / SiliconFlow / Ollama / LM Studio / vLLM 等。

### 第四步：下载 compose 文件并启动

```bash
# 下载用户版 compose 文件
curl -O https://raw.githubusercontent.com/P0luz/Ombre-Brain/main/deploy/docker-compose.user.yml

# 创建 .env 文件——把 your-key-here 换成第三步拿到的 key
echo "OMBRE_COMPRESS_API_KEY=your-key-here" > .env

# 拉取镜像并启动（第一次会下载约 500MB）
docker compose -f docker-compose.user.yml up -d
```

### 第五步：验证

```bash
curl http://localhost:8000/health
```

返回 `{"status":"ok",...}` 即成功。

浏览器打开 Dashboard：**http://localhost:8000/dashboard**

> 第一次访问会弹出密码设置向导，设好密码后所有 `/api/*` 端点都需要这个密码登录。也可以通过环境变量 `OMBRE_DASHBOARD_PASSWORD` 预设密码（设置后 UI 改密码功能会被禁用）。

### 第六步：接入 Claude

#### Claude Desktop

打开配置文件（macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`，Windows：`%APPDATA%\Claude\claude_desktop_config.json`），加入：

```json
{
  "mcpServers": {
    "ombre-brain": {
      "type": "streamable-http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

重启 Claude Desktop，工具列表里会出现 11 个工具：`breath` / `hold` / `grow` / `trace` / `pulse` / `dream` / `plan` / `letter_write` / `letter_read` / `anchor` / `release`。

#### Claude.ai 网页版（远程）

需要把服务暴露到公网。常见做法：[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) 或 [ngrok](https://ngrok.com/)。然后在 Claude.ai 添加 MCP 服务器，URL 形如：`https://你的隧道域名/mcp`。

### 把记忆挂到 Obsidian

打开 `docker-compose.user.yml`，把 `./buckets:/data` 改成你的 Vault 路径：

```yaml
- /Users/你的用户名/Documents/Obsidian Vault/Ombre Brain:/data
```

然后重启：

```bash
docker compose -f docker-compose.user.yml down
docker compose -f docker-compose.user.yml up -d
```

之后每条记忆就是 Vault 里一个 Markdown 文件，可在 Obsidian 直接浏览编辑。

---

## 从源码部署 / Deploy from Source

适合想自己改代码或不想用预构建镜像的用户。

```bash
git clone https://github.com/P0luz/Ombre-Brain.git
cd Ombre-Brain

# 创建 .env
echo "OMBRE_COMPRESS_API_KEY=你的key" > .env

# 调整 deploy/docker-compose.yml 里的 volume 挂载
# - ../buckets:/data
# 改成你的 Obsidian Vault 路径

docker compose -f deploy/docker-compose.yml up -d
```

验证：

```bash
docker logs ombre-brain   # 看到 "Uvicorn running on http://0.0.0.0:8000"
curl http://localhost:18001/health   # docker-compose.yml 默认映射 18001:8000
```

Dashboard：`http://localhost:18001/dashboard`

### 不用 Docker（纯 Python）

```bash
git clone https://github.com/P0luz/Ombre-Brain.git
cd Ombre-Brain

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config.example.yaml config.yaml   # 按需修改
export OMBRE_COMPRESS_API_KEY="你的key"

python src/server.py
```

Claude Desktop 配置改用 stdio：

```json
{
  "mcpServers": {
    "ombre-brain": {
      "command": "python",
      "args": ["/path/to/Ombre-Brain/src/server.py"],
      "env": { "OMBRE_COMPRESS_API_KEY": "你的key" }
    }
  }
}
```

---

## 部署到云平台 / Deploy to Cloud Platforms

### Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/P0luz/Ombre-Brain)

> ⚠️ **免费层不可用**：Render 免费层无持久化磁盘，重启后记忆会丢失，且无流量时会休眠。**必须使用 Starter（$7/mo）或以上**。

仓库已包含 `render.yaml`。点按钮后：

1. 设置环境变量 `OMBRE_COMPRESS_API_KEY`（必需）
2. 可选 `OMBRE_COMPRESS_BASE_URL`，例如 `https://api.deepseek.com/v1`；可选 `OMBRE_EMBED_API_KEY` 启用语义检索
3. 持久化磁盘自动挂载到 `/opt/render/project/src/buckets`
4. 部署后 Dashboard：`https://<服务名>.onrender.com/dashboard`，MCP URL：`https://<服务名>.onrender.com/mcp`

### Zeabur

[![Deploy on Zeabur](https://zeabur.com/button.svg)](https://zeabur.com/templates/OMBRE-BRAIN)

Zeabur 是「VPS + 平台托管」模式，需要先购买一台服务器（最低约 $2-3/mo），再订阅 Developer 方案（$5/mo）。Volume 直接挂在服务器上，**数据天然持久化**。

部署步骤：

1. **创建项目** — Fork 本仓库 → Zeabur **New Project** → **Deploy from GitHub** → 选 `你的用户名/Ombre-Brain`（自动识别 Dockerfile）
2. **设置环境变量**（Variables 标签页）— `OMBRE_COMPRESS_API_KEY` 必填，`OMBRE_COMPRESS_BASE_URL` / `OMBRE_EMBED_API_KEY` 可选
3. **挂载 Volume**（Volumes 标签页）— 挂载路径填 **`/app/buckets`**
4. **配置端口**（Networking 标签页）— Port `8000`，类型 `HTTP`，点 **Generate Domain**
5. 验证：`https://<域名>.zeabur.app/health`

> **不需要**手动设置 `OMBRE_TRANSPORT` 和 `OMBRE_BUCKETS_DIR`，Dockerfile 已设好默认值。

### 自有 VPS

```bash
git clone https://github.com/P0luz/Ombre-Brain.git
cd Ombre-Brain
echo "OMBRE_COMPRESS_API_KEY=你的key" > .env
docker compose up -d
```

配合 nginx / caddy 反代到 443 端口即可。

---

## Dashboard 简介

启动后浏览器打开 `/dashboard` 进入。功能：

- **记忆桶列表** — 浏览所有桶，按 domain / type 筛选，单桶可 pin / resolve / archive / delete
- **Breath 调试** — 模拟检索查询，查看每个桶的四维评分分解
- **记忆网络** — 基于 embedding 相似度的桶关系图
- **配置** — 在线修改 dehydration / embedding / merge_threshold（可持久化到 yaml）
- **导入** — 上传历史对话文件（Claude JSON / ChatGPT / DeepSeek / Markdown / 纯文本）批量导入
- **设置** — 修改密码、查看版本/embedding/decay 状态、配置宿主机 vault 路径
- **Letters** — 双向信件入口（`/letters`）

---

## 给 Claude 的使用指南 / Usage Guide for Claude

`docs/CLAUDE_PROMPT.md` 是写给 Claude 看的工具使用约定。把它放进 system prompt / custom instructions / Claude Desktop 项目说明里即可。

`docs/CLAUDE_PROMPT.md` is the usage guide written for Claude. Put it in your system prompt or custom instructions.

---

## 配置 / Configuration

所有可调参数都在 `config.yaml`（从 `config.example.yaml` 复制）。最常用的几个：

| 参数 | 说明 | 默认 |
|---|---|---|
| `transport` | `stdio`（本地）/ `streamable-http`（远程） | `stdio` |
| `buckets_dir` | 桶存储路径，可指向 Obsidian Vault | `./buckets/` |
| `dehydration.model` | 脱水/打标 LLM 模型 | `deepseek-chat` |
| `dehydration.base_url` | LLM API 地址 | `https://api.deepseek.com/v1` |
| `embedding.backend` | `local`（本地 bge-m3）/ `api`（云端） | `local` |
| `embedding.model` | embedding 模型（仅 api 后端用） | `gemini-embedding-001` |
| `decay.lambda` | 衰减速率，越大越快忘 | `0.05` |
| `merge_threshold` | 合并相似度阈值 (0-100) | `75` |

完整环境变量清单：[docs/ENV_VARS.md](docs/ENV_VARS.md)。完整开发者文档：[docs/INTERNALS.md](docs/INTERNALS.md)。

### Embedding 两后端

| backend | 类型 | 体积 | 维度 | 备注 |
|---|---|---|---|---|
| `local` | 本地 ONNX（fastembed 内置 bge-m3 优化版） | ~600–800MB 模型 + ~300–500MB 运行时 | 1024 | 默认。多语言 + 长文本，CPU 推理；首次启动自动下载（先 huggingface.co，失败回落 hf-mirror.com） |
| `api` | 云端 OpenAI 兼容 | — | 取决于模型（Gemini 默认 3072） | 需 `OMBRE_EMBED_API_KEY`，Google AI Studio 免费层够用 |

切换方式：

- **推荐**：Dashboard → 设置页 → Embedding 区 → 选目标 backend → 点「切换 / 重算所有 embedding…」。会自动备份 `embeddings.db.backup`、按新维度重算所有 bucket 向量、断点续传、3s 一刷新进度，全程后台跑、不阻塞正常使用。
- 命令行 / 配置文件：`config.yaml` 里改 `embedding.backend`，或环境变量 `OMBRE_EMBED_BACKEND=local`。这条路径**不会**自动重算旧 DB，需要手动跑 `tools/backfill_embeddings.py`（见下方）。
- 默认 Docker 镜像不含 `fastembed`；要走 `local` 后端，构建时加 `--build-arg INSTALL_LOCAL_EMBED=1`，或本机 `pip install -r requirements-local.txt`。

### 系统要求

| 模式 | 内存 | 磁盘 |
|---|---|---|
| `api` 后端（默认 Docker 镜像） | ~200MB | ~150MB（镜像） |
| `local` 后端（本机或 `INSTALL_LOCAL_EMBED=1` 镜像） | ~800MB–1.2GB（含 ONNX 运行时） | ~150MB（代码）+ ~600–800MB（首次启动自动下载的 bge-m3 ONNX 权重） |

第一次启动 `local` 后端时，模型下载进度会出现在 Dashboard 设置页 Embedding 区的「模型状态」面板。下载到 `~/.cache/fastembed/`（容器里是 `/root/.cache/fastembed/`），同一台机器的多个项目可共享。

**已有桶补 embedding**：

```bash
OMBRE_EMBED_API_KEY="你的key" python tools/backfill_embeddings.py --batch-size 20
# Docker 用户：
docker exec -e OMBRE_BUCKETS_DIR=/data ombre-brain python3 tools/backfill_embeddings.py --batch-size 20
```

---

## 更新 / How to Update

记忆数据存在 volume / 挂载目录里，更新不会丢。

### Docker Hub 镜像用户

```bash
docker pull p0luz/ombre-brain:latest
docker compose -f docker-compose.user.yml down
docker compose -f docker-compose.user.yml up -d
```

### 从源码部署用户

```bash
cd Ombre-Brain
git pull origin main
docker compose down && docker compose build && docker compose up -d
```

### 纯 Python 用户

```bash
cd Ombre-Brain
git pull origin main
pip install -r requirements.txt
# Ctrl+C 停旧进程后重新 python src/server.py
```

### Render / Zeabur

平台已连接 GitHub，Fork 同步上游后自动重新部署，或在控制台点 **Manual Deploy / Redeploy**。持久化磁盘 / Volume 数据保留。

---

## 测试 / Testing

```bash
pip install pytest pytest-asyncio
pytest tests/                  # 全部测试
pytest tests/unit/             # 单元测试
pytest tests/integration/      # 集成测试（场景全流程）
pytest tests/regression/       # 回归测试
```

所有测试都在 `tmp_path` 临时目录运行，**绝不触碰你的真实记忆数据**。

---

## 工具脚本 / Utility Scripts

| 脚本 | 用途 |
|---|---|
| `tools/backfill_embeddings.py` | 为存量桶批量补生成 embedding |
| `src/write_memory.py` | CLI 直写记忆，绕过 MCP |
| `tools/reclassify_domains.py` | 基于关键词重分类 |
| `src/reclassify_api.py` | 用 API 重打标未分类桶 |
| `tools/check_buckets.py` | 数据完整性检查 |
| `tools/check_icloud_conflicts.py` | iCloud 同步冲突文件清理（Vault 在 iCloud 时有用） |

---

## 常见问题 / Troubleshooting

| 现象 | 可能原因 | 解决 |
|---|---|---|
| Dashboard 401 | 未登录 / 密码错 | 浏览器登录或重置 `OMBRE_DASHBOARD_PASSWORD` |
| Claude Desktop 看不到工具 | URL 末尾少 `/mcp` | 确认 URL 是 `http://localhost:8000/mcp` |
| `hold` / `grow` 报 API key 错误 | LLM key 未配置或调用失败 | 检查 `OMBRE_COMPRESS_API_KEY` 是否设对。LLM key 是记忆写入的必要条件，没有 key 时 hold/grow 会直接报错而不创建桶。向量化 key（`OMBRE_EMBED_API_KEY`）缺失时桶仍可写入，但不参与语义检索；补 embed key 后可用 `tools/backfill_embeddings.py` 回填向量 |
| 重启后记忆丢失 | Volume 没挂载 | 检查 docker-compose volume 配置或 Render Disk / Zeabur Volume |
| 隧道连接偶尔断 | Cloudflare Free 闲置超时 | 内置 keepalive 已缓解，可缩短隧道超时配置 |
| 改 `host_vault_dir` 不生效 | 写入 `.env` 后需要重启 | `docker compose down && docker compose up -d` |

---

## License

MIT
