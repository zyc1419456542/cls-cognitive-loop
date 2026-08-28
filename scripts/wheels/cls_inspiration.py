#!/usr/bin/env python3
"""
cls_inspiration.py — 无人模式灵感注入组合器 v2 (2026-08-16 maintainer立项 + 窗口隔离修订)
==========================================================
灵感脉冲每 10min 由 CC cron 触发 (prompt 以 [cron] 开头, 漂移检测白名单豁免)。
本轮子判定"该不该出灵感 + 出哪条", 输出四字段注入; 无增量 → 静默 (注入三原则)。

内容判定 — 状态文件交叉对照 (不靠猜):
  本窗口信号 (已做窗口隔离, 只读自己):
    cog_step.json      声明在做什么 — 校验 _meta.window_id 前缀匹配本窗口
    trajectory.jsonl   实际在做什么 — 按 session_id 过滤本窗口最近 15 轮
    长链笔记本 .md     想到哪了 — 路径按 sid 建, 天然隔离
  机器级信号 (有意跨窗口, 语义上就该是全局的):
    autonomy_state    人在不在 — 取最新文件的 last_human_input_ts; 人在任何窗口=在场→全局静默
    active_context    机器任务焦点 — 仅作焦点兜底 (本窗口声明优先)
    content_gaze      产出质量趋势 — 文件无 session 字段, 机器级信号, 带 24h 新鲜守卫
    ops_freq.jsonl    最近工具活动 — 条目带 session 字段, 可窗口过滤
  已移除: proxy.log (代理 6/30 已撤, 文件是尸体)。脉冲本身即活性证明: CC 能处理脉冲=活着,
  无需外部活性信号。

判定链: 人在不在(全局) → 冷却 → 真事件强制(凝视/漂移) → 采样窗灵感(长链卡点/知识联想/KG回想)
       → 最多 1 条/脉冲 → 全无增量静默。
输出: 四字段【消息/为什么/级别/内容】, 同时写 inspiration_log.jsonl 供审计。
"""
import json, os, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "data" / "state"
LOG = ROOT / "data" / "state" / "inspiration_log.jsonl"

# ── 参数 ──
HUMAN_ABSENT_SEC = 300          # 无人判定: 距最后人类输入 > 5min (机器全局)
COOLDOWN_SEC = 900              # 灵感冷却 15min
S7_INTERVAL_SEC = 1800          # 二级路由介入间隔 30min (防注入疲劳)
S7_FIRE_PREFIX = "s7_last_fire"    # 实际文件 s7_last_fire_<sid8>.json (窗口隔离, 审计#1)
TRAJ_WINDOW = 15                # 漂移自查窗口: 最近 15 轮工具调用
GAZE_MAX_AGE = 24 * 3600        # 凝视数据 24h 内有效 (sweep 停跑时防陈年数据误报)


def _load_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _sid() -> str:
    """本窗口 session 标识: 前 12 位 (与长链笔记本目录一致)。

    CC 实际暴露 CLAUDE_CODE_SESSION_ID (PreCompact 同源), CLAUDE_SESSION_ID 兜底。
    """
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or "unknown")[:12]


def _sid8() -> str:
    """session 前 8 位 (trajectory/ops_freq/cog_step window_id 前缀)"""
    return _sid()[:8]


def _tail(path: Path, n_bytes: int = 8192) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = max(0, f.tell() - n_bytes)
            f.seek(pos)
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _cog_step_own() -> dict | None:
    """本窗口的步骤声明 — 校验 window_id 前缀, 别的窗口声明不算"""
    cs = _load_json(STATE / "cog_step.json")
    if not cs:
        return None
    win = (cs.get("_meta") or {}).get("window_id") or ""
    if win and not str(win).startswith(_sid8()):
        return None  # 别的窗口的声明
    return cs


