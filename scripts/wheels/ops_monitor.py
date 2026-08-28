#!/usr/bin/env python3
"""ops_monitor.py — 操作频率监控 (符号动力学行为层)
====================================================
被 PreToolUse.ps1 hook 调用，每次工具调用前记录。
不调用任何模型，纯统计 + 模式检测。

数据: data/state/ops_freq.jsonl (每行一条工具调用)
状态: data/state/ops_health.json   (滚动窗口统计)

@since: 2026-07-26
"""

import json, sys, os, time, re
from pathlib import Path
from collections import Counter, deque

ROOT = Path(__file__).resolve().parent.parent.parent
OPS_LOG = ROOT / "data" / "state" / "ops_freq.jsonl"
OPS_HEALTH = ROOT / "data" / "state" / "ops_health.json"
OPS_REPUTATION = ROOT / "data" / "state" / "ops_reputation.json"
WINDOW_SIZE = 20
BURST_THRESHOLD = 5
GAP_BASELINE = 8
GAP_MIN = 5
GAP_MAX = 12

# ── stance 档位只读 (2026-08-22 改动a·maintainer批准) ──
# stdlib-only 轻量读取, 失败/缺失一律当 farming; 写入唯一路径 = cog-context(mcp_cls_tools.py)
try:
    from stance_read import read_stance as _read_stance
except Exception:
    _read_stance = None  # fail-open

# ── 三层管道 (硬闸门标记 → 小模型语义判定 → 必要注入) ──
# @since 2026-08-01: 规则只标记不判定; 语义判定委托 SF 免费模型
SF_MODEL = "Qwen/Qwen2.5-7B-Instruct"   # 硅基流动免费, 实测 100% (prompt v3)
LOOP_COOLDOWN = 30                        # Layer1 冷却秒数, 防风暴
LOOP_MIN_FINGERPRINT = 3                  # 同命令指纹 ≥3 触发标记
LOOP_MIN_WRITE = 3                        # 同 Write 文件 ≥3 触发标记
LOOP_BASH_WRITE_MIN = 6                   # Bash/Write 占比下限
# @since 2026-08-01 maintainer定调: 反馈互动体系 (评分→降敏)
FEEDBACK_FILE = ROOT / "data" / "state" / "ops_feedback.jsonl"
FEEDBACK_TTL = 1800                       # 反馈有效期 30min, 过期自动恢复默认
FEEDBACK_DESENS_THRESHOLD = 3             # score<3 = "太吵", 触发降敏
FEEDBACK_DESENS_MIN = 8                   # 降敏后: 需同指纹≥8 才触发(真循环仍拦, 误报大幅降)
# 语义判定 system prompt (v3 定稿: 显式批量规则防过度敏感)
LOOP_SYSTEM_PROMPT = (
    "你是CLS循环检测器。判断AI agent工具序列是正常推进还是反复循环。\n"
    "NORMAL(正常): 1)每步有进展/新内容 2)失败后换思路 3)有Read/WebSearch摄入 "
    "4)同工具但参数/文件不同(批量处理) 5)有任务管理(TaskCreate)\n"
    "LOOP(循环): 1)同一命令原样重复≥4次无变化 2)同文件无依据反复改且无摄入 "
    "3)代码能跑却重复改 4)失败后原样重试\n"
    "同工具不同参数=正常批量, 不要误判! 只输出 loop 或 normal。"
)


# ── 工具分类 ──────────────────────────────────

TOOL_CATEGORY = {
    "Write": "mutate",
    "Edit": "mutate",
    "Bash": "exec",
    "PowerShell": "exec",
    "Read": "explore",       # 鼓励读memory/knowledge
    "Glob": "search",        # 代码搜索
    "Grep": "search",
    "WebFetch": "explore",   # 鼓励查资料
    "WebSearch": "explore",  # 鼓励搜索
    "Agent": "delegate",
    "TaskCreate": "delegate",
    "TaskUpdate": "delegate",
    "SendMessage": "delegate",
    "AskUserQuestion": "interact",
}
# 探索类工具 — 缺少时触发鼓励
EXPLORE_TOOLS = {"WebSearch", "WebFetch", "Read"}
# 突变类工具 — 过多时警告
MUTATE_TOOLS = {"Write", "Edit"}

