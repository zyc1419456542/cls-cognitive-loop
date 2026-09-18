#!/usr/bin/env python3
"""
knowledge_cards.py — 知识卡片压缩器 (2026-08-16 maintainer立项, P2 重构后变薄壳)
==========================================================
公共核心在 knowledge_nav_core.py (存储/压缩/导航三件套)。
本文件只保留: provider 封装 (call_ds) + 增量构建编排 (build)。

模型: opencode deepseek-v4-flash (2026-08-16 夜 maintainer定: 卡片制作走 opencode 套餐, 免费), 后台批处理 (5文件/批)。
数据源: 双轨进度 + knowledge/进度文件 + 认知系统迭代 (maintainer 2026-08-16 定)。
"""
import json, re, sys, time
from pathlib import Path

_WHEELS = str(Path(__file__).resolve().parent)
if _WHEELS not in sys.path:
    sys.path.insert(0, _WHEELS)
from knowledge_nav_core import (ROOT, CARDS_FILE, SOURCES, SYSTEM, file_hash,
                                load_cards, save_cards, scan_files, compress_batch,
                                precompute_embeddings)

MODEL = "deepseek-v4-flash"  # @fix 2026-09-10: 换自 mimo-v2.5。实测真实卡生成 payload(1792字)
                             #   mimo+Go 0/2 失败(24.0s 空文本), DS Flash+Zen 成功 6.9s 出 1377 字
PROVIDER = "opencode"  # 走 api_pipeline 统一入口 (key=keys/opencode_config.json 三key轮换)


def call_ds(messages, max_tokens=1200):
    """统一入口走 api_pipeline (轮子可换: 换 provider/model 即切换)"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.wheels.api_pipeline import call
    for _ in range(3):
        try:
            r = call(PROVIDER, MODEL, messages=messages, max_tokens=max_tokens,
                     auto_route=False, timeout_s=120)
            if r and r.get("ok"):
                return (r.get("text") or "").strip()
        except Exception:
            pass
        time.sleep(3)
    return ""


def build(incremental: bool = True):
    """增量压缩: 只处理新/改文件 (按 src_hash), 每批5个, 存回 kg_cards.json"""
    data = load_cards()
    cards = data["cards"]
    files = scan_files()
    todo = []
    for f, h, text in files:
        rel = str(f.relative_to(ROOT))
        if incremental and rel in cards and cards[rel].get("src_hash") == h:
            continue
        todo.append((rel, h, text))
    if not todo:
        print("no new files, cards=%d" % len(cards))
        return
    print("to compress: %d files (total cards: %d)" % (len(todo), len(cards)))
    done = 0
    for i in range(0, len(todo), 5):
        batch = todo[i:i + 5]
        cards_out = compress_batch([(rel, text) for rel, h, text in batch], call_ds)
        matched = {}
        for c in cards_out:
            matched[c["file"]] = c
        for rel, h, text in batch:
            base = rel.replace("\\", "/").split("/")[-1]
            c = matched.get(rel) or matched.get(base)
            if not c:
                continue  # 模型漏压 → 下轮再试
            c["src_hash"] = h
            c["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            cards[rel] = c
            done += 1
        save_cards(data)
        print("  batch %d: +%d cards" % (i // 5 + 1, len(cards_out)))
        time.sleep(1)
    print("DONE: +%d new cards, total=%d" % (done, len(cards)))
    # 预计算新卡 embedding (bge-m3, 本地零成本)
    precompute_embeddings()


if __name__ == "__main__":
    incremental = "--full" not in sys.argv
    build(incremental=incremental)
