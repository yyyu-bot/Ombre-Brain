"""
========================================
utils.py — 整个项目共享的小工具集合
========================================

配置加载、日志初始化、路径安全校验、ID 生成、token 估算、时间格式化——
所有「跨模块要用、又不属于任何业务逻辑」的小函数都在这里。

关键行为：
- load_config()：读 config.yaml，处理环境变量覆盖（OMBRE_VAULT_DIR 等），mkdir 必要目录
- setup_logger()：统一日志格式，控制台 + 可选文件
- safe_path()：禁止路径穿越（OWASP）
- generate_bucket_id()：12 位 hex，碰撞概率忽略
- count_tokens_approx()：按字符数粗估 token，离线用
- now_iso() / parse_iso()：统一时间字符串

不做什么（边界）：
- 不依赖任何业务模块（被所有模块依赖，不能反向 import）
- 不做 LLM / 网络调用
- 不做记忆桶相关业务逻辑

对外暴露：上述所有函数
========================================
"""

import os
import re
import sys
import uuid
import yaml
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional


# ============================================================
# 常量 / Named constants
# ------------------------------------------------------------
# rule.md §⑩：禁止裸魔法数字。下面这几个值原本散在函数体内，
# 抽到这里是为了：① 一眼能看清"调参面板"；② 改一处全文生效。
# ============================================================

# count_tokens_approx() 用的粗估系数。
# 经验值，不追求精确——只为判断"是否需要脱水压缩"。
_TOKEN_RATIO_PER_CN_CHAR = 1.5   # 每个中文字 ≈ 1.5 token
_TOKEN_RATIO_PER_EN_WORD = 1.3   # 每个英文词 ≈ 1.3 token
_TOKEN_RATIO_PER_CHAR = 0.05     # 标点/空格等其它字符的兜底贡献

# setup_logging() 文件日志轮转配置。
_LOG_FILE_MAX_BYTES = 1_000_000  # 单个日志文件 1 MB 后轮转
_LOG_FILE_BACKUP_COUNT = 3       # 保留 3 个历史文件
_LOG_FALLBACK_DIR = "/tmp/ombre_logs"  # 所有候选路径都失败时的最终兜底

# sanitize_name() 桶名最大长度（防止文件名过长导致 OS 报错）。
_BUCKET_NAME_MAX_LEN = 80