# @fix 2026-08-22 maintainer实地发现(召回率盲区): 模型爱用 Bash/PowerShell 改文件(sed -i/重定向/
# Set-Content), _categorize 把它们全归 exec → Bash 改文件对卡住/修复循环检测隐形。
# 文件变异命令模式: 命中即按 mutate 处理(仅用于检测分类, 不改变 hook 对 Bash 的其他逻辑)。
_BASH_MUTATE_PATTERNS = [
    r"\bsed\b[^|;&]*\s-i",                    # sed -i
    r"(?<![0-2>])>{1,2}\s*[^\s|>&]",          # > file / >> file (排除 2> 1> 2>&1)
    r"\btee\b\s+-?\w*\s*[^\s|]",              # tee file
    r"\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bNew-Item\b",
    r"\bMove-Item\b|\bCopy-Item\b|\bRemove-Item\b",
    r"(?<![\w-])mv\s+(?:-\w+\s+)*\S+\s+\S",   # mv src dst
    r"(?<![\w-])cp\s+(?:-\w+\s+)*\S+\s+\S",   # cp src dst
]
_BASH_MUTATE_RE = re.compile("|".join(_BASH_MUTATE_PATTERNS))

def _categorize(tool_name: str, tool_input: str = "") -> str:
    cat = TOOL_CATEGORY.get(tool_name, "other")
    # 召回补丁: exec 类命令若直接改写文件 → 计为 mutate (检测口径对齐真实写入行为)
    if cat == "exec" and tool_input:
        try:
            if _BASH_MUTATE_RE.search(tool_input[:400]):
                return "mutate"
        except Exception:
            pass
    return cat


# ── 记录工具调用 ─────────────────────────────

