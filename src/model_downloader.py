"""
========================================
model_downloader.py — 本地 embedding 模型下载器
========================================

【2.0.3 新增】当 embedding.backend=local 但 models/bge-m3/ 下没有 ONNX 权重时，
负责把它们从 HuggingFace 拉下来。

设计原则（来自 2026-05-02 决策）：
- 默认走 huggingface.co，国内访问失败时自动切 hf-mirror.com（HF_ENDPOINT 环境变量）
- 下载在后台线程跑，不阻塞 server 启动；Dashboard 能马上访问
- 进度写到 _model_download_status.json，前端轮询 GET /api/embedding/model/status
- 单进程同一时刻只允许一个下载任务在跑（线程锁 + JSON 里 phase 字段做幂等）
- 任何异常都写进 status，并记 OB-F004（Fatal 级，但不退出进程；用户可以从 Dashboard
  切到 API 后端兜底）

为什么委托 fastembed 自己下载（2.0.3 修正）：
- 之前用 huggingface_hub.hf_hub_download “平铺下载到 model_dir” 与 fastembed 内部
  cache_dir 约定不匹配（fastembed 要的是 models--{org}--{repo} / fast-bge-m3-* 之类
  的子目录）。
- 现在调 fastembed.TextEmbedding(cache_dir=cache_root) 这一句，本质上跟
  LocalEmbeddingEngine 加载路径完全一致。路径不匹配问题从根上消除。
- 进度上报改为“监控线程每 1s 采 cache_root 总字节数”，前端看到的是
  「已下载 MB」而不是「已下载文件数」。
- HF_ENDPOINT 环境变量仍然有效：fastembed 在下载 HF 仓库类源时会调
  huggingface_hub，后者尊重 HF_ENDPOINT。平仓 HF 直连失败后会切 hf-mirror.com
  重试。

不做什么（边界）：
- 不做模型加载（那是 LocalEmbeddingEngine 的事）
- 不做 SQLite 操作
- 不发起 MCP 调用 / 不做业务逻辑
- 不删除已有模型文件（哪怕损坏，下载新文件也是 overwrite，不主动 rmtree）

对外暴露：
- is_model_ready(model_dir) -> bool
- status_path_for(buckets_dir) -> str
- read_status(status_path) -> dict
- download_bge_m3_in_background(model_dir, status_path) -> threading.Thread
- ensure_local_model_async(embedding_engine, buckets_dir) -> threading.Thread | None
========================================
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("ombre_brain.model_downloader")


# fastembed 内置的 bge-m3 标识。这个 ID 与 LocalEmbeddingEngine.MODEL_ID 必须一致。
_MODEL_ID = "BAAI/bge-m3"

# HuggingFace 镜像。HF_ENDPOINT 是 huggingface_hub 官方支持的环境变量，
# fastembed 下载 HF 类源时间接生效。
_HF_ENDPOINT_PRIMARY = "https://huggingface.co"
_HF_ENDPOINT_MIRROR = "https://hf-mirror.com"

# 进度状态 JSON 文件名（放 buckets_dir/.logs/ 下）
_STATUS_FILE_NAME = "_model_download_status.json"

# 监控线程采样间隔
MONITOR_INTERVAL_SEC = 1.0

# 预期总下载体积（单位 MB，用于前端进度条估算）。fastembed 内置 bge-m3 优化版
# 实际 ~600–800MB；写 800 作为上限估算，超过也不报错（前端封顶 100%）。
_EXPECTED_TOTAL_MB = 800

# 进程级锁，确保单次启动只跑一个下载任务
_download_lock = threading.Lock()
_download_thread: threading.Thread | None = None


# ============================================================
# 路径与状态文件
# ============================================================

def status_path_for(buckets_dir: str) -> str:
    """约定：进度文件放 buckets_dir/.logs/_model_download_status.json。"""
    log_dir = os.path.join(buckets_dir, ".logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, _STATUS_FILE_NAME)


def is_model_ready(cache_root: str) -> bool:
    """模型权重是否就绪：递归查 cache_root 下是否有 .onnx 文件。

    与 LocalEmbeddingEngine._check_model_files 保持一致的判定。
    """
    if not os.path.isdir(cache_root):
        return False
    try:
        for _root, _dirs, files in os.walk(cache_root):
            for name in files:
                if name.endswith(".onnx"):
                    return True
    except OSError:
        return False
    return False


def _dir_size_mb(path: str) -> float:
    """递归统计目录总字节数 (MB)。IO 异常返 0.0。"""
    if not os.path.isdir(path):
        return 0.0
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                fp = os.path.join(root, name)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        return 0.0
    return round(total / (1024 * 1024), 1)


def _empty_status(cache_root: str) -> dict[str, Any]:
    return {
        "phase": "idle",          # idle | downloading | completed | failed
        "model_id": _MODEL_ID,
        "target_dir": cache_root,
        "mirror": "",
        "downloaded_mb": 0.0,
        "expected_mb": _EXPECTED_TOTAL_MB,
        "started_at": "",
        "finished_at": "",
        "error": "",
        "message": "",
    }


def read_status(status_path: str) -> dict[str, Any]:
    """读 status JSON。文件不存在或损坏时返回 phase=idle 默认结构。"""
    if not os.path.exists(status_path):
        return _empty_status("")
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_status("")
        return data
    except (OSError, json.JSONDecodeError):
        return _empty_status("")


def write_status(status_path: str, status: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(status_path), exist_ok=True)
        # 写入临时文件再 rename，避免前端轮询读到半截 JSON
        tmp = status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        os.replace(tmp, status_path)
    except OSError as e:
        logger.warning(f"[model_downloader] failed to write status: {e}")


# ============================================================
# 下载实现（同步，跑在线程里）
# ============================================================

def _start_progress_monitor(
    cache_root: str,
    status_path: str,
    stop_flag: threading.Event,
) -> threading.Thread:
    """后台线程：每 MONITOR_INTERVAL_SEC 抓一次 cache_root 总字节数写进度。"""
    def _loop() -> None:
        while not stop_flag.is_set():
            try:
                cur = read_status(status_path)
                if cur.get("phase") == "downloading":
                    cur["downloaded_mb"] = _dir_size_mb(cache_root)
                    write_status(status_path, cur)
            except Exception:
                pass
            stop_flag.wait(MONITOR_INTERVAL_SEC)

    t = threading.Thread(target=_loop, name="ombre-model-progress", daemon=True)
    t.start()
    return t


def _try_download(
    cache_root: str,
    status_path: str,
    endpoint: str,
    mirror_label: str,
) -> bool:
    """单次尝试：指定 HF_ENDPOINT，调 fastembed.TextEmbedding(cache_dir=cache_root)
    触发下载。这一调与 LocalEmbeddingEngine._ensure_model() 走完全同一条路径。
    """
    try:
        from fastembed import TextEmbedding  # type: ignore
    except ImportError as e:
        write_status(status_path, {
            **read_status(status_path),
            "phase": "failed",
            "error": f"fastembed 未安装：{e}",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "message": "请运行 pip install fastembed",
        })
        return False

    os.environ["HF_ENDPOINT"] = endpoint
    os.makedirs(cache_root, exist_ok=True)

    write_status(status_path, {
        "phase": "downloading",
        "model_id": _MODEL_ID,
        "target_dir": cache_root,
        "mirror": mirror_label,
        "downloaded_mb": _dir_size_mb(cache_root),
        "expected_mb": _EXPECTED_TOTAL_MB,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "finished_at": "",
        "error": "",
        "message": f"从 {mirror_label} 下载 {_MODEL_ID}（约 {_EXPECTED_TOTAL_MB}MB）",
    })

    stop_flag = threading.Event()
    monitor = _start_progress_monitor(cache_root, status_path, stop_flag)

    try:
        # fastembed 构造时如本地缺会下载；不需要调 .embed()。
        TextEmbedding(model_name=_MODEL_ID, cache_dir=cache_root)
    except Exception as e:
        stop_flag.set()
        monitor.join(timeout=2)
        err_str = f"{type(e).__name__}: {e}"
        logger.warning(f"[model_downloader] download via {mirror_label} failed: {err_str}")
        write_status(status_path, {
            **read_status(status_path),
            "phase": "failed",
            "error": err_str,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "message": f"{mirror_label} 下载失败：{e}",
        })
        return False

    stop_flag.set()
    monitor.join(timeout=2)

    if not is_model_ready(cache_root):
        write_status(status_path, {
            **read_status(status_path),
            "phase": "failed",
            "error": "download finished but no .onnx found in cache_root",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "message": f"{mirror_label} 下载完成但未检测到权重文件",
        })
        return False

    write_status(status_path, {
        **read_status(status_path),
        "phase": "completed",
        "downloaded_mb": _dir_size_mb(cache_root),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "message": f"模型已就绪（来自 {mirror_label}）",
    })
    logger.info(f"[model_downloader] bge-m3 ready at {cache_root} (mirror={mirror_label})")
    return True


def download_bge_m3(cache_root: str, status_path: str) -> bool:
    """同步下载 bge-m3。先直连 HF，失败切 hf-mirror。

    返回是否成功。失败时同时记 OB-F004。
    """
    if is_model_ready(cache_root):
        write_status(status_path, {
            **_empty_status(cache_root),
            "phase": "completed",
            "downloaded_mb": _dir_size_mb(cache_root),
            "message": "模型已存在，跳过下载",
        })
        return True

    logger.info(f"[model_downloader] downloading bge-m3 → {cache_root}")

    # 1) 直连 huggingface.co
    if _try_download(cache_root, status_path, _HF_ENDPOINT_PRIMARY, "huggingface.co"):
        return True

    # 2) 兜底切镜像
    logger.info("[model_downloader] retrying via hf-mirror.com")
    if _try_download(cache_root, status_path, _HF_ENDPOINT_MIRROR, "hf-mirror.com"):
        return True

    # 两次都败 → 记 OB-F004
    try:
        try:
            from errors import record_error  # type: ignore
        except ImportError:
            from .errors import record_error  # type: ignore
        record_error(
            "OB-F004",
            f"bge-m3 download failed from both huggingface.co and hf-mirror.com. cache_root={cache_root}",
        )
    except Exception:
        pass
    return False


# ============================================================
# 异步封装（线程版；MCP stdio 模式没有 event loop 也能用）
# ============================================================

def download_bge_m3_in_background(
    cache_root: str,
    status_path: str,
    on_complete=None,
) -> threading.Thread | None:
    """启动后台线程下载。同时刻只允许一个任务，重复调用返回 None。

    on_complete: 可选回调 callable(success: bool) -> None，下载结束时在子线程里调用。
    """
    global _download_thread
    if not _download_lock.acquire(blocking=False):
        logger.info("[model_downloader] another download already in progress; skip")
        return None

    def _target() -> None:
        try:
            ok = download_bge_m3(cache_root, status_path)
        except Exception as e:
            logger.error(f"[model_downloader] unexpected error: {e}")
            ok = False
        finally:
            _download_lock.release()
        if on_complete is not None:
            try:
                on_complete(ok)
            except Exception as e:
                logger.warning(f"[model_downloader] on_complete callback failed: {e}")

    t = threading.Thread(target=_target, name="ombre-model-downloader", daemon=True)
    t.start()
    _download_thread = t
    return t


def ensure_local_model_async(
    embedding_engine,
    buckets_dir: str,
) -> threading.Thread | None:
    """启动期挂钩：

    - 若 embedding_engine.backend != 'local' → no-op
    - 若模型已就绪 → 写一条 idle/completed 的 status 让前端能立即查到，no-op
    - 若模型缺失 → 暂时把 embedding_engine.enabled 置 False（搜索退到关键词），
      启动后台下载；下载完成后回调里把 enabled 翻回 True 并 warmup 模型

    返回后台线程对象（用于测试/调试），或 None。
    """
    if getattr(embedding_engine, "backend", "") != "local":
        return None

    backend_obj = getattr(embedding_engine, "_backend", None)
    if backend_obj is None:
        return None
    # LocalEmbeddingEngine.cache_root 是下载 / 加载 共享的唯一路径源
    cache_root = getattr(backend_obj, "cache_root", None) or getattr(backend_obj, "model_dir", "")
    if not cache_root:
        return None

    status_path = status_path_for(buckets_dir)

    if is_model_ready(cache_root):
        # 写一条 completed status 让前端首次查询不返回空
        write_status(status_path, {
            **_empty_status(cache_root),
            "phase": "completed",
            "downloaded_mb": _dir_size_mb(cache_root),
            "message": "模型已存在",
        })
        return None

    # 模型缺失：暂时降级 embedding，避免 generate 时反复抛 OB-F004
    embedding_engine.enabled = False
    logger.warning(
        "[model_downloader] bge-m3 model missing; embedding temporarily disabled. "
        "Background download started — search will fall back to keyword mode until ready."
    )

    def _on_complete(success: bool) -> None:
        if success:
            embedding_engine.enabled = True
            try:
                # 触发 warmup 把模型 load 到内存，避免下次 generate 才加载
                if backend_obj is not None and hasattr(backend_obj, "warmup"):
                    backend_obj.warmup()
                logger.info("[model_downloader] embedding re-enabled after model download")
            except Exception as e:
                logger.warning(f"[model_downloader] warmup after download failed: {e}")
        else:
            logger.error(
                "[model_downloader] embedding remains disabled — model download failed. "
                "Switch to backend=api or place model files manually under "
                f"{cache_root}"
            )

    return download_bge_m3_in_background(cache_root, status_path, on_complete=_on_complete)


__all__ = [
    "is_model_ready",
    "status_path_for",
    "read_status",
    "write_status",
    "download_bge_m3",
    "download_bge_m3_in_background",
    "ensure_local_model_async",
]
