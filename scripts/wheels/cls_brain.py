#!/usr/bin/env python3
"""cls_brain.py — CLS脑区统一调度器 (Qwen+Fable5评审通过, 85分)
tick() — PostToolUse每轮调用: 新鲜度+竞价+guidance+遥测  
boot() — SessionStart调用: auto_recall+file_watcher+首次检查
"""

import json, os, sys, time, uuid, random
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
GUIDANCE = ROOT / ".claude" / "cls_state" / ".guidance_injection"
COOLDOWN = ROOT / "data" / "state" / ".hook_supervisor_cooldown"
RATE_FILE = ROOT / "data" / "state" / ".hook_supervisor_rate"
TELEMETRY = ROOT / "data" / "state" / "brain_telemetry.jsonl"
WINDOW_ID = os.environ.get("CLAUDE_CODE_SESSION_ID", uuid.uuid4().hex[:12])[:12]

def now_iso(): return datetime.now(timezone.utc).isoformat()

TEMPLATES = [
    "[认知监督] {action}",
    "[系统心跳] 检测到: {action}",
    "[循环监控] {action} — 已持续{hours}小时",
    "[CLS体检] {files} 需要关注",
    "[自动巡检] {action}. 优先级: 认知循环",
    "[状态追踪] 距离上次修复已过{hours}h: {action}",
    "[守护进程] {action} (自动检测,非阻塞)",
    "[周期检查] {files} 过期.",
    "[后台探针] 捕获到: {action}",
    "[静默提醒] 你上次修这些文件是{hours}小时前. {action}",
]

# ═══════════════════════════════════════════════════════
# tick() — PostToolUse 每轮触发
# ═══════════════════════════════════════════════════════

def check_cooldown():
    count = 0
    if COOLDOWN.exists():
        try: count = json.load(open(COOLDOWN)).get("count", 0)
        except: pass
    count += 1
    json.dump({"count": count, "ts": now_iso()}, open(COOLDOWN, 'w'))
    return (count % 10 == 0)

def check_rate():
    if RATE_FILE.exists():
        try:
            if time.time() - float(open(RATE_FILE).read().strip()) < 3600: return True
        except: pass
    return False

def freshness():
    checks = {"active_context(1)": "state/active_context.json",
              "session_memory(5)": "state/session_memory.md",
              "cog_step(2)": "data/state/cog_step.json",
              "trajectory(6)": "state/trajectory.json",
              "symbolic_health": "state/symbolic_health.json"}
    stale = []
    now = time.time()
    for name, path in checks.items():
        fp = ROOT / path
        if not fp.exists(): stale.append(f"{name}(缺)"); continue
        h = (now - fp.stat().st_mtime) / 3600
        max_age = 2 if name == "symbolic_health" else (0.17 if "cog_step" in name else 24)
        if h > max_age: stale.append(f"{name}({h:.0f}h)")
    return stale

def bidding(stale_files):
    now = time.time()
    scores = {}
    # 皮层: 遥测活跃度
    if TELEMETRY.exists():
        lines = open(TELEMETRY).readlines()
        recent = [l for l in lines[-20:] if l.strip()]
        active = sum(1 for l in recent if '"written":"dedup_skipped"' not in l) if recent else 0
        scores["cortex"] = round(0.3 + 0.7 * (active / len(recent)) if recent else 0.5, 2)
    else:
        scores["cortex"] = 0.5
    # 海马: FAISS新鲜度
    faiss = ROOT / "data" / "search_index" / "vectors.faiss"
    scores["hippocampus"] = round(max(0.2, 1.0 - (now - faiss.stat().st_mtime) / 3600 / 48) if faiss.exists() else 0.3, 2)
    # 丘脑: 总线活性
    bus = ROOT / "data" / "flows" / "voice_signal.jsonl"
    scores["thalamus"] = round(max(0.2, 1.0 - (now - bus.stat().st_mtime) / 60 / 10) if bus.exists() else 0.3, 2)
    # 脑干: 过期紧迫度
    scores["brainstem"] = round(min(0.9, 0.4 + 0.1 * len(stale_files)) if stale_files else 0.4, 2)
    esc = ROOT / "data" / "state" / "freshness_escalation.json"
    if esc.exists():
        try:
            if json.load(open(esc)).get("escalated"): scores["brainstem"] = 1.0
        except: pass
    return {"scores": scores, "winner": max(scores, key=scores.get), "ts": now_iso()}

