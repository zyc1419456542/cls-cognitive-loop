#!/usr/bin/env python3
"""
incident-log记录器 — 永久固化到唯一 canonical 位置
==============================================
路径锁定: knowledge/05_CLS认知系统架构/认知系统迭代/incident-log.md
永不漂移: 路径从脚本自身位置推导，跨机自动适配

用法:
  python baobi_recorder.py record -s "症状描述" -c "根因" -f "修复方法" -l "教训"
  python baobi_recorder.py record -s "..." --domain cad --severity major -r "[[关联记忆]]"
  python baobi_recorder.py recent [--n 5]
  python baobi_recorder.py list

缩写:
  syntax:  s=症状  c=根因  f=修复  l=教训  d=领域  r=关联
  severity:  fatal / critical / major / warning / info
"""

import os, sys, re, datetime, argparse

# === 路径锁定：从脚本位置推导，永不硬编码 ===
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
CANONICAL = os.path.join(
    _PROJECT_ROOT,
    "knowledge", "05_CLS认知系统架构", "认知系统迭代", "incident-log.md"
)

# === 中文字典（用于解析已有条目的编号） ===
_CN_NUM = {
    "〇": 0, "零": 0,
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_TEN = {
    "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14,
    "十五": 15, "十六": 16, "十七": 17, "十八": 18, "十九": 19,
    "二十": 20, "二十一": 21, "二十二": 22, "二十三": 23, "二十四": 24,
    "二十五": 25,
}
_SEVERITY_ICON = {
    "fatal": "🔴", "critical": "🔴",
    "major": "🟡", "warning": "🟠", "info": "🔵",
}


def _cn_to_int(cn: str) -> int:
    """中文数字 → 整数 (〇, 一/十一/十二..., 二..., 三..., 二十...)"""
    cn = cn.strip()
    if cn in _CN_TEN:
        return _CN_TEN[cn]
    if cn in _CN_NUM:
        return _CN_NUM[cn]
    # fallback: try to parse as arabic
    try:
        return int(cn)
    except ValueError:
        return 0


def _int_to_cn(n: int) -> str:
    """整数 → 中文序数（用于 第N条）"""
    _M = {0: "〇", 1: "一", 2: "二", 3: "三", 4: "四", 5: "五",
          6: "六", 7: "七", 8: "八", 9: "九", 10: "十",
          11: "十一", 12: "十二", 13: "十三", 14: "十四", 15: "十五",
          16: "十六", 17: "十七", 18: "十八", 19: "十九", 20: "二十",
          21: "二十一", 22: "二十二", 23: "二十三", 24: "二十四", 25: "二十五"}
    if n in _M:
        return _M[n]
    return str(n)  # 超过 25 用阿拉伯数字


def _get_next_number(content: str) -> int:
    """从已有内容推断下一条编号。"""
    nums = []
    # 匹配 第N条 / 第〇条 / 第十一条
    for m in re.finditer(r'第([〇一二三四五六七八九十\d]+)条', content):
        nums.append(_cn_to_int(m.group(1)))
    return max(nums) + 1 if nums else 1


def record(args):
    """追加新incident-log条目到 canonical 文件末尾。"""
    if not args.symptom:
        print("❌ 错误: --symptom / -s 是必填项")
        sys.exit(1)

    if not os.path.exists(CANONICAL):
        print(f"❌ incident-log文件不存在: {CANONICAL}")
        print("   请先创建文件，或检查项目结构")
        sys.exit(1)

    with open(CANONICAL, "r", encoding="utf-8") as f:
        content = f.read()

    entry_num = _get_next_number(content)
    today = datetime.date.today().strftime("%Y-%m-%d")
    domain = args.domain or "general"
    severity = args.severity or "info"
    icon = _SEVERITY_ICON.get(severity, "🟡")

    # --- 构建条目 markdown ---
    lines = [f"\n---\n"]
    lines.append(f"## 第{_int_to_cn(entry_num)}条：{args.symptom}\n")
    lines.append(f"**日期**：{today}\n")
    if domain != "general":
        lines.append(f"**领域**：{domain}\n")
    lines.append(f"**严重度**：{icon} {severity}\n\n")
    lines.append(f"**现象**：{args.symptom}\n\n")
    if args.cause:
        # 自动换行为列表格式（支持已有多行）
        text = args.cause.strip()
        lines.append(f"**根因**：{text}\n\n")
    if args.fix:
        text = args.fix.strip()
        # 如果已经包含换行符，直接保留（可能是直接写好的多行修复）
        lines.append(f"**修复**：\n{text}\n\n")
    if args.lesson:
        lines.append(f"**教训**：{args.lesson.strip()}\n\n")
    if args.related:
        lines.append(f"**关联**：{args.related.strip()}\n")

    new_entry = "".join(lines)
    content = content.rstrip() + "\n" + new_entry

    with open(CANONICAL, "w", encoding="utf-8") as f:
        f.write(content)

    # ── stance 档位 (2026-08-22 改动a·maintainer批准): incident-log写入成功 → retreat 诊断保护档 ──
    # TTL 30min 自动回 farming; 写入走 cog-context 唯一写入口; fail-open 不影响暴毙记录本体
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from mcp_cls_tools import set_stance
        set_stance("retreat", ttl_seconds=1800, set_by="baobi_recorder",
                   reason="incident-log已写入, 进入诊断保护档(仅诊断类写入)")
    except Exception:
        pass

    short = args.symptom[:50] + ("..." if len(args.symptom) > 50 else "")
    print(f"✅ 第{_int_to_cn(entry_num)}条已记录")
    print(f"   路径: {CANONICAL}")
    print(f"   症状: {short}")
    print(f"   严重度: {icon} {severity}")
    if args.related:
        print(f"   关联: {args.related}")
    return True


def _parse_entries(content):
    """从内容中解析所有格式化条目。"""
    entries = re.split(r'\n---\n', content)
    numbered = []
    for e in entries:
        if re.search(r'第[〇一二三四五六七八九十\d]+条', e):
            # 去掉开头的空白行
            clean = e.lstrip('\n\r')
            numbered.append(clean)
    return numbered


def recent(args):
    """显示最近 N 条incident-log。"""
    if not os.path.exists(CANONICAL):
        print("❌ incident-log文件不存在")
        return
    with open(CANONICAL, "r", encoding="utf-8") as f:
        content = f.read()
    numbered = _parse_entries(content)
    if not numbered:
        print("⚠️  未找到格式化的条目")
        return
    n = args.n or 5
    print(f"\n📋 最近 {n} 条incident-log:")
    for i, entry in enumerate(numbered[-n:], 1):
        title = entry.split('\n')[0].strip().lstrip('# ')
        date_m = re.search(r'\*\*日期\*\*：(\S+)', entry)
        sev_m = re.search(r'\*\*严重度\*\*：(\S+)', entry)
        date_s = date_m.group(1) if date_m else "?"
        sev_s = sev_m.group(1) if sev_m else "?"
        print(f"  {i}. {title} [{date_s}] {sev_s}")


def list_all(args):
    """列出所有incident-log条目。"""
    if not os.path.exists(CANONICAL):
        print("❌ incident-log文件不存在")
        return
    with open(CANONICAL, "r", encoding="utf-8") as f:
        content = f.read()
    numbered = _parse_entries(content)
    if not numbered:
        print("⚠️  未找到格式化的条目")
        return
    print(f"\n📋 共 {len(numbered)} 条incident-log:")
    for entry in numbered:
        title = entry.split('\n')[0].strip().lstrip('# ')
        date_m = re.search(r'\*\*日期\*\*：(\S+)', entry)
        sev_m = re.search(r'\*\*严重度\*\*：(\S+)', entry)
        date_s = date_m.group(1) if date_m else "?"
        sev_s = sev_m.group(1) if sev_m else "?"
        print(f"  - {title} [{date_s}] {sev_s}")


def main():
    parser = argparse.ArgumentParser(
        description="incident-log记录器 — 单点记录固化脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # --- record ---
    rec = sub.add_parser("record", help="记录一条新暴毙（追加到末尾）")
    rec.add_argument("-s", "--symptom", required=True,
                     help="【必填】症状/现象描述")
    rec.add_argument("-c", "--cause", default="",
                     help="根因链，支持多行（用 \\n 或直接写）")
    rec.add_argument("-f", "--fix", default="",
                     help="修复方法，支持多行")
    rec.add_argument("-l", "--lesson", default="",
                     help="经验教训（一句话抽象）")
    rec.add_argument("-d", "--domain", default="general",
                     choices=["general", "cad", "pic", "quant", "safety",
                              "system", "pipeline", "hallucination",
                              "process", "mcp", "script", "other"],
                     help="所属领域")
    rec.add_argument("--severity", default="info",
                     choices=["fatal", "critical", "major", "warning", "info"],
                     help="严重度")
    rec.add_argument("-r", "--related", default="",
                     help="关联条目（如 [[memory-name]] 或 第X条）")

    # --- recent ---
    rec2 = sub.add_parser("recent", help="查看最近N条")
    rec2.add_argument("-n", "--n", type=int, default=5,
                      help="显示条数（默认 5）")

    # --- list ---
    sub.add_parser("list", help="列出所有条目")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)
    elif args.command == "record":
        record(args)
    elif args.command == "recent":
        recent(args)
    elif args.command == "list":
        list_all(args)


if __name__ == "__main__":
    main()
