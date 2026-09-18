#!/usr/bin/env python3
"""memory_consolidator.py — CLS 知识图谱自动构建器
================================================
从双轨进度/大修小修文件中提取知识, 构建 Markdown 知识图谱。

触发: Windows Task Scheduler (每小时) 或手动
模型: 本地 Ollama qwen2.5:1.5b (分类/摘要) 或 硅基流动 API

设计原则:
  1. 全自动, 不依赖 CC
  2. 只处理新文件 (增量, 靠 _consolidator_state.json 追踪)
  3. 小模型做脏活 (分类/摘要), 大模型不参与维护
  4. 输出 Markdown 知识图谱 (人类可读 + LLM 可消费)

@since: 2026-07-26
"""

import json, os, re, sys, time, hashlib, shutil
from pathlib import Path
from datetime import datetime
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent.parent

# 输入源
DUAL_TRACK_DIR = ROOT / "assistant交付" / "📚 学习资料" / "学习进度"
ITER_DIR = ROOT / "knowledge" / "05_CLS认知系统架构" / "认知系统迭代"
DELIVERY_DIR = ROOT / "assistant交付" / "🎨 assistant设计"

# 输出
KG_DIR = ROOT / "knowledge" / "知识图谱"
KG_FILE = KG_DIR / "knowledge_graph.md"
KG_INDEX = KG_DIR / "kg_index.json"
STATE_FILE = ROOT / "data" / "state" / "_consolidator_state.json"

# 配置
MAX_FILES_PER_RUN = 20      # 每次最多处理文件数
OLLAMA_MODEL = "deepseek-r1:8b"  # 本地5.2GB
SF_MODEL = "Qwen/Qwen2.5-7B-Instruct"  # 云端备用

# ── 文件发现 ──────────────────────────────────

def _scan_new_files() -> list[Path]:
    """扫描双轨/大修小修目录, 返回未处理的新文件"""
    state = _load_state()
    processed = set(state.get("processed_files", []))
    new_files = []

    for directory in [DUAL_TRACK_DIR, ITER_DIR, DELIVERY_DIR]:
        if not directory.exists():
            continue
        for f in directory.rglob("*.md"):
            if f.is_file():
                file_id = _file_id(f)
                if file_id not in processed:
                    new_files.append(f)

    # 按修改时间排序, 最新的优先
    new_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return new_files[:MAX_FILES_PER_RUN]


def _file_id(path: Path) -> str:
    """文件唯一ID (相对路径+大小+修改时间的hash)"""
    try:
        stat = path.stat()
        key = f"{path.relative_to(ROOT)}:{stat.st_size}:{stat.st_mtime}"
        return hashlib.md5(key.encode()).hexdigest()[:12]
    except Exception:
        return path.name


# ── 小模型调用 ────────────────────────────────

