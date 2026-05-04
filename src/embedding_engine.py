"""
========================================
embedding_engine.py — 向量化引擎，给 breath/search 提供语义召回
========================================

【2.0.3 重构】我把向量化拆成「门面 + 后端」两层：
- 后端实现（BaseEmbeddingEngine 子类）只负责把文本算成向量，不碰任何 IO/SQLite。
  现在只有两档：本地 fastembed+bge-m3（默认）/ Gemini API。其它历史选项全部废弃。
- 门面（EmbeddingEngine）持有一个后端实例，负责 SQLite 存取、余弦搜索、删除、
  孤儿对账、模型/维度元数据校验。对外接口零变化，bucket_manager 不需要动。

为什么这么拆：模型不是热路径，但每次模型变更要做的事很多（备份 db、清向量、重新
索引）。把生成与存储解耦后，后端就是一颗螺丝，可以独立换。

关键行为：
- generate_and_store(bucket_id, content)：写入或覆盖某个桶的向量
- search_similar(query, top_k)：返回 [(bucket_id, score)] 按相似度倒序
- search(query, top_k)：新接口，按规范只返回 bucket_id 列表
- delete_embedding(bucket_id)：与 BucketManager.delete 同步调用
- list_all_ids()：给 tools/clean_orphan_embeddings 用，找孤儿向量
- enabled=False 时所有方法 no-op，方便离线/测试
- 启动时若 db 里历史模型/维度与当前后端不一致 → 记 OB-W005 警告，不阻止启动

不做什么（边界）：
- 不读写桶文件
- 不做关键词检索（那是 BucketManager 的事）
- 不做去重 / 合并判断
- 不下载模型权重（那是 src/model_downloader.py 的事；本模块只检查文件存在）

对外暴露：
- BaseEmbeddingEngine（抽象基类，方便未来扩展第三档）
- LocalEmbeddingEngine（fastembed + bge-m3）
- APIEmbeddingEngine（OpenAI 兼容 API，默认 Gemini）
- EmbeddingEngine（门面：保持向后兼容的对外类）
========================================
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import math
import os
import sqlite3
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger("ombre_brain.embedding")


# ============================================================
# 常量 / 默认路径
# ============================================================

# 向量维度参考（bge-m3 1024，gemini-embedding-001 默认 768）
_BGE_M3_DIM = 1024
_GEMINI_DEFAULT_DIM = 768

# 项目根目录下 models/ 是模型权重默认位置；可被 OMBRE_MODEL_DIR / config 覆盖
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_MODEL_DIR = os.path.join(_REPO_ROOT, "models")

# 输入截断长度。bge-m3 支持 8192 token，gemini API 走 2000 字节足够
_MAX_INPUT_CHARS = 2000


# ============================================================
# 后端基类 / Backend Abstract Base
# ============================================================

class BaseEmbeddingEngine(abc.ABC):
    """所有 embedding 后端的契约。

    设计原则：
    - generate 是同步的（CPU/网络都在调用方用 asyncio.to_thread 包），方便子类只想
      写一份纯函数实现。API 后端额外提供 generate_async 走原生异步。
    - model_name / vector_dim 在初始化后必须能稳定返回；不允许构造完了还说不出维度。
    - 后端不开 SQLite 连接，不读写桶文件，存储/查询交给门面 EmbeddingEngine。
    """

    @abc.abstractmethod
    def generate(self, text: str) -> list[float]:
        """同步算一条向量。失败返回空列表（不抛运行期异常）。"""

    @abc.abstractmethod
    def model_name(self) -> str:
        """返回当前模型名（用于元数据写入与前端显示）。"""

    @abc.abstractmethod
    def vector_dim(self) -> int:
        """返回向量维度（用于 db meta 校验防止混用）。"""

    def warmup(self) -> None:
        """子类可选：提前把模型加载到内存，避免首次调用延迟。"""
        return None


# ============================================================
# 本地后端：fastembed + bge-m3
# ============================================================

class LocalEmbeddingEngine(BaseEmbeddingEngine):
    """fastembed (ONNX) + bge-m3。

    选 fastembed 而不是 sentence-transformers：不依赖 torch，安装包小（~50MB），
    ARM/x86 都能跑。fastembed 内置 bge-m3 是优化过的 ONNX（~600–800MB），磁盘
    占用远小于 fp32 原版（~2.2GB）。

    路径设计（2.0.3 修正）：我们只管 *cache_root*（默认 models/），fastembed 自己
    在里面创建会话级的子目录（如 models--BAAI--bge-m3 / fast-bge-m3-*）。这样
    下载与加载共用同一套路径约定，不存在不匹配。model_downloader 也只针对同一个
    cache_root 触发下载与监控。
    """

    MODEL_ID = "BAAI/bge-m3"

    def __init__(self, model_dir: str | None = None):
        # cache_root：参数 > 环境变量 > 项目内 models/
        # 注意：model_dir 这个参数名是历史遗留，语义实际是 cache_root。
        env_dir = os.environ.get("OMBRE_MODEL_DIR", "").strip()
        self.cache_root: str = (model_dir or env_dir or _DEFAULT_MODEL_DIR)
        # 保留 model_dir 同名属性（指向同一目录），供外部观察。
        self.model_dir: str = self.cache_root
        self._model: Any = None  # fastembed.TextEmbedding，懒加载
        self._dim: int = _BGE_M3_DIM

    def model_name(self) -> str:
        return self.MODEL_ID

    def vector_dim(self) -> int:
        return self._dim

    def _check_model_files(self) -> bool:
        """检查模型权重是否就绪：递归查 cache_root 下是否有 .onnx 文件。

        fastembed 会在 cache_root 里创建多层子目录，平铺扫描会漏判。
        """
        if not os.path.isdir(self.cache_root):
            return False
        try:
            for _root, _dirs, files in os.walk(self.cache_root):
                for name in files:
                    if name.endswith(".onnx"):
                        return True
        except OSError:
            return False
        return False

    def _ensure_model(self) -> None:
        """懒加载 fastembed 模型。"""
        if self._model is not None:
            return
        if not self._check_model_files():
            try:
                from errors import OBStartupError  # type: ignore
            except ImportError:
                from .errors import OBStartupError  # type: ignore
            raise OBStartupError(
                "OB-F004",
                f"cache_root={self.cache_root} 下未找到 .onnx 权重文件",
            )
        try:
            from fastembed import TextEmbedding  # type: ignore
        except ImportError as e:
            try:
                from errors import OBStartupError  # type: ignore
            except ImportError:
                from .errors import OBStartupError  # type: ignore
            raise OBStartupError(
                "OB-F004",
                f"fastembed 未安装：{e}。请运行 pip install fastembed",
            ) from e
        logger.info(f"[embedding] loading local model: {self.MODEL_ID} from {self.cache_root}")
        # fastembed 自己在 cache_root 下创建/查找子目录，路径约定由它全权决定，
        # 与 model_downloader 使用同一个 cache_root 保证路径一致。
        self._model = TextEmbedding(
            model_name=self.MODEL_ID,
            cache_dir=self.cache_root,
        )
        logger.info(f"[embedding] local model ready: dim={self._dim}")

    def warmup(self) -> None:
        try:
            self._ensure_model()
        except Exception as e:
            logger.warning(f"[embedding] local warmup skipped: {e}")

    def generate(self, text: str) -> list[float]:
        if not text or not text.strip():
            return []
        try:
            self._ensure_model()
        except Exception as e:
            # _ensure_model 抛 OBStartupError 时让它继续向上传播（启动期）；
            # 运行期的导入/IO 错误降级为空向量，由调用方决定是否记 OB-E001。
            if e.__class__.__name__ == "OBStartupError":
                raise
            logger.warning(f"[embedding] local model load failed at runtime: {e}")
            return []
        try:
            # fastembed.embed 返回 generator；list 化只取第一条
            vectors = list(self._model.embed([text[:_MAX_INPUT_CHARS]]))
            if not vectors:
                return []
            vec = vectors[0]
            return vec.tolist() if hasattr(vec, "tolist") else list(vec)
        except Exception as e:
            logger.warning(f"[embedding] local inference failed: {e}")
            return []


# ============================================================
# API 后端：OpenAI 兼容（默认 Gemini）
# ============================================================

class APIEmbeddingEngine(BaseEmbeddingEngine):
    """OpenAI 兼容的远程 embedding API（默认 Gemini）。

    必须有 api_key；空 key 会在门面层抛 OB-F001。本类只负责发请求 + 拿向量。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dim: int = _GEMINI_DEFAULT_DIM,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._dim = dim
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=30.0,
        )

    def model_name(self) -> str:
        return self.model

    def vector_dim(self) -> int:
        return self._dim

    def generate(self, text: str) -> list[float]:
        """同步接口（基类协议要求）。生产路径走 generate_async。"""
        try:
            return asyncio.run(self.generate_async(text))
        except RuntimeError:
            logger.warning("[embedding] sync generate() called inside event loop; use generate_async")
            return []

    async def generate_async(self, text: str) -> list[float]:
        if not text or not text.strip():
            return []
        try:
            response = await self._client.embeddings.create(
                model=self.model,
                input=text[:_MAX_INPUT_CHARS],
            )
            if response.data and len(response.data) > 0:
                vec = response.data[0].embedding
                # 第一次拿到向量时确认真实维度
                if vec and len(vec) != self._dim:
                    self._dim = len(vec)
                return list(vec) if vec else []
            return []
        except Exception as e:
            try:
                from errors import record_error  # type: ignore
            except ImportError:
                from .errors import record_error  # type: ignore
            record_error(
                "OB-E001",
                f"backend=api model={self.model} err={type(e).__name__}: {e}",
            )
            return []


