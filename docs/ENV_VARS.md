# 环境变量参考

## 压缩组（脱水 / 打标 / 合并）

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `OMBRE_COMPRESS_API_KEY` | 是 | — | 脱水/打标/合并/拆分所用 LLM 的 API Key。支持任何 OpenAI 兼容 API（DeepSeek / Gemini / SiliconFlow / Ollama 等） |
| `OMBRE_COMPRESS_BASE_URL` | 否 | `https://api.deepseek.com/v1` | 压缩 LLM 的 API Base URL（覆盖 `dehydration.base_url`） |
| `OMBRE_COMPRESS_MODEL` | 否 | `deepseek-chat` | 压缩 LLM 模型名（覆盖 `dehydration.model`） |

## 向量化组（Embedding）

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `OMBRE_EMBED_API_KEY` | 否 | — | 向量化 API Key。仅 `api` 后端读取；不配置时语义检索不可用，但桶仍可正常写入（仅关键词匹配） |
| `OMBRE_EMBED_BASE_URL` | 否 | `https://generativelanguage.googleapis.com/v1beta/openai/` | 向量化 API Base URL（覆盖 `embedding.base_url`，仅 `api` 后端读取） |
| `OMBRE_EMBED_MODEL` | 否 | `gemini-embedding-001` | 向量嵌入模型名（覆盖 `embedding.model`，仅 `api` 后端读取） |
| `OMBRE_EMBED_BACKEND` | 否 | `local` | 向量后端：`local`（本地 fastembed 内置 bge-m3 优化版，~600-800MB 首次启动自动下载）/ `api`（云端 OpenAI 兼容 endpoint）。旧值 `gemini` / `bge-m3` / `bge-small-zh` 自动映射到 `api` / `local` / `local`。`local` 后端需 `pip install -r requirements-local.txt`（或镜像 `--build-arg INSTALL_LOCAL_EMBED=1`），不读 `OMBRE_EMBED_API_KEY` |

## 通用 / 系统

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `OMBRE_TRANSPORT` | 否 | `stdio` | MCP 传输模式：`stdio` / `sse` / `streamable-http` |
| `OMBRE_PORT` | 否 | `8000` | HTTP/SSE 模式监听端口（仅 `sse` / `streamable-http` 生效） |
| `OMBRE_BUCKETS_DIR` | 否 | `<repo_root>/buckets` | （旧名，仍兼容）记忆桶文件存放目录。新部署建议使用 `OMBRE_VAULT_DIR` |
| `OMBRE_VAULT_DIR` | 否 | `<repo_root>/buckets` | （推荐）记忆桶文件存放目录。优先级：`OMBRE_BUCKETS_DIR` > `OMBRE_VAULT_DIR` > `config.yaml.buckets_dir`。两者只设其一即可 |
| `OMBRE_CONFIG_PATH` | 否 | — | 显式指定 `config.yaml` 完整路径；不设时按 `cwd/config.yaml` → `<repo_root>/config.yaml` 顺序查找 |
| `OMBRE_HOOK_URL` | 否 | — | Breath/Dream Webhook 推送地址（POST JSON），留空则不推送 |
| `OMBRE_HOOK_SKIP` | 否 | `false` | 设为 `true`/`1`/`yes` 跳过 Webhook 推送（即使 `OMBRE_HOOK_URL` 已设置） |
| `OMBRE_DASHBOARD_PASSWORD` | 否 | — | 预设 Dashboard 访问密码；设置后覆盖文件存储的密码，首次访问不弹设置向导 |
| `OMBRE_LOG_DIR` | 否 | `<OMBRE_BUCKETS_DIR>/.logs`，否则 `/tmp/ombre_logs` | `server.log`（RotatingFileHandler，1MB×3）落盘目录。Dashboard「日志」页通过 `/api/logs` 读它 |
| `OMBRE_LOG_FILE` | 否 | 由 `setup_logging` 自动写入 | **由系统设置，不需要手动配**。供 `/api/logs` 定位 server.log 完整路径 |

## 说明

- `OMBRE_COMPRESS_API_KEY` 和 `OMBRE_EMBED_API_KEY` 也可分别在 `config.yaml` 的 `dehydration.api_key` / `embedding.api_key` 中设置，但**强烈建议**通过环境变量传入，避免密钥写入文件。
- 压缩和向量化可以使用完全不同的 provider 和 key——这正是两组变量独立存在的意义。
- `OMBRE_DASHBOARD_PASSWORD` 设置后，Dashboard 的"修改密码"功能将被禁用（显示提示，建议直接修改环境变量）。未设置则密码存储在 `{buckets_dir}/.dashboard_auth.json`（SHA-256 + salt）。
- **项目版本号**由根目录 `VERSION` 文件提供，不是环境变量。`utils.get_version()` 读取，`/api/version` 暴露给前端。每次发版只需要改这个文件（外加 git tag）。

## Webhook 推送格式 (`OMBRE_HOOK_URL`)

设置 `OMBRE_HOOK_URL` 后，Ombre Brain 会在以下事件发生时**异步**（fire-and-forget，5 秒超时）`POST` JSON 到该 URL：

| 事件名 (`event`) | 触发时机 | `payload` 字段 |
|------------------|----------|----------------|
| `breath` | MCP 工具 `breath()` 返回时 | `mode` (`ok`/`empty`), `matches`, `chars` |
| `dream` | MCP 工具 `dream()` 返回时 | `recent`, `chars` |
| `breath_hook` | HTTP `GET /breath-hook` 命中（SessionStart 钩子） | `surfaced`, `chars` |
| `dream_hook` | HTTP `GET /dream-hook` 命中 | `surfaced`, `chars` |

请求体结构（JSON）：

```json
{
  "event": "breath",
  "timestamp": 1730000000.123,
  "payload": { "...": "..." }
}
```

Webhook 推送失败仅在服务日志中以 WARNING 级别记录，**不会影响 MCP 工具的正常返回**。
