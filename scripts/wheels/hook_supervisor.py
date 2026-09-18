#!/usr/bin/env python3
"""hook_supervisor.py — PostToolUse 轻量监督器 v2
v2 修复:
  ①反习得性忽略: 10模板轮换 (每轮不同表述)
  ②多窗口隔离: window_id 写入 guidance, SessionStart 可识别来源
  ③内容边界: guidance ≤300 chars | telemetry 旋转 >200条
"""

import json, os, sys, time, uuid, random
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
GUIDANCE = ROOT / ".claude" / "cls_state" / ".guidance_injection"
COOLDOWN = ROOT / "data" / "state" / ".hook_supervisor_cooldown"
RATE_FILE = ROOT / "data" / "state" / ".hook_supervisor_rate"
TELEMETRY = ROOT / "data" / "state" / "hook_supervisor_telemetry.jsonl"

# Window ID (from env or random)
WINDOW_ID = os.environ.get("CLAUDE_CODE_SESSION_ID", uuid.uuid4().hex[:12])[:12]

def now_iso(): return datetime.now(timezone.utc).isoformat()

# ① 反习得性忽略: 10模板轮换
TEMPLATES = [
    "[认知监督] {action}",
    "[系统心跳] 检测到: {action}",
    "[循环监控] {action} — 已持续{hours}小时",
    "[CLS体检] {files} 需要关注",
    "[自动巡检] {action}. 优先级: 认知循环",
    "[状态追踪] 距离上次修复已过{hours}h: {action}",
    "[守护进程] {action} (自动检测,非阻塞)",
    "[周期检查] {files} 过期.",
    "[后台<传感器>] 捕获到: {action}",
    "[静默提醒] 你上次修这些文件是{hours}小时前. {action}",
]

def check_cooldown():
    count = 0
    if COOLDOWN.exists():
        try: count = json.load(open(COOLDOWN)).get("count", 0)
        except: pass
    count += 1
    json.dump({"count": count, "ts": now_iso()}, open(COOLDOWN, 'w'))
    return (count % 10 == 0)

def check_rate_limit():
    if RATE_FILE.exists():
        try:
            last = float(open(RATE_FILE).read().strip())
            if time.time() - last < 3600:
                return True
        except: pass
    return False

def update_rate_limit():
    with open(RATE_FILE, 'w') as f: f.write(str(time.time()))

def check_freshness():
    checks = {"active_context(1)": "state/active_context.json",
              "session_memory(5)": "state/session_memory.md",
              "cog_step(2)": "data/state/cog_step.json",
              "trajectory(6)": "state/trajectory.json"}
    stale = []
    now = time.time()
    for name, path in checks.items():
        fp = ROOT / path
        if not fp.exists(): stale.append(f"{name}(缺失)"); continue
        age_h = (now - fp.stat().st_mtime) / 3600
        max_age = 0.17 if "cog_step" in name else 24
        if age_h > max_age: stale.append(f"{name}({age_h:.0f}h)")
    return stale

def check_supervisor():
    health = ROOT / "state" / "symbolic_health.json"
    if not health.exists(): return "supervisor_never_ran"
    try:
        age_h = (time.time() - health.stat().st_mtime) / 3600
        if age_h > 2: return f"supervisor_stale({age_h:.0f}h)"
    except: pass
    return None

