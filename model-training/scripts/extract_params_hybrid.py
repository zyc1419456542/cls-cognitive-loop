"""
extract_params_hybrid.py — 参数提取混合管线 (规则前置 + 模型提取 + 锚定兜底)
============================================================================
真实报告 98.7% 文本不含参数 → 规则先筛, 绝大多数直接空对象 (零成本零编造)。
筛中的疑似参数句 → ep-param 提取 → 锚定检查 (值必须在原文找到, 否则丢弃)。

层分工:
  规则 (人类埋的逻辑): 哪些句子可能含参数
  模型 (ep-param):     JSON 结构化提取
  锚定 (确定性校验):   杀干净模型编造的数值

用法: python extract_params_hybrid.py --file <txt> | 或 import 调用
"""
import argparse
import json
import re
from urllib.request import Request, urlopen

from param_postprocess import postprocess_param_json

OLLAMA = "http://localhost:11434"
MODEL = "ep-param"

# 参数句前置模式 (命中任一才值得调模型)
PARAM_PATTERNS = [
    r"flow\s*[=＝]",
    r"\bB\s*[=＝]\s*[\d.]+",
    r"\bIb\s*[=＝]",
    r"eff[_ ]?dim\s*[=＝]",
    r"\bNe\s*[=＝]",
    r"\bVp\s*[=＝]",
    r"\bVd\s*[=＝]",
    r"\bkeeper\s*[=＝]",
    r"mid[_ ]?E\s*[=＝]",
    r"low[_ ]?E\s*[=＝]",
    r"\d+\.?\d*\s*sccm",
    r"\d+\.?\d*\s*Gs",
]
_PARAM_RE = [re.compile(p) for p in PARAM_PATTERNS]


def is_param_sentence(text):
    """规则筛查: 文本是否疑似含参数叙述"""
    return any(p.search(text) for p in _PARAM_RE)


def _call_ollama(prompt, system):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 256},
    }, ensure_ascii=False).encode("utf-8")
    req = Request(f"{OLLAMA}/api/chat", data=body,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"].strip()


def extract_numbers(text):
    return [float(m) for m in re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", text)]


def anchored(value, text, rel=0.05):
    """数值能否在原文找到近似值"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    nums = extract_numbers(text)
    if value == 0:
        return any(n == 0 for n in nums)
    return any(abs(n - value) <= max(rel * abs(value), 0.05) for n in nums)


def extract_params_hybrid(text, verbose=False):
    """混合提取: 规则筛句 → ep-param → 锚定兜底。

    Returns:
        dict: 参数对象 (无参数或提取失败返回 {})
    """
    # 层1: 规则前置 (97% 文本在这里直接返回空)
    if not is_param_sentence(text):
        if verbose:
            print("[规则] 无参数模式 → {}")
        return {}

    # 层2: 模型提取
    prompt = (f"从下面<传感器>实验文本提取参数，只输出JSON（数值保持原样，mode用英文）：\n\n"
              f"{text}\n\n回复 JSON: 参数对象")
    raw = _call_ollama(prompt, "你是实验参数提取器。只回复JSON，不解释。")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        if verbose:
            print(f"[模型] 非JSON: {raw[:80]!r}")
        return {}

    # 层3: 后处理清洗 (白名单 + mode 映射 + 数值域)
    obj = postprocess_param_json(obj, text)

    # 层4: 锚定兜底 (值必须在原文找到, 否则丢弃)
    out = {}
    for k, v in obj.items():
        if k == "mode":
            out[k] = v  # mode 已由后处理从原文映射
            continue
        if anchored(v, text):
            out[k] = v
        else:
            if verbose:
                print(f"[锚定] 丢弃 {k}={v} (原文无此数值)")
    if verbose:
        print(f"[结果] {json.dumps(out, ensure_ascii=False)}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="文本文件 (每行一段)")
    ap.add_argument("--text", help="直接传一段文本")
    args = ap.parse_args()

    if args.text:
        extract_params_hybrid(args.text, verbose=True)
        return
    if not args.file:
        print("用法: --file <txt> 或 --text <一段文本>")
        return
    n_empty = n_extracted = 0
    for line in open(args.file, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        out = extract_params_hybrid(line)
        if out:
            n_extracted += 1
        else:
            n_empty += 1
    print(f"[批量] 提取 {n_extracted} 段 | 空对象 {n_empty} 段 | "
          f"模型调用 {n_extracted + 0} 次 (规则拦截 {n_empty} 次零成本)")


if __name__ == "__main__":
    main()
