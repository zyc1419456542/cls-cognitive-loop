#!/usr/bin/env python3
"""process_inject.py — 面向过程的定向注入
===========================================
PostToolUse 每个工具调用后触发。
分析模型刚做了什么(tool_name+tool_input)→匹配历史模式+KG实体→定向建议。

原则:
  1. 只给和当前操作直接相关的建议 (不推荐二头弯举给吃饭的人)
  2. 有则注入,无则静默 (大部分轮次不注入)
  3. 注入≤80字,一行 (不占用上下文)

触发: PostToolUse.ps1 → & pythonw process_inject.py <tool_name> <json_file>

@since: 2026-07-31
"""

import json, sys, re, time, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OPS_FILE = ROOT / "data" / "state" / "ops_freq.jsonl"
KG_INDEX = ROOT / "knowledge" / "知识图谱" / "kg_index.json"
GAZE_LOG  = ROOT / "data" / "state" / "content_gaze_log.jsonl"
HUNT_INBOX = ROOT / "data" / "hunt" / "inbox.jsonl"
MONITOR_STATE = ROOT / "data" / "state" / "_monitoring_state.json"

# 2026-08-14 三层记忆架构: L3 边缘提醒候选源 = CC auto memory 索引 (Step1已治理成触发词风格)
# 位置: ~/.claude/projects/<项目路径编码>/memory/MEMORY.md
MEMORY_INDEX = Path.home() / ".claude" / "projects" / "E-------claude-api-claude" / "memory" / "MEMORY.md"

