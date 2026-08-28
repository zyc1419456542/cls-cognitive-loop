"""
gen_mem_summary_data.py — 记忆摘要训练数据生成 v2
==================================================
从 EP knowledge (ep_sft_v2_all.jsonl) + 文本池 (text_pool.jsonl) 构造
3 字段结构化摘要样本: {topic, key_points[], decisions[]}

v2 扩充 (2026-08-15):
  - 数据源 +text_pool (EP 定稿 .tex 段落) → 总数 ~195 条
  - decisions 识别增强: 关键词 + 结论句式 (因此/表明/核心发现/→)

用法: python gen_mem_summary_data.py
输出: model-training/data/mem_summary_messages.jsonl
"""
import json
import random
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
EP_DATA = BASE / "data" / "ep_sft_v2_all.jsonl"
TEXT_POOL = BASE / "data" / "text_pool.jsonl"
OUT = BASE / "data" / "mem_summary_messages.jsonl"

# decisions 判定: 约束/结论/决策类关键词
KEYWORDS = ["必须", "不能", "禁止", "不要", "铁律", "注意", "结论", "陷阱",
            "误", "正确", "标准", "统一", "建议", "应", "则", "避免", "否则",
            "不代表", "不等于", "导致", "核心发现", "取决于", "决定", "才能"]
# 结论句式 (含这些词即判定为决策/结论句)
CONCL_PATTERNS = [r"因此", r"这表明", r"这说明", r"综上", r"关键", r"典型",
                  r"本质上", r"根源", r"->", r"→", r"意味着", r"依赖于"]


def split_points(text):
    """按句号/分号/换行分句，返回长度>4 的短句列表"""
    parts = re.split(r"[。；;\n]", text)
    return [p.strip() for p in parts if len(p.strip()) > 4]


def extract_topic(instruction=None, text=None):
    """从问句/指令/文本提炼短主题 (≤24字)"""
    t = instruction or text or ""
    # 去前缀 (assistant/核心发现 等)
    t = re.sub(r"^(assistant\s*|核心发现[:：]?\s*|问题[:：]?\s*)", "", t)
    t = re.sub(r"(各是)?什么(样|意思)?[？?]", "", t)
    t = re.sub(r"[？?]$", "", t)
    t = re.sub(r"^(如何|怎么|怎样|能否|能不能)", "", t)
    t = t.strip("，。:： ")
    return t[:24]


def is_decision(sentence):
    """句子是否为决策/结论句"""
    if any(k in sentence for k in KEYWORDS):
        return True
    return any(re.search(p, sentence) for p in CONCL_PATTERNS)


def build_sample(text, instruction=None, topic_override=None):
    """构造单条 3 字段摘要样本"""
    points = split_points(text)
    if len(points) < 1:
        return None
    decisions = [p for p in points if is_decision(p)]
    # 互斥: decisions 句不进 key_points (结论句单独归入 decisions)
    key_points = [p for p in points if p not in decisions][:4]
    topic = topic_override or extract_topic(instruction, text)
    answer = json.dumps({
        "topic": topic,
        "key_points": key_points,
        "decisions": decisions[:2],
    }, ensure_ascii=False)
    return {"task": "mem_summary", "messages": [
        {"role": "system", "content": "你是记忆摘要器。把文本压缩为 JSON 对象：topic(主题,≤24字), key_points(要点数组,每条≤30字), decisions(决策/结论/约束数组)。只回复JSON。"},
        {"role": "user", "content": f"把下面文本压缩为记忆摘要 JSON：\n{text}"},
        {"role": "assistant", "content": answer},
    ]}


def main():
    samples = []
    rng = random.Random(7)

    # 源1: EP 知识 (instruction → output)
    lines = open(EP_DATA, encoding="utf-8").read().strip().splitlines()
    for line in lines:
        it = json.loads(line)
        s = build_sample(it.get("output", ""), instruction=it.get("instruction"))
        if s:
            samples.append(s)
    print(f"[源1] ep_sft_v2_all.jsonl: {len(samples)} 条")

    # 源2: text_pool (取长度适中的 120 段)
    pool = []
    for line in open(TEXT_POOL, encoding="utf-8").read().strip().splitlines():
        it = json.loads(line)
        text = it.get("text", "")
        if 20 <= len(text) <= 800:
            pool.append(text)
    rng.shuffle(pool)
    from_pool = 0
    for text in pool[:120]:
        s = build_sample(text)
        if s:
            samples.append(s)
            from_pool += 1
    print(f"[源2] text_pool.jsonl: 取 {from_pool}/120 段")

    with open(OUT, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"[生成] {len(samples)} 条记忆摘要样本 -> {OUT}")

    # 抽查 decisions 命中率
    hit = sum(1 for s in samples if json.loads(s["messages"][2]["content"])["decisions"])
    print(f"       decisions 非空: {hit}/{len(samples)} ({hit/len(samples)*100:.0f}%)")


if __name__ == "__main__":
    main()
