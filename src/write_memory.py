#!/usr/bin/env python3
"""
========================================
write_memory.py — 手动写入记忆的命令行小工具
========================================

不走 MCP、不走 HTTP，直接把一条记忆写成一个 .md 文件。
主要用于调试 / 在 Copilot 端快速补东西 / API 不可用时的底圈。

关键行为：
- 两种用法：命令行参数、或交互 input
- 路径优先级：OMBRE_BUCKETS_DIR > config.yaml > 内置默认
- 必填：name / content；可选：domain / tags / valence / arousal / importance
- 写入 dynamic/ 目录，生成 12 位 hex bucket_id

不做什么（边界）：
- 不调 LLM、不做 analyze、不做合并查重
- 不启动 BucketManager，直接拼 frontmatter 写文件
- 不同步 embedding（后台 decay/打分下次读到会补）

对外暴露：CLI 入口。
========================================
"""

import os
import uuid
import argparse
from datetime import datetime


def _resolve_dynamic_dir() -> str:
    """
    Resolve the `dynamic/` directory under the configured bucket root.
    Priority: $OMBRE_BUCKETS_DIR > config.yaml > built-in default.
    优先级：环境变量 > config.yaml > 内置默认。
    """
    env_dir = os.environ.get("OMBRE_BUCKETS_DIR", "").strip()
    if env_dir:
        return os.path.join(os.path.expanduser(env_dir), "dynamic")
    try:
        from utils import load_config  # local import to avoid hard dep when missing
        cfg = load_config()
        return os.path.join(cfg["buckets_dir"], "dynamic")
    except Exception:
        # Fallback to project-local ./buckets/dynamic
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "buckets", "dynamic"
        )


VAULT_DIR = _resolve_dynamic_dir()


def gen_id():
    return uuid.uuid4().hex[:12]


def write_memory(
    name: str,
    content: str,
    domain: list[str],
    tags: list[str],
    importance: int = 7,
    valence: float = 0.5,
    arousal: float = 0.3,
):
    mid = gen_id()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # YAML frontmatter
    domain_yaml = "\n".join(f"- {d}" for d in domain)
    tags_yaml = "\n".join(f"- {t}" for t in tags)

    md = f"""---
activation_count: 0
arousal: {arousal}
created: '{now}'
domain:
{domain_yaml}
id: {mid}
importance: {importance}
last_active: '{now}'
name: {name}
tags:
{tags_yaml}
type: dynamic
valence: {valence}
---

{content}
"""

    path = os.path.join(VAULT_DIR, f"{mid}.md")
    os.makedirs(VAULT_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"✓ 已写入: {path}")
    print(f"  ID: {mid} | 名称: {name}")
    return mid


def interactive():
    print("=== Ombre Brain 手动写入 ===")
    name = input("记忆名称: ").strip()
    content = input("内容: ").strip()
    domain = [d.strip() for d in input("主题域(逗号分隔): ").split(",") if d.strip()]
    tags = [t.strip() for t in input("标签(逗号分隔): ").split(",") if t.strip()]
    importance = int(input("重要性(1-10, 默认7): ").strip() or "7")
    valence = float(input("效价(0-1, 默认0.5): ").strip() or "0.5")
    arousal = float(input("唤醒(0-1, 默认0.3): ").strip() or "0.3")
    write_memory(name, content, domain, tags, importance, valence, arousal)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="手动写入 Ombre Brain 记忆")
    parser.add_argument("--name", help="记忆名称")
    parser.add_argument("--content", help="记忆内容")
    parser.add_argument("--domain", help="主题域,逗号分隔")
    parser.add_argument("--tags", help="标签,逗号分隔")
    parser.add_argument("--importance", type=int, default=7)
    parser.add_argument("--valence", type=float, default=0.5)
    parser.add_argument("--arousal", type=float, default=0.3)
    args = parser.parse_args()

    if args.name and args.content and args.domain:
        write_memory(
            name=args.name,
            content=args.content,
            domain=[d.strip() for d in args.domain.split(",")],
            tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
            importance=args.importance,
            valence=args.valence,
            arousal=args.arousal,
        )
    else:
        interactive()