def record(tool_name: str, tool_input: str = ""):
    """记录一次工具调用到 JSONL"""
    ts = time.time()
    # 窗口隔离(P1): 记录 session 归属, 供多窗口审计/漂移溯源
    # CC hook 通过 PreToolUse.ps1 注入 CLAUDE_SESSION_ID env → pythonw 继承
    _sid = os.environ.get("CLAUDE_SESSION_ID", "")[:16] or f"proc_{os.getpid()}"
    entry = {
        "ts": ts,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
        "session": _sid[:8],
        "tool": tool_name,
        "category": _categorize(tool_name, tool_input),
        "input_preview": tool_input[:100] if tool_input else "",
    }
    OPS_LOG.parent.mkdir(parents=True, exist_ok=True)
    # ── drift_v2 行为漂移检测 (2026-08-18, 零额外进程开销) ──
    try:
        from drift_v2 import check_drift
        drift_alert = check_drift(tool_name, tool_input)
        if drift_alert:
            entry["drift_alert"] = drift_alert[:200]
            # 写告警文件供 hook 读取
            alert_file = OPS_LOG.parent / "drift_v2_alert.json"
            try:
                import tempfile
                tmp = alert_file.with_suffix(".tmp")
                tmp.write_text(json.dumps({"ts": ts, "alert": drift_alert}, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, alert_file)
            except Exception:
                pass
    except Exception:
        pass
    try:
        with open(OPS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + chr(10))
    except Exception:
        pass
    # 每次记录后更新声誉 + 健康状态 (供 PreToolUse 硬闸门读取)
    _update_reputation(tool_name)
    check_health()


# ── 滚动窗口分析 ─────────────────────────────

def _load_recent(n: int = WINDOW_SIZE) -> list[dict]:
    """加载最近 N 条记录"""
    if not OPS_LOG.exists():
        return []
    entries = []
    try:
        with open(OPS_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except Exception:
        pass
    return entries[-n:]


def _detect_burst(entries: list[dict]) -> list[str]:
    """检测同类型连续调用爆发"""
    alerts = []
    if len(entries) < BURST_THRESHOLD:
        return alerts
    # 按类别统计连续性
    recent_tools = [e.get("tool", "") for e in entries[-BURST_THRESHOLD:]]
    # 检查最后 N 个是否全是同类型
    unique = set(recent_tools)
    if len(unique) == 1:
        alerts.append(f"连续{BURST_THRESHOLD}次调用同一工具: {list(unique)[0]}")
    elif len(unique) <= 2 and len(entries) >= 10:
        # 检查是否有 90%+ 集中在两种工具
        counter = Counter(recent_tools)
        top2 = sum(c for _, c in counter.most_common(2))
        if top2 >= BURST_THRESHOLD:
            alerts.append(f"工具调用集中: {dict(counter.most_common(2))}")
    return alerts


def _detect_error_cascade(entries: list[dict]) -> list[str]:
    """检测写-回读-再写循环 (可能的修复循环退化)"""
    alerts = []
    if len(entries) < 6:
        return alerts
    # 模式: mutate → read → mutate → read → mutate ... (>= N 轮)
    # skirmish 档位查表 (2026-08-22 改动a): farming=3轮, skirmish=2轮(提前介入)
    # ⚠️ 历史死代码已修活(2026-08-22 maintainer裁决全档位): 原比较 `category == "read"` 恒假,
    #    现以 tool=="Read" 判据全档位生效, 修复循环报警首次真正可用, 需观察误报。
    fixloop_threshold = 3
    _skirmish_live = False
    if _read_stance is not None:
        try:
            if _read_stance().get("mode") == "skirmish":
                fixloop_threshold = 2
                _skirmish_live = True
        except Exception:
            pass
    recent = entries[-12:]
    mutate_read_pairs = 0
    for i in range(len(recent) - 1):
        _nxt = recent[i + 1]
        # @fix 2026-08-22 maintainer裁决全档位修活: 原比较 `category == "read"` 恒假 (Read 的类别是 "explore"),
        # 修复循环报警自创建起从未触发。改用"写后验证"判据(_is_verify_step: Read 工具或跑测试/运行被改文件)。
        if recent[i]["category"] == "mutate" and _is_verify_step(_nxt, _extract_file_hint(recent[i])):
            mutate_read_pairs += 1
    if mutate_read_pairs >= fixloop_threshold:
        alerts.append(f"可能的修复循环: {mutate_read_pairs} 轮 write→read 交替")
    return alerts


def _extract_file_hint(entry: dict) -> str:
    """从 input_preview 提取文件身份 (归一化为 basename, 供同文件计数; 窗口仅12条, 同名碰撞可忽略)。"""
    prev = entry.get("input_preview", "") or ""
    raw = ""
    m = re.search(r'file_path"?\s*[:=]\s*"?([^",\s]+)', prev)
    if m:
        raw = m.group(1)
    elif entry.get("category") == "mutate":
        # Bash/PS 变异命令: 从命令串抠目标文件 (sed/tee/mv/cp 的参数, 或重定向目标)
        m2 = re.search(r'(?:\bsed\b[^|;&]*?|\btee\s+\S+\s+|(?<![\w-])(?:mv|cp)\s+(?:-\w+\s+)*\S+\s+)(\S+\.\w+)', prev)
        if not m2:
            m2 = re.search(r'(?<![0-2>])>{1,2}\s*([^\s|;&]+)', prev)
        if m2:
            raw = m2.group(1)
    if not raw:
        return ""
    return re.split(r'[\\/]', raw)[-1] or raw


def _is_verify_step(entry: dict, mutated_hint: str = "") -> bool:
    """判断一条操作是否为'写后验证' (2026-08-22 召回补丁: 模型验证习惯是跑测试, 不只 Read)。

    判据: Read 工具, 或 exec 类命令引用了 python 测试/被改文件本体。
    """
    if entry.get("tool") == "Read":
        return True
    if entry.get("category") == "exec":
        cmd = entry.get("input_preview", "") or ""
        if re.search(r'\bpython[\w.]*\b|\bpytest\b|\bnpm\s+test\b|\bmake\s+test\b', cmd):
            if "test" in cmd.lower() or "pytest" in cmd.lower():
                return True
            if mutated_hint and mutated_hint in cmd:
                return True
    return False


def _detect_stuck(entries: list[dict]) -> list[str]:
    """检测'同一目标上重复尝试且无进展' (skirmish v2 核心判据, maintainer2026-08-22重设)。

    信号: ①同一文件最近12条内被 Edit/Write ≥3 次(换姿势重试的典型形态)
          ②写读摇摆(复用 _detect_error_cascade 的判据, 但阈值低一档只提示不deny)
    干预归属: 本检测只产生 skirmish 级提示(不进 deny 正则), deny 仍由修复循环判据负责。
    遗留: 同错误指纹重现需 ops_freq 增加执行结果字段(PreToolUse 时刻未知成败), 暂缓。
    """
    alerts = []
    recent = entries[-12:]
    # ① 同文件重复编辑 (按 category 计: Write/Edit 与 Bash/PS 变异命令同权, 2026-08-22 召回补丁)
    file_counts = Counter()
    for e in recent:
        if e.get("category") == "mutate":
            hint = _extract_file_hint(e)
            if hint:
                file_counts[hint] += 1
    for path, n in file_counts.most_common(2):
        if n >= 3:
            alerts.append(f"卡住模式: {path[-48:]} 最近被改 {n} 次仍未通过")
    # ② 写-验证摇摆 (2对即提示, 低于修复循环deny阈值3; 验证含Read与跑测试, 同 _is_verify_step)
    pairs = 0
    for i in range(len(recent) - 1):
        if recent[i].get("category") == "mutate" and _is_verify_step(recent[i + 1], _extract_file_hint(recent[i])):
            pairs += 1
    if pairs == 2:
        alerts.append(f"卡住前兆: {pairs} 轮 write→read 摇摆(再恶化将触发修复循环会诊)")
    return alerts


def _detect_read_storm(entries: list[dict]) -> list[str]:
    """检测大量连续搜索 (可能的盲目搜索)"""
    alerts = []
    if len(entries) < 5:
        return alerts
    recent_search = [e for e in entries[-5:] if e.get("category") in ("search", "explore")]
    if len(recent_search) >= 5:
        if len(entries) >= 10:
            prev_search = [e for e in entries[:-5] if e.get("category") in ("search", "explore")]
            if len(prev_search) >= 3:
                alerts.append(f"持续搜索: 最近5次全为搜索/探索, 前序已有{len(prev_search)}次")
    return alerts


def _load_reputation() -> dict:
    """加载行为声誉 (explore/mutate 比例)"""
    if not OPS_REPUTATION.exists():
        return {"explore_count": 0, "mutate_count": 0, "gap_threshold": GAP_BASELINE}
    try:
        return json.loads(OPS_REPUTATION.read_text(encoding="utf-8"))
    except Exception:
        return {"explore_count": 0, "mutate_count": 0, "gap_threshold": GAP_BASELINE}


def _save_reputation(rep: dict):
    OPS_REPUTATION.parent.mkdir(parents=True, exist_ok=True)
    OPS_REPUTATION.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")


def _update_reputation(tool_name: str):
    """每次工具调用后更新声誉分"""
    rep = _load_reputation()
    cat = _categorize(tool_name)
    if cat == "explore":
        rep["explore_count"] = rep.get("explore_count", 0) + 1
    elif cat == "mutate":
        rep["mutate_count"] = rep.get("mutate_count", 0) + 1

    # 动态阈值: explore多→放宽, mutate多→收紧
    total = rep["explore_count"] + rep["mutate_count"]
    if total > 0:
        explore_ratio = rep["explore_count"] / total
        # ratio > 0.3 → 放宽, ratio < 0.1 → 收紧
        if explore_ratio > 0.3:
            rep["gap_threshold"] = min(GAP_MAX, GAP_BASELINE + int(explore_ratio * 15))
        elif explore_ratio < 0.1:
            rep["gap_threshold"] = max(GAP_MIN, GAP_BASELINE - 5)
        else:
            rep["gap_threshold"] = GAP_BASELINE
    _save_reputation(rep)
    return rep


def _detect_explore_gap(entries: list[dict]) -> list[str]:
    """探索缺口检测 (动态阈值)"""
    alerts = []
    rep = _load_reputation()
    threshold = rep.get("gap_threshold", GAP_BASELINE)
    if len(entries) < threshold:
        return alerts
    recent = entries[-threshold:]
    has_explore = any(e.get("category") in ("explore", "search") for e in recent)
    has_exec = any(e.get("category") == "exec" for e in recent)
    has_web = any(e.get("tool") in ("WebSearch", "WebFetch") for e in recent)
    has_read = any(e.get("tool") == "Read" for e in recent)
    mutate_count = sum(1 for e in recent if e.get("category") == "mutate")

    if has_exec and mutate_count >= threshold - 4 and not has_explore:
        alerts.append("执行缺口: 连续{0}次Bash/Write无Web/Read, 建议先查资料再跑仿真".format(threshold))
    elif not has_explore and mutate_count >= threshold - 2:
        alerts.append("探索缺口: 连续{0}次操作无Web/Read, 建议查资料或读memory".format(threshold))
    elif not has_web and mutate_count >= threshold - 1:
        alerts.append("Web缺口: 连续修改无搜索, 建议先查GitHub/文档再改".format(threshold))
    elif not has_read and mutate_count >= threshold:
        alerts.append("Memory缺口: 多轮修改未读memory, 建议回顾项目知识和历史".format(threshold))
    return alerts


def _compute_diversity(entries: list[dict]) -> float:
    """工具多样性: 唯一工具数 / 总工具数 (0-1)"""
    if not entries:
        return 0.0
    tools = [e.get("tool", "") for e in entries]
    return len(set(tools)) / len(tools)


def _compute_tool_entropy(entries: list[dict]) -> float:
    """工具类别香农熵 (bits), 高=探索, 低=陷入局部"""
    import math
    if not entries:
        return 0.0
    cats = [e.get("category", "other") for e in entries]
    counter = Counter(cats)
    total = len(cats)
    entropy = 0.0
    for count in counter.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)
    return round(entropy, 3)


# ── 健康报告 ─────────────────────────────────

def check_health() -> dict:
    """生成当前会话的操作健康报告"""
    entries = _load_recent(WINDOW_SIZE)
    all_entries = _load_recent(200)  # 更大窗口用于趋势

    if not entries:
        return {"status": "no_data", "total_ops": 0}

    categories = Counter(e.get("category", "other") for e in entries)
    tools = Counter(e.get("tool", "unknown") for e in entries)
    all_tools = Counter(e.get("tool", "unknown") for e in all_entries)

    # 时间分布
    if len(entries) >= 2:
        first_ts = entries[0].get("ts", 0)
        last_ts = entries[-1].get("ts", 0)
        duration_s = last_ts - first_ts
        ops_per_min = len(entries) * 60 / duration_s if duration_s > 0 else 0
    else:
        duration_s = 0
        ops_per_min = 0

    alerts = []
    alerts.extend(_detect_burst(entries))
    alerts.extend(_detect_error_cascade(entries))
    alerts.extend(_detect_explore_gap(entries))
    alerts.extend(_detect_read_storm(entries))
    # skirmish v2 (2026-08-22 maintainer重设): 卡住检测提示级, 不进 deny 正则
    try:
        alerts.extend(_detect_stuck(entries))
    except Exception:
        pass

    diversity = _compute_diversity(entries)
    entropy = _compute_tool_entropy(entries)

    report = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "window": len(entries),
        "total_session": len(all_entries),
        "ops_per_min": round(ops_per_min, 1),
        "duration_min": round(duration_s / 60, 1),
        "categories": dict(categories),
        "top_tools": dict(tools.most_common(5)),
        "session_top_tools": dict(all_tools.most_common(5)),
        "diversity": round(diversity, 3),
        "tool_entropy": entropy,
        "alerts": alerts,
    }

    # 持久化健康状态
    OPS_HEALTH.parent.mkdir(parents=True, exist_ok=True)
    try:
        OPS_HEALTH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    # ── stance 档位同步 (2026-08-22 改动a, 挂在监控状态机上) ──
    # 修复循环 或 卡住模式 → skirmish(TTL 10min 兜底); 都解除 → 自动回 farming。
    # set_stance 重导入只发生在档位切换瞬间(罕见), 平时零额外负担。
    if _read_stance is not None:
        try:
            _stuck_active = any(("修复循环" in a or "卡住模式" in a or "卡住前兆" in a) for a in alerts)
            _cur = _read_stance().get("mode", "farming")
            if _stuck_active and _cur != "skirmish":
                sys.path.insert(0, str(ROOT / "scripts"))
                from mcp_cls_tools import set_stance
                set_stance("skirmish", ttl_seconds=600, set_by="ops_monitor",
                           reason="监控状态机检出修复循环, 自动降档保护")
            elif not _stuck_active and _cur == "skirmish":
                sys.path.insert(0, str(ROOT / "scripts"))
                from mcp_cls_tools import set_stance
                set_stance("farming", set_by="ops_monitor",
                           reason="修复循环解除, 监控状态机退出skirmish")
        except Exception:
            pass  # fail-open: 档位同步失败不影响健康报告

    return report


