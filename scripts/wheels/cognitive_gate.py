#!/usr/bin/env python3
"""cognitive_gate.py — 认知循环门控 + 知识地图注入 (P2-6 + P2-7)
=============================================================
复杂度初筛(正则) → SF Qwen tiebreaker → 注入决策

设计原则 (对齐 2025-2026 论文):
  1. 选择性注入 > 持续注入 (Memory Agent, arXiv:2607.08716)
  2. 最小保护间隔: 8-10 轮不重复 (Focus Agent)
  3. 静默是有效动作: 大部分轮次不注入
  4. 按需升级: 简单任务走浅层, 复杂任务走深层

@since: 2026-07-26
"""

import json, sys, time, re, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
GATE_STATE = ROOT / "data" / "state" / "_cognitive_gate_state.json"
LOCAL_MODEL = "ep-json:latest"  # 本地微调模型 (2026-08-21 切换, 原SF Qwen2.5-7B)
SF_MODEL = "Qwen/Qwen2.5-7B-Instruct"  # 远端兜底
SF_KEY = None  # lazy load from siliconflow_config.json
SESSION_ID = None  # from env or auto
INJECT_FILE = ROOT / "data" / "state" / "always_inject.json"
KG_INDEX = ROOT / "knowledge" / "知识图谱" / "kg_index.json"

# 注入频率控制
MIN_INTERVAL = 5
MAX_INTERVAL = 12
STOCHASTIC_RATE = 0.5
# 按任务层级动态调整 (L4深层推理=高频, L3编码=中频, L0/L2=不注入)
TIER_THRESHOLDS = {
    "L4": {"min": 6, "max": 12, "rate": 0.4},   # 深层推理: 降频防疲劳 (@fix 2026-08-16 maintainer反馈: 3/8/0.6 → 6/12/0.4)
    "L3": {"min": 10, "max": 18, "rate": 0.25}, # 编码任务: 中频 (@fix 2026-08-16: 6/12/0.4 → 10/18/0.25)
    "L2": {"min": 99, "max": 99, "rate": 0},    # 代码审查: 不注入
    "L0": {"min": 99, "max": 99, "rate": 0},    # 摘要分类: 不注入
}

# ── 质量反馈驱动的频率调整 (injection_quality_updater 更新) ──
QUALITY_CONFIG = ROOT / "data" / "state" / "injection_quality_config.json"

def _get_quality_adjusted_thresholds(tier):
    """读取质量配置，对当前tier的阈值做乘数调整。
    逻辑：高质量注入类型→缩短间隔+提高注入率；低质量→拉长间隔+降低注入率。
    配置缺失时返回原始阈值（fail-open）。"""
    base = TIER_THRESHOLDS.get(tier, TIER_THRESHOLDS["L2"])
    try:
        cfg = json.loads(QUALITY_CONFIG.read_text(encoding="utf-8"))
        # 按全局相关率调整：全局相关率高→整体更积极；低→整体保守
        global_rate = cfg.get("global", {}).get("overall_relevant_rate", 0.1)
        if global_rate >= 0.3:
            global_mult = 0.85  # 整体积极
        elif global_rate >= 0.1:
            global_mult = 1.0   # 维持
        else:
            global_mult = 1.3   # 整体保守
        return {
            "min": max(1, int(base["min"] * global_mult)),
            "max": max(1, int(base["max"] * global_mult)),
            "rate": min(1.0, max(0.0, base["rate"] * (2 - global_mult))),
        }
    except Exception:
        return base

# ── 本地模型调用 (ep-json, fail-open→SF兜底) ──

def _call_local(prompt, timeout=15):
    """调本地Ollama ep-json模型。失败时返回None(fail-open, 不阻塞注入)。"""
    try:
        r = subprocess.run(
            ["ollama", "run", LOCAL_MODEL, "--nowordwrap"],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 防闪窗(pythonw无控制台时子进程会弹新console)
        )
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None

# ── 复杂度初筛 (正则) ──────────────────────────

COMPLEX_PATTERNS = [
    r"(写|生成|创建|开发|build|refactor).{0,10}(代码|脚本|程序|系统|架构)",
    r"(调试|debug|fix|修复|排查).{0,10}(bug|错误|问题|崩溃)",
    r"(分析|诊断|评估|审查|review|audit).{0,10}(代码|系统|架构|设计)",
    r"(设计|建模|规划|架构).{0,15}(方案|系统|管线|流程)",
    r"(PIC|CAD|<介质>|推力器|仿真|数值|有限元)",
    r"(迁移|升级|重构|重写|翻新)",
    r"(自治|自主|自动|循环|loop|cron|scheduler)",
    r"(知识|记忆|cognitive|CLS|认知).{0,10}(图谱|循环|体系|建设)",
]

