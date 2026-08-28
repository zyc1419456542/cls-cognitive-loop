"""
extract_text_pool.py — 从定稿 tex 提取中文段落作为数据扩充文本池
===============================================================
用法: python extract_text_pool.py
输出: model-training/data/text_pool.jsonl ({"src", "text"})
"""
import json
import re
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "text_pool.jsonl"

FILES = {
    "<part-B>": r"<REPO_ROOT>\assistant交付\🎨 assistant设计\02_domainX与<介质>\<DOMAIN>研究\20260714_domainX数字孪生阶段交付\03_<传感器>微观物理\<部件B><传感器>数据\EEDF_FFT分析\宏观量-EEDF联合分析.tex",
    "<part-A>": r"<REPO_ROOT>\assistant交付\🎨 assistant设计\02_domainX与<介质>\<DOMAIN>研究\20260714_domainX数字孪生阶段交付\03_<传感器>微观物理\<部件A><传感器>数据\v2_重分析\<部件A>宏观量-EEDF联合分析.tex",
}


def is_cn(text):
    return sum(1 for c in text if '\u4e00' <= c <= '\u9fff')


def main():
    pool = []
    for name, f in FILES.items():
        try:
            t = open(f, encoding="utf-8").read()
        except Exception as e:
            print(f"[{name}] 读取失败: {e}")
            continue
        # 去 LaTeX 命令和公式标记
        t = re.sub(r"\\[a-zA-Z]+(\[[^\]]*\])?(\{[^}]*\})?", " ", t)
        t = re.sub(r"[{}]", " ", t)
        t = re.sub(r"\$[^$]*\$", " ", t)
        # 按句号分句
        parts = re.split(r"[。；]", t)
        for p in parts:
            p = " ".join(p.split()).strip()
            if 20 <= len(p) <= 200 and is_cn(p) >= 10:
                pool.append({"src": name, "text": p})
        print(f"[{name}] 提取 {sum(1 for x in pool if x['src']==name)} 段")
    # 去重
    seen, uniq = set(), []
    for x in pool:
        if x["text"] not in seen:
            seen.add(x["text"])
            uniq.append(x)
    with open(OUT, "w", encoding="utf-8") as f:
        for x in uniq:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    print(f"文本池: {len(uniq)} 段 -> {OUT}")
    random.seed(42)
    for s in random.sample(uniq, min(3, len(uniq))):
        print("  例:", s["src"], "|", s["text"][:60])


if __name__ == "__main__":
    main()
