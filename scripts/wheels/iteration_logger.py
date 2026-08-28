#!/usr/bin/env python3
"""iteration_logger.py — 认知系统迭代记录器（大修/小修自动判断）
================================================================
结构化输出，带YAML frontmatter，方便脚本批量解析。

用法:
  # 记录一次迭代
  python iteration_logger.py record \
    --task "注入质量分析体系搭建" \
    --output "injection_quality_analyzer.py等6个文件" \
    --conclusion "ep-json做注入分析100%JSON合法" \
    --lesson "小模型+微调>大模型裸跑" \
    --decision "maintainer批准方案B直接接入" \
    --source "本地微调ep-json, 90条注入分析"

  # 自动判断大修/小修
  python iteration_logger.py record --task "xxx" --components "cognitive_gate,always_injector" ...
    # ≥3个核心组件→大修, 否则→小修

  # 查看最近记录
  python iteration_logger.py recent [--n 10]

  # 更新INDEX.md
  python iteration_logger.py index

@since: 2026-08-21
"""
import argparse, json, os, re, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ITER_DIR = ROOT / "knowledge" / "05_CLS认知系统架构" / "认知系统迭代"
INDEX_FILE = ITER_DIR / "INDEX.md"

# 核心组件列表 (改动≥3个=大修)
CORE_COMPONENTS = {
    "cognitive_gate", "semantic_inject", "process_inject", "unified_inject",
    "always_injector", "cls_brain", "symbolic_observer", "symbolic_judge",
    "cog_step", "cog_context", "cog_trajectory", "cog_health",
    "fuse_board", "qwen_gate", "premise_check",
    "knowledge_cards", "kg_pipeline", "auto_capture",
    "content_gaze", "ops_monitor", "memory_fuse_watch",
    "drift_log", "injection_log", "injection_quality",
}


def _next_iter_number(iter_type):
    """获取下一个iter编号"""
    d = ITER_DIR / iter_type
    if not d.exists():
        return 1
    nums = []
    for f in d.glob("iter-*.md"):
        m = re.match(r"iter-(\d+)", f.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


def _detect_type(components, is_major_hint=None):
    """自动判断大修/小修
    规则: 核心组件改动≥3个=大修, 否则=小修。
    可通过 --type 覆盖。
    """
    if is_major_hint:
        return "大修" if is_major_hint else "小修"
    core_count = sum(1 for c in components if c in CORE_COMPONENTS)
    return "大修" if core_count >= 3 else "小修"


def _build_frontmatter(args, iter_type, iter_num):
    """构建YAML frontmatter"""
    now = datetime.datetime.now()
    fm = {
        "iter": iter_num,
        "type": iter_type,
        "date": now.strftime("%Y-%m-%d"),
        "task": args.task,
        "status": args.status or "完成",
    }
    if args.components:
        fm["components"] = args.components
    if args.conclusion:
        fm["conclusion"] = args.conclusion
    if args.decision:
        fm["decision"] = args.decision
    if args.lesson:
        fm["lesson"] = args.lesson
    if args.highlight:
        fm["highlight"] = args.highlight
    if args.difficulty:
        fm["difficulty"] = args.difficulty
    if args.source:
        fm["source"] = args.source
    if args.severity:
        fm["severity"] = args.severity
    return fm


def _build_content(args, fm):
    """构建文件正文"""
    lines = []
    lines.append(f"# iter-{fm['iter']:03d} — {args.task} ({fm['date']})\n")
    lines.append(f"> 类型: {fm['type']} | 状态: {fm['status']}")
    if args.components:
        lines.append(f"> 组件: {args.components}")
    lines.append("")

    if args.output:
        lines.append(f"## 产出\n\n{args.output}\n")
    if args.conclusion:
        lines.append(f"## 结论\n\n<!--capture:conclusion anchor=model_authored-->\n{args.conclusion}\n")
    if args.decision:
        lines.append(f"## 决策\n\n<!--capture:decision anchor=human_confirmed-->\n{args.decision}\n")
    if args.lesson:
        lines.append(f"## 教训\n\n<!--capture:lesson anchor=model_authored-->\n{args.lesson}\n")
    if args.highlight:
        lines.append(f"## 亮点\n\n<!--capture:highlight anchor=model_authored-->\n{args.highlight}\n")
    if args.difficulty:
        lines.append(f"## 克服的困难\n\n<!--capture:difficulty anchor=hard_gate-->\n{args.difficulty}\n")
    if args.source:
        lines.append(f"## 引用来源\n\n<!--capture:source-->\n{args.source}\n")
    if args.next_step:
        lines.append(f"## 下一步\n\n{args.next_step}\n")
    return "\n".join(lines)


def record(args):
    """记录一次迭代"""
    components = [c.strip() for c in (args.components or "").split(",") if c.strip()]
    iter_type = _detect_type(components, args.major)
    iter_num = _next_iter_number(iter_type)
    d = ITER_DIR / iter_type
    d.mkdir(parents=True, exist_ok=True)

    fm = _build_frontmatter(args, iter_type, iter_num)
    content = _build_content(args, fm)

    # 写文件 (YAML frontmatter + markdown)
    fname = f"iter-{iter_num:03d}-{re.sub(r'[^a-z0-9]+', '-', args.task.lower())[:50]}.md"
    fpath = d / fname
    with open(fpath, "w", encoding="utf-8") as f:
        f.write("---\n")
        for k, v in fm.items():
            if isinstance(v, list):
                f.write(f"{k}: [{', '.join(v)}]\n")
            else:
                f.write(f"{k}: \"{v}\"\n")
        f.write("---\n\n")
        f.write(content)

    print(f"✅ iter-{iter_num:03d} [{iter_type}] {args.task}")
    print(f"   文件: {fpath}")
    print(f"   类型: {iter_type} (核心组件: {sum(1 for c in components if c in CORE_COMPONENTS)}/{len(components)})")

    # 自动更新INDEX
    update_index()
    return fpath


def update_index():
    """扫描大修/小修目录，生成INDEX.md"""
    entries = []
    for iter_type in ["大修", "小修"]:
        d = ITER_DIR / iter_type
        if not d.exists():
            continue
        for f in sorted(d.glob("iter-*.md")):
            # 读frontmatter
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    lines = fp.readlines()
                in_fm = False
                fm = {}
                for line in lines:
                    if line.strip() == "---":
                        in_fm = not in_fm
                        continue
                    if in_fm and ":" in line:
                        k, v = line.split(":", 1)
                        v = v.strip().strip('"')
                        if v.startswith("[") and v.endswith("]"):
                            v = [x.strip().strip('"') for x in v[1:-1].split(",")]
                        fm[k.strip()] = v
                entries.append({
                    "file": f.relative_to(ITER_DIR),
                    "iter": fm.get("iter", "?"),
                    "type": iter_type,
                    "date": fm.get("date", "?"),
                    "task": fm.get("task", f.stem),
                    "status": fm.get("status", "?"),
                    "conclusion": fm.get("conclusion", ""),
                    "lesson": fm.get("lesson", ""),
                    "decision": fm.get("decision", ""),
                })
            except Exception:
                entries.append({"file": f.relative_to(ITER_DIR), "type": iter_type, "task": f.stem})

    # 生成INDEX
    lines = ["# 认知系统迭代 INDEX\n", f"> 自动生成: {datetime.datetime.now().isoformat()[:19]}\n"]
    lines.append(f"## 统计: {len(entries)} 条记录 (大修 {sum(1 for e in entries if e.get('type')=='大修')}, 小修 {sum(1 for e in entries if e.get('type')=='小修')})\n")

    for iter_type in ["大修", "小修"]:
        typed = [e for e in entries if e.get("type") == iter_type]
        if not typed:
            continue
        lines.append(f"\n## {iter_type}\n")
        lines.append("| # | 日期 | 任务 | 状态 | 结论/教训 |")
        lines.append("|---|------|------|------|----------|")
        for e in reversed(typed):
            note = e.get("conclusion") or e.get("lesson") or e.get("decision") or ""
            if len(note) > 40:
                note = note[:40] + "..."
            lines.append(f"| {e.get('iter','-')} | {e.get('date','-')} | {e.get('task','-')} | {e.get('status','-')} | {note} |")

    INDEX_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"📋 INDEX.md 已更新 ({len(entries)} 条)")


