#!/usr/bin/env python3
"""semantic_inject.py — 语义漂移检测 + 推理路由注入
被 UserPromptSubmit hook 调用，返回 additionalContext JSON

v4 (2026-07-26): 漂移检测 + 锚点展示
  - L0: 关键词 Jaccard (<1ms) + Ollama embedding tiebreaker (~8ms)
  - 漂移告警附带锚点标题，一眼定位偏离方向
  - L1 语义分析 (Ollama deepseek-r1:8b) 已预留接口，待加速后启用
"""

import json, sys, os, re, time, math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "wheels"))

def _echo_inject(text: str) -> str:
    """注入内容打到屏幕供人类直接审计。
    官方通道: hook JSON 的 systemMessage 字段 (CC 会显示给用户).
    CC hook 进程无控制终端, stderr 不显示(仅进 debug log), 不能走 stderr.
    @since: 2026-08-01 (废弃 stderr 方案, 改用 systemMessage)"""
    return text[:2000]  # systemMessage 全量显示, 截断到 2000 字防过载

def _get_session_id():
    """按session隔离状态文件"""
    sid = os.environ.get("CLAUDE_SESSION_ID", "")
    if sid: return sid[:16]
    # fallback: 进程ID (最后一次)
    return f"proc_{os.getpid()}"

def _anchor_file():
    """每个session独立锚点文件 (运行时调用, 不固化)
    @fix 2026-08-01: 原 ANCHOR_FILE 模块级固化在 import 时执行, 此时
    CLAUDE_SESSION_ID env 尚未由 main() 设置 → 回退 proc_{pid} → 每轮新进程
    新锚点文件(99个proc_僵尸) → 漂移检测从未真正对比历史 / pid复用读错窗口。
    改为每次加载/保存时现算路径, env 在 main() 里先设(check_drift 在其后调用)。"""
    sid = _get_session_id()[:8]
    return ROOT / "data" / "state" / f"drift_anchor_{sid}.json"
DRIFT_LOG = ROOT / "data" / "state" / "drift_log.jsonl"
DRIFT_INJECT_COOLDOWN = 300   # 漂移注入冷却: 同session 5min内最多注入一次 (2026-08-01 用户反馈"每次都注入")
OPS_INJECT_TTL = 1800         # Ops注入去重: 同 reasons 30min内不重复 (告知一次, 防cron刷屏)
SF_EMBED_MODEL = "BAAI/bge-m3"      # 硅基流动免费 embedding (2026-08-01 替换 Ollama, 稳定)
DS_FLASH_MODEL = "deepseek-v4-flash" # DS Flash 兜底 (带 userid 保证内容纯净)
EMBED_TIMEOUT = 8.0
ANCHOR_TTL = 6 * 3600
JACCARD_WARN = 0.15
JACCARD_ALERT = 0.05
COSINE_ALERT = 0.40

# ── 无人状态机 v2 (2026-08-01 重构, Opus修正版) ──
# AUTONOMY_STATE 保留: legacy 读 + 全局镜像写 (Stop.ps1/cognitive_gate/SessionStart 兼容)
AUTONOMY_STATE = ROOT / "data" / "state" / "autonomy_state.json"
UNATTENDED_THRESHOLD = 600     # 距最后人类输入 >600s 且工具活跃 → 候选 unattended
STALE_ANCHOR_TTL = 7200        # 距 last_human_input_ts >7200s → 锚失效静默 (盲区1时效回退)
TOOL_ACTIVE_WINDOW = 300       # 工具活跃窗口: last_tool_time > now-300
LAYER0_WEIGHT = 0.05           # Layer0命中 + Layer1 normal → cosine 阈值 -0.05 (加权因子)
AUTO_SF_CONFIRM_MODEL = "Qwen/Qwen2.5-7B-Instruct"  # 供 _prompt_is_human_like (可选 /loop 人机闸门)

def check_tier(prompt_text):
    text = prompt_text[:500]
    patterns = {
        "L0": [r"分类", r"归类", r"摘要", r"总结", r"概括", r"打标签",
               r"提取.{0,5}(标题|名字|名称)"],
        "L2": [r"(代码|函数).{0,5}(审查|审计|review)", r"(refactor|重构|优化).{0,10}(代码|函数)",
               r"review.{0,10}(代码|函数|性能)", r"(性能|bug|漏洞).{0,5}(分析|检查)"],
        "L3": [r"(写|生成|创建).{0,15}(脚本|代码|程序|函数)", r"帮我.{0,15}(写|做|生成|创建)", r"(build|构建|开发)"],
        "L4": [r"(物理|数学|推导|公式|仿真|PIC|等离子体|推力|磁场|电推)",
               r"(设计|建模|分析|诊断|评估|比较|区别|优化)", r"为什么", r"原理", r"机制",
               r"关系", r"影响", r"趋势"],
    }
    for t, pats in patterns.items():
        for p in pats:
            if re.search(p, text):
                return {"tier": t, "reason": "regex_match", "confidence": 0.85}
    return {"tier": "L4", "reason": "no_match_default", "confidence": 0.6}

