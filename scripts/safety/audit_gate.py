#!/usr/bin/env python3
"""
audit_gate.py — 审计声明强制验证器

用法（供 PreToolUse / PostToolUse 调）:
  python scripts/safety/audit_gate.py check "工具名称" "命令/内容"
  → 返回 {"ok": true/false, "reason": "..."}

设计：
  当检测到工具调用内容中含"审计""audit"等关键词时，
  检查 data/audit/.audit_done 是否存在且新鲜（<=10分钟）。
  不存在/过期 -> 拒绝。存在 -> 放行。

  唯一的例外：调用 audit 工具本身（写 .audit_done 的行为）。
"""
import json, os, sys, time
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent

# .audit_done 新鲜度阈值（秒）
AUDIT_TTL = 600  # 10 分钟

# 关键词 — 声称已审计的迹象
CLAIM_PATTERNS = [
    "审计", "audit", "已审",
    "verified independently", "独立验证",
    "Qwen audit", "DS audit",
]


def check(tool_name: str, content: str) -> dict:
    """检查调用是否声称已审计，以及是否合法。"""
    content_lower = content.lower()

    # 例外：调用 forced_audit.py 本身是写标志的合法行为
    if "forced_audit" in content or "audit_gate" in content or "audit-mcp" in content or "audit_code" in content:
        return {"ok": True, "reason": "gate self-call"}

    # 检测是否包含审计声明的迹象
    has_claim = any(p in content for p in CLAIM_PATTERNS)
    if not has_claim:
        return {"ok": True, "reason": "no audit claim detected"}

    # 检查 .audit_done 标志文件
    flag_path = ROOT / "data" / "audit" / ".audit_done"
    if not flag_path.exists():
        return {
            "ok": False,
            "reason": f"[AUDIT_GATE] 检测到审计声明，但未找到审计标志文件。必须先执行:\n"
                      f"  python scripts/safety/forced_audit.py --prompt \"审计内容\"\n"
                      f"声称审计但不实际调用外部 API = 违规。"
        }

    try:
        flag = json.loads(flag_path.read_text(encoding="utf-8"))
        flag_ts = datetime.fromisoformat(flag["ts"])
        now = datetime.now(timezone.utc)
        age = (now - flag_ts).total_seconds()

        if age > AUDIT_TTL:
            return {
                "ok": False,
                "reason": f"[AUDIT_GATE] 审计标志已过期 ({int(age)}s > {AUDIT_TTL}s TTL)。请重新执行:\n"
                          f"  python scripts/safety/forced_audit.py --prompt \"审计内容\""
            }

        return {"ok": True, "reason": f"audit done ({int(age)}s ago, content_hash={flag.get('content_hash','?')})"}
    except Exception as e:
        return {"ok": False, "reason": f"[AUDIT_GATE] 审计标志文件损坏: {e}"}


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "reason": "用法: audit_gate.py check <工具名> <内容>"}))
        sys.exit(1)

    mode = sys.argv[1]
    if mode == "check" and len(sys.argv) >= 4:
        tool_name = sys.argv[2]
        content = sys.argv[3]
        result = check(tool_name, content)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result["ok"] else 2)
    else:
        print(json.dumps({"ok": False, "reason": f"未知模式: {mode}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
