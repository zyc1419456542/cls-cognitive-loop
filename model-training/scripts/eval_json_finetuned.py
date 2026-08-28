"""
eval_json_finetuned.py — 评估微调后模型 (adapter + base)
========================================================
用同一套测试样本验证 classify (确认不退化) + summarize (确认复现 100%)。

用法:
  $env:JSON_BASE_MODEL="<HF_CACHE>\<基座路径>"
  python eval_json_finetuned.py --adapter <adapter路径>

指标: 与 eval_json_baseline.py 完全同款 (valid/schema/hit)
"""
import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("HF_HOME", r"<HF_CACHE>")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# 复用基线测试样本 (避免测自己人)
from eval_json_baseline import CLASSIFY_SAMPLES, SUMMARIZE_SAMPLES


def classify_prompt(text, cats):
    c = "\n".join(f"- {x}" for x in cats)
    return (f"将以下内容分类到最合适的类别。只返回类别名和置信度。\n\n"
            f"内容: {text}\n\n候选类别:\n{c}\n\n"
            f"回复 JSON: {{\"category\": \"类别名\", \"confidence\": 0.0-1.0}}"), \
           "你是文本分类器。只回复JSON，不解释。"


def summarize_prompt(text):
    return (f"用中文一句话总结以下内容（不超过40字）：\n\n{text}\n\n"
            f"回复 JSON: {{\"summary\": \"一句话摘要\"}}"), \
           "你是文本摘要器。只回复JSON，不解释。"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default=r"<REPO_ROOT>\model-training\runs\2026-08-08_json\adapter")
    ap.add_argument("--base", default=os.environ.get("JSON_BASE_MODEL", ""))
    args = ap.parse_args()
    if not args.base:
        print("[error] 需要 JSON_BASE_MODEL 环境变量或 --base")
        return

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from peft import PeftModel

    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_use_double_quant=True,
                             bnb_4bit_compute_dtype=torch.float16)
    print(f"[load] base={args.base}\n       adapter={args.adapter}")
    model = AutoModelForCausalLM.from_pretrained(
        args.base, quantization_config=bnb, device_map="auto",
        torch_dtype=torch.float16, trust_remote_code=True)
    model = PeftModel.from_pretrained(model, args.adapter)
    tokenizer = AutoTokenizer.from_pretrained(args.base, trust_remote_code=True)
    model.eval()

    def gen(system, user):
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
        text = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        inp = tokenizer(text, return_tensors="pt").to(model.device)
        with torch.no_grad():
            out = model.generate(**inp, max_new_tokens=256, do_sample=False,
                                 pad_token_id=tokenizer.eos_token_id)
        return tokenizer.decode(out[0][inp["input_ids"].shape[1]:], skip_special_tokens=True).strip()

    # ---- classify ----
    c_ok = c_hit = 0
    print("\n[classify]")
    for i, s in enumerate(CLASSIFY_SAMPLES):
        p, sys_ = classify_prompt(s["text"], s["cats"])
        raw = gen(sys_, p)
        try:
            obj = json.loads(raw)
            valid = "category" in obj
        except json.JSONDecodeError:
            obj, valid = None, False
        c_ok += valid
        if valid and obj.get("category") in s["cats"]:
            c_hit += 1
        print(f"  [{i}] {'OK' if valid else 'X'} {raw[:70]!r}")
    print(f"[classify] valid={c_ok}/{len(CLASSIFY_SAMPLES)} hit={c_hit}/{len(CLASSIFY_SAMPLES)}")

    # ---- summarize ----
    s_ok = 0
    print("\n[summarize]")
    for i, t in enumerate(SUMMARIZE_SAMPLES):
        p, sys_ = summarize_prompt(t)
        raw = gen(sys_, p)
        try:
            obj = json.loads(raw)
            valid = "summary" in obj
        except json.JSONDecodeError:
            valid = False
        s_ok += valid
        print(f"  [{i}] {'OK' if valid else 'X'} {raw[:70]!r}")
    print(f"[summarize] valid={s_ok}/{len(SUMMARIZE_SAMPLES)}")


if __name__ == "__main__":
    main()
