"""
gen_json_sft_data.py — 生成 JSON 结构化输出微调训练数据
========================================================
从 EP knowledge文本生成 summarize / classify 训练样本（Qwen ChatML messages 格式）。

数据设计（针对 CLS 真实接口）:
  summarize:  文本 → {"summary": "..."}        (重点: 修 JSON 遵循度)
  classify:   文本+类别列表 → {"category", "confidence"}   (保持已稳的分类)

用法:
  python gen_json_sft_data.py --source <jsonl> --limit 20 --dry-run   # 试点
  python gen_json_sft_data.py --limit 800                            # 全量

输出: model-training/data/json_sft_messages.jsonl (Qwen ChatML 格式)
"""
import argparse
import json
import sys
import os
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "wheels"))

SF_URL = "https://api.siliconflow.cn/v1/chat/completions"
SF_MODEL = "Qwen/Qwen2.5-7B-Instruct"  # CLS 后台同款, 确认可用

OUT = Path(__file__).resolve().parent.parent / "data" / "json_sft_messages.jsonl"
DEFAULT_SOURCE = Path(__file__).resolve().parent.parent / "data" / "ep_sft_v2_all.jsonl"

# classify 用的类别池（覆盖 CLS 场景）
CATEGORY_SETS = [
    ["物理机制", "实验数据", "诊断技巧", "材料属性", "术语定义"],
    ["<部件A>", "<部件B>", "磁场", "<介质>", "<传感器>"],
    ["聚焦放电", "过渡态", "发散放电", "不可判定"],
    ["技术文档", "实验数据", "代码", "报告"],
]

CLS_SYSTEM = "你是文本处理助手。严格遵守指令输出 JSON，不要输出任何解释或多余文字。"


def call_llm(messages: list, model: str = None, max_tokens: int = 256) -> str | None:
    """调硅基流动生成 (CLS 后台同款 API, 确认可用)。"""
    import os as _os
    key = _os.environ.get("SILICONFLOW_API_KEY", "")
    if not key:
        kf = ROOT / "keys" / "siliconflow_config.json"
        if kf.exists():
            key = json.loads(kf.read_text(encoding="utf-8"))["api_key"]
    if not key:
        print("  [SF] 无 API key", file=sys.stderr)
        return None
    body = json.dumps({
        "model": SF_MODEL, "messages": messages,
        "max_tokens": max_tokens, "temperature": 0.2,
    }, ensure_ascii=False).encode("utf-8")
    try:
        req = Request(SF_URL, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("Content-Type", "application/json")
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  [SF error] {e}", file=sys.stderr)
        return None


def extract_json(raw: str):
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1:
        return None
    try:
        return json.loads(s[a:b + 1])
    except json.JSONDecodeError:
        return None


def _valid_summary(s: str) -> bool:
    """摘要质量过滤: 排除乱码/连续重复/异常符号堆砌。"""
    import re
    if not s or not (5 <= len(s) <= 80):
        return False
    if re.search(r'(.)\1{2,}', s):          # 连续 3+ 相同字符 (如 00000, 哈哈哈)
        return False
    if re.search(r'[}>\]]{2,}', s):          # 异常符号堆砌 (如 }}})
        return False
    return True


def gen_summarize_sample(text: str) -> dict | None:
    """生成 summarize 训练样本: 大模型压缩文本成一句话, 返回 messages 格式。"""
    user = (f"用中文一句话总结以下内容（不超过40字）：\n\n{text}\n\n"
            f"回复 JSON: {{\"summary\": \"一句话摘要\"}}")
    raw = call_llm([{"role": "system", "content": CLS_SYSTEM},
                    {"role": "user", "content": user}])
    obj = extract_json(raw)
    if not obj or not obj.get("summary") or not _valid_summary(obj["summary"]):
        return None
    # 训练样本: 与 CLS 实际 prompt 一致 (无系统 JSON 强调, 保持同款)
    train_user = (f"用中文一句话总结以下内容（不超过40字）：\n\n{text}\n\n"
                  f"回复 JSON: {{\"summary\": \"一句话摘要\"}}")
    answer = json.dumps({"summary": obj["summary"]}, ensure_ascii=False)
    return {
        "task": "summarize",
        "messages": [
            {"role": "system", "content": "你是文本摘要器。只回复JSON，不解释。"},
            {"role": "user", "content": train_user},
            {"role": "assistant", "content": answer},
        ],
    }


def gen_classify_sample(text: str, cats: list[str]) -> dict | None:
    """生成 classify 训练样本: 大模型选类别+置信度。"""
    c = "\n".join(f"- {x}" for x in cats)
    user = (f"将以下内容分类到最合适的类别。只返回类别名和置信度。\n\n"
            f"内容: {text}\n\n候选类别:\n{c}\n\n"
            f"回复 JSON: {{\"category\": \"类别名\", \"confidence\": 0.0-1.0}}")
    raw = call_llm([{"role": "system", "content": "你是文本分类器。只回复JSON，不解释。"},
                    {"role": "user", "content": user}])
    obj = extract_json(raw)
    if not obj or not obj.get("category") or obj.get("category") not in cats:
        return None
    try:
        conf = min(1.0, max(0.0, float(obj.get("confidence", 0.9))))
    except (TypeError, ValueError):
        conf = 0.9
    answer = json.dumps({"category": obj["category"], "confidence": round(conf, 2)},
                        ensure_ascii=False)
    return {
        "task": "classify",
        "messages": [
            {"role": "system", "content": "你是文本分类器。只回复JSON，不解释。"},
            {"role": "user", "content": user},
            {"role": "assistant", "content": answer},
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--limit", type=int, default=100, help="要生成的条数(约每源2条)")
    ap.add_argument("--dry-run", action="store_true", help="只打印样例不写文件")
    args = ap.parse_args()

    # 收集源文本: 支持 text_pool.jsonl (含 text 字段) 或 EP 数据 (instruction/output)
    texts = []
    with open(args.source, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            it = json.loads(line)
            if "text" in it:
                texts.append(it["text"])
            else:
                texts.append(it.get("output", ""))
                texts.append(it.get("instruction", ""))
    texts = [t for t in texts if len(t) > 15]
    print(f"[源文本] {len(texts)} 条 (来自 {args.source})")

    samples = []
    seen = set()
    n = min(args.limit, len(texts) * 2)
    for i in range(0, min(args.limit * 2, len(texts))):
        text = texts[i]
        # summarize 样本
        s = gen_summarize_sample(text)
        if s:
            k = s["messages"][2]["content"]
            if k not in seen:
                seen.add(k)
                samples.append(s)
        # classify 样本 (轮换类别集)
        cats = CATEGORY_SETS[i % len(CATEGORY_SETS)]
        c = gen_classify_sample(text, cats)
        if c:
            k = c["messages"][2]["content"]
            if k not in seen:
                seen.add(k)
                samples.append(c)
        if len(samples) >= args.limit:
            break

    print(f"[生成] {len(samples)} 条训练样本 (summarize/classify 混合)")

    if args.dry_run:
        for s in samples[:5]:
            print(json.dumps(s, ensure_ascii=False, indent=2)[:600])
            print("---")
        return

    with open(OUT, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"[保存] {OUT} ({len(samples)} 条)")


if __name__ == "__main__":
    main()
