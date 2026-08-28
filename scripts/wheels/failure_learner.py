#!/usr/bin/env python3
"""
失败自动学习系统 — AI 自主积累失败经验的简单机制。

核心理念:
  - 成功不一定知道，但失败一定清楚（用户的原话）
  - 失败是 AI 自主学习的唯一可靠信号
  - 不分类、不过闸、不评审 — 原样记录，后续再分析

用法:
  python scripts/wheels/failure_learner.py record    交互式记录失败
  python scripts/wheels/failure_learner.py record --reason "xxx" --cause "yyy" --lesson "zzz"
  python scripts/wheels/failure_learner.py recent    最近 N 次失败
  python scripts/wheels/failure_learner.py today     今天的失败汇总
  python scripts/wheels/failure_learner.py pattern   分析重复模式（3+次重复→推荐加规则）

设计:
  数据路径: data/failures/failure_log.jsonl
  格式: 每行一个 JSON，含 timestamp/reason/cause/lesson/task_type/auto
  自动模式(auto=true): AI 在工具失败时自动调用
  手动模式(auto=false): 用户主动记录
"""

import os, sys, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).resolve().parent.parent.parent
FAIL_DIR = BASE / "data" / "failures"
FAIL_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = FAIL_DIR / "failure_log.jsonl"
PATTERN_FILE = FAIL_DIR / "patterns.json"


# ── 记录失败 ───────────────────────────────────────────────────────

def record_failure(reason="", cause="", lesson="", task_type="", auto=True, tags=None):
    """
    记录一条失败经验。

    参数:
      reason:    什么失败了（一句话）
      cause:     根本原因
      lesson:    下次怎么做
      task_type: 任务类型（code/cad/pic/quant/sys/other）
      auto:      是否 AI 自动记录（True=自动，False=手动）
      tags:      标签列表
    """
    if not reason and not cause:
        return None  # 什么都不填就不记

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason.strip(),
        "cause": cause.strip(),
        "lesson": lesson.strip(),
        "task_type": task_type or "other",
        "auto": auto,
        "tags": tags or [],
    }

    with open(str(LOG_FILE), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry


# ── 查看最近失败 ───────────────────────────────────────────────────

def show_recent(n=10):
    """显示最近 N 条失败记录"""
    if not LOG_FILE.exists():
        print("📭 还没有任何失败记录 — 这是好事")
        return []

    records = []
    with open(str(LOG_FILE), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    recent = records[-n:]
    print(f"📋 最近 {len(recent)} 条失败记录:\n")

    for i, r in enumerate(recent, 1):
        ts = r["timestamp"][:19].replace("T", " ")
        marker = "🤖" if r.get("auto") else "✋"
        print(f"  {i}. [{marker}] {ts}")
        print(f"     失败: {r.get('reason', '?')}")
        if r.get("cause"):
            print(f"     原因: {r['cause']}")
        if r.get("lesson"):
            print(f"     教训: {r['lesson']}")
        print(f"     类型: {r.get('task_type', 'other')}")
        print()

    return recent


# ── 今天汇总 ────────────────────────────────────────────────────────

def show_today():
    """今天失败汇总"""
    if not LOG_FILE.exists():
        print("📭 今天还没有失败记录")
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    records = []
    with open(str(LOG_FILE), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                if r["timestamp"].startswith(today):
                    records.append(r)

    if not records:
        print(f"✅ 今天 ({today}) 零失败 — 不错")
        return

    print(f"📊 今天 ({today}) 共 {len(records)} 次失败:\n")
    by_type = {}
    for r in records:
        t = r.get("task_type", "other")
        by_type.setdefault(t, []).append(r)

    for t, rs in sorted(by_type.items()):
        print(f"  [{t}] {len(rs)} 次:")
        for r in rs:
            print(f"    - {r.get('reason', '?')}")
        print()


# ── 分析重复模式 ────────────────────────────────────────────────────

def analyze_patterns(min_repeat=3):
    """
    分析重复失败模式。
    同一 reason 出现 >=3 次 → 建议加规则。
    """
    if not LOG_FILE.exists():
        print("📭 没有足够数据做模式分析")
        return

    records = []
    with open(str(LOG_FILE), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    # 按失败原因统计
    from collections import Counter
    reasons = Counter(r.get("reason", "?") for r in records)

    print("🔍 失败模式分析:\n")

    found = False
    for reason, count in reasons.most_common():
        if count >= min_repeat:
            found = True
            print(f"  ⚠ [{count}次] {reason}")
            # 找最近一次教训
            for r in reversed(records):
                if r.get("reason") == reason and r.get("lesson"):
                    print(f"     最近教训: {r['lesson']}")
                    break
            print()

    if not found:
        print(f"  无重复 >= {min_repeat} 的模式 — 失败分布较散")

    # 输出结论
    print(f"  总记录 {len(records)} 条, 唯一原因 {len(reasons)} 种")


# ── CLI ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("help", "--help", "-h"):
        print(__doc__)
        return

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "record":
        # 解析参数
        reason = ""
        cause = ""
        lesson = ""
        task_type = "other"
        auto = True

        i = 0
        while i < len(args):
            if args[i] == "--reason" and i + 1 < len(args):
                reason = args[i + 1]; i += 2
            elif args[i] == "--cause" and i + 1 < len(args):
                cause = args[i + 1]; i += 2
            elif args[i] == "--lesson" and i + 1 < len(args):
                lesson = args[i + 1]; i += 2
            elif args[i] == "--type" and i + 1 < len(args):
                task_type = args[i + 1]; i += 2
            elif args[i] == "--manual":
                auto = False; i += 1
            else:
                i += 1

        if not reason and not cause:
            # 未提供参数 → 尝试交互模式
            print("=== 记录失败（Ctrl+C 取消）===")
            reason = input("  什么失败了? ").strip()
            if not reason:
                print("  ❌ 取消")
                return
            cause = input("  原因? ").strip()
            lesson = input("  教训? ").strip()
            task_type = input("  类型 (code/cad/pic/quant/sys/other) [other]: ").strip() or "other"
            auto_input = input("  自动记录? (Y/n): ").strip().lower()
            auto = auto_input != "n"

        entry = record_failure(reason, cause, lesson, task_type, auto)
        if entry:
            ts = entry["timestamp"][:19].replace("T", " ")
            print(f"  ✅ 已记录 [{ts}] {reason[:40]}...")

    elif cmd == "recent":
        n = 10
        if args and args[0].lstrip("-").isdigit():
            n = int(args[0])
        show_recent(n)

    elif cmd == "today":
        show_today()

    elif cmd == "pattern":
        min_repeat = 3  # 来源:经验值 — 模式识别最少重复次数
        if args and args[0].lstrip("-").isdigit():
            min_repeat = int(args[0])
        analyze_patterns(min_repeat)

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
