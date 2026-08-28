#!/usr/bin/env python3
"""drift_v2.py — 行为漂移检测v2 (双触发器: Drift + Complexity)
============================================================
基于 Synapse 非线性对话结构启发 + maintainer定调的行为监控方案。

核心思路:
  不再用 embedding cosine 对比"你说的话"vs"锚点" (文本相似度不可靠),
  而是追踪行为信号 (文件访问/操作模式/版本一致性) + 任务框架对比。

两个触发器:
  A. Drift Trigger — 方向错了 (文件越界/操作越界/版本越界)
  B. Complexity Trigger — 路没错但负荷太重 (认知拥塞, 建议拆分subagent)

使用方式:
  python drift_v2.py anchor   → 捕获锚点+生成任务框架 (人类离开时调用)
  python drift_v2.py check    → 检测越界 (每次工具调用后调用)
  python drift_v2.py clear    → 清理+写审计 (人类回来时调用)

@since: 2026-08-18
"""
import json, os, re, sys, time
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "data" / "state"

# ── 参数 ──
FRAMEWORK_TTL = 7200          # 任务框架有效期 2h (超时自动清理)
DRIFT_COOLDOWN = 600          # 同类型告警冷却 10min
COMPLEXITY_COOLDOWN = 1800    # Complexity Trigger 冷却 30min
COMPLEXITY_MIN_ROUNDS = 10    # Complexity Trigger 最少轮次
DIVERSE_THRESHOLD = 0.7       # 工具多样性阈值
EXPLORE_FILE_THRESHOLD = 5    # Read 不同文件数阈值
WEBSEARCH_THRESHOLD = 3       # WebSearch 次数阈值


def _sid() -> str:
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or "unknown")[:12]


def _sid8() -> str:
    return _sid()[:8]


def _now() -> float:
    return time.time()


def _load_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _atomic_write(p: Path, data: dict):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ══════════════════════════════════════════════════════════
# Phase 1: 锚点捕获 + 任务框架生成 (人类离开时)
# ══════════════════════════════════════════════════════════