def write_guidance(stale, sup_status, bid):
    action_parts = []
    if stale: action_parts.append("过期:" + ",".join([s.split("(")[0] for s in stale[:3]]))
    if sup_status: action_parts.append(sup_status)
    if not action_parts:
        if GUIDANCE.exists(): GUIDANCE.unlink()
        return None
    msg = "; ".join(action_parts)
    if check_rate(): return "rate_limited"
    if GUIDANCE.exists():
        try:
            if json.load(open(GUIDANCE,'r',encoding='utf-8')).get("action") == msg: return "dedup"
        except: pass
    import re as _re
    _hours = []
    for s in stale:
        m = _re.search(r'(\d+)h\)', s)
        if m: _hours.append(int(m.group(1)))
    max_h = max(_hours) if _hours else 0
    tpl = TEMPLATES[hash(f"{now_iso()}_{WINDOW_ID}") % len(TEMPLATES)]
    txt = tpl.format(action=msg, files=",".join([s.split("(")[0] for s in stale[:3]]), hours=max_h)
    # 写文件 (SessionStart读取)
    GUIDANCE.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"action": txt[:300], "ts": now_iso(), "window": WINDOW_ID, "files": stale, "bid_winner": bid["winner"]},
              open(GUIDANCE,'w',encoding='utf-8'), ensure_ascii=False, indent=2)
    with open(RATE_FILE,'w') as f: f.write(str(time.time()))

    # stdout JSON — PostToolUse可消费,注入模型上下文 (@fixed 2026-07-25)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"[脑区调度] {txt[:200]} (窗口={WINDOW_ID})"
        }
    }, ensure_ascii=False))

    return txt[:100]

def broadcast(bid):
    bus = ROOT / "data" / "flows" / "voice_signal.jsonl"
    msg = {"_ts": now_iso(), "_id": f"bid_{int(time.time()*1000)}", "from": bid["winner"], "to": "all",
           "type": "salience_winner", "text": f"赢家:{bid['winner']} 皮层={bid['scores']['cortex']} 海马={bid['scores']['hippocampus']} 丘脑={bid['scores']['thalamus']} 脑干={bid['scores']['brainstem']}", "priority": 2}
    try:
        bus.parent.mkdir(parents=True, exist_ok=True)
        with open(bus, "a", encoding="utf-8") as f: f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    except: pass

def rotate_telemetry():
    """全量日志旋转 — 防止无界增长 (@expanded 2026-07-24)"""
    # 各日志文件的旋转配置: (路径, max_lines, keep_lines)
    rotations = [
        (TELEMETRY, 200, 100),                               # brain_telemetry
        (ROOT / "data" / "flows" / "voice_signal.jsonl", 5000, 2000),   # voice_signal (最胖)
        (ROOT / "data" / "symbolic_dynamics" / "alerts.jsonl", 3000, 1000),
        (ROOT / "data" / "symbolic_dynamics" / "observations" / "retrieval.jsonl", 3000, 1000),
        (ROOT / "data" / "symbolic_dynamics" / "observations" / "operations.jsonl", 2000, 500),
        (ROOT / "state" / "trajectory.jsonl", 1000, 500),
        (ROOT / "data" / "state" / "cog_telemetry.jsonl", 500, 200),
        (ROOT / "data" / "symbolic_dynamics" / "auto_capture_log.jsonl", 200, 100),
        (ROOT / "data" / "symbolic_dynamics" / "judge_log.jsonl", 200, 100),
    ]
    for filepath, max_lines, keep_lines in rotations:
        if not filepath.exists():
            continue
        try:
            lines = open(filepath, 'r', encoding='utf-8').readlines()
            if len(lines) > max_lines:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-keep_lines:])
        except Exception:
            pass