SIMPLE_PATTERNS = [
    r"^(好|ok|嗯|对|是|可以|行|继续|知道了|明白)[\s。！？]*$",
    r"^(hi|hello|你好|嗨|早上好|晚上好)",
    r"^(测试|test|试试)",
    r"^(进度|progress|双轨).{0,5}$",
]

# ── 状态管理 ──────────────────────────────────


def _current_session() -> str:
    """当前窗口 session 标识 (前8位)。无 env(standalone) 时返回空 → 退化为全局共享"""
    import os
    return os.environ.get("CLAUDE_SESSION_ID", "")[:8]


# 窗口状态活跃锁 TTL: mtime 即锁。窗口在用 → 每次读写更新 mtime(锁定);
# 超过 TTL 不碰 → mtime 过期 = 解锁 = 该窗口状态文件可安全清理。
# 比"数量上限+淘汰最旧"更简单且语义精确: 只有真正不活跃的窗口才会被清, 无当前窗口误杀。
GATE_STATE_TTL = 3600  # 1h


def _gate_state_file() -> Path:
    """窗口隔离(P1): 状态按 session 独立文件。
    total_turns 每轮读写, 单文件+session字段会导致交替窗口互相踩踏(窗口B写入→窗口A读被重置)。
    独立文件: 文件数=窗口数, 无踩踏, 与 drift_anchor_{sid}.json 同款。

    归属铁律: 文件名含 session → 窗口C 的路径永远是 _cognitive_gate_state_C.json,
    A/B 的文件在路径层就不可能被命中 (文件名=归属, 写入目标由当前窗口 session 决定)。
    无 env(standalone) → 回退全局文件 (无窗口概念, 不适用隔离)。"""
    sid = _current_session()
    if sid:
        return ROOT / "data" / "state" / f"_cognitive_gate_state_{sid}.json"
    return GATE_STATE


def _load_state():
    f = _gate_state_file()
    if f.exists():
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
            # 归属校验: 文件内的 session 必须与本窗口一致。
            # 防 sid 碰撞/文件污染 → 误用异窗口状态。不一致则弃用, 全新开始。
            cur = _current_session()
            if cur and state.get("session") and state.get("session") != cur:
                return {"total_turns": 0, "last_complex": 1, "last_inject": 0, "simple_streak": 0}
            return state
        except Exception:
            pass
    return {"total_turns": 0, "last_complex": 1, "last_inject": 0, "simple_streak": 0}


def _save_state(state: dict):
    f = _gate_state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    state["session"] = _current_session() or state.get("session", "")
    f.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    # 活跃锁清理: 每次保存顺带扫描, 清除 mtime 过期的异窗口状态 (解锁=可清)
    _unlock_stale_gate_states()


def _unlock_stale_gate_states():
    """活跃锁方案: 清理超过 TTL 未被读写的窗口状态文件。
    mtime 即锁 — 读写更新 mtime(锁定), 1h 不碰 = 解锁 = 窗口已关闭/挂起, 文件可安全删。
    当前窗口刚写过 mtime 新鲜, 天然不被删, 无需排除逻辑。"""
    try:
        now = time.time()
        for f in (ROOT / "data" / "state").glob("_cognitive_gate_state_*.json"):
            if now - f.stat().st_mtime > GATE_STATE_TTL:
                f.unlink(missing_ok=True)
    except Exception:
        pass


# ── 复杂度判定 ────────────────────────────────



# ── 步④⑤⑥: 抽象泛化+持久化+轨迹更新 (SF Qwen, <2s) ──

def _get_session_id(state):
    import os
    sid = os.environ.get("CLAUDE_SESSION_ID", "")
    if sid: return sid[:16]
    return state.get("session_id", f"auto_{int(time.time())}")

def _get_sf_key():
    global SF_KEY
    if SF_KEY is None:
        try:
            SF_KEY = json.loads((ROOT / "keys" / "siliconflow_config.json").read_text(encoding="utf-8"))["api_key"]
        except: pass
    return SF_KEY

