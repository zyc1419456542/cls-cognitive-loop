#!/usr/bin/env python3
"""
transcript_fact_scan — 主窗口行为事实分类扫描器
================================================
@since: 2026-08-21 | 来源: maintainer定调"语义监控不用数值要用事实分类"

数据源: CC session 缓存 ~/.claude/projects/<项目slug>/<sessionId>.jsonl
        每行一条JSON, assistant行含 sessionId/timestamp/isSidechain/message.content[]
        (tool_use 块: 工具名+参数) — 文件名即 sessionId, 天然窗口隔离。

事实分类 (全部可枚举, 无拍脑袋数值阈值):
  idle          空转     — 最近 N 条 assistant 消息零 tool_use (光说不干)
  fix_loop      修复循环 — 窗口内同一文件 Edit/Write >= 3 次
  tool_monotony 工具单调 — 最近 15 条中同一工具占比 > 80% 且 >= 10 条
  stale         停滞     — transcript mtime 距今 > 10min (窗口已死/挂起)

过滤: isSidechain=True 的行跳过 (子代理消息不算主窗口行为)。

用法 (AI读):
  python scripts/wheels/transcript_fact_scan.py                # 扫当前session (CLAUDE_CODE_SESSION_ID)
  python scripts/wheels/transcript_fact_scan.py --sid <id>     # 扫指定session
  python scripts/wheels/transcript_fact_scan.py --list         # 列出本项目全部transcript(按mtime)
  python scripts/wheels/transcript_fact_scan.py --text         # 人话输出 (默认JSON)

被动接线: cls_inspiration._s6_fact_check() 无人脉冲时自动调用, 有告警才上屏。
"""
import sys, os, json, time, argparse
from pathlib import Path
from collections import Counter

BASE = Path(__file__).resolve().parent.parent.parent
STALE_SEC = 600        # 停滞阈值 10min
MONOTONY_WINDOW = 15   # 工具单调检查窗口
MONOTONY_RATIO = 0.8   # 单调占比阈值 (事实: 数得出来的比例, 非健康区间)
IDLE_WINDOW = 8        # 空转检查窗口 (最近N条assistant)
FIX_LOOP_THRESHOLD = 3 # 同一文件修改次数阈值 (对齐 ops_monitor 修复循环判据)
TAIL_BYTES = 512 * 1024  # 只读尾部512KB, 大transcript不全量加载


def project_slug(cwd: str) -> str:
    """cwd → CC 项目slug: 每个非字母数字字符替换为一个'-' (实测 <REPO_ROOT> → E-------claude-api-claude)"""
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in cwd)


def transcripts_dir() -> Path:
    return Path.home() / ".claude" / "projects" / project_slug(os.getcwd())