# ── 三层管道: Layer0 硬闸门标记 ─────────────────

def _normalize_cmd(tool_input: str) -> str:
    """从 Bash input_preview 提取命令指纹 (去参数值/去时间戳/去路径尾部)
    例: 'python temp/test.py 2>&1' → 'python test' ; 'git commit -m fix' → 'git commit'
    兼容两种传入: 旧格式 '{command:cd ...}' 与 新格式(真实命令 'cd /path')"""
    cmd = tool_input.strip()
    # 兼容旧格式 {command:XXX}
    if cmd.startswith("{command:"):
        cmd = cmd[len("{command:"):].rstrip("}")
    # 兼容 JSON 包 {"command":"XXX", ...}
    if cmd.startswith('{"command"'):
        try:
            cmd = json.loads(cmd).get("command") or ""
        except Exception:
            pass
    # 去 shell 重定向/管道尾部
    cmd = re.split(r"[|&>]", cmd)[0]
    parts = cmd.split()
    if not parts:
        return ""
    # 保留前 2 个 token (命令+首个目标), 去数字/路径前缀/引号
    keep = []
    for p in parts[:2]:
        p = p.strip("'\"")
        # 去掉常见噪音: 数字、临时路径、哈希
        p = re.sub(r"temp[/\\\\]", "", p)
        p = re.sub(r"[0-9a-f]{8,}", "H", p)
        keep.append(p)
    return " ".join(keep)