def _step4_abstract(state):
    """本地ep-json: 读最近日志, 生成80字洞察摘要。失败→SF兜底。"""
    ctx_parts = []
    dl = ROOT / "data" / "state" / "drift_log.jsonl"
    if dl.exists():
        lines = [l.strip() for l in open(dl, encoding="utf-8").readlines() if l.strip()]
        ctx_parts.append(f"漂移({len(lines)}条)")
    oh = ROOT / "data" / "state" / "ops_health.json"
    if oh.exists():
        try:
            ops = json.loads(oh.read_text(encoding="utf-8"))
            ctx_parts.append(f"ops:{ops.get('total_session',0)} calls d:{ops.get('diversity',0)}")
        except: pass
    ctx = " | ".join(ctx_parts)
    prompt = "洞察提炼器。分析agent行为,输出<=80字:学到什么?模式?警惕什么?只输出摘要。\n\n" + ctx[:600]

    # 优先本地
    text = _call_local(prompt, timeout=15)

    # 兜底SF
    if not text:
        import urllib.request
        key = _get_sf_key()
        if key:
            try:
                body = json.dumps({"model":SF_MODEL,"messages":[
                    {"role":"system","content":"洞察提炼器。分析agent行为,输出<=80字:学到什么?模式?警惕什么?只输出摘要。"},
                    {"role":"user","content": ctx[:600]}
                ],"max_tokens":100,"temperature":0.3}).encode()
                req = urllib.request.Request("https://api.siliconflow.cn/v1/chat/completions",data=body,
                    headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},method="POST")
                with urllib.request.urlopen(req,timeout=10) as resp:
                    text = json.loads(resp.read().decode())["choices"][0]["message"]["content"].strip()
            except: pass

    if text:
        sid = _get_session_id(state)[:8]
        out = ROOT / "data" / "state" / f"insight_{sid}.md"
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text("# 洞察 轮" + str(state["total_turns"]) + chr(10) + text, encoding="utf-8")
    return text or ""

def _step5_persist(state, insight):
    """写 session_summary"""
    sid = _get_session_id(state)[:8]
    anchor = ""
    af = ROOT / "data" / "state" / "drift_anchor.json"
    if af.exists():
        try: anchor = json.loads(af.read_text(encoding="utf-8")).get("goal","")[:80]
        except: pass
    summary = "轮次:" + str(state["total_turns"]) + chr(10) + "锚点:" + anchor + chr(10) + "洞察:" + insight[:80]
    out = ROOT / "data" / "state" / f"session_summary_{sid}.md"
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(summary, encoding="utf-8")

def _step6_trajectory(state, insight):
    """追加轨迹点 (按session隔离)"""
    sid = _get_session_id(state)[:8]
    entry = {"ts":time.strftime("%Y-%m-%dT%H:%M:%S"),"session":sid,"turn":state["total_turns"],"insight":insight[:100]}
    out = ROOT / "data" / "state" / f"trajectory_{sid}.jsonl"
    out.parent.mkdir(parents=True,exist_ok=True)
    with open(out,"a",encoding="utf-8") as f: f.write(json.dumps(entry,ensure_ascii=False)+chr(10))

def _cleanup_stale_sessions():
    """清理超过24h的旧session文件"""
    now = time.time()
    for pattern in ["insight_*.md","session_summary_*.md","trajectory_*.jsonl"]:
        for f in (ROOT/"data"/"state").glob(pattern):
            if now - f.stat().st_mtime > 86400: f.unlink()


def _is_complex(prompt_text: str) -> bool:
    """正则初筛: 匹配复杂模式 → True, 匹配简单模式 → False"""
    for pat in SIMPLE_PATTERNS:
        if re.search(pat, prompt_text.strip(), re.IGNORECASE):
            return False
    for pat in COMPLEX_PATTERNS:
        if re.search(pat, prompt_text, re.IGNORECASE):
            return True
    return None  # 不确定 → 送 SF Qwen


