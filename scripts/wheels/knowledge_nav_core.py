#!/usr/bin/env python3
"""knowledge_nav_core.py — 知识卡片公共核心 (P2, 2026-08-16 夜抽取)
=============================================================
卡片存储/压缩/导航三件套, 供 knowledge_cards(压缩器) + unified_inject(导航) 共用。
后续 P3 灵感S5回想 / P4 process_inject候选 也走这里 (轮子可换: call_fn 注入 provider)。

三件套:
  ① 存储    load_cards / save_cards / file_hash
  ② 压缩    scan_files / compress_batch(items, call_fn)   [call_fn: (messages, max_tokens)->text]
  ③ 导航    bigrams / prescreen / format_nav_cards
"""
import hashlib, json, os, re, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CARDS_FILE = ROOT / "knowledge" / "知识图谱" / "kg_cards.json"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = "bge-m3"
SOURCES = [
    ROOT / "assistant交付" / "📚 学习资料" / "学习进度",
    ROOT / "knowledge" / "进度文件",
    ROOT / "knowledge" / "05_CLS认知系统架构" / "认知系统迭代",
    # 2026-08-27: CAD 学习沉淀入卡片总线 (此前最大断链 — cad-learn 约束图对 KG 完全不可见)
    # 条目可带patterns元组; 缺省仍只扫 *.md
    (ROOT / "knowledge" / "CAD设计", ("*.md", "*_constraint_graph.json")),
]
MAX_CARD_TEXT = 4000   # 压缩输入单文件上限
BATCH_SIZE = 5          # 每批文件数
NAV_TOP_N = 20          # 导航粗筛取前N

SYSTEM = (
    "你是knowledge压缩器。把历史工作记录压缩成一张认知卡片, 供未来的 AI 窗口做知识导航。\n"
    "铁律:\n"
    "① 这份knowledge的历史记录很多早期结论已被推翻 — 你必须谨慎鉴别: 只忠实压缩记录里写的东西, "
    "不要把记录当真理转述, 压缩本身就是\"提供知识, 不保证对错\"。\n"
    "② 时间越靠后的记录越可信, 压缩时保留记录里的日期, 没有明确日期就从文件名推断(如 20260816_xxx → 2026-08-16)。\n"
    "③ 卡片必须短: 内容≤60字, 教训/亮点各≤30字, 没有就写\"无\"。\n"
    "④ 元数据提取(供后续智能检索): domain=领域标签(如ep/cad/cls/quant/teaching/orchestration/general); "
    "entities=该卡片提及的关键实体名(如霍尔推力器/知识卡片/opencode), 2-5个; "
    "task_type=任务类型(如分析/建模/重构/审计/实验/交付/学习/维护)。\n"
    "输出严格 JSON(每批一行一个对象, 别用数组包): "
    '{"file":"<文件名>","date":"<YYYY-MM-DD或空>","title":"<任务一句话>","content":"<2-3句>","lesson":"<教训>","highlight":"<亮点>",'
    '"domain":"<领域>","entities":["<实体1>","<实体2>"],"task_type":"<类型>"}\n'
    "每批最多5个文件, 每个文件恰好输出一行 JSON。"
)


# ═══════════════ ① 卡片存储 ═══════════════