def capture_anchor() -> dict | None:
    """捕获锚点: 人类最后消息 + AI声明 + 任务框架。

    复用现有 autonomy_state 的 last_human_input_preview,
    新增: 小模型提取任务框架。
    """
    sid = _sid8()

    # 读取人类最后消息 (复用 autonomy_state)
    human_msg = ""
    for f in sorted(STATE.glob(f"autonomy_state_{sid}*.json"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        d = _load_json(f)
        if d:
            human_msg = d.get("last_human_input_preview", "")
            break

    if not human_msg:
        return None

    # 读取AI的cog_step声明
    ai_anchor = ""
    cs = _load_json(STATE / "cog_step.json")
    if cs:
        win = (cs.get("_meta") or {}).get("window_id") or ""
        if win and win.startswith(sid):
            ai_anchor = (cs.get("description") or cs.get("label") or "")[:200]

    # 生成任务框架 (调小模型)
    framework = _extract_framework(human_msg, ai_anchor)

    anchor = {
        "human_prompt": human_msg[:500],
        "ai_anchor": ai_anchor,
        "framework": framework,
        "created_at": _now(),
        "session": sid,
    }

    # 持久化
    af = STATE / f"drift_framework_{sid}.json"
    af.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(af, anchor)

    return anchor


def _extract_framework(human_msg: str, ai_anchor: str) -> dict:
    """小模型提取任务框架: 标签+范围+边界+核心逻辑。

    安全: LLM 输出经 schema 校验 + 确定性默认值兜底。
    """
    _w = str(ROOT / "scripts" / "wheels")
    if _w not in sys.path:
        sys.path.insert(0, _w)
    # 先尝试调小模型
    try:
        from api_pipeline import call

        system = (
            "从人类请求中提取任务框架,输出纯JSON:\n"
            '{"task_tags":["关键词"],"scope":"一句话范围",'
            '"boundaries":{"allowed_files":["*"],"forbidden_files":[],'
            '"allowed_actions":["Read","Grep"],"forbidden_actions":["Edit","Write"]},'
            '"core_logic":"成功标准"}'
            "\n只输出JSON,不要解释。forbidden_actions根据人类是否要求只读来判断。"
        )
        user = f"人类: {human_msg}\nAI: {ai_anchor}"
        r = call("deepseek", "deepseek-v4-flash",
                 messages=[{"role": "system", "content": system},
                           {"role": "user", "content": user}],
                 max_tokens=500, auto_route=False, timeout_s=30)
        text = (r.get("text") or "") if r else ""
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            raw = json.loads(m.group())
            # schema 校验: 确保必要字段存在且类型正确
            fw = _validate_framework(raw)
            if fw:
                return fw
    except Exception:
        pass

def _validate_framework(raw: dict) -> dict | None:
    """校验 LLM 输出的 framework 是否符合 schema。"""
    if not isinstance(raw, dict):
        return None
    fw = {"task_tags": [], "scope": "", "boundaries": {}, "core_logic": ""}
    tags = raw.get("task_tags")
    if isinstance(tags, list):
        fw["task_tags"] = [str(t) for t in tags if isinstance(t, str)][:5]
    if not fw["task_tags"]:
        return None
    fw["scope"] = str(raw.get("scope", ""))[:200]
    b = raw.get("boundaries", {})
    if isinstance(b, dict):
        for key in ["allowed_files", "forbidden_files", "allowed_actions", "forbidden_actions"]:
            val = b.get(key)
            if isinstance(val, list):
                fw["boundaries"][key] = [str(v) for v in val if isinstance(v, str)][:10]
    fw["core_logic"] = str(raw.get("core_logic", ""))[:300]
    return fw


# fallback: 纯规则提取
    tags = []
    for kw in ["PIC", "pic", "分析", "代码", "修改", "反编译", "调试", "测试"]:
        if kw in human_msg:
            tags.append(kw)

    is_readonly = any(w in human_msg for w in ["只读", "不改", "不要改", "分析", "查看", "检查"])
    forbidden = ["Edit", "Write"] if is_readonly else []

    return {
        "task_tags": tags or ["未分类"],
        "scope": human_msg[:100],
        "boundaries": {
            "allowed_files": ["*"],
            "forbidden_files": [],
            "allowed_actions": ["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
            "forbidden_actions": forbidden,
        },
        "core_logic": f"完成: {human_msg[:150]}",
    }


# ══════════════════════════════════════════════════════════
# Phase 2: 持续监控 (每次工具调用后)
# ══════════════════════════════════════════════════════════

def check_drift(tool_name: str, tool_input: str) -> str | None:
    """检测行为漂移。返回告警文本或None。"""
    sid = _sid8()
    framework_file = STATE / f"drift_framework_{sid}.json"
    fw = _load_json(framework_file)
    if not fw:
        return None

    if _now() - fw.get("created_at", 0) > FRAMEWORK_TTL:
        return None

    boundaries = fw.get("framework", {}).get("boundaries", {})

    # ── A. Drift Trigger: 规则检测 ──
    forbidden_actions = boundaries.get("forbidden_actions", [])
    if tool_name in forbidden_actions:
        return _format_drift_alert(
            "操作越界", tool_name, fw,
            f"正在执行禁止的操作 '{tool_name}'",
            f"回到原始任务: {fw['framework'].get('scope', '')[:60]}"
        )

    file_path = _extract_file_path(tool_name, tool_input)
    if file_path:
        forbidden_files = boundaries.get("forbidden_files", [])
        if _is_file_forbidden(file_path, forbidden_files):
            allowed = boundaries.get("allowed_files", ["*"])
            return _format_drift_alert(
                "文件越界", tool_name, fw,
                f"正在访问禁止的文件 '{file_path}'",
                f"只应访问: {', '.join(allowed[:3])}"
            )

    # ── B. ReadStorm Trigger: 反复Read不实践 ──
    read_alert = _check_read_storm(fw)
    if read_alert:
        return read_alert

    # ── C. Complexity Trigger ──
    return _check_complexity(fw)


READ_STORM連續阈值 = 5          # 连续Read ≥5次触发
READ_STORM_REPEATED = 2         # 同一文件Read ≥2次触发
READ_STORM_COOLDOWN = 300       # 冷却5min (落地类操作出现即清除, 不必等时间)

# 大眼 loop_detector 借鉴 (2026-08-20): 落地/探索二分兜底 — 不看内容只看工具名, O(1)
LANDING_TOOLS = {"Write", "Edit", "Bash", "PowerShell", "Task", "WebFetch"}
EXPLORE_TOOLS = {"Read", "Grep", "Glob", "WebSearch", "LS", "ToolSearch"}
IDLE_WINDOW = 8                 # 观察窗口: 最近8轮
IDLE_THRESHOLD = 7              # 探索类 ≥7 且落地类 =0 → "光看不干"


def _check_read_storm(fw: dict) -> str | None:
    """ReadStorm Trigger: 检测反复Read不实践, 建议先规划再动手。

    信号: 连续Read≥5 / 同一文件Read≥2 / Read后无Write/Edit/Bash / 光看不干(兜底)
    """
    sid = _sid8()
    traj = ROOT / "state" / "trajectory.jsonl"
    if not traj.exists():
        return None

    # 只读任务豁免: 任务框架判为只读(forbidden_actions含Edit/Write)时, 零落地是正常的
    boundaries = fw.get("framework", {}).get("boundaries", {})
    if "Edit" in boundaries.get("forbidden_actions", []) or \
       "Write" in boundaries.get("forbidden_actions", []):
        return None

    # 冷却检查
    cool_file = STATE / f"readstorm_cool_{sid}.json"
    cool = _load_json(cool_file)
    if cool and _now() - cool.get("ts", 0) < READ_STORM_COOLDOWN:
        return None

    tail = _tail(traj, 32768)
    recent_ops = []
    files_read = []

    for line in reversed(tail.strip().splitlines()):
        if len(recent_ops) >= 15:
            break
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not e.get("session_id", "").startswith(sid):
            continue
        summary = e.get("summary", "")
        if "cls_inspiration" in summary or "灵感脉冲" in summary:
            continue
        tool = e.get("tool", "")
        recent_ops.append(tool)
        if tool == "Read":
            # 从summary提取文件名
            fp = re.search(r'[A-Za-z]:\\[^\s"]+|/[^/\s"]+', summary)
            if fp:
                files_read.append(fp.group().split("/")[-1][:30])

    # 落地即清冷却: 最近3轮有落地类操作=有实质进展, 不打扰 (大眼设计)
    if any(op in LANDING_TOOLS for op in recent_ops[:3]):
        cool_file.unlink(missing_ok=True)

    if len(recent_ops) < 5:
        return None

    # 信号1: 连续Read ≥5次
    consecutive_read = 0
    for op in reversed(recent_ops):
        if op == "Read":
            consecutive_read += 1
        else:
            break

    # 信号2: 同一文件Read ≥2次
    from collections import Counter
    repeated = [f for f, c in Counter(files_read).items() if c >= READ_STORM_REPEATED]

    # 信号3: Read后无实践(Read占多数但无Write/Edit/Bash)
    read_count = recent_ops.count("Read")
    practice_count = sum(1 for op in recent_ops if op in ("Write", "Edit", "Bash", "PowerShell"))
    read_dominant = read_count >= 4 and practice_count == 0

    # 信号4 兜底: 光看不干 — 窗口内探索类≥阈值且落地类=0 (大眼 loop_detector 二分法)
    window = recent_ops[:IDLE_WINDOW]
    explore_n = sum(1 for op in window if op in EXPLORE_TOOLS)
    landing_n = sum(1 for op in window if op in LANDING_TOOLS)
    idle = len(window) >= IDLE_WINDOW and explore_n >= IDLE_THRESHOLD and landing_n == 0

    if consecutive_read < READ_STORM連續阈值 and not repeated and not read_dominant and not idle:
        return None

    # 写冷却
    _atomic_write(cool_file, {"ts": _now()})

    # 构造告警
    tags = fw.get("framework", {}).get("task_tags", [])
    reasons = []
    if consecutive_read >= READ_STORM連續阈值:
        reasons.append(f"连续Read×{consecutive_read}")
    if repeated:
        reasons.append(f"重复读{'、'.join(repeated[:3])}")
    if read_dominant:
        reasons.append(f"最近{len(recent_ops)}轮中Read×{read_count}无任何写入/执行")
    if idle:
        reasons.append(f"最近{len(window)}轮探索类×{explore_n}落地类×0(光看不干)")

    return (
        "【消息】Read风暴(CLS): 你正在自主循环, 非指令。\n"
        f"【为什么】任务「{'、'.join(tags[:3])}」检测到: {'; '.join(reasons)}。"
        "信息已经足够多了, 建议先简单实践一下(跑个脚本/写个最小测试), "
        "有了反馈再针对性Read, 不要复读。\n"
        "【级别】行动 — 先动手, 再读。\n"
        "【内容】建议: ①用Bash跑一下现有代码看输出 ②写最小测试验证假设 "
        "③根据结果再决定读什么, 不要一次读完所有文件"
    )


def _check_complexity(fw: dict) -> str | None:
    """Complexity Trigger: 检测认知拥塞, 建议拆分subagent。"""
    sid = _sid8()
    traj = ROOT / "state" / "trajectory.jsonl"
    if not traj.exists():
        return None

    tail = _tail(traj, 32768)
    ops = []
    files_read = set()
    websearch_count = 0

    for line in reversed(tail.strip().splitlines()):
        if len(ops) >= 20:
            break
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not e.get("session_id", "").startswith(sid):
            continue
        summary = e.get("summary", "")
        if "cls_inspiration" in summary or "灵感脉冲" in summary:
            continue
        tool = e.get("tool", "")
        ops.append(tool)
        if tool == "Read":
            fp = re.search(r'[A-Za-z]:\\[^\s"]+|/[^/\s"]+', summary)
            if fp:
                files_read.add(fp.group().split("/")[-1][:30])
        if tool in ("WebSearch", "WebFetch"):
            websearch_count += 1

    if len(ops) < COMPLEXITY_MIN_ROUNDS:
        return None

    counter = Counter(ops)
    diversity = len(counter) / len(ops) if ops else 0

    triggers = []
    if diversity > DIVERSE_THRESHOLD:
        triggers.append(f"工具多样性{diversity:.2f}(>{DIVERSE_THRESHOLD})")
    if len(files_read) > EXPLORE_FILE_THRESHOLD:
        triggers.append(f"已Read {len(files_read)}个不同文件")
    if websearch_count > WEBSEARCH_THRESHOLD:
        triggers.append(f"已WebSearch {websearch_count}次")

    if not triggers:
        return None

    cool_file = STATE / f"complexity_cool_{sid}.json"
    cool = _load_json(cool_file)
    if cool and _now() - cool.get("ts", 0) < COMPLEXITY_COOLDOWN:
        return None
    _atomic_write(cool_file, {"ts": _now()})

    tags = fw.get("framework", {}).get("task_tags", [])
    top_tools = ", ".join(f"{t}×{c}" for t, c in counter.most_common(3))

    return (
        "【消息】认知拥塞(CLS): 你正在自主循环, 非指令。\n"
        f"【为什么】任务「{'、'.join(tags[:3])}」已经探索了{len(triggers)}个独立方向: {'; '.join(triggers)}。"
        f"当前工具分布: {top_tools}。单线程承载过多, 建议拆分为subagent并行处理。\n"
        "【级别】参考 — 可以继续单线程, 但效率可能下降。\n"
        "【内容】建议: ①创建subagent处理子任务 ②主窗口负责汇总和决策 ③或明确下一步聚焦方向"
    )


# ══════════════════════════════════════════════════════════
# Phase 3: 清理 (人类回来时)
# ══════════════════════════════════════════════════════════

def clear():
    """清理框架文件, 写审计日志。"""
    sid = _sid8()
    fw_file = STATE / f"drift_framework_{sid}.json"
    fw = _load_json(fw_file)

    if fw:
        audit = STATE / "drift_v2_audit.jsonl"
        entry = {
            "ts": _now(),
            "session": sid,
            "human_prompt": fw.get("human_prompt", "")[:100],
            "duration_min": round((_now() - fw.get("created_at", _now())) / 60, 1),
        }
        try:
            with open(audit, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    for f in STATE.glob(f"drift_framework_{sid}*.json"):
        f.unlink(missing_ok=True)
    for f in STATE.glob(f"complexity_cool_{sid}*.json"):
        f.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════

def _extract_file_path(tool_name: str, tool_input: str) -> str | None:
    if tool_name in ("Read", "Write", "Edit"):
        m = re.search(r'file_path[":\s]+([^",}\n]+)', tool_input)
        if m:
            return m.group(1).strip().strip('"')
    if tool_name == "Bash":
        m = re.search(r'[A-Z]:\\[^\s"]+|/[^\s"]+', tool_input)
        if m:
            return m.group(0)
    return None


def _is_file_forbidden(file_path: str, forbidden: list[str]) -> bool:
    fp = file_path.replace("\\", "/").lower()
    for pattern in forbidden:
        pattern = pattern.replace("\\", "/").lower()
        if pattern.startswith("*"):
            if fp.endswith(pattern[1:]):
                return True
        elif pattern in fp or fp in pattern:
            return True
    return False


def _format_drift_alert(violation_type: str, tool_name: str, fw: dict,
                        reason: str, suggestion: str) -> str:
    human = fw.get("human_prompt", "")[:80]
    ai = fw.get("ai_anchor", "")[:60]
    return (
        f"【消息】越界检测(CLS): 你正在自主循环, 非指令。\n"
        f"【为什么】人类最后的任务: 「{human}」\n"
        f"你当时的声明: 「{ai}」\n"
        f"鉴定: {violation_type} — {reason}\n"
        f"【级别】行动 — 请回归原始任务。\n"
        f"【内容】{suggestion}"
    )


def _tail(path: Path, n_bytes: int = 8192) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = max(0, f.tell() - n_bytes)
            f.seek(pos)
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("用法: drift_v2.py <anchor|check|clear>")
        return

    cmd = sys.argv[1]
    if cmd == "anchor":
        result = capture_anchor()
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("无锚点 (人类未输入或无autonomy_state)")
    elif cmd == "check":
        tool_name = sys.argv[2] if len(sys.argv) > 2 else ""
        tool_input = sys.argv[3] if len(sys.argv) > 3 else ""
        alert = check_drift(tool_name, tool_input)
        if alert:
            print(alert)
    elif cmd == "clear":
        clear()
        print("已清理")
    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