def _is_meta_cmd(fp: str) -> bool:
    """判断指纹是否为元操作 — 工具脚步声, 不参与循环计数。
    例: 'cd' / 'cd /e/claude_api/claude' / 'pwd' / 环境变量前缀(PYTHONIOENCODING=utf-8)。
    这些是工具执行任何操作时自动带的, 不代表真的在反复跑同一命令。

    2026-08-18 修订: python 命令不再全豁免。
    - python -c "..." → 豁免 (代码内容不可比, 指纹都是 'python -c')
    - pythonw (后台) → 豁免 (高频后台操作)
    - python script.py → 参与循环计数 (同脚本重复 = 真循环)
    """
    if not fp:
        return True
    first = fp.split()[0]
    # 目录切换/查看类 (cd 变体多, 用前缀匹配)
    if first == "cd" or fp.startswith("cd ") or first in ("pwd", "ls", "dir", "Get-Location"):
        return True
    # pythonw (后台) 全静默
    if first in ("pythonw",):
        return True
    # python/python3/py: -c 豁免, script 参与计数
    if first in ("python", "python3", "py"):
        # python -c "..." → 指纹 'python -c', 代码不可比 → 豁免
        if fp.startswith("python -c") or fp.startswith("python3 -c") or fp.startswith("py -c"):
            return True
        # python script.py → 参与循环计数 (同脚本重复 = 真循环)
        return False
    # 环境变量前缀 (如 PYTHONIOENCODING=utf-8) — 非实际命令
    if "=" in first and first not in ("python", "python3", "powershell", "cmd"):
        return True
    return False