def file_hash(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def load_cards() -> dict:
    """读 kg_cards.json, 缺失/损坏返回空结构 {"_version":1, "cards":{}}"""
    if CARDS_FILE.exists():
        try:
            return json.loads(CARDS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"_version": 1, "cards": {}}


def save_cards(data: dict):
    CARDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CARDS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════ 向量工具 ═══════════════

def _embed(text: str, model: str = EMBED_MODEL) -> list | None:
    """调 Ollama /api/embed 获取 embedding (bge-m3 1024维)。失败返回 None。"""
    try:
        import urllib.request
        body = json.dumps({"model": model, "input": text}).encode()
        req = urllib.request.Request(f"{OLLAMA_URL}/api/embed", data=body,
                                    headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
        embeddings = d.get("embeddings") or []
        return embeddings[0] if embeddings else None
    except Exception:
        return None


def _cosine(a: list, b: list) -> float:
    """余弦相似度 (纯 Python, 无 numpy)"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def embed_cards(cards: dict) -> dict:
    """给每张卡片预计算 embedding 并存回 cards dict (原地修改)。
    增量: 只计算无 embedding 的卡片。返回 {"rel": [embedding]} 新增嵌入数。
    """
    new = {}
    total = len(cards)
    for i, (rel, c) in enumerate(cards.items()):
        if not isinstance(c, dict):
            continue
        if c.get("embedding"):
            continue
        # 用 title + content 拼接做 embedding (比单独 title 更有语义)
        text = f"{c.get('title', '')} {c.get('content', '')}"
        emb = _embed(text)
        if emb:
            c["embedding"] = emb  # 原地写入
            new[rel] = emb
            if (i + 1) % 50 == 0:
                print(f"  embed: {i+1}/{total} ({len(new)} new)")
                time.sleep(0.5)  # 防 Ollama 过载
    return new


def precompute_embeddings():
    """一键预计算所有卡片 embedding (由 CardBuilder 调用, 增量只算新卡)。"""
    data = load_cards()
    cards = data["cards"]
    new = embed_cards(cards)
    if new:
        save_cards(data)
        print(f"precompute: +{len(new)} embeddings, total={sum(1 for c in cards.values() if isinstance(c,dict) and c.get('embedding'))}/{len(cards)}")
    else:
        print("precompute: 0 new embeddings needed")
    return new


# ═══════════════ ② 卡片压缩 ═══════════════

def scan_files():
    """扫数据源, 返回 [(file, hash, text)] (跳过 README/二进制/过大的/过短的)。
    SOURCES 条目: Path(只扫*.md) 或 (Path, patterns元组)。"""
    out = []
    for src in SOURCES:
        src_dir, patterns = src if isinstance(src, tuple) else (src, ("*.md",))
        if not src_dir.exists():
            continue
        seen = set()
        for pat in patterns:
            for f in sorted(src_dir.rglob(pat)):
                if f.name in ("README.md",) or f.stat().st_size > 200_000 or str(f) in seen:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if len(text) < 50:
                    continue
                seen.add(str(f))
                out.append((f, file_hash(f), text))
    return out


def compress_batch(items, call_fn, max_tokens=1200) -> list[dict]:
    """items: [(rel, text)] → call_fn(messages, max_tokens)->text → 解析卡片列表。

    call_fn 由调用方注入 (轮子可换): 典型是 api_pipeline.call 封装, 如 knowledge_cards.call_ds。
    """
    user = "把下面每个文件压缩成一张卡片, 按系统提示的 JSON 格式每行一个:\n\n"
    for i, (rel, text) in enumerate(items, 1):
        user += f"=== 文件{i} {rel} ===\n{text[:MAX_CARD_TEXT]}\n\n"
    out = call_fn([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], max_tokens)
    cards = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
            if d.get("file") and d.get("content"):
                cards.append(d)
        except Exception:
            # 宽松: 抠单行大括号
            m = re.search(r"\{.*\}", line)
            if m:
                try:
                    d = json.loads(m.group())
                    if d.get("file") and d.get("content"):
                        cards.append(d)
                except Exception:
                    pass
    return cards


# ═══════════════ ③ 卡片导航 ═══════════════

def bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def prescreen(anchor: str, cards: dict, top_n: int = NAV_TOP_N) -> list:
    """锚点与卡片匹配, 返回 [(score, rel, card, evidence)] 降序。
    有 embedding 的卡 → bge-m3 向量 cosine (语义); 无 embedding 的卡 → CJK bigram 降级。
    evidence: 向量模式为 "cosine:X.XX"; bigram 模式为共享CJK词列表。
    """
    # ── 向量路径: bge-m3 cosine ──
    a_emb = _embed(anchor)
    has_emb = sum(1 for c in cards.values() if isinstance(c, dict) and c.get("embedding"))
    if a_emb and has_emb > 0:
        scored = []
        for rel, c in cards.items():
            if not isinstance(c, dict) or not c.get("embedding"):
                continue
            cos = _cosine(a_emb, c["embedding"])
            if cos > 0.25:  # 向量相关阈值 (经验值, 远高于 0=随机)
                scored.append((cos, rel, c, f"cosine:{cos:.3f}"))
        scored.sort(key=lambda x: -x[0])
        if scored:
            return scored[:top_n]

    # ── bigram 降级路径: 无 embedding 或无 Ollama ──
    a_grams = bigrams(anchor)
    scored = []
    for rel, c in cards.items():
        if not isinstance(c, dict):
            continue
        text = " ".join(str(c.get(k) or "") for k in ("title", "content", "lesson", "highlight"))
        c_grams = bigrams(text)
        shared = a_grams & c_grams
        if shared:
            scored.append((len(shared), rel, c, list(shared)[:5]))
    scored.sort(key=lambda x: -x[0])
    return scored[:top_n]


def format_nav_cards(top: list) -> tuple:
    """top: [(score, rel, card, evidence)] → (id_map, card_text)。
    id_map: "卡N"→rel; card_text 供模型看 (卡N | 日期 | 任务 | 内容 | 领域 | 实体)。
    """
    id_map = {}
    card_lines = []
    for i, tup in enumerate(top, 1):
        _ov, rel, c = tup[0], tup[1], tup[2]
        cid = f"卡{i}"
        id_map[cid] = rel
        line = (f"{cid} | 日期:{c.get('date') or '无'} | 任务:{c.get('title') or ''}"
                f" | 内容:{c.get('content') or ''[:80]}")
        domain = c.get("domain")
        if domain:
            line += f" | 领域:{domain}"
        entities = c.get("entities")
        if entities:
            line += f" | 实体:{'/'.join(entities[:3])}"
        card_lines.append(line)
    return id_map, "\n".join(card_lines)