def _window_suffix() -> str:
    """2026-08-04 窗口隔离: 状态文件按 window_id 分文件, 多窗口互不覆盖。

    窗口id取 CLAUDE_CODE_SESSION_ID 前12位(与 longchain/注入收卷一致);
    无 session_id 时用 'default'(非 CC 环境/测试)。
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return ("_" + sid[:12]) if sid else "_default"

def _mon_state_path() -> Path:
    """当前窗口的监控状态文件路径。"""
    return ROOT / "data" / "state" / ("_monitoring_state" + _window_suffix() + ".json")

# ═══════════════════════════════════════════════════
# 监控状态机 (认知循环触发 → process_inject使用)
# ═══════════════════════════════════════════════════

MONITOR_TTL = 600        # 10分钟无Write自动退出
STABLE_EXIT = 5          # 连续5次评分稳定→退出
RANDOM_INTERVAL = 3      # 随机层: 每3轮一次概率抽检
RANDOM_RATE = 0.35       # 随机层: 35%概率触发

# 2026-08-14 L3b 强制回顾: 长链任务(≥20轮)强制注入记忆正文, 防"Lost in the Middle"被忽略。
# 组合触发: ①距上次强制≥20轮 ②检测到漂移 ③信息密度连续下降≥3。
# 为什么: 平时候选提醒(文件名,可忽略)在长任务里会被当噪声; 强制回顾注入正文摘要, 标注必须阅读。
FORCE_REVIEW_INTERVAL = 20   # 每20轮一次强制回顾
FORCE_REVIEW_DECLINE = 3     # 信息密度连续下降≥3 也触发

def activate_monitoring(tier: str = "L4") -> dict:
    """认知循环检测到L3/L4任务 → 激活监控模式"""
    p = _mon_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "active": True,
        "tier": tier,
        "activated_at": time.time(),
        "write_count": 0,
        "last_inject_round": 0,
        "consecutive_stable": 0,
        "total_rounds": 0,
        "window_id": os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:12],
    }
    p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state

def deactivate_monitoring(reason: str = "") -> dict:
    """退出监控模式"""
    state = {"active": False, "reason": reason, "deactivated_at": time.time()}
    p = _mon_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state

def is_monitoring_active() -> bool:
    """检查监控模式是否活跃"""
    p = _mon_state_path()
    if not p.exists():
        return False
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        if not s.get("active"):
            return False
        # TTL检查: 10分钟无活动→自动退出
        if time.time() - s.get("activated_at", 0) > MONITOR_TTL:
            deactivate_monitoring("TTL expired")
            return False
        return True
    except Exception:
        return False

def _load_monitor_state() -> dict:
    p = _mon_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"active": False, "write_count": 0, "total_rounds": 0, "last_inject_round": 0, "last_forced_round": 0, "consecutive_stable": 0}

def _save_monitor_state(s: dict):
    p = _mon_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    s["active"] = True  # 调用此函数说明仍在监控
    s["window_id"] = os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:12]
    p.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")

def _tick_monitor(tool_name: str) -> dict:
    """每次PostToolUse调用: 更新计数器 + 检查退出条件"""
    s = _load_monitor_state()
    if not s.get("active"):
        return s

    s["total_rounds"] = s.get("total_rounds", 0) + 1

    if tool_name in ("Write", "Edit"):
        s["write_count"] = s.get("write_count", 0) + 1
        s["activated_at"] = time.time()  # 刷新TTL

    # 退出条件②: 连续N次content_gaze稳定
    if tool_name in ("Write", "Edit"):
        decline = _check_gaze_decline()
        if decline == 0:
            s["consecutive_stable"] = s.get("consecutive_stable", 0) + 1
        else:
            s["consecutive_stable"] = 0
        if s["consecutive_stable"] >= STABLE_EXIT:
            deactivate_monitoring("连续{}轮评分稳定".format(STABLE_EXIT))
            s["active"] = False
            return s

    _save_monitor_state(s)
    return s


# ═══════════════════════════════════════════════════
# 随机层 — 概率抽检 (防渐进退化)
# ═══════════════════════════════════════════════════

RANDOM_INJECT_MESSAGES = [
    "已连续写入N次,建议Read相关文件或WebSearch确认方向",
    "监控模式运行中,当前方向是否偏离原始任务目标?",
    "建议暂停写入,先检查已有产出的一致性",
    "派发多个子代理时,确认每个都在正确方向上",
]

def _hunt_retrieve(query: str, top: int = 1) -> str | None:
    """从 hunt 经验库语义检索与当前操作最相关的错误模式(方案C: 时机随机, 内容检索)。

    复用 ChromaDB 库(bge-zh embedding)。失败静默返回 None(随机抽检退回固定消息)。

    2026-08-03: anaconda 无法 in-process import venv 的 numpy/chromadb(源码目录冲突),
    改用 uv 真实 python 子进程隔离执行, 零环境污染。
    """
    try:
        import subprocess as _sp
        # @merge 2026-08-16 一号融合: uv 真实 python 优先(一号实测无环境污染) → 缺失时回退 sys.executable 派生(二号适配)
        _real_py = r"<HOME>\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe"
        if not os.path.isfile(_real_py):
            _real_py = sys.executable
            if _real_py.lower().endswith("pythonw.exe"):
                _real_py = os.path.join(os.path.dirname(_real_py), "python.exe")
        if not os.path.isfile(_real_py):
            return None
        _site = ROOT / "scripts" / "wheels" / "tribal_mcp" / ".venv" / "Lib" / "site-packages"
        _src = ROOT / "scripts" / "wheels" / "tribal_mcp" / "src"
        _code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(_site)!r})\n"
            f"sys.path.insert(0, {str(_src)!r})\n"
            "from mcp_server_tribal.services.chroma_storage import ChromaStorage\n"
            "import asyncio\n"
            f"s = ChromaStorage(persist_directory={str(ROOT / 'scripts' / 'wheels' / 'tribal_mcp' / 'chroma_db')!r})\n"
            "rows = asyncio.run(s.search_similar(__import__('sys').stdin.read(), max_results=1))\n"
            "if rows:\n"
            "    r = rows[0]\n"
            "    print(json.dumps({'err': r.context.error_message, 'lesson': r.solution.description}, ensure_ascii=False))\n"
        )
        proc = _sp.run([_real_py, "-c", _code], input=query, capture_output=True,
                       text=True, timeout=15, encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        import json as _json
        d = _json.loads(proc.stdout.strip())
        err = (d.get("err") or "").strip()
        lesson = (d.get("lesson") or "").strip()
        if len(err) < 10:
            return None
        return f"hunt经验: {err[:100]} → {lesson[:100]}"
    except Exception:
        return None


def _random_inject(s: dict, tool_name: str, tool_input: str = "") -> str | None:
    """随机层: 每RANDOM_INTERVAL轮一次概率抽检(方案C: 时机随机, 内容从hunt库语义检索)"""
    if tool_name not in ("Write", "Edit", "Agent"):
        return None

    round_num = s.get("total_rounds", 0)
    last_inj = s.get("last_inject_round", 0)

    # 间隔不够 → 不触发
    if round_num - last_inj < RANDOM_INTERVAL:
        return None

    # 概率判定
    import random
    if random.random() > RANDOM_RATE:
        return None

    # 方案C: 先试 hunt 语义检索(内容与当前操作相关), 失败退固定消息
    msg = None
    is_hunt = False
    if tool_input and len(tool_input) > 5:
        msg = _hunt_retrieve(tool_input[:300])
        if msg:
            is_hunt = True
    if not msg:
        # 选一条固定消息
        write_c = s.get("write_count", 0)
        msg = random.choice(RANDOM_INJECT_MESSAGES)
        msg = msg.replace("N", str(write_c))

    # 更新最后注入轮次
    s["last_inject_round"] = round_num
    _save_monitor_state(s)

    # 2026-08-03: 问卷标记 — 只对 hunt 注入带(固定消息是监控噪音, 不问卷);
    # 格式与 auto_capture 一致, Stop hook 收卷进 injection_feedback.jsonl
    if is_hunt:
        import hashlib as _hl
        _iid = _hl.md5((str(round_num) + msg[:50]).encode()).hexdigest()[:8]
        msg += (" 📌 注入问卷(必答): 本轮你【采纳|参考|忽略】这条经验? "
                f"请在回复中输出 [注入回应:{_iid}:采纳] 或 [注入回应:{_iid}:参考] "
                f"或 [注入回应:{_iid}:忽略], 附一句话理由。")

    text = "【监控抽检-第{}轮】{}".format(round_num, msg)
    try:  # P7 统一审计
        from knowledge_inject_audit import log as _audit_ki
        _audit_ki("process_inject", text, "随机抽检" if not is_hunt else "hunt经验", tool_name)
    except Exception:
        pass
    return text


# ── 领域检测 ──────────────────────────────────

DOMAIN_PATTERNS = [
    (r"等离子|推力器|阴极|阳极|Langmuir|探针|EEPF|PIC|仿真|电推", "ep"),
    (r"cad|build123d|STEP|装配|约束图|FreeCAD|零件|建模", "cad"),
    (r"回测|因子|策略|止损|仓位|K线|CPCV|量化|quant|trading", "quant"),
    (r"认知循环|cognitive|cls_brain|hook|闸门|brain|trajectory|symbol", "cls"),
    (r"课件|试题|试卷|教辅|数学|证明|变式|audit_solution", "teaching"),
    (r"swarm|workflow|agent|管线|pipeline|编排|并行", "orchestration"),
]


def _detect_domain(text: str, file_path: str = "") -> str:
    combined = (text + " " + file_path).lower()[:1000]
    for pat, dom in DOMAIN_PATTERNS:
        if re.search(pat, combined, re.IGNORECASE):
            return dom
    return "general"


# ── KG 匹配 ───────────────────────────────────

def _match_kg(query: str, max_results: int = 3) -> list[str]:
    """在知识图谱中搜索与query相关的实体"""
    if not KG_INDEX.exists():
        return []
    try:
        kg = json.loads(KG_INDEX.read_text(encoding="utf-8"))
        entities = kg.get("entities", {})
        query_lower = query.lower()
        matches = []
        for eid, e in entities.items():
            name = e.get("name", "")
            summary = e.get("summary", "")
            domain = e.get("domain", "")
            combined = f"{name} {summary} {domain}".lower()
            # 关键词匹配
            score = 0
            for word in query_lower.split()[:20]:
                if word in combined:
                    score += 1
            if score > 0:
                matches.append((score, e))
        matches.sort(key=lambda x: -x[0])
        return [f"{m[1].get('name','?')}: {m[1].get('summary','')[:30]}" for m in matches[:max_results]]
    except Exception:
        pass
    return []


def _match_cards(query: str, max_results: int = 3) -> list[str]:
    """L3 候选: 知识卡片匹配 (P4 卡片化, 2026-08-16 夜: 卡片比KG实体高信息密度, 优先于 _match_kg)"""
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _w = str(_Path(__file__).resolve().parent)
        if _w not in _sys.path:
            _sys.path.insert(0, _w)
        from knowledge_nav_core import load_cards, prescreen
    except Exception:
        return []
    cards = load_cards().get("cards", {}) or {}
    if not cards:
        return []
    top = prescreen(query, cards, top_n=max_results)
    if not top:
        return []
    out = []
    for tup in top:
        c = tup[2]
        title = (c.get("title") or "").strip()[:24] or "无题"
        date = c.get("date") or ""
        content = (c.get("content") or "").strip()[:30]
        out.append(f"{title}({date}): {content}")
    return out


# ═══════════════════════════════════════════════
# 2026-08-14 三层记忆架构 L3: 候选推荐 (读 CC MEMORY.md 索引)
# ═══════════════════════════════════════════════
# 原则: 小模型只做候选推荐(匹配MEMORY.md索引触发词), 决定权在大模型。
# 文本详细+分层: 为什么推荐 | 候选分条(带文件路径) | 使用指引, 防止被当噪声忽略。
# 中文分词: query/MEMORY描述都做 2-gram, 解决 _match_kg 中文整句 split 匹配失败的问题。

_CN_2GRAM_STOP = {"这个", "那个", "我们", "他们", "没有", "已经", "还是", "就是", "什么", "怎么",
                  "一个", "进行", "处理", "当前", "操作", "模型", "文件", "系统", "使用",
                  # 模板词 (MEMORY.md 索引固定前缀, 所有条目都有, 无区分度)
                  "时读", "触发", "读时"}

# query 侧功能动词/泛词 — 在几乎所有写作场景出现, 命中它们无区分度 (DF倒置陷阱):
# 这些词 df 可能低(索引里恰好出现1次)但本身是泛词, 会误匹配无关条目。
_QUERY_CN_STOP = {"分析", "判断", "生成", "输出", "使用", "计算", "处理", "完成", "需要",
                  "进行", "以及", "包括", "相关", "根据", "通过", "实现", "当前", "本次",
                  "代码", "函数", "文件", "脚本", "逻辑", "方法"}


def _query_2grams(text: str) -> set:
    """query 侧 2-gram: 剔除非区分度功能词 (分析/判断/生成等) 后再匹配"""
    grams = set()
    for m in re.finditer(r'[a-zA-Z0-9_\.\-/\\]+', text):
        grams.add(m.group().lower())
    for seg in re.findall(r'[一-鿿]+', text):
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if g not in _CN_2GRAM_STOP and g not in _QUERY_CN_STOP:
                grams.add(g)
    return grams

# 2026-08-14: IDF 降权 — 索引里高频出现的泛词, 权重低(无区分度)。df>=4 视为泛词。
_DF_HIGH_THRESHOLD = 4


def _cn_2grams(text: str) -> set:
    """中文 2-gram + 英文/数字 token, 用于跨文本模糊匹配"""
    grams = set()
    # 英文/数字 token
    for m in re.finditer(r'[a-zA-Z0-9_\.\-/\\]+', text):
        grams.add(m.group().lower())
    # 中文 2-gram (跳过停用词开头)
    for seg in re.findall(r'[一-鿿]+', text):
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if g not in _CN_2GRAM_STOP:
                grams.add(g)
    return grams


def _memory_gram_df() -> dict:
    """MEMORY.md 索引的 2-gram 文档频率表 (IDF)。缓存到模块级避免每次重算。

    过滤模板词(触发/时读)后统计; df 高的 = 泛词, 匹配时降权。
    """
    global _MEMORY_DF_CACHE
    if _MEMORY_DF_CACHE is not None:
        return _MEMORY_DF_CACHE
    df = {}
    try:
        lines = MEMORY_INDEX.read_text(encoding="utf-8").splitlines()
    except Exception:
        _MEMORY_DF_CACHE = df
        return df
    for line in lines:
        m = re.match(r'- \[.*?\]\(([^)]+\.md)\)\s*—\s*(.*)$', line.strip())
        if not m:
            continue
        for g in _cn_2grams(m.group(2)):
            df[g] = df.get(g, 0) + 1
    _MEMORY_DF_CACHE = df
    return df


_MEMORY_DF_CACHE = None  # 模块级缓存


def _match_memory(query: str, max_results: int = 3) -> list[dict]:
    """候选推荐: 从 CC MEMORY.md 索引匹配与 query 相关的记忆条目 (IDF 加权)

    返回 [{file, desc, score, matched}] — file 是可 Read 的 .md 文件名, desc 是索引触发词描述。
    打分: 命中词的区分度 = max(1, 5 - df) (df=1 专词得4分, df>=5 泛词得1分)。
    score<2 (只有泛词命中) 不返回 — 防"分析/设计"等泛词误匹配。
    """
    if not MEMORY_INDEX.exists():
        return []
    try:
        lines = MEMORY_INDEX.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    q_grams = _query_2grams(query)
    if not q_grams:
        return []

    df = _memory_gram_df()
    hits = []
    for line in lines:
        m = re.match(r'- \[.*?\]\(([^)]+\.md)\)\s*—\s*(.*)$', line.strip())
        if not m:
            continue
        fname, desc = m.group(1), m.group(2)
        d_grams = _cn_2grams(desc)
        overlap = q_grams & d_grams
        if not overlap:
            continue
        # IDF 加权: 专词权重高, 泛词权重低
        score = 0
        for g in overlap:
            g_df = df.get(g, 1)
            score += max(1, 5 - g_df)
        # 单命中降权: 只撞上1个词 = 弱相关(可能是"判断/分析"等泛动词恰好出现) → 权重减半
        if len(overlap) == 1:
            score = score * 0.5
        # 过滤纯泛词弱命中 (score<2 = 只有一个高频泛词)
        if score < 2:
            continue
        hits.append({"file": fname, "desc": desc, "score": score, "matched": overlap})
    hits.sort(key=lambda h: -h["score"])
    return hits[:max_results]


def _format_memory_candidates(query: str, hits: list[dict], limit: int = 3) -> str:
    """候选推荐注入文本: 为什么推荐 | 候选分条 | 使用指引

    格式参考 cognitive_gate 四段式 — 有理由解释(消噪声感), 候选带路径(可Read), 明确可忽略。
    """
    if not hits:
        return ""
    lines = []
    # 第一段: 这是什么 + 为什么 (展示最高命中条目的关键匹配词, 过滤模板词)
    top = hits[0]["matched"]
    df = _memory_gram_df()
    top_strong = [g for g in sorted(top) if df.get(g, 1) < _DF_HIGH_THRESHOLD][:4]
    top_str = "/".join(top_strong) if top_strong else "关键词"
    lines.append("【记忆候选】检测到当前操作与以下本地记忆可能相关(匹配: {}):".format(top_str))
    # 第二段: 候选分条 (带文件路径)
    for i, h in enumerate(hits[:limit], 1):
        lines.append("  {}. {} — {}".format(i, h["file"], h["desc"]))
    # 第三段: 使用指引
    lines.append("  请自行判断: 相关则 Read 对应 .md 参考, 无关则忽略。候选非结论, 不占用决策权。")
    return "\n".join(lines)


# ═══════════════════════════════════════════════
# L3b 强制回顾 (2026-08-14): 长链任务防记忆被忽略
# ═══════════════════════════════════════════════
# 与 L3a 候选提醒(文件名,可忽略)不同: 强制回顾把记忆**正文摘要**注入, 标注"必须阅读"。
# 触发: ①距上次强制≥FORCE_REVIEW_INTERVAL(20)轮 ②检测到漂移 ③信息密度连续下降≥3。
# 背景: "Lost in the Middle" — 长上下文中间位置的数据易被模型忽略; 平时候选会被当噪声。

def _read_memory_body(fname: str, max_len: int = 160) -> str:
    """读 CC memory 文件正文前 max_len 字, 用于强制回顾注入"""
    try:
        p = MEMORY_INDEX.parent / fname
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8")
        # 去掉 frontmatter (--- 到 ---)
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                text = parts[2]
        text = text.strip()
        return text[:max_len]
    except Exception:
        return ""


def _detect_drift() -> bool:
    """drift_log.jsonl 最近5条是否含漂移"""
    try:
        dl = ROOT / "data" / "state" / "drift_log.jsonl"
        if not dl.exists():
            return False
        lines = [json.loads(l.strip()) for l in open(dl, encoding="utf-8", errors="replace").readlines()[-5:] if l.strip()]
        return any(d.get("drifted") for d in lines)
    except Exception:
        return False


def _check_force_review(mon: dict, query: str) -> str | None:
    """组合触发判断: ①≥20轮间隔 ②漂移 ③信息密度下降≥3

    命中 → 返回强制回顾注入文本(记忆正文); 未命中 → None。
    """
    # ① 轮次阈值: 距上次强制 ≥20 轮
    total = mon.get("total_rounds", 0)
    last_forced = mon.get("last_forced_round", 0)
    interval_hit = (total >= FORCE_REVIEW_INTERVAL) and (total - last_forced >= FORCE_REVIEW_INTERVAL)

    # ② 漂移检测
    drift_hit = _detect_drift()

    # ③ 信息密度连续下降
    decline_hit = _check_gaze_decline() >= FORCE_REVIEW_DECLINE

    if not (interval_hit or drift_hit or decline_hit):
        return None

    # 触发 → 匹配记忆并读正文
    hits = _match_memory(query, max_results=2)
    if not hits:
        return None

    lines = ["【强制回顾·记忆正文】检测到长链任务({}轮)可能漂移/信息密度下降, 以下记忆已注入正文, 请阅读后再继续:".format(total)]
    for h in hits:
        body = _read_memory_body(h["file"])
        lines.append("  ◈ {} — {}".format(h["file"], h["desc"]))
        if body:
            lines.append("    ▸ {}".format(body.replace("\n", " ")))
    lines.append("  这是强制回顾(非候选): 相关记忆应纳入当前推理, 无关可忽略但请确认。")
    return "\n".join(lines)


# ── 操作频率 ──────────────────────────────────

def _count_recent(tool_name: str, file_path: str = "", n: int = 10) -> int:
    """最近N次操作中某工具的调用次数"""
    if not OPS_FILE.exists():
        return 0
    try:
        lines = open(OPS_FILE, encoding="utf-8").readlines()
        count = 0
        for line in lines[-n*2:]:  # ops记录可能含其他数据,多读点
            if line.strip():
                d = json.loads(line)
                if d.get("tool") == tool_name:
                    if file_path and file_path not in d.get("input_preview", ""):
                        continue
                    count += 1
        return count
    except Exception:
        return 0


def _count_file_edits(file_path: str) -> int:
    """同一文件最近被Edit的次数(检测修复循环)

    @fix 2026-08-14: open 加 errors="replace" — ops_freq.jsonl 可能混入 GBK 字节(历史写入污染),
    原 utf-8 硬解码抛 UnicodeDecodeError 被 except 吞 → 修复循环检测永远返回 0。
    """
    if not OPS_FILE.exists():
        return 0
    try:
        lines = open(OPS_FILE, encoding="utf-8", errors="replace").readlines()
        count = 0
        for line in lines[-20:]:
            if line.strip():
                d = json.loads(line)
                if d.get("tool") in ("Write", "Edit") and file_path in d.get("input_preview", ""):
                    count += 1
        return count
    except Exception:
        return 0


def _check_gaze_decline() -> int:
    """连续信息密度下降次数"""
    if not GAZE_LOG.exists():
        return 0
    try:
        entries = []
        for line in open(GAZE_LOG, encoding="utf-8").readlines()[-10:]:
            if line.strip():
                entries.append(json.loads(line))
        count = 0
        for e in reversed(entries):
            if e.get("info_density") == "下降":
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


def _has_recent_error() -> str | None:
    """最近是否有Bash错误→返回错误摘要"""
    if not HUNT_INBOX.exists():
        return None
    try:
        lines = open(HUNT_INBOX, encoding="utf-8").readlines()
        if not lines:
            return None
        last = json.loads(lines[-1])
        age = time.time() - (last.get("ts", 0) if isinstance(last.get("ts"), (int, float)) else 0)
        if age < 120:  # 2分钟内
            return last.get("error", "")[:60]
    except Exception:
        pass
    return None


# ── anchor_analysis 知识候选 (2026-08-21) ──
ANCHOR_ANALYSIS_FILE = ROOT / "data" / "state" / "anchor_analysis.json"

def _check_anchor_analysis() -> str | None:
    """读 anchor_analysis.json，根据任务域注入相关知识候选。
    返回注入文本或 None（不注入）。
    """
    if not ANCHOR_ANALYSIS_FILE.exists():
        return None
    try:
        data = json.loads(ANCHOR_ANALYSIS_FILE.read_text("utf-8"))
    except Exception:
        return None

    domain = data.get("domain", "")
    hints = data.get("knowledge_hints", [])
    stage = data.get("stage", "")
    summary = data.get("summary", "")

    if not domain or domain == "unknown":
        return None

    # 构造注入文本（≤80字，不占用上下文）
    parts = []
    if hints:
        parts.append("相关知识: " + "、".join(hints[:3]))
    if stage:
        parts.append(f"阶段: {stage}")

    if parts:
        return "【锚点供血】" + " | ".join(parts)
    return None


# ── 主入口 ────────────────────────────────────

def inject(tool_name: str, tool_input: dict = None, file_path: str = "",
           error: str = "", tool_output: str = "") -> str | None:
    """分析当前操作 → 定向建议 → 注入文本 或 None(不注入)

    三层门控:
      0. 监控模式未激活 → 只做关键模式检测,不介入
      1. 精确层: 反模式匹配 (<1ms)
      2. 随机层: 概率抽检 (每3轮,35%)
    """

    tool_name = tool_name.lower()
    tool_input = tool_input or {}

    # ── 门控: 更新计数器+检查退出 ──
    mon = _tick_monitor(tool_name)
    monitoring = mon.get("active", False)

    # ── 关键模式: 即使未激活监控也检测 (总是生效) ──
    # 修复循环 + Agent修复 → 这些是致死模式,必须随时拦截
    always_check = None

    if tool_name in ("write", "edit"):
        fp = file_path or tool_input.get("file_path", "")
        edit_count = _count_file_edits(fp)
        if edit_count >= 3:
            always_check = "【关键警告】该文件已修改{}次,修复>3轮不会收敛(incident-log#17)。建议先Read/WebSearch确认根因。".format(edit_count)

    elif tool_name in ("agent", "task"):
        desc = tool_input.get("description", "")[:60]
        if "fix" in desc.lower() or "修复" in desc or "修" in desc:
            always_check = "【关键警告】Agent任务含修复关键词。注意: 连续修复>3轮不会收敛(incident-log#17)。建议先确认根因。"

    if always_check:
        return always_check

    # ── 监控模式未激活 → 不继续 (避免打扰) ──
    if not monitoring:
        return None

    # ── anchor_analysis 知识候选注入 (2026-08-21): 根据 ANCHOR 锚点推断的任务域注入相关知识 ──
    anchor_inject = _check_anchor_analysis()
    if anchor_inject:
        return anchor_inject

    # ── L3b 强制回顾 (2026-08-14): 长链任务(≥20轮)/漂移/信息密度下降 → 注入记忆正文 ──
    # 优先级高于 L3a 候选提醒 — 长链漂移时先强制回顾, 平时才走候选。
    if tool_name in ("write", "edit", "bash", "read"):
        fr_query = ""
        if tool_name in ("write", "edit"):
            fr_content = tool_input.get("content", "") or tool_input.get("new_string", "")
            fr_fp = file_path or tool_input.get("file_path", "")
            fr_query = (fr_content[:300] + " " + fr_fp) if fr_content else fr_fp
        elif tool_name == "read":
            fr_query = file_path or tool_input.get("file_path", "")
        elif tool_name == "bash":
            fr_query = tool_input.get("command", "")[:300]
        if fr_query:
            forced = _check_force_review(mon, fr_query)
            if forced:
                # 记录本轮强制回顾, 供下次 ≥20 轮判定
                mon["last_forced_round"] = mon.get("total_rounds", 0)
                _save_monitor_state(mon)
                return forced

    # ═══════════════════════════════════════════
    # 精确层 (监控模式激活时)
    # ═══════════════════════════════════════════

    precise_result = None

    if tool_name in ("write", "edit"):
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
        fp = file_path or tool_input.get("file_path", "")
        if not content and not fp:
            return None

        domain = _detect_domain(content[:500], fp)
        parts = []

        # ① 内容凝视趋势
        decline = _check_gaze_decline()
        if decline >= 2:
            parts.append(f"信息密度连续{decline}次下降,建议换策略或先查外部资料")

        # ② 记忆候选推荐 (2026-08-14 三层记忆架构 L3: 小模型推荐候选, 大模型决定)
        query = (content[:300] + " " + fp) if content else fp
        mem_hits = _match_memory(query, max_results=3) if domain != "general" else []
        if mem_hits:
            cand = _format_memory_candidates(query, mem_hits)
            if cand:
                parts.append(cand)

        if parts:
            precise_result = "【过程注入-{}】模型刚写入{}。 {}".format(
                "Write" if tool_name == "write" else "Edit",
                Path(fp).name if fp else "文件",
                " | ".join(parts)
            )

    elif tool_name == "bash":
        if error:
            recent_err = _has_recent_error()
            if recent_err:
                precise_result = "【过程注入-Bash】命令执行失败: {}。最近相似错误: {}。建议先Read/WebSearch查原因。".format(
                    error[:60], recent_err[:40])

    elif tool_name == "read":
        fp = file_path or tool_input.get("file_path", "")
        if fp and any(kw in fp for kw in ["knowledge", "memory", "wheels", "hooks", "认知", "schema"]):
            kg = _match_cards(fp, max_results=2) or _match_kg(fp, max_results=2)  # P4: 卡片优先, KG兜底
            if kg:
                precise_result = "【过程注入-Read】刚读{} | 相关知识: {}".format(
                    Path(fp).name, "; ".join(kg))
                try:  # P7 统一审计
                    from knowledge_inject_audit import log as _audit_ki
                    _audit_ki("process_inject", precise_result, "过程注入-Read", f"file:{Path(fp).name}")
                except Exception:
                    pass

    if precise_result:
        return precise_result

    # ═══════════════════════════════════════════
    # 随机层 (监控模式激活 + 精确层无匹配时)
    # ═══════════════════════════════════════════

    # 2026-08-03 方案C: tool_input 传给随机层做 hunt 语义检索
    ti_str = ""
    try:
        if isinstance(tool_input, dict):
            ti_str = " ".join(str(v) for v in tool_input.values() if isinstance(v, str))
        elif isinstance(tool_input, str):
            ti_str = tool_input
    except Exception:
        ti_str = ""
    return _random_inject(mon, tool_name, ti_str)


# ── CLI ───────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        sys.exit(0)

    tool_name = sys.argv[1]

    # 读取 tool_input JSON (从文件或stdin)
    tool_input = {}
    if len(sys.argv) >= 3:
        input_file = sys.argv[2]
        try:
            if input_file == "-":
                tool_input = json.loads(sys.stdin.read())
            elif Path(input_file).exists():
                tool_input = json.loads(Path(input_file).read_text(encoding="utf-8"))
        except Exception:
            pass

    file_path = ""
    error = ""
    for i, arg in enumerate(sys.argv):
        if arg == "--file" and i+1 < len(sys.argv):
            file_path = sys.argv[i+1]
        if arg == "--error" and i+1 < len(sys.argv):
            error = sys.argv[i+1]

    result = inject(tool_name, tool_input, file_path, error)
    if result:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": result
            }
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