# ============================================================
# 门面：EmbeddingEngine — 对外保持原接口
# ============================================================

class EmbeddingEngine:
    """SQLite 存储 + 搜索 + 元数据校验，持有一颗 BaseEmbeddingEngine。

    向后兼容点：
    - 保持类名、保持 enabled / model / backend / db_path 这几个属性可读
    - 保持 generate_and_store / search_similar / delete_embedding / list_all_ids /
      get_embedding 这些方法的签名
    """

    # 向后兼容的 backend 名称别名
    _BACKEND_ALIASES = {
        "gemini": "api",
        "bge-m3": "local",
        "bge-small-zh": "local",  # 已废弃；做兼容映射并打警告
    }

    def __init__(self, config: dict):
        embed_cfg = config.get("embedding", {}) or {}

        # 1) 解析 backend：env > config > 默认 local
        env_backend = os.environ.get("OMBRE_EMBED_BACKEND", "").strip().lower()
        raw_backend = (env_backend or embed_cfg.get("backend", "local") or "local").strip().lower()
        if raw_backend == "bge-small-zh":
            logger.warning(
                "[embedding] backend=bge-small-zh 已废弃，自动切换为 local(bge-m3)。"
                "请在 config.yaml 或环境变量 OMBRE_EMBED_BACKEND 中改为 'local'。"
            )
        self.backend = self._BACKEND_ALIASES.get(raw_backend, raw_backend)
        if self.backend not in ("local", "api"):
            logger.warning(f"[embedding] 未知 backend '{raw_backend}'，回退到 local")
            self.backend = "local"

        # 2) 解析 enabled。OB-F001：enabled=true 但 api_key 空，且后端是 api → 拒启
        enabled_cfg = embed_cfg.get("enabled", True)

        # 3) 解析 SQLite 路径（允许测试 fixture 通过 db_path 覆盖）
        custom_db = (embed_cfg.get("db_path") or "").strip()
        if custom_db:
            self.db_path = custom_db
        else:
            self.db_path = os.path.join(config["buckets_dir"], "embeddings.db")

        # 4) 实例化后端
        self._backend: BaseEmbeddingEngine | None = None
        self.enabled = False
        # model 是镜像属性（server.py 里的热重载会直接 setattr，所以保留）
        self.model: str = ""

        if not enabled_cfg:
            # 显式关闭：no-op 模式，仍初始化 db 让 list_all_ids 能跑
            self._init_db()
            return

        if self.backend == "api":
            api_key = (embed_cfg.get("api_key") or "").strip()
            if not api_key:
                # 兼容历史 env：OMBRE_EMBED_API_KEY
                api_key = os.environ.get("OMBRE_EMBED_API_KEY", "").strip()
            if not api_key:
                try:
                    from errors import OBStartupError  # type: ignore
                except ImportError:
                    from .errors import OBStartupError  # type: ignore
                raise OBStartupError(
                    "OB-F001",
                    "backend=api, embedding.enabled=true, api_key=<empty>",
                )
            base_url = (
                (embed_cfg.get("base_url") or "").strip()
                or "https://generativelanguage.googleapis.com/v1beta/openai/"
            )
            model = embed_cfg.get("model") or "gemini-embedding-001"
            self._backend = APIEmbeddingEngine(
                api_key=api_key,
                base_url=base_url,
                model=model,
            )
        else:  # local
            local_cfg = embed_cfg.get("local", {}) or {}
            model_dir = (local_cfg.get("model_dir") or "").strip() or None
            self._backend = LocalEmbeddingEngine(model_dir=model_dir)
            # 注意：构造时不强制加载模型权重（懒加载）。如果文件缺失，会在
            # 第一次 generate 时抛 OB-F004。Server 启动时由 model_downloader 兜底。

        self.model = self._backend.model_name()
        self.enabled = True

        # 5) 初始化 SQLite + 校验元数据
        self._init_db()
        self._check_meta_consistency()

    # -------------------- SQLite 初始化 --------------------

    def _init_db(self) -> None:
        """建表。embeddings 主表 + embeddings_meta 元数据表（2.0.3 新增）。"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    bucket_id TEXT PRIMARY KEY,
                    embedding TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _read_meta(self) -> dict[str, str]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT key, value FROM embeddings_meta").fetchall()
            return {k: v for k, v in rows}
        finally:
            conn.close()

    def _write_meta(self, key: str, value: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def _check_meta_consistency(self) -> None:
        """对账历史 model_name / vector_dim 与当前后端是否一致。

        - 主表为空：第一次写入，覆盖 meta，无害
        - meta 与当前后端不一致：记 OB-W005 警告，提示用户跑迁移
        """
        if not self._backend:
            return
        meta = self._read_meta()
        conn = sqlite3.connect(self.db_path)
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        finally:
            conn.close()

        cur_name = self._backend.model_name()
        cur_dim = str(self._backend.vector_dim())

        if cnt == 0:
            self._write_meta("model_name", cur_name)
            self._write_meta("vector_dim", cur_dim)
            return

        old_name = meta.get("model_name", "")
        old_dim = meta.get("vector_dim", "")
        if not old_name and not old_dim:
            # 老库（2.0.3 之前）没有 meta 表数据，写一次但不报警
            self._write_meta("model_name", cur_name)
            self._write_meta("vector_dim", cur_dim)
            return

        if old_name != cur_name or old_dim != cur_dim:
            try:
                from errors import record_error  # type: ignore
            except ImportError:
                from .errors import record_error  # type: ignore
            record_error(
                "OB-W005",
                (
                    f"embeddings.db meta mismatch: "
                    f"db(model={old_name},dim={old_dim}) vs current(model={cur_name},dim={cur_dim}). "
                    f"Run /api/embedding/migrate to re-index."
                ),
            )

    # -------------------- 生成 + 存储 --------------------

    async def _generate_async(self, text: str) -> list[float]:
        """统一的异步生成：API 走原生 async，本地走 to_thread。"""
        if not self._backend:
            return []
        if isinstance(self._backend, APIEmbeddingEngine):
            return await self._backend.generate_async(text)
        return await asyncio.to_thread(self._backend.generate, text)

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        """为内容生成 embedding 并存入 SQLite。成功返回 True。"""
        if not self.enabled or not content or not content.strip():
            return False
        try:
            embedding = await self._generate_async(content)
            if not embedding:
                return False
            self._store_embedding(bucket_id, embedding)
            return True
        except Exception as e:
            logger.warning(f"Embedding generation failed for {bucket_id}: {e}")
            return False

    def _store_embedding(self, bucket_id: str, embedding: list[float]) -> None:
        try:
            from utils import now_iso  # type: ignore
        except ImportError:
            from .utils import now_iso  # type: ignore
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (bucket_id, embedding, updated_at) VALUES (?, ?, ?)",
                (bucket_id, json.dumps(embedding), now_iso()),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_embedding(self, bucket_id: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (bucket_id,))
            conn.commit()
        finally:
            conn.close()

    def list_all_ids(self) -> list[str]:
        """孤儿对账用：embeddings 表里所有 bucket_id。"""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT bucket_id FROM embeddings").fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT embedding FROM embeddings WHERE bucket_id = ?", (bucket_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        return None

    # -------------------- 搜索 --------------------

    async def search_similar(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """返回 [(bucket_id, similarity)] 按相似度倒序。"""
        if not self.enabled:
            return []
        try:
            query_embedding = await self._generate_async(query)
            if not query_embedding:
                return []
        except Exception as e:
            logger.warning(f"Query embedding failed: {e}")
            return []

        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT bucket_id, embedding FROM embeddings").fetchall()
        finally:
            conn.close()
        if not rows:
            return []

        results: list[tuple[str, float]] = []
        for bucket_id, emb_json in rows:
            try:
                stored_embedding = json.loads(emb_json)
                sim = self._cosine_similarity(query_embedding, stored_embedding)
                results.append((bucket_id, sim))
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    async def search(self, query: str, top_k: int = 10) -> list[str]:
        """规范新接口：只返回 bucket_id 列表。"""
        pairs = await self.search_similar(query, top_k=top_k)
        return [bid for bid, _ in pairs]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    # -------------------- 前端可读的状态 --------------------

    def status(self) -> dict[str, Any]:
        """前端 /api/embedding/status 用。"""
        if not self._backend:
            return {
                "enabled": False,
                "backend": self.backend,
                "model": "",
                "vector_dim": 0,
                "db_path": self.db_path,
                "embedding_count": 0,
            }
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                cnt = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            cnt = -1
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "model": self._backend.model_name(),
            "vector_dim": self._backend.vector_dim(),
            "db_path": self.db_path,
            "embedding_count": cnt,
        }


__all__ = [
    "BaseEmbeddingEngine",
    "LocalEmbeddingEngine",
    "APIEmbeddingEngine",
    "EmbeddingEngine",
]