def _project_root() -> str:
    """Return absolute path to the project root (parent of src/ where utils.py lives).
    项目根目录（src/ 的上一层）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(config_path: Optional[str] = None) -> dict:
    """
    Load configuration file.
    加载配置文件。

    Priority: environment variables > config.yaml > built-in defaults.
    优先级：环境变量 > config.yaml > 内置默认值。
    """
    project_root = _project_root()
    # --- Built-in defaults (fallback so it runs even without config.yaml) ---
    # --- 内置默认配置（兜底，保证即使没有 config.yaml 也能跑）---
    defaults = {
        "transport": "stdio",
        "log_level": "INFO",
        "buckets_dir": os.path.join(project_root, "buckets"),
        "merge_threshold": 75,
        "dehydration": {
            "model": "gemini-2.0-flash",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "api_key": "",
            "max_tokens": 4096,
            "temperature": 0.1,
        },
        "decay": {
            "lambda": 0.05,
            "threshold": 0.3,
            "check_interval_hours": 24,
            "emotion_weights": {
                "base": 1.0,
                "arousal_boost": 0.8,
            },
        },
        "matching": {
            "fuzzy_threshold": 50,
            "max_results": 5,
        },
    }

    # --- Load user config from YAML file ---
    # --- 从 YAML 文件加载她/他的自定义配置 ---
    if config_path is None:
        # Search order: $OMBRE_CONFIG_PATH → cwd/config.yaml → project_root/config.yaml
        # 查找顺序：环境变量 > 当前工作目录 > 项目根目录
        env_cfg = os.environ.get("OMBRE_CONFIG_PATH", "").strip()
        if env_cfg and os.path.exists(env_cfg):
            config_path = env_cfg
        elif os.path.exists(os.path.join(os.getcwd(), "config.yaml")):
            config_path = os.path.join(os.getcwd(), "config.yaml")
        else:
            config_path = os.path.join(project_root, "config.yaml")

    config = defaults.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
            if isinstance(file_config, dict):
                config = _deep_merge(defaults, file_config)
            else:
                logging.warning(
                    f"Config file is not a valid YAML dict, using defaults / "
                    f"配置文件不是有效的 YAML 字典，使用默认配置: {config_path}"
                )
        except yaml.YAMLError as e:
            logging.warning(
                f"Failed to parse config file, using defaults / "
                f"配置文件解析失败，使用默认配置: {e}"
            )

    # --- Environment variable overrides (highest priority) ---
    # --- 环境变量覆盖敏感/运行时配置（优先级最高）---
    # 这里曾经有 6 段几乎一模一样的 if-block，每段都在做同一件事：
    #   "若环境变量非空 → 写到 config 的某个嵌套 key 上"
    # 现在统一走 _apply_env_override()，新增一项只要加一行表项。

    # 压缩组（脱水/打标/合并）—— 写到 config["dehydration"][*]
    _apply_env_override(config, "OMBRE_COMPRESS_API_KEY", "dehydration", "api_key")
    _apply_env_override(config, "OMBRE_COMPRESS_BASE_URL", "dehydration", "base_url")
    _apply_env_override(config, "OMBRE_COMPRESS_MODEL", "dehydration", "model")
    # Accept both names: OMBRE_COMPRESS_FORMAT (dashboard) and OMBRE_COMPRESS_API_FORMAT (legacy)
    _apply_env_override(config, "OMBRE_COMPRESS_FORMAT", "dehydration", "api_format")
    _apply_env_override(config, "OMBRE_COMPRESS_API_FORMAT", "dehydration", "api_format")

    # 向量化组（embedding）—— 写到 config["embedding"][*]
    _apply_env_override(config, "OMBRE_EMBED_API_KEY", "embedding", "api_key")
    _apply_env_override(config, "OMBRE_EMBED_BASE_URL", "embedding", "base_url")
    _apply_env_override(config, "OMBRE_EMBED_MODEL", "embedding", "model")
    _apply_env_override(config, "OMBRE_EMBED_FORMAT", "embedding", "api_format")

    # 顶层运行时
    _apply_env_override(config, "OMBRE_TRANSPORT", "transport")
    _apply_env_override(config, "OMBRE_BUCKETS_DIR", "buckets_dir")
    env_buckets_dir = os.environ.get("OMBRE_BUCKETS_DIR", "")

    # MCP OAuth 开关（布尔，单独处理）—— OMBRE_MCP_REQUIRE_AUTH
    # 不能走 _apply_env_override：它只写字符串，而 server.py 用
    # bool(config.get("mcp_require_auth", True)) 判定——字符串 "false" 是 truthy，
    # 会导致设了 =false 反而仍开启鉴权。这里显式解析成真正的 bool。
    # 用途：把 OB 接进自有前端 / GPT / GLM 等不走 OAuth 的客户端时，
    # 设 OMBRE_MCP_REQUIRE_AUTH=false（或 config.yaml: mcp_require_auth: false）即可免认证直连 /mcp。
    # 仅在显式设置为可识别的值时才覆盖；不设 / 设成乱七八糟的值都保持默认（安全：默认开启）。
    _env_mcp_auth = os.environ.get("OMBRE_MCP_REQUIRE_AUTH", "").strip().lower()
    if _env_mcp_auth in ("0", "false", "no", "off"):
        config["mcp_require_auth"] = False
    elif _env_mcp_auth in ("1", "true", "yes", "on"):
        config["mcp_require_auth"] = True

    # iter 1.9 F: 统一推荐 OMBRE_VAULT_DIR；老变量 OMBRE_BUCKETS_DIR 仍兼容
    # Priority: OMBRE_BUCKETS_DIR (legacy explicit) > OMBRE_VAULT_DIR > config.yaml.buckets_dir
    # We keep BUCKETS_DIR with higher priority than VAULT_DIR for two reasons:
    #   1) Existing tests use monkeypatch.setenv("OMBRE_BUCKETS_DIR", ...) extensively;
    #      flipping priority would break them when conftest also sets VAULT_DIR globally.
    #   2) Anyone who already had BUCKETS_DIR working should keep working unchanged.
    # New users / new docs should prefer OMBRE_VAULT_DIR; both names map to the same path.
    env_vault_dir = os.environ.get("OMBRE_VAULT_DIR", "")
    if env_vault_dir and not env_buckets_dir:
        config["buckets_dir"] = env_vault_dir
    elif env_buckets_dir and not env_vault_dir:
        # Only legacy var set — emit one INFO hint so users know about the new name.
        try:
            import logging as _logging
            _logging.getLogger(__name__).info(
                "OMBRE_BUCKETS_DIR is the legacy name; OMBRE_VAULT_DIR is preferred "
                "/ 旧变量 OMBRE_BUCKETS_DIR 仍可用，但建议改用 OMBRE_VAULT_DIR"
            )
        except Exception:
            pass

    # --- Ensure bucket storage directories exist ---
    # --- 确保记忆桶存储目录存在 ---
    buckets_dir: str = str(config["buckets_dir"])
    for subdir in ["permanent", "dynamic", "archive"]:
        os.makedirs(os.path.join(buckets_dir, subdir), exist_ok=True)

    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Deep-merge two dicts; override values take precedence.
    深度合并两个字典，override 的值覆盖 base。
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_override(config: dict, env_name: str, *path: str) -> None:
    """把单个环境变量按 path 写入嵌套 dict（仅当值非空）。

    设计原因：load_config() 里曾有 6 段几乎一模一样的覆盖代码——
        env = os.environ.get("XXX", "")
        if env:
            config["a"]["b"] = env
    长度膨胀且新增一项就要再抄一遍。统一抽出后：
      * 新增覆盖只要写一行 `_apply_env_override(config, "OMBRE_FOO", "a", "b")`
      * 行为一致：空字符串视为"未设置"，绝不覆盖默认值
      * 自动 setdefault 中间层 dict，避免 KeyError

    参数：
        config   ：被修改的配置字典（in-place）
        env_name ：环境变量名
        *path    ：嵌套 key 路径。一层 key 传 1 个，两层传 2 个。
                   例如 ("dehydration", "api_key") 会写到
                   config["dehydration"]["api_key"]。

    边界（rule.md §⑨ 防御式编程）：
      * 环境变量为空 / 未设置 → 直接 return，不动 config
      * path 为空 → 直接 return（调用方写错路径不应静默覆盖整个 config）
    """
    value = os.environ.get(env_name, "").strip()
    if not value or not path:
        return
    # 走到倒数第二层，逐层 setdefault 出嵌套 dict
    cursor = config
    for key in path[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[path[-1]] = value


def _resolve_log_dir(explicit: str | None) -> str:
    """决定 server.log 落到哪个目录。

    优先级（rule.md §1.13 + iter 1.6 §3）：
        explicit 参数 > $OMBRE_LOG_DIR > <buckets_dir>/.logs > /tmp 兜底

    抽出来的原因：原 setup_logging() 内联了 4 段 if-fallback，逻辑分支
    挤在一起读不清。独立后单元测试可以直接打它，且改优先级不必动
    setup_logging 主体。
    """
    if explicit:
        return explicit
    env_dir = os.environ.get("OMBRE_LOG_DIR", "").strip()
    if env_dir:
        return env_dir
    bd = os.environ.get("OMBRE_BUCKETS_DIR", "").strip()
    if bd:
        return os.path.join(bd, ".logs")
    return _LOG_FALLBACK_DIR


def setup_logging(level: str = "INFO", log_dir: str | None = None) -> None:
    """
    Initialize logging system.
    初始化日志系统。

    Note: In MCP stdio mode, stdout is occupied by the protocol;
    logs must go to stderr.
    注意：MCP stdio 模式下 stdout 被协议占用，日志只能走 stderr。

    iter 1.6 §3：除 stderr 外，同时写一份 ``server.log``（RotatingFileHandler）。
    Dashboard 的「日志」标签页通过 ``/api/logs`` 读取这个文件，方便她/他在网页上
    直接看 ERROR/WARNING。日志路径优先级：
        log_dir 参数 > 环境变量 OMBRE_LOG_DIR > <buckets_dir>/.logs > /tmp/ombre_logs
    """
    log_level = getattr(logging, level.upper(), None)
    if not isinstance(log_level, int):
        log_level = logging.INFO

    handlers: list[logging.Handler] = [logging.StreamHandler()]  # 默认 stderr

    # ---- 文件日志（按需开启，失败时静默降级到仅 stderr）----
    chosen_dir = _resolve_log_dir(log_dir)

    try:
        from logging.handlers import RotatingFileHandler
        os.makedirs(chosen_dir, exist_ok=True)
        log_path = os.path.join(chosen_dir, "server.log")
        fh = RotatingFileHandler(
            log_path,
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(log_level)
        handlers.append(fh)
        # 暴露给 server.py，供 /api/logs 读取
        os.environ["OMBRE_LOG_FILE"] = log_path
    except Exception as e:
        # 文件日志失败不应阻塞服务启动
        sys.stderr.write(f"[setup_logging] file handler disabled: {e}\n")

    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    # 接入统一错误体系的 in-memory log buffer，给 E 级报错附 tail
    try:
        try:
            from errors import attach_log_buffer_handler  # type: ignore
        except ImportError:
            from .errors import attach_log_buffer_handler  # type: ignore
        attach_log_buffer_handler(level=log_level)
    except Exception as _e:
        sys.stderr.write(f"[setup_logging] buffer handler attach failed: {_e}\n")


def generate_bucket_id() -> str:
    """
    Generate a unique bucket ID (12-char short UUID for readability).
    生成唯一的记忆桶 ID（12 位短 UUID，方便人类阅读）。
    """
    return uuid.uuid4().hex[:12]


def strip_wikilinks(text: str) -> str:
    """
    Remove Obsidian wikilink brackets: [[word]] → word
    去除 Obsidian 双链括号
    """
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", text) if text else text


# ===============================================================
# Wikilinks / 双链解析（iter 1.7 §F1）
# ---------------------------------------------------------------
# 设计：Obsidian 用 `[[目标桶名]]` 写双向链接，可带 alias 和 section：
#   [[Memory]]                 → target = "Memory"
#   [[Memory#section]]         → target = "Memory"     (# 后是段落锚)
#   [[Memory|这件事]]          → target = "Memory"     (| 后是显示别名)
# 正则只抓「第一段」目标名；遇到 # 或 | 就停止。
# Python 小知识：
#   * re.compile 把正则预编译，反复用时比 re.findall 每次现编译快
#   * 字符类里 `[^\]\|#]+` 表示「不是 ] 不是 | 不是 # 的连续字符」
#   * (?:...)  非捕获分组，只为分支选择，不占 group 编号
# ===============================================================
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:[#\|][^\]]*)?\]\]")


def extract_wikilinks(text: str) -> list[str]:
    """Extract Obsidian-style [[wikilinks]] target names from text.

    抽取正文里所有 `[[xxx]]` 的目标名，去重保序，去掉 `|alias` 和 `#section`。
    返回 list[str]（不是 set，因为下游希望保持出现顺序）。

    Example / 例：
        >>> extract_wikilinks("see [[A]] and [[B|别名]] also [[A]]")
        ['A', 'B']
    """
    # 防御：传 None 或空串直接返回空列表，避免下游 for 循环崩
    if not text:
        return []
    # 用 list + 手工查重而不是 set()，是为了保留首次出现顺序
    # （Python 3.7+ 的 dict 也保序，用 dict.fromkeys 也行，这里写法更直观）
    seen: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).strip()  # group(1) = 第一个括号 ([^\]\|#]+) 抓到的内容
        if target and target not in seen:
            seen.append(target)
    return seen


def get_version() -> str:
    """Read project version from `<repo_root>/VERSION`.

    存在两份 VERSION：src/VERSION 与根目录 VERSION。读取顺序：src/VERSION 优先。
    任何路径都读不到时返回 "0.0.0+unknown"，方便排查。

    ⚠️ 为什么是 src 优先（别再改成根目录优先）：
      热更新（web/meta.py do-update）解压时只覆盖 src/ 和 frontend/，所以 src/VERSION
      一定被刷新，而很多用户的根目录 VERSION 是历史安装遗留的老版本（从没人读、也没人更）。
      若改成根目录优先，用户一更新就会读到那个尘封的旧根 VERSION → 版本号当场倒退
      （2.3.10 真踩过：有人从 2.3.8 更新后显示成 2.1.3）。
      一致性由 do-update「强制把 zip 的根 VERSION 同写到两处」保证；这里只管读那个
      最可靠新鲜的 src/VERSION。
      发版请同时 bump 两个 VERSION（根 + src/）。

    Python 小知识：
      * `with open(...) as f:` 是「上下文管理器」，离开 with 块自动关文件
        即使中途抛异常也会关——比 try/finally 干净
      * `OSError` 涵盖文件不存在、权限不够、磁盘错误等所有 IO 异常
        比裸 `except:` 安全，比 `except FileNotFoundError` 全面
    """
    candidates = [
        # 优先：src/ 旁的副本——热更新一定会刷新它，最可靠新鲜
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
        # fallback：项目根目录 VERSION（Docker 里由 Dockerfile COPY 进 /app/VERSION）
        os.path.join(_project_root(), "VERSION"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            # 这一条候选路径读不到就试下一条，不打日志（启动期无日志器）
            continue
    return "0.0.0+unknown"


def sanitize_name(name: str) -> str:
    """
    Sanitize bucket name, keeping only safe characters.
    Prevents path traversal attacks (e.g. ../../etc/passwd).
    清洗桶名称，只保留安全字符。防止路径遍历攻击。
    """
    if not isinstance(name, str):
        return "unnamed"
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", name, flags=re.UNICODE)
    cleaned = cleaned.strip()[:_BUCKET_NAME_MAX_LEN]
    return cleaned if cleaned else "unnamed"


def safe_path(base_dir: str, filename: str) -> Path:
    """
    Construct a safe file path, ensuring it stays within base_dir.
    Prevents directory traversal.
    构造安全的文件路径，确保最终路径始终在 base_dir 内部。
    """
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    # 用 is_relative_to 而不是 startswith，避免前缀混淆：
    # 例如 base=/data/buckets，target=/data/buckets_evil/f.md，
    # str 前缀检查会误判为安全，is_relative_to 不会。
    if not target.is_relative_to(base):
        raise ValueError(
            f"Path safety check failed / 路径安全检查失败: "
            f"{target} is not inside / 不在 {base} 内"
        )
    return target


def count_tokens_approx(text: str) -> int:
    """
    Rough token count estimate.
    粗略估算 token 数。

    Chinese ≈ 1 char = 1.5 tokens, English ≈ 1 word = 1.3 tokens.
    Used to decide whether dehydration is needed; precision not required.
    中文 ≈ 1字=1.5token，英文 ≈ 1词=1.3token。
    用于判断是否需要脱水压缩，不追求精确。
    """
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    return int(
        chinese_chars * _TOKEN_RATIO_PER_CN_CHAR
        + english_words * _TOKEN_RATIO_PER_EN_WORD
        + len(text) * _TOKEN_RATIO_PER_CHAR
    )


def now_iso() -> str:
    """
    Return current time as ISO format string.
    返回当前时间的 ISO 格式字符串。
    """
    return datetime.now().isoformat(timespec="seconds")
