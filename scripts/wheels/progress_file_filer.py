#!/usr/bin/env python3
"""
进度文件归类器 / 进度记录器 — 路径永久固化的双轨进度管理
=========================================================
路径锁定:
  主进度 → knowledge/进度文件/progress_YYYYMMDD.md
  双轨   → assistant交付/📚 学习资料/学习进度/YYYYMMDD_HHMM_双轨进度_轨道.md

用法:
  # 记录新进度
  python progress_file_filer.py record \
      --task-source "maintainer给临时.txt 15 项 CLAUDE.md 修复任务" \
      --output "CLAUDE.md 修复 15 项全落地; 被动监控栈上线" \
      --completed "①[transcript_fact_scan.py L107] 新轮子... 动机:... 改法:... 设计亮点:..." \
      --next-step "①重启 CC 窗口使 MCP 生效" \
      --extra "scripts/wheels/transcript_fact_scan.py L107-159 | .claude/hooks/PostToolUse.ps1 L427-439" \
      --track ALL

  # 将已有文件归位到标准位置
  python progress_file_filer.py move <源文件路径> --type main|dual [--track A|B|C|ALL] [--date YYYYMMDD]

  # 扫描散落的进度/双轨文件
  python progress_file_filer.py scan [--dry-run]

  # 查看当前状态
  python progress_file_filer.py status

填写标准（陌生人接手原则）:
  --task-source: 今天干什么来的？maintainer给的/自主发现的/临时.txt 批注？
  --output:      一段话概括本日总产出, 含关键数值和文件路径。
                 标准：陌生人看到这段话能知道今天做出了什么东西。
  --completed:   编号列表，每项必须包含三段：
                 ①动机（为什么改/问题怎么死的/根因）
                 ②改法（怎么治理/逻辑链/代码结构/修复路径）
                 标准：陌生人看到这些能接手后续工作。
                 示例：
                   ①[scripts/wheels/xxx.py L100] 修复空转检测
                     动机：原代码 not msgs 提前 return 导致 stale 告警被短路
                     改法：stale 提到 msgs 判断之前，只依赖 mtime
  --next-step:   含具体文件路径+要验证的假设。
                 标准：明天开工第一件事就知道干什么。
  --extra:       数据锚定——列出本轮使用的所有原始数据路径+代码行号。
                 标准：事后能回溯到具体文件和具体行。
"""

import os, sys, re, datetime, argparse, shutil

# === 路径锁定（从脚本位置推导，永不硬编码） ===
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))

# 固定路径
MAIN_DIR = os.path.join(_PROJECT_ROOT, "knowledge", "进度文件")
DUAL_DIR = os.path.join(_PROJECT_ROOT, "assistant交付", "📚 学习资料", "学习进度")

# 禁止放文件的位置（散落检测黑名单）
BANNED_DIRS = [
    os.path.join(_PROJECT_ROOT, "data", "records"),
    os.path.join(_PROJECT_ROOT, "memory_backup"),
]
# 允许放进度/双轨的项目结构目录（不视为散落）
PROJECT_STRUCTURE_DIRS = [
    os.path.join(_PROJECT_ROOT, "knowledge"),
    os.path.join(_PROJECT_ROOT, "认知工程"),
    os.path.join(_PROJECT_ROOT, "梳理系统日志"),
]


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def _ts_now():
    return datetime.datetime.now().strftime("%H%M")


def _date_now():
    return datetime.datetime.now().strftime("%Y%m%d")


def _is_progress_file(filename):
    """判断文件名是否与进度/双轨相关。"""
    return bool(re.search(r'(progress|双轨|进度)', filename, re.IGNORECASE))


def _make_title(args) -> str:
    """生成双轨文件名内容简介: --title 优先, 缺省从 --output 提取前30字符。
    @since 2026-08-20 maintainer要求: 文件名含内容简介, 方便知识卡片匹配和回溯。
    """
    raw = (args.title or "").strip()
    if not raw:
        raw = (args.output or "").strip().split("\n")[0]
    # 清洗文件名非法字符 + 截断30字
    raw = re.sub(r'[\\/:*?"<>|\s]+', "", raw)[:30]
    return raw