def _compute_symbolic_health() -> dict:
    """轻量级符号动力学健康计算 (替代退役supervisor, @added 2026-07-21)

    不导入重依赖(symbolic_dynamics_engine依赖numpy/scipy),
    纯文件IO统计: 告警数、观测活跃度、域覆盖率。
    耗时 <2ms。
    """
    now = time.time()
    result = {"ts": now_iso(), "healthy": True, "domains": {}, "alerts_24h": 0, "observer_active": False}

    # 告警统计 (最近24h)
    alert_file = ROOT / "data" / "symbolic_dynamics" / "alerts.jsonl"
    if alert_file.exists():
        cutoff = now - 86400
        try:
            with open(alert_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        rec = json.loads(line.strip())
                        ts = rec.get("ts", "")
                        if ts:
                            # 简单解析ISO时间戳
                            from datetime import datetime as _dt
                            t = _dt.fromisoformat(ts).timestamp()
                            if t > cutoff:
                                result["alerts_24h"] += 1
                    except: pass
        except: pass

    # 观测活跃度 (operations最近1h)
    ops_file = ROOT / "data" / "symbolic_dynamics" / "observations" / "operations.jsonl"
    if ops_file.exists():
        ops_age = (now - ops_file.stat().st_mtime) / 60
        result["observer_active"] = ops_age < 60
        result["observer_stale_min"] = round(ops_age, 1)

    # 域覆盖率
    domain_dir = ROOT / "data" / "symbolic_dynamics" / "domains"
    if domain_dir.exists():
        for f in domain_dir.glob("*.json"):
            age_h = (now - f.stat().st_mtime) / 3600
            result["domains"][f.stem] = {"stale_h": round(age_h, 1), "active": age_h < 2}

    # 健康判定: 任一指标不达标则 unhealthy
    if result["alerts_24h"] > 1000:
        result["healthy"] = False
        result["reason"] = f"告警洪泛: {result['alerts_24h']}条/24h"
    elif not result["observer_active"]:
        result["healthy"] = False
        result["reason"] = "observer不活跃"
    elif sum(1 for d in result["domains"].values() if d["active"]) == 0:
        result["healthy"] = False
        result["reason"] = "零活跃域"

    return result

def tick():
    """PostToolUse 每轮调用。开销 <5ms, 10轮执行一次检查。"""
    if not check_cooldown(): return None
    try:
        st = freshness()

        # 真实符号动力学健康计算 (替代退役supervisor, @added 2026-07-21)
        sh_data = _compute_symbolic_health()
        sh_file = ROOT / "state" / "symbolic_health.json"
        try:
            sh_file.parent.mkdir(parents=True, exist_ok=True)
            json.dump(sh_data, open(sh_file, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        except: pass

        if not sh_data.get("healthy"):
            sup = sh_data.get("reason", "symbolic_unhealthy")
        else:
            sup = None

        bid = bidding(st)
        written = write_guidance(st, sup, bid)
        broadcast(bid)
        result = {"stale": st, "bid_winner": bid["winner"], "bid_scores": bid["scores"],
                  "symbolic_health": sh_data.get("healthy"), "written": written,
                  "window": WINDOW_ID, "ts": now_iso()}
    except Exception as e:
        result = {"error": str(e)[:100], "ts": now_iso()}
    TELEMETRY.parent.mkdir(parents=True, exist_ok=True)
    with open(TELEMETRY, 'a', encoding='utf-8') as f:
        f.write(json.dumps(result, ensure_ascii=False) + '\n')
    alert_file = ROOT / "data" / "symbolic_dynamics" / "alerts.jsonl"
    try:
        alert_age = (time.time() - alert_file.stat().st_mtime) / 3600 if alert_file.exists() else 999
        if alert_age > 2:
            alert_file.parent.mkdir(parents=True, exist_ok=True)
            msg = json.dumps({"ts": now_iso(), "type": "heartbeat", "domain": "cls_brain"}, ensure_ascii=False)
            with open(alert_file, "a", encoding="utf-8") as af: af.write(msg + "\n")
    except: pass
    _run_anchor_crawler()  # ANCHOR 爬取（内容变化驱动，无变化零消耗）
    rotate_telemetry()
    return result

# ═══════════════════════════════════════════════════════
# boot() — SessionStart 调用: auto_recall 恢复
# ═══════════════════════════════════════════════════════
def _auto_repair(stale_files):
    repaired = []
    for s in stale_files:
        name = s.split("(")[0]
        fp, d = None, None
        if name == "active_context":
            fp = ROOT / "state" / "active_context.json"
            d = {"current_focus": "CLS auto-repair", "domain": "general", "updated_at": now_iso(), "_meta": {"auto_repaired": True}}
        elif name == "session_memory":
            fp = ROOT / "state" / "session_memory.md"
            d = "<!-- Session Memory " + now_iso() + " -->\n## Auto-repaired\n" + now_iso() + "\n"
        elif name == "cog_step":
            # @fix 2026-08-21 maintainer定: cog_step 不做 auto_repair
            # 原因: auto_repair 写入的文件会被 PreToolUse CHECK 15 视为"已声明"，
            # 导致 AI 从未调用 cog-step-declare 就能通过闸门，认知步骤声明形同虚设。
            # cog_step 是 AI 主动声明认知步骤的闸门，不是普通状态文件，不能自动补。
            # 此处跳过，让 CHECK 15 deny → AI 被迫调 cog-step-declare → 闸门生效。
            continue
        if fp and d:
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(d, str):
                    with open(fp, "w", encoding="utf-8") as fh: fh.write(d)
                else:
                    json.dump(d, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                repaired.append(name)
            except: pass
    return repaired

def boot():
    lines = []
    if GUIDANCE.exists():
        try:
            g = json.load(open(GUIDANCE, "r", encoding="utf-8"))
            lines.append("[brain.boot] " + g.get("action", "?")[:200])
        except: pass

    # ── FAISS 索引自动构建 (@added 2026-07-21) ──
    faiss_index = ROOT / "data" / "search_index" / "vectors.faiss"
    if not faiss_index.exists():
        lines.append("[brain.boot] FAISS索引缺失, 触发后台构建...")
        try:
            import subprocess
            idx_script = ROOT / "scripts" / "semantic_index.py"
            if idx_script.exists():
                # pythonw 防弹窗: 替代 sys.executable(python.exe→conhost)
                pyw = sys.executable.replace("python.exe", "pythonw.exe")
                subprocess.Popen(
                    [pyw, str(idx_script)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    cwd=str(ROOT), creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                )
                lines.append("[brain.boot] FAISS后台构建已启动")
        except Exception as e:
            lines.append(f"[brain.boot] FAISS构建触发失败: {e}")

    st = freshness()
    if st:
        repaired = _auto_repair(st)
        if repaired:
            lines.append("[brain.boot] repaired: " + ", ".join(repaired))
            st2 = freshness()
            still = [s for s in st2 if s.split("(")[0] not in repaired]
            if still:
                lines.append("[brain.boot] still stale: " + ", ".join(still[:3]))
            else:
                lines.append("[brain.boot] all clear")
        else:
            lines.append("[brain.boot] stale: " + ", ".join(st[:4]))
    return chr(10).join(lines) if lines else None


# ── anchor_crawler 挂载 (2026-08-21) ──
# 每次 heartbeat 顺带触发 ANCHOR 爬取（内容变化驱动，无变化零消耗）
def _run_anchor_crawler():
    try:
        ac_script = ROOT / "scripts" / "wheels" / "anchor_crawler.py"
        if ac_script.exists():
            import subprocess
            pyw = sys.executable.replace("python.exe", "pythonw.exe")
            subprocess.Popen(
                [pyw, str(ac_script), "run"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(ROOT),
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
    except Exception:
        pass


if __name__ == "__main__":
    if "--boot" in sys.argv:
        result = boot()
        if result: print(result)
    else:
        result = tick()
        if result: print(json.dumps(result, ensure_ascii=False, indent=2))