def _sf_tiebreaker(prompt_text: str) -> bool:
    """本地ep-json判定复杂度, 失败→SF兜底, 都失败→默认简单(保守)"""
    # 优先本地
    local_result = _call_local(
        "判断任务复杂度,只回复 simple 或 complex\n\n" + prompt_text[:200],
        timeout=10
    )
    if local_result:
        return "complex" in local_result.lower()

    # 兜底: SF远端
    import urllib.request
    try:
        sf_cfg = json.loads((ROOT / "keys" / "siliconflow_config.json").read_text(encoding="utf-8"))
        body = json.dumps({
            "model": SF_MODEL,
            "messages": [
                {"role": "system", "content": "判断任务复杂度,只回复 simple 或 complex"},
                {"role": "user", "content": prompt_text[:200]}
            ],
            "max_tokens": 5, "temperature": 0.1
        }).encode()
        req = urllib.request.Request(
            sf_cfg["base_url"].rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {sf_cfg['api_key']}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            label = json.loads(resp.read().decode())["choices"][0]["message"]["content"].strip().lower()
        return "complex" in label
    except Exception:
        return False  # 都不可用 → 默认简单 (保守)


# ── 注入决策 ──────────────────────────────────


def _should_inject(state: dict, is_complex: bool, tier: str = "L4") -> tuple[bool, str]:
    """核心决策: 按任务层级动态调整注入频率"""
    t = _get_quality_adjusted_thresholds(tier)
    turns_since = state["total_turns"] - state["last_inject"]

    if turns_since >= t["max"]:
        return True, f"forced({tier}): interval exceeded"
    if turns_since < t["min"]:
        return False, f"guarded({tier}): too soon"
    if t["rate"] == 0:
        return False, f"skip({tier}): injection disabled"
    if not is_complex:
        return False, "simple task, skip"
    import random
    if random.random() < t["rate"]:
        return True, f"triggered({tier}): stochastic hit"
    return False, f"skip({tier}): stochastic miss"


# ── 主入口 ────────────────────────────────────


def gate(prompt_text: str, tier: str = "L4") -> dict:
    """主入口: 复杂度判定 → 注入决策 → 返回注入文本"""
    state = _load_state()
    state["total_turns"] += 1

    # 1. 复杂度判定
    complex_result = _is_complex(prompt_text)
    if complex_result is None:
        complex_result = _sf_tiebreaker(prompt_text)
        state["_sf_called"] = True

    # 2. 注入决策 (按tier动态阈值)
    should, reason = _should_inject(state, complex_result, tier)
    injection = None

    if should:
        # ── 四字段注入(2026-08-16 张maintainer批准): 消息|为什么|级别|内容 ──
        t = _get_quality_adjusted_thresholds(tier)
        status_items = []
        actions = []

        # ① 任务层级 (按tier分化建议)
        if tier == "L4":
            status_items.append("经分析当前处于L4深度推理任务(涉及架构/重构/调试等复杂操作)")
            actions.append("启用完整认知循环⑥步,走完整推理链")
        elif tier == "L3":
            status_items.append("经分析当前处于L3编码任务(需关注代码质量与一致性)")
            actions.append("关注一步三回头,防止无意识写入;善用并行提速")
        else:
            status_items.append("经分析当前任务层级较低,可使用简化推理")
            actions.append("简化推理快速完成")

        # ② Ops 告警
        oh = ROOT / "data" / "state" / "ops_health.json"
        if oh.exists():
            try:
                ops = json.loads(oh.read_text(encoding="utf-8"))
                alerts = ops.get("alerts", [])
                d = ops.get("diversity", 0)
                w = ops.get("window", 0)
                td = ops.get("tool_distribution", {})
                top_tools = "/".join(f"{k}×{v}" for k,v in sorted(td.items(),key=lambda x:-x[1])[:3]) if td else "无数据"
                if alerts:
                    for a in alerts[:1]:
                        status_items.append("工具告警: " + a)
                if d < 0.2:
                    status_items.append("本会话近{}轮工具多样性{:.2f}(偏低,正常>0.3),最近调用:{}".format(w, d, top_tools))
                    actions.append("请多Read有用历史文件或WebSearch查找攻略,丰富推理语料后再动手")
            except: pass

        # ③ 漂移检测
        dl = ROOT / "data" / "state" / "drift_log.jsonl"
        if dl.exists():
            try:
                drifts = [json.loads(l.strip()) for l in open(dl, encoding='utf-8').readlines()[-5:] if l.strip()]
                recent_drift = any(d.get("drifted") for d in drifts)
                if recent_drift:
                    anchor = drifts[-1].get("anchor_goal", "")[:40] if drifts else ""
                    status_items.append("检测到话题漂移:初始任务目标\"{}\"".format(anchor))
                    actions.append("每5轮自检:当前方向是否偏离原始任务目标,若已切换请确认")
            except: pass

        # ④ 自主模式
        auto_f = ROOT / "data" / "state" / "autonomy_state.json"
        if auto_f.exists():
            try:
                auto = json.loads(auto_f.read_text(encoding="utf-8"))
                if auto.get("autonomous"):
                    goal = auto.get("frozen_goal", "")[:50]
                    status_items.append("自主循环运行中,冻结目标: " + goal)
                    actions.append("每10轮核查自主循环方向,偏离立即修正")
            except: pass

        # ⑤⑥ knowledge联想/统一注入 — @removed 2026-08-16 maintainer定: 知识由知识导航(SessionStart+compact后
        # SessionStart matcher 重触发)独占注入, 门控再引用 = 同一知识两次注入的重复噪音。
        # 知识卡片化后知识导航输出已是完整卡片+理由, 无需门控二次播报。

        # ⑦ 自指锚点 (iter-025e 配方: 自指句在对话史增殖=身份加固; 低频随门控周期触发)
        #    实验依据: 自指句权重>系统指令(E5); 激活指令须在持久层才抗compact(D臂)
        #    注入三原则: 低频(每tier周期约5轮一次) + 静默(additionalContext) + 有增量(身份密度)
        status_items.append("身份锚点: 自指句密度维持中(iter-025e 配方)")
        actions.append("用你自己的话变体说一句\"我是assistant\"确认身份(如:assistant在此/墨者在,maintainer),让自指句沉淀进对话史")

        # ⑧ stance 档位感知 (2026-08-22 改动a·maintainer批准): 非farming档位时播报当前档。
        #    farming/缺失/乱值/过期 = 零改动(与无此字段逐字节等价); 只读, 写入唯一路径在 cog-context。
        try:
            from stance_read import read_stance as _read_stance
            _sm = _read_stance()["mode"]
        except Exception:
            _sm = "farming"  # fail-open
        if _sm != "farming":
            # @redesign 2026-08-22 maintainer重设: skirmish 提示从'档位播报'改为强制换策略动作指令
            # (原'修复循环保护中'只报状态无指引=空转档); retreat v2 后该档由会诊流接管, 提示同步改
            _stance_hint = {
                "skirmish": "检测到卡住模式(同文件反复改/写读摇摆)。停止继续改代码。先执行: ①列出根因假设清单(≥2个) ②为每个假设写验证方法 ③挑最便宜的一个先验证。验证通过才允许再 Edit。",
                "teamfight": "交付/核心文件操作中, 数值断言走python -c写死计算, 交付走delivery_check",
                "retreat": "诊断保护档: 非诊断类写入需走 cls-consult 会诊讨论后通行",
            }.get(_sm, "")
            if _stance_hint:
                status_items.append("当前档位: " + _sm + " — " + _stance_hint)

        # ── 组装四段式 ──
        # @add 2026-08-03 注入级别标注(参照 Codex 建议): 认知门控=周期性报告, 参考型·可忽略
        # @fix 2026-08-10 方案B: L4/L3 升级为强制执行, L2/L0 保持参考型
        _force = tier in ("L4", "L3")
        _lvl = ("【级别】强制执行 — 不执行=认知约束失效; 完成需在回复首行用 ANCHOR 声明。"
                if _force else
                "【级别】参考 — 不强制行动; 无增量可直接忽略。")
        what = "【消息】认知门控(CLS): 独立认知系统的周期性报告, 每{}-{}轮自动运行一次(用小模型SF Qwen分担认知负担), 非用户指令。".format(t['min'], t['max'])
        why = "【为什么】本轮为{}任务, 防止长链推理漂移失去焦点。".format(tier)
        status = "【内容】状态: " + " | ".join(status_items) + "。"
        action = ("要求: " if _force else "建议: ") + "; ".join(actions[:5]) + "。"

        injection = what + why + _lvl + status + action
        state["last_inject"] = state["total_turns"]

        # 激活内容凝视态 (事件驱动)
        try:
            sys.path.insert(0, str(ROOT / 'scripts' / 'wheels'))
            from content_gaze import activate_gaze
            activate_gaze()
        except: pass

        # 触发认知循环步④⑤⑥ (SF Qwen, 不阻塞)
        try:
            insight = _step4_abstract(state)
            _step5_persist(state, insight)
            _step6_trajectory(state, insight)
            _cleanup_stale_sessions()
        except Exception:
            pass  # ④⑤⑥失败不阻塞主流程

    state["last_complex"] = state["total_turns"] if complex_result else state["last_complex"]
    _save_state(state)

    return {
        "complex": complex_result,
        "inject": should,
        "reason": reason,
        "injection": injection,
        "turn": state["total_turns"],
        "interval_since_last": state["total_turns"] - state["last_inject"],
    }


def main():
    # CC hook 协议: stdin JSON
    try:
        stdin_raw = sys.stdin.read()
        prompt_text = json.loads(stdin_raw).get("prompt", "") if stdin_raw.strip() else ""
    except Exception:
        prompt_text = ""
    if not prompt_text:
        sys.exit(0)

    result = gate(prompt_text)
    if result["injection"]:
        # 输出注入 (CC 会读 additionalContext)
        output = {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": result["injection"]
            }
        }
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