# ── 反馈互动体系 (评分→降敏) ─────────────────────
# @since 2026-08-01 maintainer定调: 告警单向→双向。assistant读完告警回 0-10 评分,
# score<3="太吵" → 该指纹降敏(阈值 3→8) + 去重(同 reasons 不重复注入)。TTL 过期自动恢复。

def _feedback_score(fp: str, now: float) -> float | None:
    """返回 fp 在 TTL 内最近反馈的评分; 前缀匹配(feedback 'python' 覆盖 'python -c')"""
    if not FEEDBACK_FILE.exists():
        return None
    best = None
    try:
        for line in FEEDBACK_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not e.get("fp"):
                continue
            if now - e.get("ts", 0) > FEEDBACK_TTL:
                continue
            if fp.startswith(e["fp"]) and (best is None or e["ts"] > best["ts"]):
                best = e
    except Exception:
        return None
    return best["score"] if best else None


def _feedback_threshold(fp: str) -> int:
    """反馈降敏: 该指纹近期被评<3(太吵) → 阈值 3→8; 真循环(8次原样重复)仍拦"""
    try:
        sc = _feedback_score(fp, time.time())
        if sc is not None and sc < FEEDBACK_DESENS_THRESHOLD:
            return FEEDBACK_DESENS_MIN
    except Exception:
        pass
    return LOOP_MIN_FINGERPRINT