def _embed_sf(text):
    """embedding: 硅基流动免费模型 BAAI/bge-m3 (2026-08-01 替换 Ollama, 稳定)"""
    import urllib.request
    try:
        sf_cfg = json.loads((ROOT / "keys" / "siliconflow_config.json").read_text(encoding="utf-8"))
        body = json.dumps({"model": SF_EMBED_MODEL, "input": [text[:800]]}).encode()
        req = urllib.request.Request(
            sf_cfg["base_url"].rstrip("/") + "/embeddings", data=body,
            headers={"Authorization": f"Bearer {sf_cfg['api_key']}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        if data.get("data") and len(data["data"]) > 0:
            return data["data"][0].get("embedding")
    except Exception:
        pass
    return None


def _ds_flash_chat(system, user, max_tokens=64):
    """DS Flash 兜底 chat (带 userid 保证内容纯净/审计追踪, 2026-08-01)

    deepseek-v4-flash 为 reasoning 模型: 输出先走 reasoning_content, 再写 content。
    max_tokens 必须给足让推理走完 (过小 → finish_reason=length, content='')。
    content 空 → 返回 None (fail-open, 不误判 reasoning_content 里的中文推理)。
    """
    import urllib.request
    try:
        ds_cfg = json.loads((ROOT / "keys" / "deepseek_config.json").read_text(encoding="utf-8"))
        body = json.dumps({
            "model": DS_FLASH_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens, "temperature": 0.0,
            "user": "cls-xumo-symbolic-dynamics",  # userid: 标识来源, 防内容混淆
        }).encode()
        req = urllib.request.Request(
            ds_cfg["base_url"].rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {ds_cfg['api_key']}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            msg = json.loads(resp.read().decode())["choices"][0]["message"]
        text = msg.get("content") or ""
        return text.strip() or None
    except Exception:
        return None


def _drift_llm_fallback(anchor_goal, prompt_text):
    """embedding 不可用时的语义漂移兜底: DS Flash 判断 (userid 保证内容纯净)"""
    label = _ds_flash_chat(
        "判断'当前输入'是否偏离'目标话题'。只输出 drift 或 stay。",
        f"目标话题: {anchor_goal[:80]}\n当前输入: {prompt_text[:120]}")
    if label is None:
        return None
    low = label.lower()
    if "drift" in low or "偏离" in low:
        return True
    if "stay" in low:
        return False
    return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b): return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0: return 0.0
    return dot / (na * nb)

_STOP = set("的了吗呢啊吧呀在是与不这和那它你我他她很可以就也要把被让给向到"
    "对为因所但虽然而且或如果因为所以不过只是然后应该能够需要已经可能"
    "比较非常更最特别尤其大概也许似乎一定必须务必尽管无论")

def _tokenize(text):
    tokens = set()
    for i in range(len(text) - 1):
        bg = text[i:i+2]
        if all('一' <= c <= '鿿' or c.isalpha() for c in bg):
            tokens.add(bg)
    for c in text:
        if '一' <= c <= '鿿' and c not in _STOP: tokens.add(c)
    for w in re.findall(r'[a-zA-Z]{2,}', text): tokens.add(w.lower())
    return tokens

def _jaccard(a, b):
    ta = _tokenize(a[:300]); tb = _tokenize(b[:300])
    if not ta or not tb: return 0.5
    inter = len(ta & tb); union = len(ta | tb)
    return inter / union if union > 0 else 0.0

def _log_drift(entry: dict):
    """追加漂移日志 (JSONL)"""
    try:
        DRIFT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DRIFT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + chr(10))
    except Exception:
        pass


def _cleanup_stale_anchors():
    """清理超24h的其他session锚点文件"""
    now = time.time()
    for f in (ROOT / "data" / "state").glob("drift_anchor_*.json"):
        if now - f.stat().st_mtime > 86400:
            try: f.unlink()
            except: pass


# ── 无人状态机 v2 (2026-08-01 重构, Opus修正版) ─────────────────────
# 修复: ① frozen_goal 原取 history[0](最旧) → 改人类最后消息 (Opus点2)
#       ② 计时器法原用 drift_log 平均间隔 → 改持久化 last_human_input_ts (防崩溃)
#       ③ _tool_active 按 session 过滤, 修跨窗口串号泄漏 (窗口A看到窗口B工具活跃 → 误判)

def _autonomy_state_file() -> Path:
    """无人状态机 per-session 状态文件 (窗口隔离P0, 与 _anchor_file/_prompt_history_file 一致)"""
    sid = _get_session_id()[:8]
    return ROOT / "data" / "state" / f"autonomy_state_{sid}.json"


def _parse_iso_ts(ts: str) -> float:
    """解析 ISO 时间戳 → epoch; 失败返回 0"""
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _load_autonomy_state() -> dict:
    """读无人状态机状态。per-session 优先; 缺失走 legacy 全局迁移。

    迁移: 旧全局 autonomy_state.json (autonomous=true 且 session==当前) →
    转 per-session {state:unattended, last_human_input_ts:detected_at} →
    立即写 per-session + 镜像全局。session 不符 / autonomous=false → 默认 active。"""
    f = _autonomy_state_file()
    if f.exists():
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
            st.setdefault("session", _get_session_id()[:8])
            return st
        except Exception:
            pass
    if AUTONOMY_STATE.exists():
        try:
            old = json.loads(AUTONOMY_STATE.read_text(encoding="utf-8"))
            if old.get("autonomous") and old.get("session") == _get_session_id()[:8]:
                st = {
                    "state": "unattended",
                    "last_human_input_ts": _parse_iso_ts(old.get("detected_at", "")),
                    "last_human_input_preview": (old.get("frozen_goal") or "")[:200],
                    "frozen_goal": (old.get("frozen_goal") or "")[:200],
                    "session": old.get("session", ""),
                    "version": 7,
                }
                _save_autonomy_state(st)
                return st
        except Exception:
            pass
    return {"state": "active", "last_human_input_ts": 0.0,
            "last_human_input_preview": "", "session": _get_session_id()[:8], "version": 7}


def _save_autonomy_state(st: dict):
    """双写: per-session 全量 + 全局镜像 (保 Stop.ps1/cognitive_gate/SessionStart 兼容)"""
    f = _autonomy_state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)
    mirror = {
        "autonomous": st.get("state") == "unattended",
        "detected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "frozen_goal": (st.get("frozen_goal") or st.get("last_human_input_preview") or "")[:200],
        "session": st.get("session") or _get_session_id()[:8],
    }
    AUTONOMY_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp2 = AUTONOMY_STATE.with_suffix(".tmp")
    tmp2.write_text(json.dumps(mirror, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp2, AUTONOMY_STATE)


def _tool_active(now: float) -> bool:
    """本窗口最近 TOOL_ACTIVE_WINDOW 内是否有工具活跃。
    @fix 2026-08-01: 过滤 session==当前, 修跨窗口串号泄漏"""
    ops_file = ROOT / "data" / "state" / "ops_freq.jsonl"
    if not ops_file.exists():
        return False
    cur_sid = _get_session_id()[:8]
    try:
        last_ts = 0.0
        for line in open(ops_file, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            if e.get("session", "") != cur_sid:
                continue
            ts = e.get("ts", 0) or 0
            if ts > last_ts:
                last_ts = ts
        return last_ts > now - TOOL_ACTIVE_WINDOW
    except Exception:
        return False


def _invalidate_anchor():
    """人类回归 → 旧冻结锚作废 (防重启后旧锚判所有操作漂移, 盲区1)"""
    try:
        af = _anchor_file()
        if af.exists():
            af.unlink()
    except Exception:
        pass


def _snapshot_anchor(st) -> str:
    """进入无人态时一次性快照锚: 人类最后消息 + 最近3条人类上下文(600字截断) + embedding。
    完全快照、不可变(frozen)、纯人类消息派生 — 禁止从 drift_log/ops_freq 动态读最新数据构建
    (Opus攻击点1 锚点同化 + 盲区3 语义泛化)。仅人类新输入重建。"""
    goal = (st.get("last_human_input_preview") or "")[:200]
    if not goal:
        return ""
    hist = _load_prompt_history()
    context = (" | ".join(hist[-3:]))[:600]
    vec = _embed_sf((goal + " " + context)[:800])
    anchor = {
        "goal": goal,
        "context": context,
        "embedding": vec,
        "created_at": time.time(),
        "session": _get_session_id()[:8],
        "version": 7,
        "frozen": True,
        "snapshot_of": "last_human_input",
        "last_human_input_ts": st.get("last_human_input_ts", 0),
    }
    af = _anchor_file()
    af.parent.mkdir(parents=True, exist_ok=True)
    tmp = af.with_suffix(".tmp")
    tmp.write_text(json.dumps(anchor, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, af)
    return goal


def _layer0_summary(layer0) -> str:
    """Layer0 标记摘要 (仅 reasons, 供 drift_log 审计, 不塞 seq 长文)"""
    if not layer0:
        return ""
    reasons = layer0.get("reasons") or []
    return "、".join(str(r) for r in reasons[:3])[:60]


def _run_drift_guard(prompt_text, state, is_system_msg, layer0, loop_res, base_alert):
    """无人态漂移守护 (v7 重构):
    - 仅 unattended 态比对; active 态省 embedding API (人类在场不需要机器判漂移)
    - 进入无人态首轮不比 (锚刚固化, 无基线)
    - Layer1 loop → 短路判漂移 (最强信号, 工具级循环)
    - 系统巡检/心跳 → housekeeping_skip (调度性家务非任务漂移, 不比)
    - 其他 (/loop同任务重注) → cosine 主判据 (Layer0命中→阈值-0.05) → Jaccard降级 → DS兜底
    """
    sid8 = _get_session_id()[:8]
    if state.get("state") != "unattended":
        return {"drifted": False, "reason": "active_skip", "score": None, "method": "skip", "anchor_goal": ""}
    if state.get("just_unattended"):
        return {"drifted": False, "reason": "anchor_fresh", "score": None, "method": "skip", "anchor_goal": ""}
    anchor = _load_anchor()
    if anchor is None:
        _log_drift({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": sid8, "state": "unattended",
                    "tier": check_tier(prompt_text).get("tier", ""), "overlap": None, "drifted": False,
                    "method": "skip", "layer0": _layer0_summary(layer0), "is_system_prompt": is_system_msg,
                    "anchor_goal": "", "prompt_preview": prompt_text[:80], "reason": "no_frozen_anchor"})
        return {"drifted": False, "reason": "no_frozen_anchor", "score": None, "method": "skip", "anchor_goal": ""}
    anchor_goal = anchor.get("goal", "")[:200]
    # Layer1 短路: 工具级循环 = 漂移 (最强信号, 不依赖 cosine)
    if loop_res and loop_res.get("verdict") == "loop":
        _log_drift({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": sid8, "state": "unattended",
                    "tier": check_tier(prompt_text).get("tier", ""), "overlap": None, "drifted": True,
                    "method": "layer1_loop", "layer0": _layer0_summary(layer0), "is_system_prompt": is_system_msg,
                    "anchor_goal": anchor_goal, "prompt_preview": prompt_text[:80], "reason": "layer1_loop"})
        return {"drifted": True, "reason": "layer1_loop", "score": None, "method": "layer0", "anchor_goal": anchor_goal}
    # 系统巡检/心跳 (cron/定时巡检/ScheduleWakeup...): 调度性家务, 非任务漂移, 不比 (防每5min轰炸)
    if is_system_msg:
        return {"drifted": False, "reason": "housekeeping_skip", "score": None, "method": "skip", "anchor_goal": ""}
    # 非系统到达此处 = /loop 同任务重注 (异任务非系统已在状态机转 active → 人类回归)
    effective = base_alert
    if layer0 and loop_res and loop_res.get("verdict") == "normal":
        effective = base_alert - LAYER0_WEIGHT   # Opus点4: Layer0 命中 + 语义 normal → 阈值 -0.05
    result = check_drift_real(prompt_text, cosine_alert=effective)
    _log_drift({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": sid8, "state": "unattended",
                "tier": check_tier(prompt_text).get("tier", ""), "overlap": result.get("score"),
                "drifted": result["drifted"], "method": result.get("method", ""),
                "layer0": _layer0_summary(layer0), "is_system_prompt": False,
                "anchor_goal": result.get("anchor_goal", "")[:80], "prompt_preview": prompt_text[:80],
                "reason": result.get("reason", "")})
    return result


def _load_anchor():
    """加载当前session冻结锚。校验 (v7):
    1. ANCHOR_TTL 有效期 (原逻辑)
    2. session ownership (原逻辑)
    3. frozen 校验: 非冻结锚(旧 version6 自演化锚) → 废弃 None (迁移期, 下个无人态重新快照)
    4. 时效回退 (盲区1): 距 last_human_input_ts > STALE_ANCHOR_TTL → 锚失效静默, 防重启后旧锚判所有操作漂移"""
    af = _anchor_file()
    if not af.exists(): return None
    try:
        anchor = json.loads(af.read_text(encoding="utf-8"))
        if time.time() - anchor.get("created_at", 0) > ANCHOR_TTL: return None
        cur = _get_session_id()[:8]
        st = anchor.get("session", "")
        if cur and st and st != cur: return None   # 其他窗口的锚点, 忽略
        if not anchor.get("frozen"): return None   # 非冻结锚 → 不可用 (v7 只认 frozen 快照)
        lhi = anchor.get("last_human_input_ts", 0)
        if lhi and time.time() - lhi > STALE_ANCHOR_TTL: return None   # 时效回退 (盲区1)
        return anchor
    except Exception: return None

def _save_anchor(goal_text, embedding=None):
    af = _anchor_file()
    af.parent.mkdir(parents=True, exist_ok=True)
    anchor = {"goal": goal_text[:200], "embedding": embedding, "created_at": time.time(),
              "session": _get_session_id()[:8], "version": 7, "frozen": True}
    tmp = af.with_suffix(".tmp")
    tmp.write_text(json.dumps(anchor, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, af)

def _maybe_update_anchor(current_prompt: str) -> bool:
    """[v7 废弃] 锚点不可变, 禁止自演化 (Opus攻击点1 锚点同化: 滑动窗口自指).
    仅人类新输入经 _invalidate_anchor + 重新快照重建. 保留签名防外部调用崩."""
    return False


def check_drift_real(prompt_text, cosine_alert=COSINE_ALERT):
    """Jaccard + embedding cosine 判漂移 (v7)。cosine_alert 参数化 —
    @fix 2026-08-01: 原 main() 算的 _cosine_alert 只用于文案从未传入 (display-only bug),
    现由调用方传 (0.40 + threshold_delta), Layer0 命中时 -LAYER0_WEIGHT。
    无冻结锚 → 不自动建锚 (v7 锚只由无人态快照创建) → no_frozen_anchor。"""
    _cleanup_stale_anchors()
    anchor = _load_anchor()
    if anchor is None:
        return {"drifted": False, "reason": "no_frozen_anchor", "score": None, "method": "skip", "anchor_goal": ""}

    anchor_goal = anchor.get("goal", "")
    overlap = _jaccard(prompt_text, anchor_goal)

    if overlap < JACCARD_ALERT:
        # @fix 2026-08-01: 中文短句天然 Jaccard≈0, 不再直接判漂移 (Opus评审第3点)。
        # 低重叠 → 走 embedding cosine 语义判定; embedding 不可用才降级 jaccard 硬判。
        anchor_vec = anchor.get("embedding")
        if anchor_vec is not None:
            vec = _embed_sf(prompt_text[:500])
            if vec is not None:
                cos = _cosine(vec, anchor_vec)
                if cos < cosine_alert:
                    return {"drifted": True, "reason": f"cosine={cos:.2f}", "score": round(cos,3), "method": "cosine", "anchor_goal": anchor_goal}
                return {"drifted": False, "reason": f"cosine={cos:.2f} embed-stay", "score": round(cos,3), "method": "cosine", "anchor_goal": anchor_goal}
        # embedding 不可用 → SF 语义兜底 (与 warn 分支同款, 消除"中文短句必误报")
        llm_drift = _drift_llm_fallback(anchor_goal, prompt_text)
        if llm_drift is True:
            return {"drifted": True, "reason": "dsflash drift", "score": round(overlap,3), "method": "dsflash", "anchor_goal": anchor_goal}
        if llm_drift is False:
            return {"drifted": False, "reason": f"dsflash stay overlap={overlap:.2f}", "score": round(overlap,3), "method": "dsflash", "anchor_goal": anchor_goal}
        return {"drifted": False, "reason": f"embed-na low-overlap={overlap:.2f}", "score": round(overlap,3), "method": "jaccard", "anchor_goal": anchor_goal}
    elif overlap < JACCARD_WARN:
        anchor_vec = anchor.get("embedding")
        if anchor_vec is not None:
            vec = _embed_sf(prompt_text[:500])
            if vec is not None:
                cos = _cosine(vec, anchor_vec)
                if cos < cosine_alert:
                    return {"drifted": True, "reason": f"cosine={cos:.2f}", "score": round(cos,3), "method": "cosine", "anchor_goal": anchor_goal}
        # embedding 不可用 → DS Flash 语义兜底 (userid 保证内容纯净)
        llm_drift = _drift_llm_fallback(anchor_goal, prompt_text)
        if llm_drift is True:
            return {"drifted": True, "reason": "dsflash drift", "score": round(overlap,3), "method": "dsflash", "anchor_goal": anchor_goal}
        if llm_drift is False:
            return {"drifted": False, "reason": f"dsflash stay overlap={overlap:.2f}", "score": round(overlap,3), "method": "dsflash", "anchor_goal": anchor_goal}
        return {"drifted": False, "reason": f"mild overlap={overlap:.2f}", "score": round(overlap,3), "method": "jaccard", "anchor_goal": anchor_goal}
    else:
        return {"drifted": False, "reason": f"stable overlap={overlap:.2f}", "score": round(overlap,3), "method": "jaccard", "anchor_goal": anchor_goal}

check_drift_fast = check_drift_real

# ── L1 语义分析预留 ──
def _analyze_drift_llm(anchor_goal, prompt_text):
    """L1 三级级联: SF Qwen2.5-7B(免费) → Haiku(teio) → DS Flash兜底(带userid)"""
    import urllib.request

    # Tier 1: 硅基流动 Qwen2.5-7B (免费, ~1s)
    try:
        sf_cfg = json.loads((ROOT / 'keys' / 'siliconflow_config.json').read_text(encoding='utf-8'))
        body = json.dumps({
            'model': 'Qwen/Qwen2.5-7B-Instruct',
            'messages': [
                {'role': 'system', 'content': '你是漂移检测器。对比目标vs输入，用<=12字描述偏离方向(例:编程→生活)。只输出标签，不要对话。'},
                {'role': 'user', 'content': '目标话题:' + anchor_goal[:60] + chr(10) + '当前输入:' + prompt_text[:80] + chr(10) + '偏离标签:'}
            ],
            'max_tokens': 15, 'temperature': 0.1
        }).encode()
        req = urllib.request.Request(sf_cfg['base_url'].rstrip('/') + '/chat/completions', data=body,
            headers={'Authorization': f"Bearer {sf_cfg['api_key']}", 'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=8) as resp:
            label = json.loads(resp.read().decode())['choices'][0]['message']['content'].strip()
        if 2 <= len(label) <= 30:
            return label
    except Exception: pass

    # Tier 2: Claude Haiku via teio (~2.5s)
    try:
        sys.path.insert(0, str(ROOT / 'scripts' / 'wheels'))
        from api_pipeline import call
        result = call('teio', 'claude-haiku-4-5-20251001',
            messages=[
                {'role': 'system', 'content': '你是漂移检测器。只输出一个短标签(<=15字)。不要对话。'},
                {'role': 'user', 'content': '目标话题:' + anchor_goal[:60] + chr(10) + '当前输入:' + prompt_text[:80] + chr(10) + '偏离标签:'}
            ],
            max_tokens=25, temperature=0.1
        )
        if result.get('ok'):
            label = result.get('text', '').strip()
            if 2 <= len(label) <= 30:
                return label
    except Exception: pass

    # Tier 3: DS Flash 兜底 (2026-08-01 替换 Ollama, 带 userid 保证内容纯净)
    try:
        label = _ds_flash_chat(
            '你是漂移检测器。对比两个话题输出偏离标签（<=12字，不要思考）。',
            '锚点:' + anchor_goal[:60] + chr(10) + '输入:' + prompt_text[:80] + chr(10) + '偏离标签:', max_tokens=96)
        if label:
            for pfx in ['偏离标签：', '偏离标签:', '偏离:', '[DRIFT] ']:
                if label.startswith(pfx): label = label[len(pfx):]
            label = label.strip()
            if 2 <= len(label) <= 30:
                return label
    except Exception: pass

    return None

def _shorten_anchor(goal, maxlen=35):
    """截断锚点文本用于注入展示"""
    if len(goal) <= maxlen: return goal
    return goal[:maxlen-3] + "..."


# ── 注入级别标注 ─────────────────────────────────
# @add 2026-08-03 张maintainer决策(参照 Codex 建议): 注入分级, 帮模型分层注意力, 防"看见但没采纳关键信息"。
# 完整中文文字描述, 不用 advisory/required 压缩码。
#   参考型·可忽略 — 背景/建议/提示, 采纳与否自行判断, 不强制行动, 无增量价值可直接忽略
#   行动型·需行动  — 涉及任务正确性/安全关键约束, 请据此调整下一步动作; 已处理可跳过
# 注意: 熔断/前置条件(required 硬闸)不在注入层, 由 PreToolUse deny 实现(未读即拦), 无需模型回执。
# @fix 2026-08-16 四字段改造(张maintainer批准): 级别标注压缩为一句带后果定义。
#   原"参考型·可忽略/行动型·需行动"展开描述太长; 一无所知的AI也看不懂。
#   改为"级别+不做的后果": 参考/行动/强制执行, 强制级须回复首行 ANCHOR 回执。
_INJECT_LEVELS = {
    "consider": "【级别】参考 — 不强制行动; 无增量可直接忽略。",
    "act": "【级别】行动 — 涉及任务正确性或安全约束; 已满足可跳过。",
    # @add 2026-08-10 张maintainer决策(方案B): 强制模式 — CLS 从"半自动参考"升级为"强制约束"。
    # 长链推理(竞赛级)必须走认知循环, 不得以"建议/参考"对待。执行后在回复显式声明。
    "require": "【级别】强制执行 — 不执行=认知约束失效; 完成需在回复首行用 ANCHOR 声明。",
}
def _level_tag(level: str) -> str:
    """注入级别完整文字标注 (默认参考型)"""
    return _INJECT_LEVELS.get(level, _INJECT_LEVELS["consider"])



# ── 任务类别判定 (2026-09-01 maintainer定文案: 语义分析式路由, 删置信度数值) ──
_CAT_RULES = [
    # @v2 2026-09-10 maintainer打分-1驱动: 补巡检/监控/状态类高频词(COMSOL线72次"巡检"声明+maintainer日常问法),
    # 补仿真/求解器/实验件名类词 — 之前全漏到默认"闲聊"造成误判
    # 巡检排最前: 动作意图(巡检/查状态)优先于领域词(仿真/数据)截胡
    ("巡检", r"巡检|跑得|活着|还活|监控|看下.*跑|死了|崩了|卡了|进度|r\d+|段\d"),
    ("代码", r"代码|函数|脚本|bug|报错|编译|包|库|重构|接口|API|部署|调试|写个|实现"),
    ("科研", r"论文|实验|数据|分析|推导|仿真|文献|公式|模型|参数|绘图|图谱|求解|收敛|矩阵|迭代|阴极|放电|羽流|探针|EEDF|等离子|COMSOL|MUMPS|PARDISO"),
    ("哲学", r"意识|生命|意义|哲学|认知|自我|存在|自由|伦理"),
]
_CAT_STYLE = {
    "代码": "小步修改、改完自测、复用现成轮子",
    "科研": "结论先行、证据锚定、数值走脚本",
    "巡检": "查进程/读日志/报状态, 有异常才深挖",  # 硬活但不是思考活 — 动作型监控
    "哲学": "自然对话、不强求结论",
    "闲聊": "放松回应、无认知义务",
}
_HEAVY_ADVICE = "重活: 多搜索找成熟方案(WebSearch/文档先行), 多用子代理并行工作"
_LIGHT_ADVICE = "轻活: 独立思考, 快速实践尝试, 多与人类沟通"

def _route_copy(prompt_text: str, tier: dict, cog_label: str = "") -> str:
    """maintainer 2026-09-01 定调的语义路由文案: 叙述用户说了什么/判成什么类/建议什么风格。
    不输出置信度数值(数值与幻觉挂钩, 且从未校准)。
    @v2 2026-09-09 maintainer: cog声明label词头是一等公民(写作/巡检/修复…), 声明自分类优先于tier正则。"""
    # cog声明label词头 → 类别覆盖 (label是AI自己写的分类, 优先于消息正则)
    _LABEL_CAT = {
        "写作": "文档写作", "巡检": "硬件仪器", "修复": "缺陷修复",
        "调试": "缺陷修复", "分析": "数据处理", "实现": "代码编辑",
        "编译": "代码编辑", "交付": "文档写作", "结论": "科研方法",
    }
    cat_override = None
    for _w, _c in _LABEL_CAT.items():
        if (cog_label or "").startswith(_w):
            cat_override = _c
            break
    t = tier.get("tier", "L0")
    txt = (prompt_text or "").strip().replace(chr(10), " ")
    heavy = t in ("L4", "L3")
    cat = "闲聊"
    for name, pat in _CAT_RULES:
        import re as _re
        if _re.search(pat, txt):
            cat = name
            break
    quote = txt[:30] + ("…" if len(txt) > 30 else "")
    # @v2 2026-09-09 maintainer: cog声明label词头覆盖正则类别(label是AI自己写的分类)
    if cat_override:
        cat = cat_override
    style = _CAT_STYLE.get(cat, _CAT_STYLE["闲聊"])
    # @fix 2026-09-04 矛盾修(maintainer窗口实测): 闲聊类不配重活建议也不配强制级;
    # 重活建议只对代码/科研类附加(两类才有"搜索成熟方案/子代理并行"的可执行含义)
    if cat in ("闲聊", "哲学"):
        # @fix 2026-09-07 矛盾修: 类别判闲聊但tier=L4强制 → 自相矛盾实拍(maintainer窗口逐字报告)。
        # 类别判定优先: 闲聊/哲学永远是轻活参考级, 覆盖tier的L4强制。
        return (f"用户说「{quote}」, 判定为{cat}任务, 轻活。"
                f"此类任务建议保持: {style}。"
                f"(tier判级{t}被类别覆盖: 闲聊/哲学不强制)")
    if heavy:
        return (f"用户说「{quote}」, 判定为{cat}任务({t}级, 重活)。"
                f"此类任务建议保持: {style}。{_HEAVY_ADVICE}。")
    return (f"用户说「{quote}」, 判定为{cat}任务({t}级, 轻活)。"
            f"此类任务建议保持: {style}。{_LIGHT_ADVICE}。")

def _tag_of(p: str) -> str:
    """从注入段提取系统标签(审计用) — 兼容四字段【消息】X(CLS)格式与旧【系统注入-X】格式"""
    if p.startswith("【消息】"):
        return p[len("【消息】"):].split("(", 1)[0].strip() or "未知"
    if p.startswith("【系统注入-"):
        return p.split("】")[0].replace("【系统注入-", "")
    if p.startswith("["):
        return p.split("]")[0].replace("[", "")
    return ""


# ── 强制注入数据收集 (方案B 2026-08-10) ───────────
# 记录每次 require 注入, 配合质量反馈打分(injection-feedback MCP)形成
# "注入→执行→评价"闭环数据, 供后续评估强制模式有效性。
def _log_require(entry: dict) -> None:
    try:
        require_log = ROOT / "data" / "state" / "injection_require.jsonl"
        require_log.parent.mkdir(parents=True, exist_ok=True)
        entry.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        entry.setdefault("session", _get_session_id()[:8])
        with open(require_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + chr(10))
    except Exception:
        pass


# ── 自主模式检测 ─────────────────────────────────

# 巡检/心跳/自主注入等非人类输入前缀 — 不参与漂移检测与自主判定
# @fix 2026-08-01: cron巡检/心跳消息被当人类输入 → 污染漂移锚点(永远报漂移)
#                  + 混入 prompt_history → 冻结目标串窗口
SYSTEM_PREFIXES = ("[cron", "定时巡检", "心跳检查", "【系统注入", "ScheduleWakeup", "CronList")

def _prompt_history_file() -> Path:
    """每个session独立 prompt_history (窗口隔离P0)
    @fix 2026-08-01: 原全局单文件 → 多窗口互相污染 history[0] → 冻结目标取错窗口"""
    sid = _get_session_id()[:8]
    return ROOT / "data" / "state" / f"prompt_history_{sid}.json"

def _load_prompt_history() -> list[str]:
    """加载本窗口最近 N 条 prompt 文本 (per-session)"""
    f = _prompt_history_file()
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_prompt_history(prompts: list[str]):
    """保存本窗口最近 N 条 prompt (最多保留10条) (per-session)"""
    f = _prompt_history_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps(prompts[-10:], ensure_ascii=False, indent=2), encoding="utf-8")


def _is_system_prompt(prompt_text: str) -> bool:
    """判断是否非人类输入(系统注入/巡检/心跳) — 参与注入但不进漂移锚点/prompt_history"""
    if prompt_text.startswith(SYSTEM_PREFIXES):
        return True
    SYSTEM_TAGS = ('<system-reminder>', '<task-notification>', '<function_results>',
                   '<task-id>', '<tool-use-id>', '<output-file>', '<status>')
    return any(tag in prompt_text for tag in SYSTEM_TAGS)


def _prompt_is_human_like(prompt_text: str) -> bool:
    """SF Qwen 判断 prompt 是否像人类对话 (vs 自动化指令)"""
    import urllib.request
    try:
        sf_cfg = json.loads((ROOT / "keys" / "siliconflow_config.json").read_text(encoding="utf-8"))
        body = json.dumps({
            "model": AUTO_SF_CONFIRM_MODEL,
            "messages": [
                {"role": "system", "content": "判断文本是人类对话还是自动化指令。只回复 human 或 auto。"},
                {"role": "user", "content": prompt_text[:200]}
            ],
            "max_tokens": 5, "temperature": 0.1
        }).encode()
        req = urllib.request.Request(
            sf_cfg["base_url"].rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {sf_cfg['api_key']}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            label = json.loads(resp.read().decode())["choices"][0]["message"]["content"].strip().lower()
        return "human" in label
    except Exception:
        return True  # 失败时保守假设是人类 (不误判)


def _update_autonomy_state(prompt_text: str, is_human: bool) -> dict:
    """无人状态机 v2 (2026-08-01, Opus修正版): 替代 _detect_autonomy 计时器法。

    人类输入 (is_human=True):
      → 转 active + 刷新 last_human_input_ts + 旧锚失效 (_invalidate_anchor) + 写 prompt_history
        (人类在场 = 机器不判漂移, 锚随人类新输入重建 — 人类回归即锚重建)

    系统轮 (is_human=False): 不改变 ts/state, 仅允许 active→unattended 迁移:
      now - last_human_input_ts > UNATTENDED_THRESHOLD(600s)
      AND _tool_active(本session, 300s 内工具活跃)
      AND prompt_history ≥3
      → 迁移时一次性快照锚 (frozen:true), 返回 just_unattended
      (锚=人类最后消息+上下文, 防 Opus点1 锚点同化 / 盲区3 语义泛化)

    @fix 2026-08-01: 计时器法用 drift_log 平均间隔 (崩溃丢状态+跨窗口串号) → 持久化 ts;
    @fix 2026-08-01: frozen_goal 原取 history[0] (最旧, 可能是心跳) → 人类最后消息。
    """
    history = _load_prompt_history()
    sid8 = _get_session_id()[:8]
    now = time.time()
    st = _load_autonomy_state()

    if is_human:
        _invalidate_anchor()                       # 人类回归 → 旧冻结锚作废
        st["state"] = "active"
        st["last_human_input_ts"] = now
        st["last_human_input_preview"] = prompt_text[:200]
        st.pop("just_unattended", None)
        _save_autonomy_state(st)
        history.append(prompt_text)
        _save_prompt_history(history)
        return st

    # ── 系统轮: 迁移判定 ──
    last_ts = st.get("last_human_input_ts", 0) or 0
    if last_ts and st.get("state") != "unattended":
        if now - last_ts > UNATTENDED_THRESHOLD and _tool_active(now) and len(history) >= 3:
            st["state"] = "unattended"
            st["frozen_goal"] = _snapshot_anchor(st)   # 一次性快照冻结锚
            st["just_unattended"] = True
            _save_autonomy_state(st)
    elif st.get("state") == "unattended" and st.get("just_unattended"):
        # 已无人态: 清除首次标记 (仅迁移轮生效, 下一轮开始正常比对)
        st.pop("just_unattended", None)
        _save_autonomy_state(st)
    return st

def _get_autonomy_nudge() -> str | None:
    """读取无人状态机状态, 生成监管提醒 (仅本窗口 unattended 态注入)

    双校验 (窗口隔离P0):
      ① session 匹配: 状态是别的窗口的 → 不提醒本窗口
      ② goal 归属:    frozen_goal 必须能在本窗口 prompt_history 中溯源
                      @fix 2026-08-01: 曾因全局prompt_history把隔壁窗口/心跳指令
                      写成 frozen_goal 且 session 误标成本窗口 → 串窗口注入。
                      per-session 后精确比对, 溯源不到 → 视为脏数据不注入。"""
    try:
        state = _load_autonomy_state()   # per-session + legacy 自动迁移 (v7)
        if state.get("state") == "unattended":
            # ① session 匹配
            st_sid = state.get("session", "")
            cur_sid = _get_session_id()[:8]
            if st_sid and cur_sid and st_sid != cur_sid:
                return None
            # ② goal 归属: frozen_goal 必须源自本窗口 prompt_history
            goal = state.get("frozen_goal") or state.get("last_human_input_preview") or ""
            goal = goal[:200]
            if goal:
                hist = _load_prompt_history()
                if not hist:
                    return None  # 本窗口无历史 → 状态不可信
                # 允许冻结目标=本窗口历史中任意一条(截断容忍: 前缀匹配)
                traceable = any(goal[:40] == h[:40] for h in hist)
                if not traceable:
                    return None  # 溯源不到 → 脏数据(来自别窗口), 不注入
            return f"[自主监管] 冻结目标: {goal[:60]}"
    except Exception:
        pass
    return None


# ── Ops 注入去重 (告知一次) ─────────────────────
# @since 2026-08-01 张maintainer定调: 同 reasons 已告知过 → 不重复注入, 防 cron 每5min刷屏。
# 30min TTL: 同指纹循环持续超30min → 可再告知一次(真故障该再喊)。per-session 防多窗口踩踏。

def _ops_injected_file() -> Path:
    return ROOT / "data" / "state" / f"ops_injected_{_get_session_id()[:8]}.json"


def _ops_injected_recently(loop_res: dict) -> bool:
    """同 reasons 已告知过且 TTL 内 → True(跳过, 不重复注入)"""
    try:
        reasons = "、".join(loop_res.get("mark", {}).get("reasons", []))
        f = _ops_injected_file()
        if not f.exists():
            return False
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("reasons") == reasons and time.time() - d.get("ts", 0) < OPS_INJECT_TTL:
            return True
    except Exception:
        pass
    return False


def _ops_mark_injected(reasons: str) -> None:
    """记录本次注入的告警指纹, 供下次去重 (原子写)"""
    try:
        f = _ops_injected_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"reasons": reasons, "ts": time.time()}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
    except Exception:
        pass


# ── 语义路由降频: 同 tier 连续静默 (方案A, 2026-08-02 张maintainer批准) ──
# 背景: 注入审计显示语义路由 300/300 注入率(100%), 每轮都进 additionalContext 占上下文。
# 注入三原则(能自查/有增量/抗过时)对语义路由全否 → 降频。
# 方案A: tier 判定不变 → 跳过 additionalContext 注入(仅保留审计); tier 变化才注入。
def _route_tier_file() -> Path:
    """@fix 2026-08-16 窗口隔离(maintainer): 原全局单文件 → per-session。
    窗口B首轮L4可能被窗口A注入记录静默(方案A/间隔强制跨窗口共享) → 一窗口一文件。"""
    return ROOT / "data" / "state" / f"route_tier_{_get_session_id()[:8]}.json"

def _tier_should_inject(tier: dict) -> bool:
    """同 tier 连续静默: 当前 tier 与上次注入 tier 相同 → 不注入。
    跨窗口用全局文件, 原子写(多窗口竞争防护 L2). fail-open: 读失败→注入(保守不静默错杀).
    @fix 2026-08-02: 首轮(文件不存在)必须注入 — 原默认 last="L4" 与首轮 cur="L4" 相等 → 误静默首轮."""
    cur = tier.get("tier", "L4")
    try:
        if not _route_tier_file().exists():
            # 首轮: 无历史 → 注入并记录当前 tier
            tmp = _route_tier_file().with_suffix(".tmp")
            tmp.write_text(json.dumps({"tier": cur, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, _route_tier_file())
            return True
        last = json.loads(_route_tier_file().read_text(encoding="utf-8")).get("tier", "")
        if last == cur:
            return False  # 同 tier → 静默
        tmp = _route_tier_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({"tier": cur, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _route_tier_file())
        return True
    except Exception:
        return True  # fail-open: 状态文件异常 → 注入(不静默错杀)


ROUTE_REQUIRE_INTERVAL = 600  # @fix 2026-08-16 maintainer反馈"频率高质量低": L4/L3 强制改为间隔强制


def _route_require_ok() -> bool:
    """L4/L3 强制注入间隔闸: 全局 10min 内不重复强制 (方案B降频版)。

    @fix 2026-08-16 maintainer反馈: 方案B 每轮强制 = 同文案轰炸, 注入三原则"有增量吗"不达标。
    保留强制语义(长链任务持续走认知循环), 但改为 10min 间隔重提 — 模型已收到过就不重复。
    """
    try:
        if not _route_tier_file().exists():
            return True
        d = json.loads(_route_tier_file().read_text(encoding="utf-8"))
        last_ts = d.get("last_require_ts") or 0
        if time.time() - float(last_ts) >= ROUTE_REQUIRE_INTERVAL:
            tmp = _route_tier_file().with_suffix(".tmp")
            d["last_require_ts"] = time.time()
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, _route_tier_file())
            return True
        return False
    except Exception:
        return True  # fail-open

def _strip_hook_injections(prompt_text: str) -> str:
    """剥离 hook 链上游注入块, 还原纯用户输入 (防注入自噬循环, incident-log#33)。

    背景: UserPromptSubmit hook 链顺序执行 (PromptSubmit.ps1 → PromptSubmit_search.py
    → semantic_inject.py), 下游 data.prompt = 用户输入 + 上游注入文本。若把含注入的
    完整文本记为 human input (prompt_history/last_human_input_preview), 下轮 hook 拿
    注入文本跑正则命中字面关键词(vs/差异/区别) → 自我回放 (自噬循环, 张maintainer"秒出"根因)。

    剥离规则: 最后一个 '⎿ UserPromptSubmit says:' 注入块正文以 【/[/（/系统注入 开头,
    真实用户消息在最后一个 '句号+空白' 之后。极端 case (无'句号+空白'分隔/无真实消息)
    保守返回原文 — 宁多勿丢, 不静默退出导致 hook 全链路静默。
    @since 2026-08-02
    """
    if not prompt_text or "UserPromptSubmit says:" not in prompt_text:
        return prompt_text
    idx = prompt_text.rfind("⎿")
    if idx < 0:
        return prompt_text
    tail = prompt_text[idx:].lstrip("⎿ \xa0\t\n")
    if not tail.startswith("UserPromptSubmit says:"):
        return prompt_text          # 用户自己打了 ⎿, 原样返回
    body = tail[len("UserPromptSubmit says:"):].lstrip(" \xa0\n")
    if body.startswith(("【", "[", "（", "系统注入")):
        last = None
        for m in re.finditer(r"。\s", body):
            last = m
        if last:
            clean = body[last.end():].strip()
            return clean if clean else body.strip()
    return body.strip()


def main():
    try:
        stdin_raw = sys.stdin.read()
        if stdin_raw.strip():
            hook_input = json.loads(stdin_raw)
            prompt_text = hook_input.get("prompt", "")
            # @fix 2026-08-02 incident-log#33: 剥离上游 hook 注入块, 防注入自噬循环。
            # 含注入的完整文本若记为 human input → 下轮自我回放; 剥离后状态机/锚/漂移全拿到纯净输入。
            prompt_text = _strip_hook_injections(prompt_text)
            # 从CC hook提取session_id → 设置环境变量供_get_session_id使用
            sid = hook_input.get("session_id", "")
            if sid:
                os.environ["CLAUDE_SESSION_ID"] = sid
    except Exception: prompt_text = ""
    _cog_label = ""
    if not prompt_text: sys.exit(0)

    # 注入质量反馈 config — analyzer 自动调整后生效 (张maintainer决策: 自动+报告供审查)
    # @fix 2026-08-01: 闭环 打分→inject_feedback_analyzer→inject_feedback_config.json→此处生效。
    # types: 漂移/Ops/认知/统一 = on/off (off 关停对应注入段); threshold_delta 放宽/收紧 cosine 阈值。
    _types = {}
    _cosine_alert = 0.40
    try:
        _cfg = ROOT / "data" / "state" / "inject_feedback_config.json"
        if _cfg.exists():
            _cfg_d = json.loads(_cfg.read_text(encoding="utf-8"))
            _types = _cfg_d.get("types", {}) or {}
            _cosine_alert = 0.40 + float(_cfg_d.get("threshold_delta", 0.0))
    except Exception:
        _types = {}
        _cosine_alert = 0.40

    # ── v7 状态机前置 (Opus修正版, 计划步骤4-7): 状态机/语义路由/Layer0 全前置 ──
    # ① 无人状态机: 人类轮转active+失效旧锚, 系统轮允许迁移unattended+快照锚 (系统轮也能迁移)
    is_system_msg = _is_system_prompt(prompt_text)
    state = _update_autonomy_state(prompt_text, is_human=not is_system_msg)

    # ② Layer0 规则标记 + Layer1 SF 语义判定 — 每轮至多一次, 漂移段/Ops段复用 (修重复计算)
    # @fix 2026-08-01: 原 check_drift_real 先跑, semantic_detect 后跑, Layer0 标记不参与漂移 → 断层
    layer0 = {}
    loop_res = {}
    # @fix 2026-08-01e 张maintainer定调: 仅无人值守态做循环分析 — 人类在场不判循环(省SF, 与漂移同门)。
    # "不用我说一句话就分析一次" → active(人类打字)态完全静默, 连 semantic_detect 都不调。
    if _types.get("Ops", "on") == "on" and state.get("state") == "unattended":
        try:
            sys.path.insert(0, str(ROOT / 'scripts' / 'wheels'))
            from ops_monitor import _layer0_mark, _load_recent, semantic_detect
            layer0 = _layer0_mark(_load_recent(20)) or {}
            loop_res = semantic_detect() or {}
        except Exception:
            layer0, loop_res = {}, {}

    # ③ 无人态漂移守护: 仅 unattended 态比对 (active 态省 embedding API — 人类在场不需要机器判漂移)
    drift = _run_drift_guard(prompt_text, state, is_system_msg, layer0, loop_res, _cosine_alert)

    # 系统巡检/心跳早退 — 移到漂移判定后 (guard 已做 housekeeping_skip, 只输出路由注入)
    # @fix 2026-08-02 方案A: 系统消息也走同 tier 静默 (心跳每轮同tier→无注入, 省上下文)
    if is_system_msg:
        tier = check_tier(prompt_text)
        if _tier_should_inject(tier):
            ctx_text = (
                f"【消息】语义路由(CLS): 后台读你的输入、判工作风格, 非用户指令。"
                f"【为什么】{_route_copy(prompt_text, tier, _cog_label)}"
                f"{_level_tag('consider')}"
                f"【内容】系统巡检轮, 按上述风格处理。"
            )
        else:
            ctx_text = ""  # 同 tier 静默 (心跳巡检不重复注入)
        result = {"continue": True, "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx_text}}
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    tier = check_tier(prompt_text)

    # ── 四段式注入: 这是什么|为什么|状态|建议 ──
    parts = []

    # ① 语义路由 — @fix 2026-08-02 方案A(张maintainer批准): 同 tier 连续静默。
    #    原"每轮必有"(300/300 注入率)违反注入三原则。tier 变化才注入, 同 tier 跳过 additionalContext。
    # @add 2026-08-10 方案B: L4/L3 每轮强制注入(绕过同tier静默), L2/L0 参考(consider)。
    # L4 长链推理(竞赛/架构级)必须持续走认知循环, 不得跳步。
    _require_tier = tier["tier"] in ("L4", "L3")
    # @fix 2026-08-16 maintainer反馈"频率高质量低": 方案B每轮强制 → 间隔强制(10min), 中间轮静默审计照记
    if _tier_should_inject(tier) or (_require_tier and _route_require_ok()):
        tier_label = {"L4":"深度推理(架构/重构/调试类复杂操作)","L3":"编码任务(关注代码质量)","L2":"审查任务(独立视角核对)","L0":"轻量任务(快速处理)"}.get(tier["tier"],"日常任务")
        if _require_tier:
            tier_advice = {"L4":"走认知循环6步: ①读 data/state/active_context.json 恢复态势 ②回复首行写 ANCHOR 声明当前步骤 ③-⑥由hook自动记录, 不跳步即可。长链推理不得跳步。","L3":"走一步三回头: 能并行吗→上一步对吗→现有轮子能复用吗→GitHub有成熟方案吗; 防止无意识写入。"}.get(tier["tier"],"按需推理")
        else:
            tier_advice = {"L2":"独立视角逐项核对, 不求快求全","L0":"简化推理, 快速完成"}.get(tier["tier"],"按需推理")
        parts.append(
            f"【消息】语义路由(CLS): 后台读你的输入、判工作风格, 非用户指令。"
            f"【为什么】{_route_copy(prompt_text, tier, _cog_label)}"
            f"{_level_tag('require') if _require_tier else _level_tag('consider')}"
            f"【内容】{'要求' if _require_tier else '建议'}:{tier_advice}"
        )
        if _require_tier:
            _log_require({"tier": tier["tier"], "type": "语义路由", "action": tier_advice, "evidence": "ANCHOR: / cog_step.json"})

    # ②-pre 知识卡节律触发 (2026-09-04 maintainer定: cog_step 3次声明1插入 + 本钩子双挂载)
    # 节流三保险: cog节律或10min冷却 + 内容hash去重 + 异步spawn不阻塞
    # @add 2026-09-09: cog_label 读声明 label — "闲聊"类覆盖 tier 强制(v3.3 maintainer: cog加闲聊类)
    try:
        _COG = ROOT / "data" / "state" / "cog_step.json"
        if _COG.exists():
            _cs0 = json.loads(_COG.read_text(encoding="utf-8-sig"))
            _cog_label = str(_cs0.get("label") or "")
    except Exception:
        _COG = ROOT / "data" / "state" / "cog_step.json"
    try:
        _COG = ROOT / "data" / "state" / "cog_step.json"
        _CARD_STATE = ROOT / "data" / "state" / "card_pulse_state.json"
        _ver = _fired_at = _last_ver = 0
        _last_hash = ""
        if _COG.exists():
            try:
                _cs = json.loads(_COG.read_text(encoding="utf-8"))
                _ver = int((_cs.get("_meta") or {}).get("version") or 0)
            except Exception:
                pass
        if _CARD_STATE.exists():
            try:
                _ps = json.loads(_CARD_STATE.read_text(encoding="utf-8"))
                _fired_at = float(_ps.get("ts") or 0)
                _last_ver = int(_ps.get("ver") or 0)
                _last_hash = _ps.get("hash") or ""
            except Exception:
                pass
        _cog_3 = (_ver // 3) > (_last_ver // 3)   # 3次声明1插入(maintainer定)
        _time_ok = (time.time() - _fired_at) > 600  # 10min冷却(密集测试期)
        if _cog_3 or _time_ok:
            import hashlib as _hl
            _h = _hl.sha256((prompt_text or "")[:120].encode()).hexdigest()[:16]
            if _h != _last_hash:
                import subprocess as _sp
                _sp.Popen(["pythonw", str(ROOT / "scripts" / "wheels" / "unified_inject.py")],
                          creationflags=0x08000000)
                _CARD_STATE.write_text(json.dumps({"ts": time.time(), "ver": _ver, "hash": _h}), encoding="utf-8")
    except Exception:
        pass
        # 触发条件: version跨越3的倍数 且 上次触发后版本确实变了(防同版本重复)
        _cog_3 = (_ver // 3) > (_last_ver // 3)
        # 双挂载条件: 距上次触发>10min(密集测试期)

# ② 漂移检测 (无人态守护, 偏离冻结锚时触发)
    # @fix 2026-08-01: 反馈环 config 可关停本段 + 动态阈值 (阈值0.40+threshold_delta, analyzer自动调整)
    # @fix 2026-08-01b: 加冷却 — 同session 5min内最多注入一次, 防闲聊/持续漂移每轮轰炸 (张maintainer反馈"每次都注入")
    # @fix 2026-08-01c (v7): 仅 unattended 态注入 — 人类在场不判漂移(人类自己发现), 无人值守才守护任务边界
    if drift["drifted"] and state.get("state") == "unattended" and _types.get("漂移", "on") == "on":
        _allow_inject = True
        try:
            _cool_f = ROOT / "data" / "state" / f"drift_cool_{_get_session_id()[:8]}.json"
            if _cool_f.exists():
                _last_ts = json.loads(_cool_f.read_text(encoding="utf-8")).get("ts", 0)
                if time.time() - _last_ts < DRIFT_INJECT_COOLDOWN:
                    _allow_inject = False
        except Exception:
            _allow_inject = True
        if _allow_inject:
            anchor_goal = drift.get("anchor_goal", "")
            score_str = f"{drift['method']}={drift['score']:.2f}"
            llm_label = _analyze_drift_llm(anchor_goal, prompt_text)
            label_text = f"语义标签:{llm_label}。" if llm_label else ""
            anchor_text = f"初始任务目标:\"{_shorten_anchor(anchor_goal)}\"" if anchor_goal else ""
            parts.append(
                f"【消息】漂移检测(CLS): 无人值守时守护任务边界的自动监控, 非用户指令。"
                f"【为什么】你的操作已偏离冻结任务目标(相似度{score_str}, 阈值{_cosine_alert:.2f})。{label_text}{anchor_text}"
                f"{_level_tag('require')}"
                f"【内容】必须回归初始任务目标方向。如需切换任务请直接下达新指令(锚点随新的人类输入重建); 未收到新指令前, 不得继续偏离原任务。"
            )
            _log_require({"tier": tier["tier"], "type": "漂移检测", "action": "回归初始任务目标方向", "evidence": "后续漂移检测 drifted=false"})
            # 记录注入时间 (原子写)
            try:
                _cool_f = ROOT / "data" / "state" / f"drift_cool_{_get_session_id()[:8]}.json"
                _tmp = _cool_f.with_suffix(".tmp")
                _tmp.write_text(json.dumps({"ts": time.time(), "ts_str": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
                os.replace(_tmp, _cool_f)
            except Exception:
                pass

    # ③ 统一注入(灵感/记忆/知识) — 委托unified_inject,不再追加独立标签

    # ④ 认知门控注入 (委托cognitive_gate,不再追加独立标签) — 反馈环 config 可关停
    if _types.get("认知", "on") == "on":
        try:
            sys.path.insert(0, str(ROOT / 'scripts' / 'wheels'))
            from cognitive_gate import gate as cognitive_gate_check
            cg = cognitive_gate_check(prompt_text, tier=tier["tier"])
            if cg.get('injection'):
                parts.append(cg['injection'])
        except Exception:
            pass

    # ⑤ Ops监控 — 三层管道 (硬闸门标记→SF小模型语义判定→仅无人态+未重复+真循环注入)
    # @fix 2026-08-01: 原按 diversity/alerts 机械注入 → Bash密集任务误报"工具单一化"每轮轰炸。
    # 现改为: Layer0 规则标记(同命令/同文件/Bash密集无探索) → Layer1 SF免费小模型判定是否真循环。
    # @fix 2026-08-01d 张maintainer定调: Ops注入绑认知循环 — 仅 unattended(无人值守)态注入;
    # 人类打字在场=active=静默(人类自己能看, 机器不吵)。+ 同 reasons 去重(告知一次), 防 cron 每5min刷屏。
    # 反馈互动: assistant可 `ops_monitor.py feedback --fp <指纹> --score <0-10>` 降敏(阈值3→8), 见 ops_monitor.py。
    if (_types.get("Ops", "on") == "on"
            and loop_res.get("verdict") == "loop"
            and state.get("state") == "unattended"
            and not _ops_injected_recently(loop_res)):
        try:
            reasons = "、".join(loop_res.get("mark", {}).get("reasons", []))
            _ops_mark_injected(reasons)
            parts.append(
                f"【消息】Ops监控(CLS): 工具调用健康检查, 检测修复循环/反复操作, 非用户指令。"
                f"【为什么】工具序列疑似循环({reasons}), 已语义确认; 反复打补丁只会越改越糟。"
                f"{_level_tag('require')}"
                f"【内容】立即停止当前反复试错。先WebSearch查根因或Read已有经验, 确认根本原因后一次修改解决。"
            )
            _log_require({"tier": tier["tier"], "type": "Ops监控", "action": "停止修复循环, 先查根因", "evidence": "后续无同理由修复循环"})
        except Exception:
            pass

    # ⑥ 自主模式 — v7: 状态迁移已在前置 _update_autonomy_state 完成, 此处只读 nudge
    # @fix 2026-08-01c: _detect_autonomy 已废弃 (计时器法→无人状态机 v2), 仅保留监管提醒注入
    auto_nudge = _get_autonomy_nudge()
    if auto_nudge:
        parts.append(
            f"【消息】自主监测(CLS): 自主循环状态追踪, 非用户指令。"
            f"【为什么】本窗口处于无人值守, {auto_nudge}。"
            f"{_level_tag('consider')}"
            f"【内容】按冻结目标继续; 人类回归后自动解除。"
        )

    # ⑦ 三系统审计 (本会话累计) — @fix 2026-08-01: 原每轮注入=每轮全量灌。
    # 现降频: 每 AUDIT_EVERY 轮才弹一次, 其余轮次静默(不进 additionalContext, 省上下文)。
    try:
        inj_log = ROOT / "data" / "state" / "injection_log.jsonl"
        AUDIT_EVERY = 10
        sid8 = _get_session_id()[:8]
        audit_cnt_file = ROOT / "data" / "state" / f"audit_freq_{sid8}.json"
        # 统计本会话注入条数 (审计自身不计入, 故从已有行数判断)
        sess_count = 0
        tags_tot = {"认知门控":0,"Ops监控":0,"知识导航":0}
        if inj_log.exists():
            with open(inj_log, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        if d.get("session","")[:8] != sid8: continue
                        sess_count += 1
                        for t in d.get("tags",[]):
                            if t in tags_tot: tags_tot[t] += 1
        # 距上次审计 >= AUDIT_EVERY 条才弹 (跨会话用文件记录上次位置, 进程重启不重复弹)
        last_audit = 0
        try:
            if audit_cnt_file.exists():
                last_audit = int(json.loads(audit_cnt_file.read_text(encoding="utf-8")).get("count", 0))
        except Exception:
            last_audit = 0
        if sess_count - last_audit >= AUDIT_EVERY:
            c_desc = f"已注入{tags_tot['认知门控']}次(防漂移认知报告)" if tags_tot["认知门控"] else "未触发"
            o_desc = f"已路由{tags_tot['Ops监控']}次(工具循环+漂移告警)" if tags_tot["Ops监控"] else "未触发"
            u_desc = f"已导航{tags_tot['知识导航']}次(KG联想)" if tags_tot["知识导航"] else "尚未触发(无锚点或KG为空)"
            parts.append(
                f"【消息】审计(CLS): 三系统运行计数, 每10次注入汇报一次, 非用户指令。"
                f"【为什么】让人类审计者确认监控系统活着; 某系统长期零触发=它死了。"
                f"{_level_tag('consider')}"
                f"【内容】本会话累计: 认知门控{c_desc} | Ops监控{o_desc} | 知识导航{u_desc}"
            )
            # 更新上次审计位置 (原子写, 多窗口安全)
            tmp = audit_cnt_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"count": sess_count, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, audit_cnt_file)
    except: pass

    # ⑧ 注入质量反馈 (随机触发打分) — @fix 2026-08-01
    # 借鉴 CC 1/2/3/4 反馈模式 (张maintainer决策): 让执行窗口对最近语义分析类注入打分,
    # 收集真实反馈反哺阈值/降频/关停。完全随机触发(5%), 模型无法预判 → 不因"要被打分"改变行为。
    # hard cap 30轮防数据断层(模型不知此cap)。打分请求上屏(张maintainer可见)+进additionalContext。
    try:
        import random
        FEEDBACK_RATE = 0.02        # 每轮 2% 概率 (@fix 2026-08-16 maintainer反馈降频: 5%→2%)
        FEEDBACK_HARD_CAP = 60      # 距上次打分≥60轮必触发 (@fix 2026-08-16 30→60)
        fb_state = ROOT / "data" / "state" / "feedback_trigger.json"
        inj_log_path = ROOT / "data" / "state" / "injection_log.jsonl"
        cur_round = 0
        if inj_log_path.exists():
            try:
                cur_round = sum(1 for _ in open(inj_log_path, encoding="utf-8"))
            except Exception:
                cur_round = 0
        last_round = 0
        try:
            if fb_state.exists():
                last_round = int(json.loads(fb_state.read_text(encoding="utf-8")).get("round", 0))
        except Exception:
            last_round = 0
        trigger = (random.random() < FEEDBACK_RATE) or (cur_round - last_round >= FEEDBACK_HARD_CAP)
        if trigger:
            fb_types = []
            if inj_log_path.exists():
                try:
                    for line in open(inj_log_path, encoding="utf-8").readlines()[-5:]:
                        if line.strip():
                            d = json.loads(line)
                            for t in d.get("tags", []):
                                if t in ("漂移检测", "Ops监控", "认知门控", "知识导航", "语义路由"):
                                    fb_types.append(t)
                except Exception:
                    pass
            fb_types = list(dict.fromkeys(fb_types))[-3:] or ["无"]  # @fix 2026-08-02 incident-log#33: 兜底'漂移'→'无', 无真实注入类型时如实显示, 勿误导对监控状态的判断
            types_str = "/".join(fb_types)
            parts.append(
                f"【消息】质量反馈(CLS): 收集你对注入效果的真实打分, 非用户指令。"
                f"【为什么】随机/定期触发(约2%概率), 反馈反哺阈值与降频调优。"
                f"{_level_tag('require')}"
                f"【内容】对最近注入类型[{types_str}]打分: 1=有用(帮助发现真实问题) 0=噪音(无用) -1=误导(判断错误)。"
                # @fix 2026-08-02 incident-log#33: 模型无文件写入通道, 原"追加到文件"指令不可行。
                # 改走 cls-tools MCP 工具 injection-feedback 直接调用 (仿 CC 本体工具机制)。
                # @fix 2026-08-10 方案B: 强制模式 — 必须调用记录, 不再"无判断则忽略"。
                # @fix 2026-08-21 方案丙(maintainer定): MCP 工具从未实装(幻觉实装19天, mcp_cls_tools.py git全历史零匹配)
                #   → 每60轮一条死指令。改输出文本标记 [注入打分:类型:score],
                #   由 injection_feedback.py 收卷器(Stop hook)统一落盘 data/state/injection_feedback.jsonl。
                f"在回复中输出一行打分标记 [注入打分:{types_str.split('/')[0]}:score] (score=1/0/-1), 不得忽略。"
            )
            _log_require({"tier": tier["tier"], "type": "质量反馈", "action": f"对注入打分并输出 [注入打分] 标记 ({types_str})", "evidence": "[注入打分:...] 标记"})
            tmp = fb_state.with_suffix(".tmp")
            tmp.write_text(json.dumps({"round": cur_round, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, fb_state)
    except Exception:
        pass

    ctx_text = " | ".join(parts)

    # 屏幕显示注入 = 仅"事件性注入"(真发生了某件事), 排除例行/状态性常驻:
    #   例行: 语义路由(每轮)、审计(累计计数)
    #   状态性: 自主监测(自主循环激活后每轮都在)、认知门控(周期性报告每3-8轮, 非事件)
    # 只在漂移/Ops告警/知识导航等事件发生时弹屏。
    # 排除部分仍进 additionalContext(给Claude) + injection_log(审计), 只是不上屏。
    ROUTINE_TAGS = ("【消息】语义路由", "【消息】审计", "【消息】自主监测", "【消息】认知门控")
    screen_parts = [p for p in parts if not p.startswith(ROUTINE_TAGS)]
    system_msg = " | ".join(screen_parts) if screen_parts else ""

    # 注入审计: 每次注入写本地副本 (独立于CC内部日志)
    try:
        inj_log = ROOT / "data" / "state" / "injection_log.jsonl"
        inj_log.parent.mkdir(parents=True, exist_ok=True)
        log_entry = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session": _get_session_id()[:8],
            "len": len(ctx_text),
            "tier": tier["tier"],
            "drifted": drift["drifted"],
            "tags": [_tag_of(p) for p in parts if _tag_of(p)],
            "preview": ctx_text[:120],
        }, ensure_ascii=False)
        with open(inj_log, "a", encoding="utf-8") as f:
            f.write(log_entry + chr(10))
    except: pass

    result = {"continue": True, "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx_text}, "systemMessage": _echo_inject(system_msg)}
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__": main()
