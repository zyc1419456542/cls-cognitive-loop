"""
阶段 0: EP 领域 QLoRA 微调（Qwen3-3B / 8GB VRAM 笔记本）

用法:
    python train_qlora.py [--base Qwen/Qwen3-3B] [--data <jsonl>] [--epochs 3]

产物:
    model-training/runs/<date>/adapter/   (LoRA adapter + tokenizer)
    model-training/runs/<date>/           (训练日志)

依赖 (training env):
    torch 2.6  transformers  peft  datasets  trl  bitsandbytes  accelerate

数据格式 (jsonl, 每行):
    {"instruction": "...", "output": "...", "source": "...", "grade": "fact|experience|hypothesis"}

8GB 显存配置要点:
    - 4bit bitsandbytes 量化 (nf4, double_quant)
    - LoRA rank 16, 全 7 个 linear 层
    - per_device_batch=1 + grad_accum=8 (等效 bs=8)
    - gradient_checkpointing + fp16
    - max_seq_len 1024 (样本短)
"""
import argparse
import datetime
import json
import os
import sys

# 权重/缓存全部落 E 盘，避开 C 盘 (仅剩 13.8GB)
os.environ.setdefault("HF_HOME", r"<HF_CACHE>")
os.environ.setdefault("HF_DATASETS_CACHE", r"<HF_CACHE>")

RUNS_DIR = r"<REPO_ROOT>\model-training\runs"
DEFAULT_BASE = r"<HF_CACHE>\Qwen2.5-3B-Instruct"
DEFAULT_DATA = r"<REPO_ROOT>\model-training\data\ep_sft_seed.jsonl"

# 8GB VRAM 参数
LORA_R = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
BATCH = 1
GRAD_ACC = 8
LR = 2e-4
WARMUP_RATIO = 0.05
TEST_QUESTIONS = [
    "放电形态（聚焦/过渡/发散）的主控变量是什么？",
    "双温崩塌在哪一档流量发生？",
    "为什么流量越高<传感器>测到的电子密度反而越低？",
]

# 训练集外问题 (OOD): 测泛化。参考答案(maintainer判):
# 1. Ne 上升(低流量电离浅、输运损失少、出口外剩余电子多, r=-0.41) + eff_dim 下降(流量主控 r=0.90, 结构简并/双温崩塌)
# 2. 可疑——Vp_diff>7V 几乎不出现在发散态(92% 排除规则), 人眼标签可能误判
# 3. <传感器>旁轴位置已热化, 但束流核心可能维持非热化电子结构(空间采样差异)
OOD_QUESTIONS = [
    "<DOMAIN设备><部件A>流量从 3.0 降到 1.5 sccm，<传感器>测到的 Ne 和 eff_dim 分别会怎么变？为什么？",
    "一个 sweep 的 Vp_diff=9V，但人眼标为发散放电，这个标注可能有什么问题？",
    "聚焦放电的 EEDF 在<传感器>位置完全热化（eff_dim≤2），但<流场>呈蓝色聚焦，怎么解释？",
]


def load_items(path):
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--max-seq-len", type=int, default=1024)
    ap.add_argument("--lr", type=float, default=LR)
    args = ap.parse_args()

    run_dir = os.path.join(RUNS_DIR, datetime.date.today().isoformat())
    os.makedirs(run_dir, exist_ok=True)

    import torch
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        TrainingArguments,
    )
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from datasets import Dataset
    from trl import SFTTrainer

    # ---- 4bit 量化 ----
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,
    )
    print(f"[load] {args.base} (4bit)")
    model = AutoModelForCausalLM.from_pretrained(
        args.base,
        quantization_config=bnb,
        device_map="auto",
        torch_dtype=torch.float16,
        trust_remote_code=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(args.base, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = prepare_model_for_kbit_training(model)

    # ---- LoRA ----
    lora = LoraConfig(
        r=LORA_R,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    # ---- 数据: instruction/output -> chat 对话 ----
    items = load_items(args.data)
    print(f"[data] {len(items)} 条样本 -> {args.data}")
    ds = Dataset.from_list([
        {"chat": [
            {"role": "user", "content": it["instruction"]},
            {"role": "assistant", "content": it["output"]},
        ]}
        for it in items
    ])

    def fmt(example):
        return tokenizer.apply_chat_template(
            example["chat"], tokenize=False, add_generation_prompt=False
        )

    train_args = TrainingArguments(
        output_dir=run_dir,
        per_device_train_batch_size=BATCH,
        gradient_accumulation_steps=GRAD_ACC,
        learning_rate=args.lr,
        num_train_epochs=args.epochs,
        fp16=True,
        logging_steps=5,
        save_strategy="epoch",
        save_total_limit=2,
        gradient_checkpointing=True,
        optim="paged_adamw_8bit",
        report_to="none",
        warmup_ratio=WARMUP_RATIO,
        lr_scheduler_type="cosine",
    )
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        args=train_args,
        train_dataset=ds,
        formatting_func=fmt,
        max_seq_length=args.max_seq_len,
    )
    trainer.train()
    trainer.save_model(os.path.join(run_dir, "adapter"))
    tokenizer.save_pretrained(os.path.join(run_dir, "adapter"))
    print(f"[save] adapter -> {run_dir}/adapter")

    # ---- 推理测试 (不自评, 输出给maintainer判) ----
    print("\n[test] 训练后推理 (训练集内 vs 训练集外 OOD 对照):")
    model.eval()
    for group, qs in [("训练集内", TEST_QUESTIONS), ("训练集外 OOD", OOD_QUESTIONS)]:
        print(f"\n===== {group} =====")
        for q in qs:
            msgs = [{"role": "user", "content": q}]
            text = tokenizer.apply_chat_template(
                msgs, tokenize=False, add_generation_prompt=True
            )
            inp = tokenizer(text, return_tensors="pt").to(model.device)
            with torch.no_grad():
                out = model.generate(
                    **inp, max_new_tokens=200, do_sample=False,
                    pad_token_id=tokenizer.eos_token_id,
                )
            ans = tokenizer.decode(out[0][inp["input_ids"].shape[1]:], skip_special_tokens=True)
            print(f"\nQ: {q}\nA: {ans}")


if __name__ == "__main__":
    main()
