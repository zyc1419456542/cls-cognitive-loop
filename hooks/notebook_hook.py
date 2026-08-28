#!/usr/bin/env python3
"""
notebook_hook.py — PreToolUse hook: 长链笔记本拦截
==================================================
被 PreToolUse.ps1 调用。

功能:
  1. 检测 Write → 记录全量遥测 (Layer A)
  2. 重要 Write → 如果有活跃笔记本，注入 check-in 提醒 (Layer B)
  3. 不阻塞、不阻止任何操作
"""

import json, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# ── 重要目录（触发 Layer B 拦截） ──
IMPORTANT_PREFIXES = [
    "assistant交付",
    "knowledge",
    "scripts",
    ".claude/workflows",
    "认知系统迭代",
]

# ── Hook 入口 ──

def handle(tool_name: str, params: dict) -> dict:
    """
    返回: {"additionalContext": str | None}
    如果返回 additionalContext，会注入到我的上下文里让我看到。
    """
    if tool_name != "Write":
        return {}

    file_path = (params or {}).get("file_path", "")
    if not file_path:
        return {}

    # ── Layer A: 全量遥测 ──
    telem_dir = ROOT / "data" / "notebooks" / "telemetry"
    telem_dir.mkdir(parents=True, exist_ok=True)
    telem_file = telem_dir / "write_telemetry.jsonl"

    entry = {
        "ts": time.time(),
        "file": file_path,
        "tool": "Write",
    }
    try:
        with open(telem_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 静默失败

    # ── Layer B: 认知拦截（仅重要目录） ──
    rel = file_path.replace("\\", "/")
    is_important = any(rel.startswith(p.replace("\\", "/")) for p in IMPORTANT_PREFIXES)
    if not is_important:
        return {}

    # 读取活跃笔记本
    active_dir = ROOT / "data" / "notebooks" / "active"
    notebooks = []
    if active_dir.exists():
        for fp in sorted(active_dir.glob("*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    nb = json.load(f)
                notebooks.append({
                    "id": nb["_meta"]["id"],
                    "title": nb.get("title", ""),
                    "domain": nb.get("domain", ""),
                    "checkins": len(nb.get("checkins", [])),
                })
            except Exception:
                pass

    if not notebooks:
        return {}

    # 注入 check-in 提醒
    context = "## 📋 长链笔记本提醒\n\n当前活跃笔记本：\n"
    for nb in notebooks:
        context += f"- **{nb['title']}** (领域: {nb['domain']}, 已 check-in {nb['checkins']} 次)\n"
    context += "\n需要 check-in 吗？执行 `python scripts/wheels/notebook_core.py checkin --id <id> --step <索引>`"

    return {"additionalContext": context}


if __name__ == "__main__":
    # 从环境变量读取调用信息（由 PreToolUse.ps1 传入）
    tool_name = os.environ.get("CLAUDE_TOOL_NAME", "")
    params_json = os.environ.get("CLAUDE_TOOL_PARAMS", "{}")
    try:
        params = json.loads(params_json)
    except json.JSONDecodeError:
        params = {}

    result = handle(tool_name, params)
    if result.get("additionalContext"):
        print(result["additionalContext"])