def current_sid() -> str:
    return (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID") or "")


def list_transcripts() -> list:
    d = transcripts_dir()
    if not d.exists():
        return []
    out = []
    for f in d.glob("*.jsonl"):
        st = f.stat()
        out.append({"sid": f.stem, "mtime_age_min": round((time.time() - st.st_mtime) / 60, 1),
                    "size_kb": round(st.st_size / 1024, 1)})
    return sorted(out, key=lambda x: x["mtime_age_min"])


def load_assistant_tail(sid: str) -> list:
    """读 transcript 尾部, 返回主窗口 assistant 消息列表 (时间升序)。
    每条: {ts, tools: [{name, target}]} — target 取 file_path/command/pattern 等首个定位参数。"""
    path = transcripts_dir() / f"{sid}.jsonl"
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = max(0, f.tell() - TAIL_BYTES)
            f.seek(pos)
            raw = f.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    msgs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or '"type":"assistant"' not in line and '"type": "assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue  # 尾部截断的半行
        if d.get("isSidechain"):
            continue  # 子代理消息不算主窗口行为
        m = d.get("message") or {}
        content = m.get("content")
        tools = []
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    inp = blk.get("input") or {}
                    target = (inp.get("file_path") or inp.get("notebook_path")
                              or inp.get("command") or inp.get("pattern")
                              or inp.get("url") or "")
                    tools.append({"name": blk.get("name", "?"),
                                  "target": str(target)[:120]})
        msgs.append({"ts": d.get("timestamp", ""), "tools": tools})
    return msgs


def valid_sid(sid: str) -> bool:
    """sid 白名单: 只允许 UUID 特征字符, 防 --sid 路径逃逸 (审计#6)"""
    return bool(sid) and all(c.isalnum() or c == "-" for c in sid)


def scan(sid: str) -> dict:
    if not valid_sid(sid):
        return {"sid": sid, "alerts": [], "note": "sid格式非法(仅允许字母数字和横杠)"}
    path = transcripts_dir() / f"{sid}.jsonl"
    if not path.exists():
        return {"sid": sid, "alerts": [], "note": "transcript不存在(可能sid错误或非CC窗口)"}
    alerts = []
    # ── 0. stale 停滞 (只依赖mtime, 任何情况下都检查 — 审计#2: 原被not msgs短路) ──
    age = time.time() - path.stat().st_mtime
    if age > STALE_SEC:
        alerts.append({"category": "stale",
                       "fact": f"transcript {age/60:.0f}min 无写入",
                       "suggestion": "窗口停滞/已死: 确认进程状态, 勿再向其投任务"})
    msgs = load_assistant_tail(sid)
    if not msgs:
        return {"sid": sid, "alerts": alerts,
                "note": "尾部无assistant消息" + ("(已报stale)" if alerts else "")}

    # ── 1. idle 空转 ──
    recent = msgs[-IDLE_WINDOW:]
    if len(recent) >= 3 and sum(len(m["tools"]) for m in recent) == 0:
        alerts.append({"category": "idle", "fact": f"最近{len(recent)}条assistant消息零工具调用",
                       "suggestion": "光说不干嫌疑: 检查是否在空转自洽循环"})

    # ── 2. fix_loop 修复循环 (限定最近30条消息, 防几小时前旧账误报 — 审计#5) ──
    edit_targets = Counter()
    for m in msgs[-30:]:
        for t in m["tools"]:
            if t["name"] in ("Edit", "Write", "NotebookEdit") and t["target"]:
                edit_targets[t["target"]] += 1
    for target, n in edit_targets.most_common(3):
        if n >= FIX_LOOP_THRESHOLD:
            alerts.append({"category": "fix_loop",
                           "fact": f"同一文件修改{n}次: {target}",
                           "suggestion": "修复循环: >3轮不会收敛, 停下换策略或回滚"})

    # ── 3. tool_monotony 工具单调 ──
    names = [t["name"] for m in msgs for t in m["tools"]]
    window = names[-MONOTONY_WINDOW:]
    if len(window) >= 10:
        name, cnt = Counter(window).most_common(1)[0]
        ratio = cnt / len(window)
        if ratio > MONOTONY_RATIO:
            alerts.append({"category": "tool_monotony",
                           "fact": f"最近{len(window)}次调用中 {name} 占{cnt}次({ratio:.0%})",
                           "suggestion": "工具过度集中, 换角度: 官方文档/换工具/拆任务"})

    return {"sid": sid, "scanned_msgs": len(msgs), "alerts": alerts}


def fmt_text(r: dict) -> str:
    if not r.get("alerts"):
        return f"[fact_scan] {r['sid']}: 无告警" + (f" ({r['note']})" if r.get("note") else "")
    lines = [f"[fact_scan] {r['sid']}: {len(r['alerts'])}条事实告警"]
    for a in r["alerts"]:
        lines.append(f"  [{a['category']}] {a['fact']} → {a['suggestion']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", default="", help="目标sessionId (缺省=当前环境session)")
    ap.add_argument("--list", action="store_true", help="列出本项目全部transcript")
    ap.add_argument("--text", action="store_true", help="人话输出 (默认JSON)")
    a = ap.parse_args()

    if a.list:
        rows = list_transcripts()
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    sid = a.sid or current_sid()
    if not sid:
        print(json.dumps({"error": "无sessionId: 传--sid或在CC窗口内运行"}, ensure_ascii=False))
        return
    r = scan(sid[:36])
    print(fmt_text(r) if a.text else json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