def _ollama_classify(text: str) -> dict:
    """Ollama 本地分类 + 摘要 (免费)"""
    import urllib.request
    prompt = f"分析以下内容, 输出JSON(不要其他文字):\n{{\"domain\":\"领域(cad/pic/quant/cls/general)\",\"entities\":[\"关键实体1\",\"实体2\"],\"summary\":\"一句话摘要(<=30字)\",\"importance\":\"high/medium/low\"}}\n\n内容:\n{text[:500]}"
    try:
        body = json.dumps({
            "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
            "options": {"num_predict": 300, "temperature": 0.1}
        }).encode()
        req = urllib.request.Request("http://localhost:11434/api/generate",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        # 提取 JSON
        response = data.get("response", "") or data.get("thinking", "")
        json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass
    return _fallback_classify(text)


def _sf_classify(text: str) -> dict:
    """SF主→DS Flash兜底 分类 + 摘要 (@merge 2026-08-16 一号融合: 二号闸门版为基底 + 一号 cls_api_fallback 兜底链回补)"""
    try:
        from cls_api_fallback import chat
        # @fix 2026-08-14 一号机融合: 残缺JSON骨架prompt误导Qwen2.5-7B输出畸形JSON
        # (kg_index domains 曾出现 "技术术"/"霍尔尔尔推推器器" 等脏值) → 完整合法JSON示例
        response = chat(
            messages=[
                {"role": "system", "content": '你是知识分类器。domain必须从[cad,pic,quant,cls,general]中选一个: cad=机械/CAD设计, pic=等离子/仿真, quant=量化交易, cls=认知系统/CLS, general=其他。输出合法JSON,格式严格如下(必须保留冒号和引号):\n{"domain":"cls","entities":["关键术语1","关键术语2"],"summary":"一句话摘要不超过30字","importance":"high"}\n只输出JSON,不要解释。'},
                {"role": "user", "content": text[:500]}
            ],
            max_tokens=150, temperature=0.1, timeout=10
        )
        if not response:
            return _fallback_classify(text)
        json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass
    return _fallback_classify(text)


def _fallback_classify(text: str) -> dict:
    """无模型降级: 纯关键词分类"""
    text_lower = text.lower()
    domain = "general"
    for kw, dom in [("cad", "cad"), ("pic", "pic"), ("等离子", "pic"),
                     ("量化", "quant"), ("cls", "cls"), ("认知", "cls"),
                     ("符号", "cls"), ("闸门", "cls")]:
        if kw in text_lower: domain = dom; break

    return {"domain": domain, "entities": [], "summary": text[:30],
            "importance": "low", "_fallback": True}


def _classify(text: str) -> dict:
    """SF主通道 → fallback. 不碰本地GPU."""
    result = _sf_classify(text)
    if result and not result.get("_fallback"):
        return result
    return _fallback_classify(text)


# ── 知识图谱构建 ──────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"processed_files": [], "last_run": None, "total_entities": 0}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_kg() -> dict:
    """加载现有知识图谱"""
    if KG_INDEX.exists():
        try:
            return json.loads(KG_INDEX.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"domains": {}, "entities": {}, "relations": [], "updated": None}


def _save_kg(kg: dict):
    KG_DIR.mkdir(parents=True, exist_ok=True)
    kg["updated"] = datetime.now().isoformat()
    KG_INDEX.write_text(json.dumps(kg, ensure_ascii=False, indent=2), encoding="utf-8")
    _render_markdown(kg)


def _render_markdown(kg: dict):
    """生成人类可读的 Markdown 知识图谱"""
    lines = [
        f"# CLS 知识图谱",
        f"> 自动生成: {kg['updated']}",
        f"> 总实体: {len(kg['entities'])} | 域: {len(kg['domains'])}",
        "",
    ]
    for domain, info in sorted(kg["domains"].items()):
        lines.append(f"## {domain} ({info.get('count', 0)}条)")
        entities = [e for eid, e in kg["entities"].items() if e.get("domain") == domain]
        for e in entities[-10:]:  # 每域最近10条
            lines.append(f"- **{e.get('name', '?')}**: {e.get('summary', '')} `[{e.get('importance', '?')}]`")
        lines.append("")

    KG_FILE.write_text("\n".join(lines), encoding="utf-8")


# ── 主流程 ────────────────────────────────────

def _extract_text(filepath: Path) -> str:
    """从 Markdown 文件中提取可分析文本"""
    try:
        content = filepath.read_text(encoding="utf-8")
        # 去掉 YAML frontmatter
        content = re.sub(r'^---\n.*?\n---\n', '', content, flags=re.DOTALL)
        # 取前 500 字符
        return content[:500].strip()
    except Exception:
        return ""


# ── KG 清洗闸门 (2026-08-16 maintainer裁决: 修复 kg_index 噪声污染) ──
# 污染源: 模型抽取的实体名/importance 无校验直写索引 — 噪声实体("次"/"r-"/code-preview 类)
# importance 虚高 353+ 霸榜, 覆盖真实实体(≤87)。规则与 unified_inject 候选清洗一致。
# @fix 2026-08-16 灵感脉冲实测: 提取提示词的 few-shot 示例文本("关键术语1"/"一句话摘要不超过30字")
# 漏进实体名与摘要 → 19 条模板泄漏实体已清。补模式拒未来泄漏。
_ENTITY_JUNK_RE = re.compile(r"code-preview|seseed|seeded|execution|关键术语|摘要示例|不超过.{0,3}字", re.I)


def _valid_entity_name(name: str) -> bool:
    """实体名清洗: 2-30字符 / 过滤提取噪声 / 非纯小写短词
    @fix 2026-08-16 灵感脉冲实测: "1A-"/"339Ti"/"44GB装完" 类文本碎片名混入
    (含 CJK 但以数字开头, 或 ASCII 短名混 CJK) → 补 数字开头/尾标点/无CJK短名 三拒"""
    name = str(name or "").strip()
    if len(name) < 2 or len(name) > 30:
        return False
    if _ENTITY_JUNK_RE.search(name):
        return False
    if re.fullmatch(r"[a-z0-9\-]{1,12}", name):
        return False
    if re.match(r"^\d", name):
        return False  # "339Ti"/"44GB装完" 数字开头=正文片段截取
    if re.search(r"[-:]$", name):
        return False  # "1A-" 截断残留
    if not re.search(r"[一-鿿]", name) and len(name) < 8:
        return False  # 无中文且过短 = 碎片
    return True


def _normalize_importance(imp) -> str:
    """importance 归一化: 只允许 high/medium/low, 其余(数字/乱值)→low"""
    return imp if imp in ("high", "medium", "low") else "low"


def _sanitize_kg(kg: dict) -> list:
    """全量清洗已有索引: 剔除噪声实体 + importance 归一化。返回被剔除的 eid 列表。"""
    dropped = []
    for eid, e in kg.get("entities", {}).items():
        if not _valid_entity_name(e.get("name")) or not (e.get("summary") or "").strip():
            dropped.append(eid)
            continue
        e["importance"] = _normalize_importance(e.get("importance"))
    for eid in dropped:
        kg["entities"].pop(eid, None)
    return dropped


def consolidate():
    """主入口: 扫描新文件 → 分类 → 更新知识图谱"""
    new_files = _scan_new_files()
    if not new_files:
        return {"status": "no_new_files", "processed": 0}

    state = _load_state()
    kg = _load_kg()
    processed_count = 0
    log_entries = []

    for filepath in new_files[:MAX_FILES_PER_RUN]:
        file_id = _file_id(filepath)
        text = _extract_text(filepath)
        if not text:
            continue

        # 分类 + 提取
        result = _classify(text)
        domain = result.get("domain", "general")
        entities = result.get("entities", [])
        summary = result.get("summary", "")[:60]
        importance = result.get("importance", "low")

        # 更新图谱
        if domain not in kg["domains"]:
            kg["domains"][domain] = {"count": 0}
        kg["domains"][domain]["count"] += 1

        for entity_name in entities:
            if not _valid_entity_name(entity_name):
                continue   # 噪声实体不入库 (2026-08-16 KG污染闸门)
            eid = hashlib.md5(entity_name.encode()).hexdigest()[:8]
            importance_n = _normalize_importance(importance)
            if eid not in kg["entities"]:
                kg["entities"][eid] = {
                    "name": entity_name, "domain": domain,
                    "first_seen": datetime.now().isoformat(),
                    "summary": summary, "importance": importance_n,
                }
            else:
                kg["entities"][eid]["summary"] = summary  # 更新

        # 记录
        state["processed_files"].append(file_id)
        state["total_entities"] = len(kg["entities"])
        processed_count += 1

        log_entries.append({
            "time": datetime.now().isoformat(),
            "file": str(filepath.relative_to(ROOT)),
            "domain": domain, "entities": entities,
            "summary": summary, "model": "sf" if not result.get("_fallback") else "fallback",
        })

    # 保存
    _save_state(state)
    _save_kg(kg)

    return {
        "status": "ok", "processed": processed_count,
        "domains": dict(kg["domains"]),
        "total_entities": len(kg["entities"]),
        "log": log_entries,
    }


# ── CLI ──────────────────────────────────────

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--clean":
        # 清洗索引: 备份原文件到 temp/ → 剔除噪声实体 → importance 归一化 → 保存
        kg = _load_kg()
        bak = ROOT / "temp" / "kg_index_bak_20260816.json"
        shutil.copy2(KG_INDEX, bak)
        dropped = _sanitize_kg(kg)
        kg["total_entities"] = len(kg["entities"])
        _save_kg(kg)
        print(json.dumps({
            "dropped": len(dropped), "dropped_eids": dropped[:50],
            "remaining": len(kg["entities"]), "backup": str(bak),
        }, ensure_ascii=False, indent=2))
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        state = _load_state()
        kg = _load_kg()
        print(json.dumps({
            "last_run": state.get("last_run"),
            "processed_files": len(state.get("processed_files", [])),
            "total_entities": len(kg.get("entities", {})),
            "domains": list(kg.get("domains", {}).keys()),
        }, ensure_ascii=False, indent=2))
        return

    result = consolidate()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