def _write_feedback(fp: str, score: float, note: str = "") -> None:
    """记录assistant对某命令指纹告警的反馈评分 (0-10; <3=太吵触发降敏, ≥7=有用保持)"""
    ts = time.time()
    entry = {
        "ts": ts,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
        "session": _session_id()[:8],
        "fp": fp,
        "score": float(score),
        "note": note,
    }
    try:
        FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _layer0_mark(entries: list[dict]) -> dict | None:
    """Layer0 硬闸门: 规则只做标记, 不做判定。
    触发条件(任一): ①同命令指纹≥N ②同Write目标文件≥N ③Bash/Write密集且无探索摄入
    返回标记信息(供 Layer1 语义判定), 无命中返回 None"""
    if len(entries) < LOOP_MIN_FINGERPRINT:
        return None
    cmd_count = Counter()
    write_targets = Counter()
    bash_write = 0
    explore_count = 0
    for e in entries:
        tool = e.get("tool", "")
        ti = e.get("input_preview", "") or ""
        cat = e.get("category", "")
        if tool == "Bash":
            fp = _normalize_cmd(ti)
            # 元操作(cd/切目录/环境前缀/python)是工具脚步声/高频操作, 不计入循环指纹与密集计数
            if fp and not _is_meta_cmd(fp):
                cmd_count[fp] += 1
                bash_write += 1
        elif tool in ("Write", "Edit"):
            # 从 file_path 提取目标文件指纹
            m = re.search(r"file_path:([^,}\"]+)", ti)
            if m:
                wp = m.group(1).replace("\\", "/").split("/")[-1][:40]
                write_targets[wp] += 1
            bash_write += 1
        elif tool == "PowerShell":
            bash_write += 1
        if cat in ("explore", "search"):
            explore_count += 1
    reasons = []
    if cmd_count:
        top_fp, c = cmd_count.most_common(1)[0]
        # 反馈降敏: 该指纹被assistant评过"太吵" → 阈值提高(3→8), 真循环仍拦
        if c >= _feedback_threshold(top_fp):
            reasons.append(f"同命令指纹x{c}: {top_fp}")
    if write_targets and write_targets.most_common(1)[0][1] >= LOOP_MIN_WRITE:
        wp, c = write_targets.most_common(1)[0]
        reasons.append(f"同文件写入x{c}: {wp}")
    if bash_write >= LOOP_BASH_WRITE_MIN and explore_count == 0:
        reasons.append(f"Bash/Write密集x{bash_write}无探索摄入")
    if not reasons:
        return None
    # 生成供语义判定的序列摘要 (最近15条, 含session/时间)
    seq_lines = []
    for e in entries[-15:]:
        iso = e.get("iso", "") or ""
        tool = e.get("tool", "")
        ti = (e.get("input_preview", "") or "")[:70]
        seq_lines.append(f"{iso[11:]} {tool}:{ti}")
    return {
        "reasons": reasons,
        "seq": "\n".join(seq_lines),
        "window": len(entries),
        "cmd_count": dict(cmd_count.most_common(3)),
        "write_targets": dict(write_targets.most_common(2)),
    }


# ── 三层管道: Layer1 小模型语义判定 ─────────────