def write_guidance(stale_files, supervisor_status):
    if not stale_files and not supervisor_status:
        if GUIDANCE.exists(): GUIDANCE.unlink()
        return None

    # Dedup: same files + same supervisor status → skip
    action_key = f"{sorted(stale_files)}_{supervisor_status}"
    if GUIDANCE.exists():
        try:
            old = json.load(open(GUIDANCE, 'r', encoding='utf-8'))
            if old.get("_dedup") == action_key:
                return "dedup_skipped"
        except: pass

    # Rate limit: max 1 write per hour (unless supervisor just came back from dead)
    if check_rate_limit() and supervisor_status == (old.get("supervisor") if GUIDANCE.exists() and old else None):
        return "rate_limited"

    # Max hours for template
    max_h = 0
    for s in stale_files:
        import re
        m = re.search(r'(\d+)h', s)
        if m: max_h = max(max_h, int(m.group(1)))

    # ① Build message with template
    files_str = ", ".join([s.split("(")[0] for s in stale_files[:3]]) if stale_files else "all_ok"
    action_str = "过期:" + files_str if stale_files else "全部正常"
    if supervisor_status: action_str += "; " + supervisor_status

    template_idx = hash(f"{now_iso()}_{WINDOW_ID}") % len(TEMPLATES)
    template = TEMPLATES[template_idx]
    msg = template.format(action=action_str, files=files_str, hours=max_h)

    # ② Multi-window: tag with window_id
    guidance_data = {
        "action": msg[:300],
        "ts": now_iso(),
        "window": WINDOW_ID,
        "files": stale_files,
        "supervisor": supervisor_status,
        "_dedup": action_key
    }

    GUIDANCE.parent.mkdir(parents=True, exist_ok=True)
    json.dump(guidance_data, open(GUIDANCE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    update_rate_limit()
    return msg[:100]

def rotate_telemetry():
    """③ 内容边界: telemetry >200条 → 旋转"""
    if TELEMETRY.exists():
        try:
            lines = open(TELEMETRY).readlines()
            if len(lines) > 200:
                # Keep last 100
                with open(TELEMETRY, 'w') as f:
                    f.writelines(lines[-100:])
        except: pass

def main():
    if not check_cooldown(): return
    try:
        stale = check_freshness()
        sup = check_supervisor()
        written = write_guidance(stale, sup)
        result = {"stale": stale, "supervisor": sup, "written": written,
                  "window": WINDOW_ID, "ts": now_iso()}
    except Exception as e:
        result = {"error": str(e)[:100], "ts": now_iso()}
    # 竞价: 4脑区打分→赢家广播 (零额外API调用,纯本地计算)
    try:
        bid = _salience_bidding()
        _broadcast_winner(bid)
        result["bid_winner"] = bid["winner"]
        result["bid_scores"] = bid["scores"]
    except Exception as e:
        result["bid_error"] = str(e)[:80]

    TELEMETRY.parent.mkdir(parents=True, exist_ok=True)
    with open(TELEMETRY, 'a', encoding='utf-8') as f:
        f.write(json.dumps(result, ensure_ascii=False) + '\n')
    rotate_telemetry()

if __name__ == "__main__":
    main()

# ═══════════════════════════════════════════════════════
# 竞价积分 (2026-07-19): 4脑区打分→最高分广播→全局协调
# ═══════════════════════════════════════════════════════

def _salience_bidding():
    """4脑区显著性竞价: 各自打分→赢家广播到voice_signal总线"""
    now = time.time()
    scores = {}

    # 皮层(cortex): assistant活跃度 — 最近工具调用频率
    telemetry = ROOT / "data" / "state" / "hook_supervisor_telemetry.jsonl"
    cortex_score = 0.5  # baseline
    if telemetry.exists():
        try:
            lines = open(telemetry).readlines()
            recent = [l for l in lines[-20:] if l.strip()]
            if recent:
                # 最近20次监督触发中dedup(状态未变)=皮层安分, written(状态变了)=皮层活跃
                active_count = sum(1 for l in recent if '"written":"dedup_skipped"' not in l)
                cortex_score = 0.3 + 0.7 * (active_count / len(recent))
        except:
            pass
    scores["cortex"] = round(cortex_score, 2)

    # 海马体(hippocampus): 记忆活性 — FAISS索引新鲜度
    faiss_v = ROOT / "data" / "search_index" / "vectors.faiss"
    hippo_score = 0.3
    if faiss_v.exists():
        age_h = (now - faiss_v.stat().st_mtime) / 3600
        hippo_score = max(0.2, 1.0 - age_h / 48)  # 48h衰减
    scores["hippocampus"] = round(hippo_score, 2)

    # 丘脑(thalamus): 感觉流 — voice_signal总线最后写入时间
    bus = ROOT / "data" / "flows" / "voice_signal.jsonl"
    thalamus_score = 0.3
    if bus.exists():
        age_m = (now - bus.stat().st_mtime) / 60
        thalamus_score = max(0.2, 1.0 - age_m / 10)  # 10min衰减
    scores["thalamus"] = round(thalamus_score, 2)

    # 脑干(brainstem): 调控紧迫度 — 文件过期程度+escalation
    brainstem_score = 0.4
    stale = check_freshness()
    if stale:
        brainstem_score = min(0.9, 0.4 + 0.1 * len(stale))
    esc_file = ROOT / "data" / "state" / "freshness_escalation.json"
    if esc_file.exists():
        try:
            esc = json.load(open(esc_file))
            if esc.get("escalated"):
                brainstem_score = 1.0  # escalation = 最高优先级
        except:
            pass
    scores["brainstem"] = round(brainstem_score, 2)

    # 找出赢家
    winner = max(scores, key=scores.get)
    return {"scores": scores, "winner": winner, "ts": now_iso()}


def _broadcast_winner(bidding_result):
    """将竞价赢家写入voice_signal总线"""
    bus = ROOT / "data" / "flows" / "voice_signal.jsonl"
    winner = bidding_result["winner"]
    scores = bidding_result["scores"]
    msg = {
        "_ts": now_iso(),
        "_id": f"bid_{int(time.time()*1000)}",
        "from": winner,
        "to": "all",
        "type": "salience_winner",
        "text": f"竞价赢家: {winner} (皮层={scores['cortex']} 海马={scores['hippocampus']} 丘脑={scores['thalamus']} 脑干={scores['brainstem']})",
        "priority": 2
    }
    try:
        bus.parent.mkdir(parents=True, exist_ok=True)
        with open(bus, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    except:
        pass
