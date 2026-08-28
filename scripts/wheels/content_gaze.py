#!/usr/bin/env python3
"""content_gaze.py — 内容凝视: SF Qwen 独立评估 agent 文件产出质量
=================================================================
事件驱动: 每次 Write 触发读文件 → 评三维 → 持续下降 → 告警

维度:
  ① 信息密度: 有新增知识吗? (vs 前次输出)
  ② 策略变化: 方法有变还是重复?
  ③ 逻辑自洽: 结论是否与前面矛盾?

@since: 2026-07-27
"""

import json, sys, os, time, re
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent
GAZE_LOG = ROOT / "data" / "state" / "content_gaze_log.jsonl"
GAZE_STATE = ROOT / "data" / "state" / "_content_gaze_state.json"
SESSION_ID = None  # set from PostToolUse hook arg
SF_MODEL = "Qwen/Qwen2.5-7B-Instruct"

# 连续下降阈值
DECLINE_STREAK = 3
# 凝视态: 只有 cognitive_gate 注入后才激活
GAZE_TTL = 600  # 凝视态10分钟自动退出


# ── 凝视态管理 ────────────────────────────────

def _set_session_id(sid: str):
    global SESSION_ID
    SESSION_ID = sid[:16] if sid else "unknown"

def _session_write_count() -> int:
    """只计本session的Write数量"""
    import json, time
    ops_file = ROOT / "data" / "state" / "ops_freq.jsonl"
    if not ops_file.exists(): return 0
    now = time.time()
    count = 0
    try:
        for line in open(ops_file, encoding="utf-8").readlines()[-30:]:
            if line.strip():
                d = json.loads(line)
                if d.get("tool") in ("Write", "Edit") and now - d.get("ts", 0) < 300:
                    count += 1
    except: pass
    return count

def auto_activate_if_writing():
    """自动激活: 最近5分钟有3次以上Write → 进入凝视态"""
    import json, time
    ops_file = ROOT / "data" / "state" / "ops_freq.jsonl"
    if not ops_file.exists():
        return False
    writes = _session_write_count()
    if writes >= 3 and not is_gaze_active():
        activate_gaze()
        return True
    return False


def is_gaze_active() -> bool:
    if not GAZE_STATE.exists():
        return False
    try:
        state = json.loads(GAZE_STATE.read_text(encoding="utf-8"))
        return time.time() - state.get("activated_at", 0) < GAZE_TTL
    except:
        return False


def activate_gaze():
    GAZE_STATE.parent.mkdir(parents=True, exist_ok=True)
    GAZE_STATE.write_text(json.dumps({"activated_at": time.time(), "mode": "gaze"}, ensure_ascii=False), encoding="utf-8")


def deactivate_gaze():
    if GAZE_STATE.exists():
        GAZE_STATE.unlink()


# ── SF Qwen 内容评分 ───────────────────────────

def _parse_scores(response: str) -> dict:
    """容错解析 SF 三维评分 — 免费模型偶发 JSON 语法错误(漏逗号/垃圾尾部), 三级降级"""
    # 1) 标准解析
    m = re.search(r'\{[^{}]*\}', response, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group())
            if isinstance(d, dict) and d:
                return d
        except Exception:
            pass
    # 2) raw_decode 容错 (容忍 JSON 后杂文本)
    try:
        d, _ = json.JSONDecoder().raw_decode(response, response.find('{'))
        if isinstance(d, dict) and d:
            return d
    except Exception:
        pass
    # 3) 关键词启发式 (JSON 内部语法错误时兜底)
    def _pick(pat, default):
        mm = re.search(pat, response)
        return mm.group(1) if mm else default
    sm = re.search(r'summary["\']?\s*[:：]\s*["\']([^"\'\n,}]{0,30})', response)
    return {
        "info_density": _pick(r'(上升|持平|下降)', "持平"),
        "strategy_change": _pick(r'(新方法|重复|微调)', "微调"),
        "logic_consistency": _pick(r'(自洽|小矛盾|严重矛盾)', "自洽"),
        "summary": sm.group(1) if sm else "",
    }