def recent(args):
    """显示最近N条记录"""
    entries = []
    for iter_type in ["大修", "小修"]:
        d = ITER_DIR / iter_type
        if not d.exists():
            continue
        for f in sorted(d.glob("iter-*.md"), reverse=True):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    head = fp.read(500)
                fm = {}
                in_fm = False
                for line in head.split("\n"):
                    if line.strip() == "---":
                        in_fm = not in_fm
                        continue
                    if in_fm and ":" in line:
                        k, v = line.split(":", 1)
                        fm[k.strip()] = v.strip().strip('"')
                entries.append({
                    "file": str(f.relative_to(ITER_DIR)),
                    "iter": fm.get("iter", "?"),
                    "type": iter_type,
                    "date": fm.get("date", "?"),
                    "task": fm.get("task", f.stem),
                    "status": fm.get("status", "?"),
                })
            except Exception:
                pass

    entries.sort(key=lambda e: e.get("date", ""), reverse=True)
    for e in entries[:args.n]:
        print(f"  [{e.get('type','?')[0]}] iter-{e.get('iter','-')} | {e.get('date','-')} | {e.get('task','-')} | {e.get('status','-')}")


def main():
    ap = argparse.ArgumentParser(description="认知系统迭代记录器")
    sub = ap.add_subparsers(dest="command")

    rec = sub.add_parser("record", help="记录一次迭代")
    rec.add_argument("--task", "-t", required=True, help="任务名称")
    rec.add_argument("--output", "-o", default="", help="产出物")
    rec.add_argument("--conclusion", default="", help="结论")
    rec.add_argument("--decision", default="", help="决策(maintainer定调)")
    rec.add_argument("--lesson", default="", help="教训")
    rec.add_argument("--highlight", default="", help="亮点")
    rec.add_argument("--difficulty", default="", help="克服的困难")
    rec.add_argument("--source", default="", help="引用来源")
    rec.add_argument("--next-step", default="", help="下一步")
    rec.add_argument("--components", "-c", default="", help="涉及组件(逗号分隔)")
    rec.add_argument("--status", "-s", default="完成", help="状态:完成/进行中/阻塞")
    rec.add_argument("--type", choices=["大修", "小修", "auto"], default="auto", help="类型(默认自动判断)")
    rec.add_argument("--major", action="store_true", help="强制大修")
    rec.add_argument("--minor", action="store_true", help="强制小修")
    rec.add_argument("--severity", default="info", help="严重度")

    sub.add_parser("index", help="更新INDEX.md")

    recent_cmd = sub.add_parser("recent", help="最近记录")
    recent_cmd.add_argument("-n", type=int, default=10, help="显示条数")

    args = ap.parse_args()
    if args.command == "record":
        if args.major:
            args.major = True
        elif args.minor:
            args.major = False
        else:
            args.major = None
        record(args)
    elif args.command == "index":
        update_index()
    elif args.command == "recent":
        recent(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
