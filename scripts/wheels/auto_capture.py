#!/usr/bin/env python3
"""
auto_capture.py — 认知循环③⑤自动化 (关联学习+上下文持久化)
==========================================================
@since 2026-07-24 | 设计经 Fable5+GPT5.5 双审

合并社区最佳实践(SessionEnd主捕获+PostToolUse微捕获+SessionStart注入):
  ③ auto-capture: PostToolUse每40轮, 硅基Qwen2.5-7B提取知识→自动/capture
  ⑤ auto-summary: 同上周期, 增量写session_memory.md (替代不可靠的SessionEnd)

架构:
  PostToolUse Hook → (每40轮) → auto_capture.py run → 硅基Qwen2.5-7B
    → 提取知识项 → quality_gate → /capture写入
    → 增量摘要 → append session_memory.md

用法:
  python scripts/wheels/auto_capture.py run       # 执行一轮(由PostToolUse调用)
  python scripts/wheels/auto_capture.py status    # 查看统计
"""

import json, os, re, sys, time, uuid, hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
COOLDOWN_FILE = ROOT / "data" / "state" / ".auto_capture_cooldown"
DEDUP_FILE = ROOT / "data" / "state" / ".auto_capture_dedup"
TRAJECTORY = ROOT / "state" / "trajectory.jsonl"
SESSION_MEMORY = ROOT / "state" / "session_memory.md"
CAPTURE_LOG = ROOT / "data" / "symbolic_dynamics" / "auto_capture_log.jsonl"

# 配置
ROUNDS_INTERVAL = 10       # 每10轮执行一次
MAX_TRAJ_ENTRIES = 8       # 每次送模型的trajectory条数
DEDUP_WINDOW_HOURS = 24    # 去重窗口:24小时内相同fact不重复捕获

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]

def should_run() -> bool:
    """检查是否达到执行轮次"""
    try:
        if COOLDOWN_FILE.exists():
            data = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8"))
            count = data.get("count", 0)
        else:
            count = 0
        count += 1
        COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOLDOWN_FILE.write_text(json.dumps({"count": count, "ts": now_iso()}), encoding="utf-8")
        return (count % ROUNDS_INTERVAL == 0)
    except Exception:
        return False

def is_duplicate(fact: str) -> bool:
    """SHA-256去重: 24小时内相同fact不重复"""
    h = _hash(fact)
    try:
        if DEDUP_FILE.exists():
            data = json.loads(DEDUP_FILE.read_text(encoding="utf-8"))
        else:
            data = {}
        now = time.time()
        # 清理超过24小时的条目
        data = {k: v for k, v in data.items() if now - v < DEDUP_WINDOW_HOURS * 3600}
        if h in data:
            return True
        data[h] = now
        DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEDUP_FILE.write_text(json.dumps(data), encoding="utf-8")
        return False
    except Exception:
        return False

def get_recent_context() -> tuple[str, str]:
    """获取最近trajectory记录 + session_memory摘要"""
    traj_text = ""
    if TRAJECTORY.exists():
        try:
            lines = TRAJECTORY.read_text(encoding="utf-8-sig").strip().split("\n")
            recent = lines[-MAX_TRAJ_ENTRIES:]
            traj_text = "\n".join(recent)
        except Exception:
            pass

    mem_text = ""
    if SESSION_MEMORY.exists():
        try:
            mem_text = SESSION_MEMORY.read_text(encoding="utf-8")[-500:]
        except Exception:
            pass

    return traj_text, mem_text

