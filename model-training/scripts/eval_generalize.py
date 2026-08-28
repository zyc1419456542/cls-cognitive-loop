"""
eval_generalize.py — 真实报告泛化测试 (50+ 段)
================================================
从 text_pool.jsonl (EP 定稿 .tex 真实段落) 取 N 段, 测两个模型:

  ep-param: JSON valid 率 + 数值原文锚定率 (提取的每个数值必须能在原文找到, 防伪造)
  ep-mem:   结构 (JSON+3键+topic长度) + key_points 原文重叠率 (防胡编)

用法: python eval_generalize.py [--n 50] [--model-param ep-param] [--model-mem ep-mem]
"""
import argparse
import json
import random
import re
from pathlib import Path
from urllib.request import Request, urlopen

OLLAMA = "http://localhost:11434"
BASE = Path(__file__).resolve().parent.parent
TEXT_POOL = BASE / "data" / "text_pool.jsonl"


def call(model, prompt, system, num_predict=400):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": num_predict},
    }, ensure_ascii=False).encode("utf-8")
    req = Request(f"{OLLAMA}/api/chat", data=body,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"].strip()


def extract_numbers(text):
    return [float(m) for m in re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", text)]


def anchored(value, text, rel=0.05):
    """数值 value 能否在原文 text 找到近似值 (相对容差 rel)"""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if value == 0:
        return any(n == 0 for n in extract_numbers(text))
    return any(abs(n - value) <= max(rel * abs(value), 0.05)
               for n in extract_numbers(text))


def jaccard_chars(s1, s2):
    """字符级 Jaccard 重叠 (2-gram 近似)"""
    def grams(s):
        s = re.sub(r"\s", "", s)
        return set(s[i:i + 2] for i in range(len(s) - 1))
    g1, g2 = grams(s1), grams(s2)
    if not g1 or not g2:
        return 0.0
    return len(g1 & g2) / len(g1 | g2)


def test_param(samples):
    print(f"\n===== ep-param 参数提取泛化 ({len(samples)} 段) =====")
    valid_n = total_vals = anchored_vals = 0
    mode_cases = mode_hits = 0
    for i, text in enumerate(samples):
        user = (f"从下面<传感器>实验文本提取参数，只输出JSON（数值保持原样，mode用英文）：\n\n"
                f"{text}\n\n回复 JSON: 参数对象")
        raw = call(args.model_param, user, "你是实验参数提取器。只回复JSON，不解释。")
        try:
            obj = json.loads(raw)
            valid_n += 1
            for k, v in obj.items():
                if k == "mode":
                    mode_cases += 1
                    zh_map = {"focused": "聚焦", "diffuse": "发散", "transition": "过渡"}
                    if isinstance(v, str) and zh_map.get(v, v) in text:
                        mode_hits += 1
                    continue
                total_vals += 1
                if anchored(v, text):
                    anchored_vals += 1
                else:
                    print(f"  [{i}] 锚定失败 {k}={v} | 原文: {text[:50]}...")
        except json.JSONDecodeError:
            print(f"  [{i}] 非JSON: {raw[:80]!r}")
    print(f"[param result] valid={valid_n}/{len(samples)} | "
          f"数值锚定={anchored_vals}/{total_vals} ({anchored_vals/max(total_vals,1)*100:.0f}%) | "
          f"mode映射={mode_hits}/{max(mode_cases,1)}")


def test_mem(samples):
    print(f"\n===== ep-mem 记忆摘要泛化 ({len(samples)} 段) =====")
    valid_n = kp_n = kp_overlap = 0
    for i, text in enumerate(samples):
        user = f"把下面文本压缩为记忆摘要 JSON：\n{text}"
        raw = call(args.model_mem, user,
                   "你是记忆摘要器。只回复 JSON 对象：topic(≤24字), key_points(数组), decisions(数组)。")
        try:
            obj = json.loads(raw)
            ok = (isinstance(obj.get("topic"), str)
                  and isinstance(obj.get("key_points"), list)
                  and isinstance(obj.get("decisions"), list)
                  and len(obj.get("key_points") or []) >= 1)
            if not ok:
                print(f"  [{i}] 结构异常: {raw[:100]!r}")
                continue
            valid_n += 1
            for kp in obj.get("key_points", []):
                kp_n += 1
                if jaccard_chars(kp, text) > 0.15:
                    kp_overlap += 1
                else:
                    print(f"  [{i}] 要点偏离: {kp[:50]}...")
        except json.JSONDecodeError:
            print(f"  [{i}] 非JSON: {raw[:80]!r}")
    print(f"[mem result] valid={valid_n}/{len(samples)} | "
          f"要点原文重叠={kp_overlap}/{max(kp_n,1)} ({kp_overlap/max(kp_n,1)*100:.0f}%)")


def main():
    global args
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--model-param", default="ep-param")
    ap.add_argument("--model-mem", default="ep-mem")
    args = ap.parse_args()

    pool = [json.loads(l)["text"] for l in open(TEXT_POOL, encoding="utf-8") if l.strip()]
    rng = random.Random(42)
    rng.shuffle(pool)
    samples = [t for t in pool if 30 <= len(t) <= 400][:args.n]
    print(f"[源] text_pool.jsonl 选 {len(samples)} 段 (30-400字)")

    test_param(samples)
    test_mem(samples)


if __name__ == "__main__":
    main()
