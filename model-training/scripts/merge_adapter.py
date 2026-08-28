"""
merge_adapter.py — LoRA adapter 合并进基座 → 完整模型
=====================================================
用途: 方案A 写回本地的前置 — 合并出可部署的完整 safetensors 模型。

用法:
  $env:JSON_BASE_MODEL="<HF_CACHE>\<基座路径>"
  python merge_adapter.py --adapter <adapter路径> --out <输出目录>

注意: 合并用 fp16 全精度加载 (不用 4bit), 否则 merge 结果质量受损。
"""
import argparse
import os

os.environ.setdefault("HF_HOME", r"<HF_CACHE>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("JSON_BASE_MODEL", ""))
    ap.add_argument("--adapter", default=r"<REPO_ROOT>\model-training\runs\2026-08-08_json\adapter")
    ap.add_argument("--out", default=r"<REPO_ROOT>\model-training\runs\2026-08-08_json\merged")
    args = ap.parse_args()
    if not args.base:
        print("[error] 需要 JSON_BASE_MODEL 环境变量或 --base")
        return

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel

    print(f"[load] base={args.base}")
    print(f"       adapter={args.adapter}")
    model = AutoModelForCausalLM.from_pretrained(
        args.base, torch_dtype=torch.float16, trust_remote_code=True)
    model = PeftModel.from_pretrained(model, args.adapter)
    model = model.merge_and_unload()
    tokenizer = AutoTokenizer.from_pretrained(args.base, trust_remote_code=True)

    os.makedirs(args.out, exist_ok=True)
    model.save_pretrained(args.out, safe_serialization=True)
    tokenizer.save_pretrained(args.out)
    print(f"[save] merged -> {args.out}")

    size = sum(p.numel() * 2 for p in model.parameters()) / 1e9
    print(f"[info] 参数量 {sum(p.numel() for p in model.parameters())/1e6:.1f}M | fp16 大小 ~{size:.1f}GB")


if __name__ == "__main__":
    main()