def _human_absent() -> bool:
    """人在不在 (session隔离): 读本session的autonomy_state, last_human_input_ts > 5min前→无人。

    2026-08-18 修复: 跨session读取导致cron误判"人在"(其他窗口的recent human input干扰)。
    改为只读本session文件, 无文件→视为无人。
    """
    now = time.time()
    sid = _sid8()
    # 优先读本session文件
    for f in sorted(STATE.glob(f"autonomy_state_{sid}*.json"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        d = _load_json(f)
        if not d:
            continue
        ts = d.get("last_human_input_ts") or 0
        if isinstance(ts, str):
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
        if ts and isinstance(ts, (int, float)):
            return now - float(ts) > HUMAN_ABSENT_SEC
    # 无本session文件 → 也检查全局 active_context.json 的 updated_at
    ac = ROOT / "state" / "active_context.json"
    d = _load_json(ac)
    if d and d.get("updated_at"):
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(d["updated_at"].replace("Z", "+00:00")).timestamp()
            return now - ts > HUMAN_ABSENT_SEC
        except Exception:
            pass
    return True  # 无任何状态文件 → 视为无人


def _cooldown_ok() -> bool:
    """灵感冷却: 距上次灵感 > 15min"""
    try:
        lines = _tail(LOG).strip().splitlines()
        if not lines:
            return True
        last = json.loads(lines[-1])
        return time.time() - float(last.get("ts", 0)) > COOLDOWN_SEC
    except Exception:
        return True


def _ai_active() -> bool:
    """AI是否在自主干活 (session隔离): 轨迹最近5min有调用 OR 活跃上下文30min内 OR 状态机active。

    2026-08-18 修复:
    ① trajectory session_id 是16位, _sid() 是12位 → 改用 startswith 前缀匹配
    ② cron 自身产生的 bash 调用(cls_inspiration) 不算"AI在干活"
    """
    now = time.time()
    sid = _sid()

    # ① trajectory.jsonl: 本窗口最近5分钟有工具调用 (排除 cron 自身)
    try:
        traj = ROOT / "state" / "trajectory.jsonl"
        tail = _tail(traj, 16384)
        for line in reversed(tail.strip().splitlines()):
            try:
                e = json.loads(line)
            except Exception:
                continue
            # 前缀匹配: trajectory 16位, _sid() 12位
            e_sid = e.get("session_id", "")
            if not e_sid.startswith(sid):
                continue
            # 排除 cron 自身: summary 含 cls_inspiration 的不算
            summary = e.get("summary", "")
            if "cls_inspiration" in summary or "灵感脉冲" in summary:
                continue
            ts_str = e.get("ts", "")
            if not ts_str:
                continue
            from datetime import datetime as _dt
            try:
                ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if now - ts < 300:  # 5分钟内
                return True
            break  # 第一条过旧 → 停止扫描
    except Exception:
        pass

    # ② active_context.json: 30分钟内有更新 (非session隔离, 全局)
    try:
        ac = ROOT / "state" / "active_context.json"
        d = _load_json(ac)
        if d:
            ts_str = d.get("updated_at", "")
            if ts_str:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                if now - ts < 1800:  # 30分钟内
                    return True
    except Exception:
        pass

    # ③ autonomy_state: 本窗口 state=active (session隔离)
    try:
        files = sorted(STATE.glob(f"autonomy_state_{sid}*.json"),
                       key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        for f in files:
            d = _load_json(f)
            if d and d.get("state") == "active":
                return True
    except Exception:
        pass

    return False


def _own_trajectory_ops() -> list[str]:
    """本窗口最近 15 轮工具调用名 — 按 session_id 过滤, 防串窗口"""
    traj = ROOT / "state" / "trajectory.jsonl"
    tail = _tail(traj, 32768)
    ops = []
    for line in reversed(tail.strip().splitlines()):
        if len(ops) >= TRAJ_WINDOW:
            break
        # 前缀匹配: trajectory session_id 16位, _sid8() 8位
        if f'"session_id":"{_sid8()}' not in line:
            continue
        # 排除 cron 自身
        if "cls_inspiration" in line or "灵感脉冲" in line:
            continue
        m = re.search(r'"summary":"[^"]*"', line)  # summary 不是工具名, 跳过
        tm = re.search(r'"tool":\s*"(\w+)"', line)
        if tm:
            ops.append(tm.group(1))
    # 尾读可能截断, 若不足窗口再用 ops_freq (同样带 session 字段) 补
    if len(ops) < TRAJ_WINDOW:
        of = _tail(ROOT / "data" / "state" / "ops_freq.jsonl", 8192)
        for line in reversed(of.strip().splitlines()):
            if len(ops) >= TRAJ_WINDOW:
                break
            if f'"session":"{_sid8()}"' not in line:
                continue
            tm = re.search(r'"tool":\s*"(\w+)"', line)
            if tm:
                ops.append(tm.group(1))
    return ops[-TRAJ_WINDOW:]


# ── 灵感源 (漂移+知识并行, 各最多1条, 共最多2条) ──

def _s1_drift_check() -> str:
    """S1 工具单调性检测 — 检测死循环/工具单一/写而不读/修复循环

    2026-08-18 v3重写: 不再对比声明vs实际(永远匹配不了), 改为检测确定性模式。
    """
    ops = _own_trajectory_ops()
    if len(ops) < 5:
        return ""

    from collections import Counter
    counter = Counter(ops)
    most_tool, most_count = counter.most_common(1)[0]
    total = len(ops)
    pct = most_count / total

    # 模式1: 死循环 — 同一工具连续≥8次
    streak = 0
    for op in ops:
        if op == most_tool:
            streak += 1
        else:
            streak = 0
    if streak >= 8:
        return (
            "【消息】灵感·死循环(CLS): 工具调用模式异常, 非指令。"
            f"【为什么】你已连续 {most_tool}×{streak} 次。如果是修复循环, 先Read错误日志/WebSearch根因再继续, 否则会越改越烂(incident-log#17)。"
            "【级别】行动 — 建议暂停当前工具链, 换个思路。"
            f"【内容】连续 {most_tool}×{streak} | 最近15轮: {', '.join(f'{t}×{c}' for t, c in counter.most_common(3))}")

    # 模式2: 工具单一 — 最近15轮同一工具占比≥80%
    if pct >= 0.8 and total >= 8:
        suggestions = {
            "bash": "考虑: 查官方文档(WebSearch)? 换PowerShell? 拆分任务?",
            "write": "考虑: 先Read确认当前状态? 用Edit替代Write更精准?",
            "read": "考虑: 找到目标后该Write/Edit落地了?",
            "edit": "考虑: 修改次数多的话, 是否该重写整个文件?",
            "grep": "考虑: 换Glob找文件? 或直接Read目标文件?",
        }
        hint = suggestions.get(most_tool, "考虑换一种工具或方法。")
        return (
            "【消息】灵感·单调(CLS): 工具使用过于集中, 非指令。"
            f"【为什么】最近 {total} 轮中 {most_tool} 占 {pct:.0%}({most_count}/{total})。长时间单一工具容易陷入局部, 换个角度可能更快。"
            f"【级别】参考 — {hint}"
            f"【内容】{most_tool}×{most_count}/{total} | 其余: {', '.join(f'{t}×{c}' for t, c in counter.most_common(3) if t != most_tool)[:80]}")

    # 模式3: 写而不读 — Write/Edit≥3次但Read=0
    writes = counter.get("write", 0) + counter.get("edit", 0)
    reads = counter.get("read", 0) + counter.get("Read", 0)
    if writes >= 3 and reads == 0:
        return (
            "【消息】灵感·盲写(CLS): 修改文件但未读取, 非指令。"
            f"【为什么】最近 {total} 轮中 Write/Edit×{writes} 但 Read×0。不读就写容易在错误基础上盖楼。"
            "【级别】行动 — 建议先Read目标文件确认当前状态。"
            f"【内容】Write/Edit×{writes} | Read×0")

    # 模式4: 修复循环 — 同一文件被Write/Edit≥4次 (从trajectory summary提取)
    # 这个需要文件名信息, 简化为: Write/Edit总次数≥6 (高频修改暗示修复循环)
    if writes >= 6:
        return (
            "【消息】灵感·修复循环(CLS): 高频修改, 非指令。"
            f"【为什么】最近 {total} 轮中 Write/Edit×{writes}。修改超过3轮通常不收敛(incident-log#17), 建议先WebSearch根因或查官方文档。"
            "【级别】行动 — 停下来, 换个思路。"
            f"【内容】Write/Edit×{writes}/{total}")

    return ""


def _s2_gaze_check() -> str:
    """S2 内容凝视告警: 最近条目 decline_streak >= 2, 且数据 24h 内 (防 sweep 停跑后的陈年误报)"""
    gaze_file = ROOT / "data" / "state" / "content_gaze_log.jsonl"
    tail = _tail(gaze_file).strip().splitlines()
    if not tail:
        return ""
    try:
        last = json.loads(tail[-1])
        ts = last.get("ts") or ""
        from datetime import datetime as _dt
        if isinstance(ts, str):
            age = time.time() - _dt.fromisoformat(ts).timestamp()
        else:
            age = time.time() - float(ts)
        if age > GAZE_MAX_AGE:
            return ""
        streak = last.get("decline_streak") or 0
        if isinstance(streak, (int, float)) and int(streak) >= 2:
            return (
                "【消息】灵感·凝视(CLS): 无人值守期间的质量观察, 非指令。"
                f"【为什么】内容凝视连续 {int(streak)} 次判定产出质量下滑。"
                "【级别】行动 — 值得检查当前做法是否该换。"
                f"【内容】decline_streak={streak} | 最近产出: {(last.get('file') or '')[-60:]}")
    except Exception:
        pass
    return ""


def _s3_notebook_check() -> str:
    """S3 长链卡点: 本窗口笔记本尾部含卡点且 > 20min 未更新 → 提示接续 (路径按 sid 隔离)"""
    today = time.strftime("%Y%m%d")
    nb = ROOT / "data" / "longchain" / today / f"{_sid()}.md"
    if not nb.exists():
        return ""
    try:
        text = nb.read_text(encoding="utf-8")
        age = time.time() - nb.stat().st_mtime
    except Exception:
        return ""
    if age < 1200:
        return ""  # 20min 内更新过 → 不打扰
    if not re.search(r"卡点|阻塞|待定|未解决|排除", text[-800:]):
        return ""
    tail = [l.strip() for l in text.strip().splitlines() if l.strip()][-6:]
    return (
        "【消息】灵感·续接(CLS): 长链笔记本观察, 非指令。"
        f"【为什么】笔记本 {int(age // 60)} 分钟未更新, 尾部仍挂着未解决项。"
        "【级别】参考 — 若卡点已绕过可忽略。"
        "【内容】" + " / ".join(tail[-3:])[:200])


def _focus() -> str:
    """本窗口任务焦点: 本窗口声明 → 机器焦点 → goal.txt (三级)"""
    cs = _cog_step_own()
    if cs and (cs.get("description") or cs.get("label")):
        return (cs.get("description") or cs.get("label") or "")[:80]
    ac = _load_json(ROOT / "state" / "active_context.json")
    if ac and (ac.get("current_focus") or "").strip():
        return ac.get("current_focus")[:80]
    gt = ROOT / "data" / "longchain" / "_state" / "goal.txt"
    if gt.exists():
        try:
            return gt.read_text(encoding="utf-8").strip()[:80]
        except Exception:
            pass
    return ""


def _s4_knowledge_link() -> str:
    """S4 知识联想: 复用 unified_inject (新锚点三级来源版)"""
    try:
        from unified_inject import inject
        r = inject()
        return r if r else ""
    except Exception:
        return ""


def _s5_kg_candidates() -> str:
    """S5 L3 边缘提醒: 知识卡片候选推荐 (P3 卡片化, 2026-08-16 夜: KG实体碎片→知识卡片, 高信息密度)"""
    focus = _focus().lower()
    if not focus:
        return ""
    try:
        import sys as _sys
        _w = str(Path(__file__).resolve().parent)
        if _w not in _sys.path:
            _sys.path.insert(0, _w)
        from knowledge_nav_core import load_cards, prescreen
    except Exception:
        return ""
    cards = load_cards().get("cards", {}) or {}
    if not cards:
        return ""
    # 候选按当前焦点 CJK bigram 重叠挑选; 返回共享词供理由组装
    top = prescreen(focus, cards, top_n=5)
    if not top:
        return ""
    focus_words = focus.replace(" ", "")[:30]
    lines = []
    for tup in top:
        _ov, _rel, c, evidence = tup[0], tup[1], tup[2], tup[3]
        title = (c.get("title") or "").strip()[:24] or "无题"
        date = c.get("date") or ""
        content = (c.get("content") or "").strip()[:40]
        # evidence: 向量模式="cosine:0.701"(字符串); bigram模式=共享CJK词列表
        if isinstance(evidence, list):
            ev_str = "、".join(evidence[:3])
        else:
            ev_str = str(evidence)[:30]
        lines.append(f"{title}({date}): {content} | {ev_str}")
    # 【为什么】逐卡逻辑理由
    why_lines = []
    for tup in top:
        _ov, _rel, c, evidence = tup[0], tup[1], tup[2], tup[3]
        title = (c.get("title") or "").strip()[:20] or "无题"
        if isinstance(evidence, list):
            why_lines.append(f"当前任务「{focus_words[:20]}」与卡片「{title}」共享关键词[{'、'.join(evidence[:3])}]")
        else:
            why_lines.append(f"当前任务「{focus_words[:20]}」与卡片「{title}」语义相关度{evidence}")
    return (
        "【消息】灵感·回想(CLS): 后台从知识卡片挑出 3-5 条可能相关的候选, 非指令。"
        "【为什么】" + "；".join(why_lines) + "。是否真相关由你判断(三层记忆 L3 原则: 小模型只推荐)。"
        "【级别】参考 — 不相关可全部忽略。"
        f"【内容】{' / '.join(lines)}")


def _s6_fact_check() -> str:
    """行为事实分类 (transcript_fact_scan) + idle/stale时现实对质 (external_anchor)。

    2026-08-21 maintainer定调: 语义监控不用数值阈值, 用 transcript 可枚举事实。
    数据源: ~/.claude/projects/<slug>/<sessionId>.jsonl, 文件名即sessionId天然窗口隔离。
    只报事实, 判断权在主窗口大模型。
    """
    try:
        import transcript_fact_scan as tfs
    except Exception:
        return ""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or ""
    if not sid:
        return ""
    try:
        r = tfs.scan(sid[:36])
    except Exception:
        return ""
    alerts = r.get("alerts") or []
    if not alerts:
        return ""
    anchor_line = ""
    if any(a["category"] in ("idle", "stale") for a in alerts):
        # 空转/停滞 → 采一次物理现实对质 (git/进程), 静默失败证据
        try:
            import subprocess as sp
            out = sp.run([sys.executable, str(Path(__file__).parent / "external_anchor.py"), "--sample"],
                         capture_output=True, text=True, encoding="gbk", errors="replace", timeout=25)
            if out.returncode == 0 and out.stdout.strip():
                anchor_line = "现实采样: " + out.stdout.strip().splitlines()[-1][:150]
        except Exception:
            pass
    lines = [
        "【消息】行为事实(CLS): 本窗口最近行为扫描出 %d 条可枚举事实, 非指令。" % len(alerts),
        "【为什么】语义监控用事实分类不用数值阈值 (maintainer定调 2026-08-21); transcript为CC官方session缓存。",
        "【级别】参考 — 事实列出, 判断权在你。",
    ]
    for a in alerts:
        lines.append(f"- [{a['category']}] {a['fact']} → {a['suggestion']}")
    if anchor_line:
        lines.append(anchor_line)
    return chr(10).join(lines)


# 方向人话映射: primary → (主推说法, 方法描述, 事实依据逻辑链)
_S7_DIRECTIONS = {
    "P1": ("顺着干", "从已知条件往目标正推",
           "这个任务目标明确、条件到目标的路径看得见、目前没有卡壳迹象 — 这类特征下正推效率最高"),
    "P2": ("倒着干", "从目标状态反推当前还缺什么",
           "这个任务目标清楚, 但一时不易看出从哪下手 — 这种情况从终点往回找缺口更快"),
    "P3": ("换个问法", "重新框问题再动手",
           "检测到僵局信号 — 在旧框架里继续绕效率低, 先换一种问法描述这个问题"),
}


def _s7_complexity_route() -> str:
    """二级复杂度路由 (maintainer批准 2026-08-21): tier-router 判 L3/L4 → strategy_selector 出方向参考。

    讲人话提示词(顺着干/倒着干/换个问法), 事实+逻辑链无数字。
    防疲劳: 距上次介入>30min; selector纯内存计算零写盘; 失败=静默放弃绝不阻塞脉冲。
    """
    try:
        # 窗口隔离 focus: 只用本窗口信号 (cog_step声明 + 本sid的goal.txt)。
        # 不用 _focus() 的 active_context 全局兜底 — 那是别的窗口的焦点, 串窗 (2026-08-21 实测发现)。
        cs = _cog_step_own()
        focus = ((cs.get("description") or cs.get("label") or "") if cs else "")
        if not focus:
            gt = ROOT / "data" / "longchain" / _sid() / "goal.txt"
            if not gt.exists():
                gt = ROOT / "data" / "longchain" / "_state" / "goal.txt"
            if gt.exists():
                try:
                    focus = gt.read_text(encoding="utf-8").strip()[:80]
                except Exception:
                    pass
        if not focus:
            return ""
        fire_file = STATE / f"{S7_FIRE_PREFIX}_{_sid8()}.json"
        last = _load_json(fire_file) or {}
        try:
            last_ts = float(last.get("ts", 0))
        except (TypeError, ValueError):
            last_ts = 0.0  # 状态文件损坏 → 视为过期, 允许重新触发 (审计#4: 防永久静默)
        if time.time() - last_ts < S7_INTERVAL_SEC:
            return ""
        try:
            import tier_router
            r = tier_router.route(focus)
            tier = str(r.get("tier", "")).upper()
        except Exception:
            return ""
        if tier not in ("L3", "L4"):
            return ""
        try:
            import strategy_selector as ss
            res = ss.classify_task(focus)
            primary = str(res.get("primary", "")).strip()
        except Exception:
            return ""
        if primary not in _S7_DIRECTIONS:
            return ""
        main_word, method, why = _S7_DIRECTIONS[primary]
        alt = [v for k, v in _S7_DIRECTIONS.items() if k != primary]
        alt_names = "、".join("「%s」" % a[0] for a in alt[:2])
        lines = [
            "【消息】方向参考(CLS): 当前任务较重, 系统建议主推「%s」 — %s; 备选 %s。" % (main_word, method, alt_names),
            "【为什么】%s。" % why,
            "【级别】参考 — 方向倾向仅供参考, 判断权在你。",
            "【内容】连续两步无进展 → 切到备选方向; 已经卡死 → 用「换个问法」重新框问题再动手。",
        ]
        try:
            fire_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = fire_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"ts": time.time(), "sid": _sid8(), "tier": tier, "primary": primary},
                ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, fire_file)  # 原子写, 防竞态半截JSON (审计#3)
        except Exception:
            pass
        return chr(10).join(lines)
    except Exception:
        return ""


def run() -> str:
    """脉冲入口 v3: 漂移+知识并行, 各自独立输出, 共最多2条"""
    def _emit(source: str, mode: str, text: str, label: str):
        """记录(本地 _log + P7 统一审计)"""
        _log(source, mode)
        try:
            from knowledge_inject_audit import log as _audit_ki
            _audit_ki(f"cls_inspiration/{source}", text, label, "无人灵感脉冲")
        except Exception:
            pass

    # 人不在 AND AI 也不在干活 → 跳过(省 token)
    if _human_absent() and not _ai_active():
        return ""
    if not _cooldown_ok():
        return ""

    parts = []  # 最多2条: 漂移1条 + 知识1条

    # ── 漂移线 (不受采样窗限制) ──
    s_gaze = _s2_gaze_check()
    if s_gaze:
        _emit("gaze", "forced", s_gaze, "凝视告警")
        parts.append(s_gaze)
    else:
        s_drift = _s1_drift_check()
        if s_drift:
            _emit("drift", "forced", s_drift, "漂移告警")
            parts.append(s_drift)

    # ── 事实分类线 (不受采样窗限制, maintainer定调 2026-08-21) ──
    if len(parts) < 2:
        s_fact = _s6_fact_check()
        if s_fact:
            _emit("fact_scan", "forced", s_fact, "行为事实告警")
            parts.append(s_fact)

    # ── 二级复杂度路由线 (maintainer批准 2026-08-21, 不受采样窗限制) ──
    if len(parts) < 2:
        s_route = _s7_complexity_route()
        if s_route:
            _emit("complexity_route", "forced", s_route, "方向参考")
            parts.append(s_route)

    # ── 知识线 (独立评估, 不被漂移阻塞) ──
    # 采样窗: 每30min窗口的后15min跳过知识源(降频, 不完全关闭)
    in_sample_window = (int(time.time()) % 1800) < 900
    if in_sample_window:
        s_know = _s4_knowledge_link()
        if s_know:
            _emit("knowledge", "sampled", s_know, "知识卡片导航")
            parts.append(s_know)
        else:
            s_cand = _s5_kg_candidates()
            if s_cand:
                _emit("candidates", "sampled", s_cand, "知识卡片候选")
                parts.append(s_cand)

    # ── 笔记本卡点 (独立, 不受采样窗限制) ──
    if len(parts) < 2:
        s_nb = _s3_notebook_check()
        if s_nb:
            _emit("notebook", "sampled", s_nb, "长链笔记本")
            parts.append(s_nb)

    return "\n\n".join(parts) if parts else ""


def _log(source: str, mode: str):
    try:
        entry = {"ts": time.time(), "source": source, "mode": mode, "sid": _sid()}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    out = run()
    if out:
        print(out)
    # 空输出 = 静默脉冲