def _sf_semantic_check(seq_text: str) -> str | None:
    """Layer1: SF 免费小模型语义判定工具序列是否真循环。
    免费优先 (Qwen2.5-7B), 实测 prompt v3 100% 准确, 延迟 0.4-0.7s。
    fail-open: 任何异常返回 None (不误报, 由上层决定)"""
    import urllib.request
    try:
        sf_cfg = json.loads((ROOT / "keys" / "siliconflow_config.json").read_text(encoding="utf-8"))
        body = json.dumps({
            "model": SF_MODEL,
            "messages": [
                {"role": "system", "content": LOOP_SYSTEM_PROMPT},
                {"role": "user", "content": seq_text[:1200]},
            ],
            "max_tokens": 20, "temperature": 0.0,
            "user": "cls-xumo-loop-detect",  # userid: 缓存命中 + 审计追踪
        }).encode()
        req = urllib.request.Request(
            sf_cfg["base_url"].rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {sf_cfg['api_key']}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            msg = json.loads(resp.read().decode())["choices"][0]["message"]
        text = (msg.get("content") or "").strip().lower()
        if "loop" in text:
            return "loop"
        if "normal" in text:
            return "normal"
        return None
    except Exception:
        return None


# ── 三层管道: 组合入口 ─────────────────────────

def _semantic_cooldown_file() -> Path:
    """冷却状态文件 (per-session, 避免多窗口互相踩踏)"""
    sid = _session_id()
    return ROOT / "data" / "state" / f"loop_cooldown_{sid[:8]}.json"


def _session_id() -> str:
    import os as _os
    return _os.environ.get("CLAUDE_SESSION_ID", "") or f"proc_{_os.getpid()}"


def semantic_detect() -> dict:
    """三层管道主入口: Layer0 标记 → (命中才) Layer1 语义判定。
    返回完整判定结果供注入层使用。冷却期内直接返回 normal 不调模型。
    冷却设计: 30s 内不重复调 SF, 防多轮风暴; 用文件持久化, 跨进程生效"""
    entries = _load_recent(20)
    if not entries:
        return {"verdict": None, "reason": "no_data"}
    mark = _layer0_mark(entries)
    if mark is None:
        return {"verdict": "normal", "reason": "layer0_clean", "mark": None}
    # 冷却检查: 30s 内已判定过 → 不重复调模型, 用最近一次结果
    cdf = _semantic_cooldown_file()
    last = {}
    try:
        if cdf.exists():
            last = json.loads(cdf.read_text(encoding="utf-8"))
    except Exception:
        pass
    now = time.time()
    if now - last.get("ts", 0) < LOOP_COOLDOWN:
        return {"verdict": last.get("verdict"), "reason": f"cooldown({int(now-last.get('ts',0))}s)", "mark": mark}
    # Layer0 命中 → Layer1 语义判定
    verdict = _sf_semantic_check(mark["seq"])
    # 持久化冷却 (原子写)
    try:
        cdf.parent.mkdir(parents=True, exist_ok=True)
        tmp = cdf.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": now, "verdict": verdict}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cdf)
    except Exception:
        pass
    return {
        "verdict": verdict,          # "loop" | "normal" | None
        "reason": "layer1_semantic",
        "mark": mark,
    }


# ── CLI ──────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("用法: ops_monitor.py <record|health> [tool_name] [tool_input]")
        return

    cmd = sys.argv[1]

    if cmd == "record":
        tool_name = sys.argv[2] if len(sys.argv) > 2 else ""
        tool_input = sys.argv[3] if len(sys.argv) > 3 else ""
        record(tool_name, tool_input)

    elif cmd == "health":
        report = check_health()
        # 输出简化版告警（供 hook 注入）
        alerts = report.get("alerts", [])
        if alerts:
            print(f"[Ops] 告警: {alerts[0]}")
        print(json.dumps(report, ensure_ascii=False, indent=2))

    elif cmd == "semantic":
        """三层管道: Layer0 规则标记 → Layer1 SF小模型语义判定"""
        result = semantic_detect()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "feedback":
        """assistant反馈评分 (0-10): score<3=太吵降敏, ≥7=有用保持
        用法: python ops_monitor.py feedback --fp "python -c" --score 2 --note "正常验证别吵" """
        args = sys.argv[2:]
        fp = score = note = None
        i = 0
        while i < len(args):
            if args[i] == "--fp" and i + 1 < len(args):
                fp = args[i + 1]; i += 2
            elif args[i] == "--score" and i + 1 < len(args):
                try:
                    score = float(args[i + 1])
                except ValueError:
                    score = None
                i += 2
            elif args[i] == "--note" and i + 1 < len(args):
                note = args[i + 1]; i += 2
            else:
                i += 1
        if fp is None or score is None:
            print("用法: ops_monitor.py feedback --fp <指纹> --score <0-10> [--note ...]")
            return
        score = max(0.0, min(10.0, score))
        _write_feedback(fp, score, note or "")
        print(f"反馈已记录: fp='{fp}' score={score} note={note or ''} (TTL {FEEDBACK_TTL}s, <3 触发降敏)")
        # 显示降敏效果
        th = _feedback_threshold(fp)
        print(f"降敏状态: 该指纹当前触发阈值 = {th} 次" + (" (已降敏)" if th > LOOP_MIN_FINGERPRINT else " (默认)"))

    elif cmd == "reset":
        if OPS_LOG.exists():
            OPS_LOG.unlink()
        if OPS_HEALTH.exists():
            OPS_HEALTH.unlink()
        print("已重置")


if __name__ == "__main__":
    main()