def call_model(prompt: str, timeout_s: int = 10) -> str | None:
    """硅基流动 Qwen2.5-7B (免费API, CLS全基础设施统一)"""
    try:
        import urllib.request
        api_key = os.environ.get("SILICONFLOW_API_KEY", "")
        if not api_key:
            kf = ROOT / "keys" / "siliconflow_key.txt"
            if kf.exists():
                try: api_key = kf.read_text(encoding="utf-8").strip()
                except: pass
        if api_key:
            req_data = json.dumps({
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "messages": [
                    {"role": "system", "content": "你是一个知识提取器。只输出JSON,不输出其他内容。"},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 500, "temperature": 0.1,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.siliconflow.cn/v1/chat/completions",
                data=req_data,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception:
        pass
    return None

def passive_capture() -> int:
    """被动捕获 (2026-08-20 maintainer数据飞轮): 扫双轨进度文件的结构化标记段, 零LLM直捕。

    双轨文件是大模型当场结构化的最优质压缩 — 教训/亮点/困难/结论/决策
    以 <!--capture:TYPE anchor=LEVEL--> 标记写入, 此处只做解析入库。
    相比小模型从trajectory流水里猜知识, 这是确定性提取。
    """
    dual_dir = ROOT / "assistant交付" / "📚 学习资料" / "学习进度"
    concl_file = ROOT / "knowledge" / "知识图谱" / "kg_conclusions.jsonl"
    if not dual_dir.exists():
        return 0

    # 已捕过的文件不再扫 (记录 mtime)
    seen_file = ROOT / "data" / "state" / ".passive_capture_seen"
    seen = {}
    try:
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
    except Exception:
        seen = {}

    marker_re = re.compile(
        r"<!--capture:(\w+)(?:\s+anchor=(\w+))?-->\n(.+?)(?=\n\n|\n##|\Z)", re.DOTALL)
    captured = 0
    for md in sorted(dual_dir.glob("*.md")):
        key = md.name
        mtime = md.stat().st_mtime
        if seen.get(key) == mtime:
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in marker_re.finditer(text):
            ctype, anchor, body = m.group(1), m.group(2), m.group(3).strip()
            if ctype == "source":
                continue  # 纯路径引用, 不单独入库 — 挂在其他条目的 source 字段才有意义
            if not body or len(body) < 8:
                continue
            fact = body[:500]
            if is_duplicate(fact):
                continue
            entry = {
                "ts": now_iso(),
                "type": ctype,
                "anchor_level": anchor or "model_authored",
                "fact": fact,
                "source": f"assistant交付/📚 学习资料/学习进度/{md.name}",
            }
            try:
                concl_file.parent.mkdir(parents=True, exist_ok=True)
                with open(concl_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                captured += 1
            except Exception:
                pass
        seen[key] = mtime

    try:
        seen_file.parent.mkdir(parents=True, exist_ok=True)
        seen_file.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return captured


def run_capture() -> dict:
    """执行一轮③+⑤: 被动捕获(双轨标记) + 主动提取(小模型兜底) + 增量摘要"""
    result = {"ts": now_iso(), "captured": 0, "summary_appended": False}

    # 被动路径: 无轮次门槛, 每次都扫 (纯本地解析, 毫秒级)
    passive = passive_capture()
    result["passive_captured"] = passive

    if not should_run():
        return result

    traj_text, mem_text = get_recent_context()
    if not traj_text.strip():
        return result

    prompt = f"""Extract 0-3 reusable knowledge items from these agent tool call records.

Records:
{traj_text[-1500:]}

Existing summary:
{mem_text[-200:]}

Output EXACTLY this JSON format (no markdown, no extra text):
{{"facts":[{{"fact":"...","type":"pattern"}}],"summary":"one English sentence ≤80 chars"}}

If nothing worth saving, return {{"facts":[],"summary":"Routine session."}}
Use ONLY field names: "fact" and "type" (not "复用知识" or translated names).
Type must be one of: pattern, correction, experience.

CRITICAL: Only extract knowledge conclusions/patterns/lessons (e.g. "完成了X/教训Y/决定Z/发现W").
Do NOT extract tool actions, shell commands, git commit messages, file paths, or technical chores
(e.g. "Commit ...", "Disable ... via PowerShell", "Check ... process", "E:\\path") — those are not knowledge.
If nothing is genuine knowledge, return empty facts.

DO NOT wrap in ```json``` code blocks. Output raw JSON only."""

    raw = call_model(prompt)
    if not raw:
        return result

    # 解析JSON (5层容错, 同symbolic_judge.py)
    parsed = None
    import re
    raw_clean = raw.strip()

    # 1: 直接解析
    try: parsed = json.loads(raw_clean)
    except: pass

    # 2: 从```json```代码块提取
    if not parsed:
        m = re.search(r'```(?:json)?\s*\n?(.+?)\n?```', raw_clean, re.DOTALL)
        if m:
            try: parsed = json.loads(m.group(1).strip())
            except: pass

    # 3: 修复常见错误后解析
    if not parsed:
        fixed = raw_clean
        fixed = re.sub(r'"复用知识"', '"fact"', fixed)  # 翻译字段名
        fixed = re.sub(r'"facts"::', '"facts":', fixed)   # 双冒号
        fixed = re.sub(r',\s*}', '}', fixed)               # 尾部多余逗号
        fixed = re.sub(r',\s*]', ']', fixed)               # 数组尾部逗号
        try: parsed = json.loads(fixed)
        except: pass

    # 4: 正则提取含facts的JSON对象
    if not parsed:
        m = re.search(r'\{[^{}]*"facts"[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw_clean, re.DOTALL)
        if m:
            try: parsed = json.loads(m.group(0))
            except: pass

    # 5: 修复被截断的JSON (补全缺失的花括号)
    if not parsed:
        truncated = raw_clean.rstrip()
        open_braces = truncated.count('{') - truncated.count('}')
        if open_braces > 0:
            truncated += '}' * open_braces
        try: parsed = json.loads(truncated)
        except: pass

    # 6: 提取所有 {\"fact\":...} 对象并汇总
    if not parsed:
        facts = []
        summary_text = ""
        for m in re.finditer(r'\{"fact"\s*:\s*"([^"]+)"\s*,\s*"type"\s*:\s*"([^"]+)"\}', raw_clean):
            facts.append({"fact": m.group(1), "type": m.group(2)})
        sm = re.search(r'"summary"\s*:\s*"([^"]+)"', raw_clean)
        if sm:
            summary_text = sm.group(1)
        if facts:
            parsed = {"facts": facts, "summary": summary_text or "Session with extracted knowledge."}

    if not parsed:
        result["error"] = f"parse_failed: {raw[:120]}"
        _log(result)
        return result

    # ③ 处理facts
    facts = parsed.get("facts", [])
    for f in facts:
        fact_text = f.get("fact", "").strip()
        fact_type = f.get("type", "pattern")
        if not fact_text or len(fact_text) < 10:
            continue
        if is_duplicate(fact_text):
            continue
        if _is_noise_fact(fact_text):  # 过滤轨迹噪音 (commit/命令/路径/工具动作)
            result.setdefault("noise_skipped", []).append(fact_text[:50])
            continue
        # 自动/capture: 写memory文件
        try:
            _write_capture(fact_text, fact_type)
            result["captured"] += 1
        except Exception as e:
            result.setdefault("errors", []).append(str(e)[:100])

    # ⑤ 增量摘要
    summary = parsed.get("summary", "").strip()
    if summary and len(summary) > 5:
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            line = f"## {ts}\n{summary}\n\n"
            SESSION_MEMORY.parent.mkdir(parents=True, exist_ok=True)
            with open(SESSION_MEMORY, "a", encoding="utf-8") as f:
                f.write(line)
            result["summary_appended"] = True
        except Exception as e:
            result.setdefault("errors", []).append(str(e)[:100])

    # ④ 每10次capture触发一次跨会话抽象 (@added 2026-07-24)
    if result["captured"] > 0:
        capture_dir = ROOT / "data" / "memory" / "on_demand"
        auto_files = list(capture_dir.glob("auto_capture_*.md")) if capture_dir.exists() else []
        if len(auto_files) >= 10:
            # 异步触发(通过last_run文件防重复,每天最多1次)
            last_run = ROOT / "data" / "state" / ".last_abstraction"
            do_abstraction = True
            if last_run.exists():
                try:
                    age_h = (time.time() - last_run.stat().st_mtime) / 3600
                    if age_h < 24:
                        do_abstraction = False
                except Exception:
                    pass
            if do_abstraction:
                last_run.parent.mkdir(parents=True, exist_ok=True)
                last_run.write_text(now_iso())
                run_abstraction()

    _log(result)
    return result

def _write_capture(fact: str, fact_type: str):
    """写入memory文件(兼容/capture路径)"""
    mem_dir = ROOT / "data" / "memory" / "on_demand"
    mem_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"auto_capture_{ts}_{_hash(fact)}.md"
    note = _epmem_note(fact)  # ep-mem 本地3字段结构化笔记 (A落点, 2026-08-17maintainer批)
    content = f"""---
name: auto-capture-{_hash(fact)}
description: {fact_type}: {fact[:80]}
metadata:
  type: {fact_type}
  source: auto_capture.py
  verified: false
---

{fact}

**Why:** 自动从会话中提取
**How to apply:** 待人工审查
{note}"""
    (mem_dir / fname).write_text(content, encoding="utf-8")


def _epmem_note(fact: str) -> str:
    """ep-mem 本地 3 字段结构化笔记 {topic, key_points, decisions} — A落点。
    零成本本地, 数据不出机; 失败静默, 不影响主流程。"""
    try:
        sys.path.insert(0, str(ROOT / "scripts" / "wheels"))
        from small_model import mem_summarize
        r = mem_summarize(fact) or {}
        topic = (r.get("topic") or "").strip() or fact[:60]
        lines = [f"\n**Topic:** {topic[:80]}"]
        kps = [str(k).strip()[:80] for k in (r.get("key_points") or []) if str(k).strip()]
        if kps:
            lines.append("**Key points:** " + "; ".join(kps[:3]))
        decs = [str(d).strip()[:80] for d in (r.get("decisions") or []) if str(d).strip()]
        if decs:
            lines.append("**Decisions:** " + "; ".join(decs[:3]))
        return "\n".join(lines)
    except Exception:
        return ""


def _is_noise_fact(fact: str) -> bool:
    """过滤轨迹噪音事实 (2026-08-17): 路径/commit/命令/工具动作 ≠ 知识洞察。
    实测 auto_capture 抓取噪音 10/12 (路径/commit/bash命令), 是 KG 碎片化根源之一。
    """
    f = fact.strip()
    if not f or len(f) < 10:
        return True
    # 1. Windows/UNC 路径 (含 GBK 乱码路径)
    if re.search(r"[A-Za-z]:[\\/]", f) or "\\\\" in f:
        return True
    # 2. commit/revert/merge 消息
    if re.match(r"^(Commit|Revert|Merge|Fix)\s", f, re.I):
        return True
    # 3. shell 命令特征 (via PowerShell/bash/python, 或 .py/.ps1 引用)
    if re.search(r"\bvia\s+(PowerShell|Bash|cmd|python|pip|git)\b", f, re.I):
        return True
    if re.search(r"\bpython\s+\S+\.py|\$?\w+\s+--|\.\/(\w+\.)+", f):
        return True
    # 4. 纯英文动作碎片 (无中文洞察 + 首词是命令动词)
    cmd_verbs = ("check", "show", "disable", "enable", "run", "fix", "revert",
                 "commit", "install", "remove", "build", "compile", "debug",
                 "diagnose", "scan", "post", "list", "search", "update", "edit")
    words = re.findall(r"[a-z]+", f.lower())
    if not re.search(r"[一-鿿]", f) and words and words[0] in cmd_verbs:
        return True
    # 5. 中文工具杂务 (工具动作动词 + 工具/目标词) — 但含结论标记的真知识豁免
    insight_markers = ("确认", "根因", "原因", "问题出在", "教训", "发现", "解决",
                       "结论", "决定", "方案", "注意", "踩坑", "建议", "因为")
    if not any(m in f for m in insight_markers):
        if re.search(r"(检查|编译|修改|禁用|启用|运行|安装|回退|诊断|提交|显示|扫描|重启|清理|切换).{0,15}"
                     r"(PowerShell|python|Bash|cmd|进程|配置|代理|文件|脚本|命令|日志|端口|缓存)", f):
            return True
    return False

def run_abstraction():
    """④ cross-session: 每10次capture触发一次跨会话模式提炼"""
    # 检查捕获计数
    capture_dir = ROOT / "data" / "memory" / "on_demand"
    if not capture_dir.exists():
        return
    auto_files = sorted(capture_dir.glob("auto_capture_*.md"))
    if len(auto_files) < 10:
        return

    # 读最近10条capture
    recent = []
    for f in auto_files[-10:]:
        try:
            text = f.read_text(encoding="utf-8")[:300]
            recent.append(text)
        except Exception:
            pass
    if not recent:
        return

    context = "\n---\n".join(recent)
    prompt = f"""从以下跨会话知识片段中找出:
1. 重复出现的模式 (≥2次)
2. 矛盾的知识点
3. 演化趋势

片段:
{context[-3000:]}

输出JSON:
{{"patterns": ["模式1", "模式2"], "contradictions": [], "trends": []}}
如果没有发现,返回空数组。"""

    raw = call_model(prompt, timeout_s=15)
    if not raw:
        return
    try:
        import re
        m = re.search(r'\{.*"patterns".*\}', raw, re.DOTALL)
        parsed = json.loads(m.group(0)) if m else json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return

    patterns = parsed.get("patterns", [])
    if patterns:
        patterns_file = ROOT / "data" / "state" / "patterns.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(patterns_file, "a", encoding="utf-8") as f:
            f.write(f"\n## {ts} (跨会话抽象)\n")
            for p in patterns:
                f.write(f"- {p}\n")
        # 标记已处理,避免重复
        for f in auto_files[-10:]:
            try:
                f.rename(f.with_suffix(".md.processed"))
            except Exception:
                pass

def _log(result: dict):
    """写审计日志"""
    try:
        CAPTURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CAPTURE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    except Exception:
        pass

def status():
    """查看统计"""
    print("=== auto_capture 状态 ===")
    if COOLDOWN_FILE.exists():
        data = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8"))
        print(f"  冷却计数: {data.get('count', 0)}/{ROUNDS_INTERVAL}")
        print(f"  上次更新: {data.get('ts', '?')[:19]}")
    else:
        print("  冷却计数: 0 (未初始化)")
    if CAPTURE_LOG.exists():
        lines = CAPTURE_LOG.read_text(encoding="utf-8").strip().split("\n")
        print(f"  捕获日志: {len(lines)} 条")
        for l in lines[-5:]:
            d = json.loads(l)
            print(f"    {d['ts'][:19]} | captured={d.get('captured',0)} summary={d.get('summary_appended',False)}")
    print(f"  去重窗口: {DEDUP_WINDOW_HOURS}h")
    print(f"  执行间隔: {ROUNDS_INTERVAL}轮")

def run_injection():
    """知识注入: 语义任务检测→深度分析(32B) or grep兜底

    @fixed 2026-07-25: ①同任务持续5+轮→触发32B语义深度分析 ②否则grep兜底
    窗口隔离: CC additionalContext天然session-scoped,不跨窗口
    """
    TRAJ = ROOT / "state" / "trajectory.jsonl"
    if not TRAJ.exists():
        return

    # 1. 从trajectory提取关键词+检测任务连续性
    try:
        lines = TRAJ.read_text(encoding="utf-8-sig").strip().split("\n")
        recent = lines[-10:]  # 最近10条
        texts = []
        domain_words = set()
        for l in recent:
            try:
                d = json.loads(l.strip())
                s = d.get("summary", "")
                if s and len(s) > 3:
                    texts.append(s)
                    # 提取领域词(≥5字母, CamelCase拆分)
                    import re as _re
                    dw = _re.findall(r'[A-Z][a-z]{3,}|[a-z]{5,}', s)
                    domain_words.update(w.lower() for w in dw[:3])
            except Exception:
                pass
        combined = " ".join(texts)
        en_words = _re.findall(r'[a-z]{4,}', combined.lower())
        cn_words = _re.findall(r'[一-鿿]{2,}', combined)
        keywords = set(en_words[:10] + cn_words[:5])
        noise = {'true', 'false', 'source', 'event', 'session', 'json', 'file', 'tool',
                 'post', 'event_id', 'this', 'that', 'with', 'from', 'have', 'been'}
        keywords = keywords - noise
        if not keywords:
            return

        # 检测任务连续性: 同一领域词在>5/10条中出现→深度分析
        task_focused = False
        dominant_domain = None
        for dw in domain_words:
            count = sum(1 for t in texts if dw.lower() in t.lower())
            if count >= 5:
                task_focused = True
                dominant_domain = dw
                break
    except Exception:
        return

    # 2. 搜索本地知识源
    sources = [
        ROOT / "knowledge" / "进度文件",
        ROOT / "认知系统迭代" / "大修",
        ROOT / "认知系统迭代" / "小修",
        ROOT / "data" / "memory" / "on_demand",
        # CC 原生 memory (双重保险)
        Path(os.environ.get("HOME", "")) / ".claude" / "projects" / "E-------claude-api-claude" / "memory",
    ]
    matches = []
    for src_dir in sources:
        if not src_dir.exists():
            continue
        for kw in list(keywords)[:5]:
            # 2026-08-03: 用 os.walk 原生搜索替换外部 grep.exe —
            # 外部 grep 每次 spawn 新建 conhost/OpenConsole 终端窗口 → 弹窗
            # (监控实证: auto_capture 触发 10+ 次 grep+conhost 三件套)
            try:
                kwl = kw.lower()
                for root, _dirs, files in os.walk(src_dir):
                    for fn in files:
                        if len(matches) >= 8:
                            break
                        if not fn.endswith(('.md', '.txt', '.json')):
                            continue
                        fp = os.path.join(root, fn)
                        try:
                            with open(fp, 'r', encoding='utf-8', errors='replace') as fh:
                                head = fh.read(20000)
                            if kwl in head.lower():
                                # 2026-08-04: 存相对路径(去绝对路径前缀), 防小模型误读 E:\ 反斜杠→乱码
                                try:
                                    rel = os.path.relpath(fp, ROOT)
                                except Exception:
                                    rel = fp
                                matches.append(rel)
                        except (OSError, IOError):
                            continue
                    if len(matches) >= 8:
                        break
            except Exception:
                pass
    if not matches and not task_focused:
        return
    matches = matches[:8]

    # 3. 注入决策: 深度分析 or grep兜底
    injections = []
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")[:12]

    if task_focused and dominant_domain:
        # 语义深度分析: 32B vLLM做任务级别的知识推荐
        ctx = "\n".join(matches[:8]) if matches else combined[:1000]
        prompt = f"""The user is working intensively on: {dominant_domain}. Recent context:
{combined[:800]}

Local knowledge matches:
{ctx[:1200]}

Generate 1-3 specific, actionable injection reminders. Each ≤80 chars.
Focus on: relevant past decisions, known patterns, tool recommendations.
Format: one per line starting with 💡
If nothing useful: NONE"""
        raw = call_model(prompt, timeout_s=15)
        if raw and 'NONE' not in raw:
            import re as _re
            injections = _re.findall(r'💡\s*(.+)', raw)
    else:
        # 轻量模式: grep匹配+小模型判断
        if matches:
            ctx = "\n".join(matches[:5])
            prompt = f"""Topics: {', '.join(list(keywords)[:8])}
Matches: {ctx[:1500]}
Generate 0-2 reminders. Format: 💡 per line. If nothing: NONE."""
            raw = call_model(prompt, timeout_s=10)
            if raw and 'NONE' not in raw:
                import re as _re
                injections = _re.findall(r'💡\s*(.+)', raw)

        # 模型不可用→grep兜底
        if not injections and matches:
            fnames = []
            for m in matches[:3]:
                f = Path(m).name[:40]
                if f not in fnames:
                    fnames.append(f)
            if fnames:
                sep = ', '
                injections = ['Local knowledge matches: ' + sep.join(fnames[:2]) + ' [自行甄别时效性]']

    # 4. 提取注入行 (模型不可用时用grep结果兜底)
    import re as _re
    injections = []
    if raw and 'NONE' not in raw:
        injections = _re.findall(r'💡\s*(.+)', raw)

    if not injections and matches:
        # 模型不可用或返回NONE → 用grep匹配路径生成简单提醒
        fnames = []
        for m in matches[:3]:
            f = Path(m).name[:40]
            if f not in fnames:
                fnames.append(f)
        if fnames:
            sep = ', '
            injections = ['Local knowledge matches: ' + sep.join(fnames[:2]) + ' [自行甄别时效性]']

    # 5. 写入 guidance_injection (SessionStart会读) + 问卷标记 (2026-08-03 A+B)
    try:
        gi_file = ROOT / ".claude" / "cls_state" / ".guidance_injection"
        gi_file.parent.mkdir(parents=True, exist_ok=True)
        ts = now_iso()
        import hashlib as _hl
        injection_id = _hl.md5((ts + str(injections[:2])).encode()).hexdigest()[:8]
        disclaimer = " (knowledge仅供参考:近期知识可信度更高,早期条目可能存在矛盾或已被推翻,请自行甄别)"
        # 问卷: 文本提问(对话义务, 非工具) — 要求回复中带标记, Stop hook 收卷
        survey = (f"\n📌 注入问卷(必答, 一行即可): 本轮你【采纳|参考|忽略】这条注入? "
                  f"请在回复中输出 [注入回应:{injection_id}:采纳] 或 [注入回应:{injection_id}:参考] "
                  f"或 [注入回应:{injection_id}:忽略], 附一句话理由。")
        gi_data = {
            "action": "; ".join(injections[:2]) + disclaimer + survey,
            "ts": ts,
            "source": "auto_capture.run_injection",
            "type": "knowledge_injection",
            "injection_id": injection_id,
        }
        gi_file.write_text(json.dumps(gi_data, ensure_ascii=False), encoding="utf-8")
        # 同时输出stdout JSON (CC PostToolUse可消费) — 带问卷标记
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "; ".join(injections[:2]) + survey,
            }
        }, ensure_ascii=False))
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        status()
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        # 强制测试: 跳过去重和冷却
        COOLDOWN_FILE.write_text(json.dumps({"count": ROUNDS_INTERVAL - 1, "ts": now_iso()}), encoding="utf-8")
        result = run_capture()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "inject":
        run_injection()
    else:
        result = run_capture()
        print(json.dumps(result, ensure_ascii=False))
        run_injection()  # 每次capture同步执行注入检查
