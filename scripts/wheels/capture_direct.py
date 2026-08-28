#!/usr/bin/env python3
"""capture_direct.py — 知识捕获 (无MCP依赖, 直接写knowledge)
=====================================================
用法: python capture_direct.py "<标题>" "<内容>" [域]
域: cad/pic/cls/general (默认general)

@since: 2026-07-28
"""

import json, sys, os
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent
KB_DIR = ROOT / "knowledge" / "学习资料" / "经验库"
LOG_FILE = ROOT / "knowledge" / "学习资料" / "经验库" / "_capture_log.md"


def capture(title: str, content: str, domain: str = "general"):
    KB_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{domain}_{ts}.md"
    filepath = KB_DIR / fname

    entry = f"# {title}\n\n> 域: {domain}\n> 捕获时间: {datetime.now().isoformat()}\n\n{content}\n"
    filepath.write_text(entry, encoding="utf-8")

    # 追加日志
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"- [{ts}] **{title}** [{domain}] → {fname}\n")

    return {"ok": True, "file": str(filepath.relative_to(ROOT)), "domain": domain}


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: capture_direct.py <title> <content> [domain]"}, ensure_ascii=False))
        return

    title = sys.argv[1]
    content = sys.argv[2]
    domain = sys.argv[3] if len(sys.argv) > 3 else "general"

    result = capture(title, content, domain)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