def record(args):
    """记录一条新进度。"""
    _ensure_dir(MAIN_DIR)
    _ensure_dir(DUAL_DIR)

    track = (args.track or "ALL").upper()
    if track not in ("ALL", "A", "B", "C"):
        print(f"❌ 无效轨道: {track}，支持 ALL/A/B/C")
        sys.exit(1)

    date_str = args.date or _date_now()
    ts = args.time or _ts_now()

    # --- 主进度写入（覆盖写，一天一段） ---
    main_path = os.path.join(MAIN_DIR, f"progress_{date_str}.md")
    _write_main_progress(main_path, args, date_str)

    # --- 双轨写入 ---
    track_label = track
    title = _make_title(args)
    dual_path = os.path.join(DUAL_DIR, f"{date_str}_{ts}_双轨进度_{track_label}_{title}.md")
    _write_dual_progress(dual_path, args, date_str, track, ts)

    print(f"✅ 双轨进度已记录")
    print(f"   主进度: {main_path}")
    print(f"   双轨:   {dual_path}")
    return True


def _write_main_progress(path, args, date_str):
    """写入主进度文件（覆盖写，一天一段）。

    @since 2026-09-01 改为覆盖写 + 强制动机/改法/设计亮点三段式：
          每个完成项必须包含"为什么改+怎么改+设计亮点"，
          标准：陌生人看到日志就能接手工作。
    """
    output = args.output or "(未填)"
    completed = args.completed or "(未填)"
    next_step = args.next_step or "(未填)"
    task_source = args.task_source or "(未填)"
    extra = args.extra or "(未填)"

    lines = [f"# 进度记录 - {date_str}\n\n"]

    # 任务来源（必填）
    lines.append(f"## 0. 任务来源\n\n{task_source}\n\n")

    # 数据锚定（必填）
    lines.append(f"> 锚定: {extra}\n\n")

    # 产出
    lines.append(f"## 产出\n\n{output}\n\n")

    # 完成（提示词要求每项含动机+改法+设计亮点）
    lines.append(f"## 完成\n\n{completed}\n\n")

    # 下一步
    lines.append(f"## 下一步\n\n{next_step}\n\n")

    content = "".join(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _write_dual_progress(path, args, date_str, track, ts):
    """写入双轨进度文件（带YAML frontmatter，方便脚本批量解析）。

    @since 2026-08-20 结构化知识段: 教训/亮点/克服的困难 用固定标记写入,
    auto_capture 被动解析这些标记直接入库(零LLM) — 大模型当场结构化,
    记录本身即最优质压缩 (maintainer: 数据飞轮, 不搞小模型反复折腾)。
    @since 2026-08-21 加YAML frontmatter: 所有结构化字段机器可读。
    """
    output = args.output or "(未填)"
    completed = args.completed or "(未填)"
    next_step = args.next_step or "(未填)"
    title = _make_title(args)

    # YAML frontmatter
    fm_lines = [
        "---",
        f"date: {date_str}",
        f"time: {ts}",
        f"track: {track}",
        f"title: \"{title}\"",
    ]
    if args.conclusion:
        fm_lines.append(f"conclusion: \"{args.conclusion[:200]}\"")
    if args.decision:
        fm_lines.append(f"decision: \"{args.decision[:200]}\"")
    if args.lesson:
        fm_lines.append(f"lesson: \"{args.lesson[:200]}\"")
    if args.highlight:
        fm_lines.append(f"highlight: \"{args.highlight[:200]}\"")
    if args.difficulty:
        fm_lines.append(f"difficulty: \"{args.difficulty[:200]}\"")
    if args.source:
        fm_lines.append(f"source: \"{args.source[:200]}\"")
    fm_lines.append("---")

    lines = [l + "\n" for l in fm_lines]
    lines.append(f"\n# 双轨进度 — {track} 轨道\n")
    lines.append(f"> 记录时间: {date_str} {ts}\n\n")
    lines.append(f"## 产出\n\n{output}\n\n")
    lines.append(f"## 完成\n\n{completed}\n\n")

    # 结构化知识段 — 有则写, 标记固定供 auto_capture 被动解析(零LLM)
    if args.conclusion:
        lines.append(f"## 结论\n\n<!--capture:conclusion anchor=model_authored-->\n{args.conclusion}\n\n")
    if args.decision:
        lines.append(f"## 决策\n\n<!--capture:decision anchor=human_confirmed-->\n{args.decision}\n\n")
    if args.difficulty:
        lines.append(f"## 克服的困难\n\n<!--capture:difficulty anchor=hard_gate-->\n{args.difficulty}\n\n")
    if args.lesson:
        lines.append(f"## 教训\n\n<!--capture:lesson anchor=model_authored-->\n{args.lesson}\n\n")
    if args.highlight:
        lines.append(f"## 亮点\n\n<!--capture:highlight anchor=model_authored-->\n{args.highlight}\n\n")
    if args.source:
        lines.append(f"## 引用来源\n\n<!--capture:source-->\n{args.source}\n\n")

    lines.append(f"## 下一步\n\n{next_step}\n")
    content = "".join(lines)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def move(args):
    """将已有进度文件归位到标准位置。"""
    src = args.source
    if not os.path.exists(src):
        print(f"❌ 源文件不存在: {src}")
        sys.exit(1)

    file_type = args.type or "dual"
    track = (args.track or "ALL").upper()
    date_str = args.date or _date_now()

    if file_type == "main":
        target_dir = MAIN_DIR
        fname = f"progress_{date_str}.md"
    else:
        target_dir = DUAL_DIR
        ts = args.time or _ts_now()
        fname = f"{date_str}_{ts}_双轨进度_{track}.md"

    _ensure_dir(target_dir)
    dst = os.path.join(target_dir, fname)

    shutil.copy2(src, dst)  # copy2 preserves metadata
    print(f"✅ 文件已归位")
    print(f"   源: {src}")
    print(f"   目标: {dst}")

    # 删除源文件（除非 --dry-run）
    if args.dry_run:
        print(f"   [dry-run] 保留源文件")
    else:
        os.remove(src)
        print(f"   源文件已删除")
    return True


def scan(args):
    """扫描散落的进度/双轨文件。"""
    found = []
    # 扫描项目根目录（不递归进入 .git、node_modules 等）
    for root, dirs, files in os.walk(_PROJECT_ROOT):
        # 跳过无关目录
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', '.git')]
        # 跳过目标目录本身
        if os.path.abspath(root) in (os.path.abspath(MAIN_DIR), os.path.abspath(DUAL_DIR)):
            continue

        for f in files:
            if _is_progress_file(f):
                fpath = os.path.join(root, f)
                found.append(fpath)

    if not found:
        print("✅ 未发现散落的进度/双轨文件")
        return

    # 过滤掉项目结构目录和 BANNED_DIRS 中已有的
    scattered = []
    for f in found:
        abspath = os.path.abspath(f)
        # 在项目结构目录中且不在 banned 目录的不算散落
        in_project = any(
            abspath.startswith(os.path.abspath(d))
            for d in PROJECT_STRUCTURE_DIRS
        )
        in_banned = any(
            abspath.startswith(os.path.abspath(d))
            for d in BANNED_DIRS
        )
        if in_banned or not in_project:
            scattered.append(f)

    if not scattered:
        print("✅ 未发现非标准位置的进度/双轨文件")
        return

    print(f"\n⚠️  发现 {len(scattered)} 个散落文件:")
    for f in scattered:
        rel = os.path.relpath(f, _PROJECT_ROOT)
        print(f"   {rel}")

    if not args.dry_run:
        print(f"\n提示: 用以下命令移动:")
        for f in scattered:
            rel = os.path.relpath(f, _PROJECT_ROOT)
            print(f"  python progress_file_filer.py move \"{f}\" --type dual")


def status(args):
    """查看当前状态。"""
    print("\n📊 进度文件状态")
    print("=" * 50)
    print(f"  主进度目录: {MAIN_DIR}")
    if os.path.exists(MAIN_DIR):
        files = [f for f in os.listdir(MAIN_DIR) if f.endswith('.md')]
        print(f"  文件数: {len(files)}")
        for f in sorted(files, reverse=True)[:5]:
            size = os.path.getsize(os.path.join(MAIN_DIR, f))
            print(f"    {f} ({size} bytes)")
    else:
        print(f"  目录不存在")

    print(f"\n  双轨目录: {DUAL_DIR}")
    if os.path.exists(DUAL_DIR):
        files = [f for f in os.listdir(DUAL_DIR) if f.endswith('.md')]
        print(f"  文件数: {len(files)}")
        for f in sorted(files, reverse=True)[:5]:
            size = os.path.getsize(os.path.join(DUAL_DIR, f))
            print(f"    {f} ({size} bytes)")
    else:
        print(f"  目录不存在")

    print(f"\n  incident-log: {os.path.join(_PROJECT_ROOT, 'knowledge', '05_CLS认知系统架构', '认知系统迭代', 'incident-log.md')}")
    baobi_path = os.path.join(_PROJECT_ROOT, 'knowledge', '05_CLS认知系统架构', '认知系统迭代', 'incident-log.md')
    if os.path.exists(baobi_path):
        print(f"    存在 ({os.path.getsize(baobi_path)} bytes)")
    else:
        print(f"    不存在")


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 解析器 (@add 2026-09-10)。

    独立成函数是为了让 MCP 包装器(mcp_cls_tools.progress_record)能复用同一份字段定义取默认值。
    原包装器手写 argparse.Namespace、靠人工与 filer 对齐字段, 已两次因漏字段而崩:
      'title'      2026-08-27 → 补 title
      'task_source' 2026-09-10 → 本次
    字段定义收敛到单一来源后, 该类"漏字段"漂移不再可能复发。
    """
    parser = argparse.ArgumentParser(
        description="进度文件归类器 / 进度记录器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # --- record ---
    rec = sub.add_parser("record", help="记录新进度到标准位置")
    rec.add_argument("--output", "-o", default="",
                     help="本阶段产出（必填）：一段话概括，含关键数值和文件路径。"
                          "标准：陌生人看到这段话能知道今天做出了什么东西。")
    rec.add_argument("--title", default="",
                     help="双轨文件名内容简介(缺省从output提取前30字)")
    rec.add_argument("--completed", "-c", default="",
                     help="完成的事项（必填）：编号列表，每项必须包含："
                          "①动机（为什么改/问题怎么死的）"
                          "②改法（怎么治理/逻辑链/代码结构）"
                          "③设计亮点（好处/验证/实测数值）"
                          "标准：陌生人看到这些能接手后续工作。"
                          "示例："
                          "  ①[scripts/wheels/xxx.py L100] 修复空转检测"
                          "    动机：原代码 not msgs 提前 return 导致 stale 告警被短路"
                          "    改法：stale 提到 msgs 判断之前，只依赖 mtime")
    rec.add_argument("--next-step", "-n", default="",
                     help="下一步计划（必填）：含具体文件路径+要验证的假设。"
                          "标准：明天开工第一件事就知道干什么。")
    rec.add_argument("--task-source", default="",
                     help="任务来源（必填）：今天干什么来的？maintainer给的/自主发现的/临时.txt 批注？")
    rec.add_argument("--extra", "-e", default="",
                     help="数据锚定（必填）：列出本轮使用的所有原始数据路径+代码行号。"
                          "标准：事后能回溯到具体文件和具体行。")
    rec.add_argument("--track", "-t", default="ALL", choices=["ALL", "A", "B", "C"],
                     help="轨道（ALL/A/B/C）")
    rec.add_argument("--date", default="", help="日期 YYYYMMDD（默认今天）")
    rec.add_argument("--time", default="", help="时间 HHMM（默认当前）")
    rec.add_argument("--lesson", default="",
                     help="教训(踩坑+根因, 结构化知识段)")
    rec.add_argument("--highlight", default="",
                     help="亮点(做得好的方法/巧解)")
    rec.add_argument("--difficulty", default="",
                     help="克服的困难(硬闸验证过的难题, anchor=hard_gate)")
    rec.add_argument("--conclusion", default="",
                     help="结论(本轮确立的知识性结论)")
    rec.add_argument("--decision", default="",
                     help="决策(maintainer定调+理由, anchor=human_confirmed)")
    rec.add_argument("--source", default="",
                     help="引用来源(论文/URL/文件路径, 逗号分隔)")

    # --- move ---
    mv = sub.add_parser("move", help="将已有文件归位到标准位置")
    mv.add_argument("source", help="源文件路径")
    mv.add_argument("--type", default="dual", choices=["main", "dual"],
                    help="文件类型（主进度/双轨）")
    mv.add_argument("--track", default="ALL", choices=["ALL", "A", "B", "C"],
                    help="轨道")
    mv.add_argument("--date", default="", help="日期 YYYYMMDD（默认今天）")
    mv.add_argument("--time", default="", help="时间 HHMM")
    mv.add_argument("--dry-run", action="store_true", help="仅扫描不动手")

    # --- scan ---
    sc = sub.add_parser("scan", help="扫描散落的进度/双轨文件")
    sc.add_argument("--dry-run", action="store_true", help="仅扫描不动手")

    # --- status ---
    sub.add_parser("status", help="查看状态")
    return parser


def main():
    args = build_parser().parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    elif args.command == "record":
        record(args)
    elif args.command == "move":
        move(args)
    elif args.command == "scan":
        scan(args)
    elif args.command == "status":
        status(args)


if __name__ == "__main__":
    main()