def _sf_score(text: str, prev_text: str = "") -> dict:
    """SF Qwen 三维评分"""
    import urllib.request
    ctx = text[:800]
    prev_ctx = prev_text[:300] if prev_text else "(无前次)"

    body = json.dumps({
        "model": SF_MODEL,
        "messages": [
            {"role": "system", "content": "你是内容评估器。读agent产出,评三个维度,只输出JSON: "
             '{"info_density":"上升/持平/下降","strategy_change":"新方法/重复/微调",'
             '"logic_consistency":"自洽/小矛盾/严重矛盾","summary":"<=10字评估"} 不要解释。'},
            {"role": "user", "content": f"前次输出:\n{prev_ctx}\n\n当前输出:\n{ctx}"}
        ],
        "max_tokens": 80, "temperature": 0.1
    }).encode()
    try:
        sf_cfg = json.loads((ROOT / "keys" / "siliconflow_config.json").read_text(encoding="utf-8"))
        req = urllib.request.Request(
            sf_cfg["base_url"].rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {sf_cfg['api_key']}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            response = json.loads(resp.read().decode())["choices"][0]["message"]["content"]
        d = _parse_scores(response)
        if d:
            # key 归一化: 免费模型可能返回等价别名 (准确性/相关性/流畅性) 而非标准三维度
            alias = {
                "info_density": ["info_density", "信息密度", "密度", "准确性"],
                "strategy_change": ["strategy_change", "策略变化", "方法", "相关性"],
                "logic_consistency": ["logic_consistency", "逻辑自洽", "自洽", "流畅性"],
            }
            out = {k: next((d[a] for a in v if a in d), "持平") for k, v in alias.items()}
            out["summary"] = d.get("summary", "")
            return out
    except:
        pass
    return {"info_density": "持平", "strategy_change": "微调", "logic_consistency": "自洽", "summary": "评估不可用"}


# ── 日志与趋势 ────────────────────────────────

def _load_log(n: int = 10) -> list[dict]:
    if not GAZE_LOG.exists():
        return []
    entries = []
    with open(GAZE_LOG, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                entries.append(json.loads(line))
    return entries[-n:]


def _decline_count() -> int:
    """连续下降计数: info_density='下降' 的连续次数"""
    entries = _load_log(DECLINE_STREAK)
    count = 0
    for e in reversed(entries):
        if e.get("info_density") == "下降":
            count += 1
        else:
            break
    return count


# ── 主入口 ────────────────────────────────────

def _scan_recent_files(limit: int = 5) -> list:
    """扫描最近修改的产出文件 (排除状态/日志/临时目录)"""
    now = time.time()
    candidates = []
    for root in (ROOT / "assistant交付", ROOT / "knowledge"):
        if root.exists():
            try:
                for p in root.rglob("*"):
                    if (p.is_file() and p.suffix in (".py", ".md", ".json", ".txt")
                            and p.stat().st_size >= 50):
                        candidates.append((now - p.stat().st_mtime, p))
            except OSError:
                pass
    candidates.sort(key=lambda x: x[0])
    return [p for _, p in candidates[:limit]]


def gaze(file_path: str) -> dict | None:
    """凝视一个文件: 读内容 → 评分 → 写日志 → 检测趋势"""
    # 自动激活: 检测到密集写入
    auto_activate_if_writing()
    if not is_gaze_active():
        return None
    # 如果传入的文件不存在, 从最近修改的文件中找一个
    if not Path(file_path).exists():
        recent = _scan_recent_files()
        if recent:
            file_path = str(recent[0])

    fpath = Path(file_path).resolve()
    if not fpath.exists() or fpath.stat().st_size < 50:
        return None
    # 只凝视 .py .md .json .txt
    if fpath.suffix not in (".py", ".md", ".json", ".txt"):
        return None

    try:
        text = fpath.read_text(encoding="utf-8")[:1000]
    except:
        return None

    # 读前次内容
    prev_text = ""
    entries = _load_log(1)
    if entries:
        prev_text = entries[-1].get("content_preview", "")[:300]

    # 评分
    scores = _sf_score(text, prev_text)
    decline = _decline_count()

    # 日志
    entry = {
        "ts": datetime.now().isoformat(),
        "file": str(fpath.relative_to(ROOT)),
        "size": fpath.stat().st_size,
        "content_preview": text[:200],
        **scores,
        "decline_streak": decline,
    }
    GAZE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(GAZE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + chr(10))

    # 趋势告警
    if decline >= DECLINE_STREAK:
        return {
            "alert": True,
            "message": f"[内容警告] 连续{decline}次信息密度下降, 策略可能在退化。建议换方法或查新资料。",
            "scores": scores,
        }

    # 连续上升 → 自动退出凝视
    rising = sum(1 for e in _load_log(3) if e.get("info_density") == "上升")
    if rising >= 3:
        deactivate_gaze()

    return {"alert": False, "scores": scores}


# ── 定期整理 (--sweep) ──────────────────────────────
# @fix 2026-08-02 maintainer决策: 内容凝视改定期脚本自动整理, 不再每次 Write 触发。
# 扫描最近 SWEEP_WINDOW 秒内修改的产出文件, 逐个评估写日志(替代事件驱动)。
# 消除: ①每次 Write spawn 进程空转+弹窗 ②自动激活死链(ops_freq 末30行挤满/GAZE_TTL 过期)。
SWEEP_WINDOW = 24 * 3600  # 扫最近24h修改

def _sweep(max_files: int = 5) -> dict:
    """定期整理: 扫最近24h修改的产出文件 → 评估三维 → 写 content_gaze_log。"""
    from pathlib import Path
    now = time.time()
    candidates = []
    for base in (ROOT / "assistant交付", ROOT / "knowledge"):
        if not base.exists():
            continue
        try:
            for p in base.rglob("*"):
                if (p.is_file() and p.suffix in (".py", ".md", ".json", ".txt")
                        and p.stat().st_size >= 50
                        and now - p.stat().st_mtime <= SWEEP_WINDOW):
                    candidates.append((now - p.stat().st_mtime, p))
        except OSError:
            pass
    candidates.sort(key=lambda x: x[0])  # 最近修改优先
    evaluated, skipped = 0, 0
    for age, fpath in candidates[:max_files]:
        # 跳过状态/日志文件自身
        rel = fpath.relative_to(ROOT)
        if str(rel).startswith("data" + os.sep) or str(rel).startswith(".claude"):
            skipped += 1
            continue
        try:
            text = fpath.read_text(encoding="utf-8")[:1000]
        except Exception:
            skipped += 1
            continue
        scores = _sf_score(text, "")
        entry = {
            "ts": datetime.now().isoformat(),
            "file": str(rel),
            "size": fpath.stat().st_size,
            "content_preview": text[:200],
            **scores,
            "decline_streak": _decline_count(),
            "source": "sweep",
        }
        GAZE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(GAZE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + chr(10))
        evaluated += 1
    return {"evaluated": evaluated, "skipped": skipped, "window_s": SWEEP_WINDOW}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: content_gaze.py <file_path>"}, ensure_ascii=False))
        return

    # Parse --sid from args
    global SESSION_ID
    for i, arg in enumerate(sys.argv):
        if arg == "--sid" and i+1 < len(sys.argv):
            _set_session_id(sys.argv[i+1])
            break
    cmd = sys.argv[1]
    if cmd == "--activate":
        activate_gaze()
        print(json.dumps({"activated": True}, ensure_ascii=False))
    elif cmd == "--deactivate":
        deactivate_gaze()
        print(json.dumps({"deactivated": True}, ensure_ascii=False))
    elif cmd == "--status":
        print(json.dumps({"active": is_gaze_active()}, ensure_ascii=False))
    elif cmd == "--trend":
        entries = _load_log(5)
        print(json.dumps({"entries": len(entries), "decline": _decline_count()}, ensure_ascii=False, indent=2))
    elif cmd == "--sweep":
        print(json.dumps(_sweep(), ensure_ascii=False))
    else:
        result = gaze(cmd)
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
