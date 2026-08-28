#!/usr/bin/env python3
"""
cog_telemetry_report.py — 认知循环遥测报告生成
===============================================
读取 data/state/cog_telemetry.jsonl，输出统计摘要。

用法:
  python scripts/wheels/cog_telemetry_report.py                    # 全量摘要
  python scripts/wheels/cog_telemetry_report.py --last 100         # 最近N条
  python scripts/wheels/cog_telemetry_report.py --by-phase         # 按阶段分组
  python scripts/wheels/cog_telemetry_report.py --json             # JSON 输出
  python scripts/wheels/cog_telemetry_report.py --watch 5          # 每5秒刷新

统计指标:
  - 各 cog-tool 调用次数
  - 锁竞争率 (lock_conflict / total)
  - 各阶段平均耗时
  - 错误率
  - 触发场景分布（manual vs workflow）
"""

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

_SYS_ENC = "gbk" if sys.platform == "win32" else "utf-8"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TELEMETRY_FILE = PROJECT_ROOT / "data" / "state" / "cog_telemetry.jsonl"


def load_entries(last_n: int = 0) -> list[dict]:
    """从 telemetry JSONL 加载记录，按时间排序。"""
    if not TELEMETRY_FILE.exists():
        print(f"[cog_telemetry_report] 遥测文件不存在: {TELEMETRY_FILE}", file=sys.stderr)
        return []

    entries = []
    with TELEMETRY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    entries.sort(key=lambda x: x.get("ts", 0))

    if last_n > 0 and len(entries) > last_n:
        entries = entries[-last_n:]

    return entries


def compute_stats(entries: list[dict]) -> dict:
    """计算统计指标。"""
    if not entries:
        return {"total": 0, "message": "无遥测数据"}

    total = len(entries)

    # 按 phase 分组
    by_phase = defaultdict(list)
    for e in entries:
        by_phase[e.get("phase", "unknown")].append(e)

    # 按 event 分组
    by_event = defaultdict(int)
    for e in entries:
        by_event[e.get("event", "unknown")] += 1

    # 锁状态统计
    lock_statuses = defaultdict(int)
    lock_conflicts = 0
    for e in entries:
        ls = e.get("lock_status", "")
        lock_statuses[ls] += 1
        if "conflict" in ls.lower() or ls == "timeout":
            lock_conflicts += 1

    # 各 phase 平均耗时
    phase_durations = {}
    for phase, items in by_phase.items():
        durations = [e.get("duration_ms", 0) for e in items if e.get("duration_ms", 0) > 0]
        phase_durations[phase] = {
            "count": len(items),
            "avg_ms": round(sum(durations) / len(durations), 1) if durations else 0,
            "max_ms": max(durations) if durations else 0,
            "min_ms": min(durations) if durations else 0,
        }

    # 触发场景
    triggers = defaultdict(int)
    for e in entries:
        triggers[e.get("trigger", "unknown")] += 1

    # 错误统计
    errors = [e for e in entries if e.get("error")]
    error_count = len(errors)

    # 窗口分布
    windows = defaultdict(int)
    for e in entries:
        windows[e.get("window_id", "unknown")] += 1

    # 时间跨度
    timestamps = [e.get("ts", 0) for e in entries if e.get("ts")]
    time_span_s = round(max(timestamps) - min(timestamps), 1) if len(timestamps) >= 2 else 0

    return {
        "total": total,
        "time_span_s": time_span_s,
        "time_span_str": f"{time_span_s / 3600:.1f}h" if time_span_s > 3600 else f"{time_span_s / 60:.1f}min",
        "by_phase": {k: len(v) for k, v in sorted(by_phase.items())},
        "by_event": dict(sorted(by_event.items(), key=lambda x: -x[1])),
        "lock_statuses": dict(sorted(lock_statuses.items())),
        "lock_conflict_rate": round(lock_conflicts / total * 100, 2) if total else 0,
        "lock_conflicts": lock_conflicts,
        "phase_durations": phase_durations,
        "triggers": dict(sorted(triggers.items(), key=lambda x: -x[1])),
        "error_count": error_count,
        "error_rate": round(error_count / total * 100, 2) if total else 0,
        "window_count": len(windows),
        "windows": dict(sorted(windows.items(), key=lambda x: -x[1])),
    }


def format_text(stats: dict) -> str:
    """人类可读格式输出。"""
    if stats.get("total", 0) == 0:
        return f"📊 认知循环遥测: {stats.get('message', '无数据')}"

    lines = []
    lines.append("=" * 50)
    lines.append(f"📊 认知循环遥测报告")
    lines.append(f"   总计 {stats['total']} 条记录 | 跨度 {stats['time_span_str']}")
    lines.append("=" * 50)

    lines.append(f"\n📌 按阶段 (Phase):")
    for phase, count in stats["by_phase"].items():
        dur = stats["phase_durations"].get(phase, {})
        dur_str = f" | avg {dur.get('avg_ms', 0)}ms, max {dur.get('max_ms', 0)}ms" if dur.get("count", 0) > 0 else ""
        lines.append(f"  {phase:<20} | {count:>4} 次{dur_str}")

    lines.append(f"\n🔒 锁状态:")
    for status, count in stats["lock_statuses"].items():
        lines.append(f"  {status:<15} | {count:>4}")
    lines.append(f"  冲突率: {stats['lock_conflict_rate']}% ({stats['lock_conflicts']}/{stats['total']})")

    lines.append(f"\n⚡ 触发场景:")
    for trigger, count in stats["triggers"].items():
        lines.append(f"  {trigger:<15} | {count:>4}")

    lines.append(f"\n⚠️ 错误率: {stats['error_rate']}% ({stats['error_count']}/{stats['total']})")
    lines.append(f"🪟 窗口数: {stats['window_count']}")
    for wid, count in stats["windows"].items():
        lines.append(f"  {wid[:16]} | {count}")

    lines.append(f"\n🏷️ Event 分布:")
    for event, count in stats["by_event"].items():
        lines.append(f"  {event:<30} | {count:>4}")

    return "\n".join(lines)


def watch_loop(interval_s: int):
    """持续监控模式。"""
    while True:
        entries = load_entries()
        stats = compute_stats(entries)
        print("\033[2J\033[H")  # clear screen
        print(format_text(stats))
        print(f"\n--- 每 {interval_s}s 刷新 | Ctrl+C 退出 ---")
        try:
            time.sleep(interval_s)
        except KeyboardInterrupt:
            print("\n退出")
            break


def main():
    import argparse

    parser = argparse.ArgumentParser(description="认知循环遥测报告")
    parser.add_argument("--last", type=int, default=0, help="仅分析最近 N 条")
    parser.add_argument("--by-phase", action="store_true", help="按阶段分组详细输出")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--watch", type=int, default=0, help="持续监控模式（间隔秒数）")
    args = parser.parse_args()

    if args.watch > 0:
        watch_loop(args.watch)
        return

    entries = load_entries(last_n=args.last)
    stats = compute_stats(entries)

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print(format_text(stats))


if __name__ == "__main__":
    main()
