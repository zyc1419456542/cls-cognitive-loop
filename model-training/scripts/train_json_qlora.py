"""
train_json_qlora.py — 结构化 JSON 输出微调 (本地小模型)
========================================================
目标: 修 summarize/classify 的 JSON 遵循度 (基线: summarize 67%)

数据: json_sft_messages.jsonl (Qwen ChatML messages 格式)
要点: DataCollatorForCompletionOnlyLM → response-only loss masking
      (只学 assistant 的 JSON 输出, 不学 system/user 文本)

用法:
  python train_json_qlora.py --base <模型本地路径> [--epochs 6] [--data <jsonl>]

产物: model-training/runs/<date>_json/adapter
评估: 训练后直接跑 summarize 测试样本, 打印 JSON valid rate
"""
import argparse
import datetime
import json
import os

os.environ.setdefault("HF_HOME", r"<HF_CACHE>")
os.environ.setdefault("HF_DATASETS_CACHE", r"<HF_CACHE>")

RUNS_DIR = r"<REPO_ROOT>\model-training\runs"
DEFAULT_BASE = os.environ.get("JSON_BASE_MODEL", "")
DEFAULT_DATA = r"<REPO_ROOT>\model-training\data\json_sft_messages.jsonl"

LORA_R = 16
LORA_ALPHA = 32
BATCH = 1
GRAD_ACC = 8
LR = 2e-4

# 与 eval_json_baseline.py 同款测试样本 (避免测自己人)
TEST_SUMMARIZE = [
    "<部件A>流量从3.0降到1.5 sccm时，<传感器>测到的电子密度上升而EEDF复杂度下降，这是电离深度变化的结果。",
    "真空罐背压对<DOMAIN设备>呼吸模强度有显著影响，背压升高呼吸模振幅增大，跨罐对比时必须标注背压差异。",
    "BN-Si3N4陶瓷具有优异的耐高温性能，在500°C下仍能保持结构稳定，适合用作<DOMAIN设备>通道壁材料。",
    "呼吸振荡不是寄生噪声，而是放电自持的功能标志，无呼吸代表反馈环断裂。",
    "<sensor><传感器>直接测量局域EEDF，但不能测量总电离量，<传感器>位置决定采样环节。",
    "Eff_dim是EEDF的FFT频谱有效维度，大表示多尺度结构，小表示近Maxwell分布。",
    "聚焦放电只需要足够强的B场，与<部件A>流量无关，flow=1.5也能出现聚焦态。",
    "双温崩塌发生在flow=2.0，此流量以下电子群温度结构不可逆变化。",
    "keeper电流控制<部件A>双层强度，流量只影响散射抹平，不改变EEDF基本框架。",
    "K=0.5A时双温比值被锁在1.12，0-D模型用双鞘和束电子贡献解释。",
    "Vp_diff大于7V可以排除发散放电，是<传感器>最有用的单指标信息。",
    "<部件A>流量通过回流传导调控<部件B>电离深度，是EEDF复杂度的主控变量。",
    "全息投影技术在航天器装配中的应用前景广阔，可提高装配精度。",
    "量化交易策略需要经过严格的回测才能上线，避免过拟合。",
    "这篇论文综述了<DOMAIN设备>近二十年的研究进展。",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE, help="模型本地路径 (必须, 或设环境变量 JSON_BASE_MODEL)")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--epochs", type=float, default=6.0)
    ap.add_argument("--max-seq-len", type=int, default=1024)
    ap.add_argument("--tag", default="json", help="run 目录后缀 (区分任务)")
    args = ap.parse_args()
    if not args.base:
        print("[error] 必须提供 --base 或设置 JSON_BASE_MODEL 环境变量")
        return

    run_dir = os.path.join(RUNS_DIR, datetime.date.today().isoformat() + "_" + args.tag)
    os.makedirs(run_dir, exist_ok=True)

    if not os.path.isdir(args.base):
        print(f"[error] 基座模型目录不存在: {args.base}")
        return

    import torch
    from transformers import (AutoModelForCausalLM, AutoTokenizer,
                              BitsAndBytesConfig, TrainingArguments)
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from datasets import Dataset
    from trl import SFTTrainer, DataCollatorForCompletionOnlyLM

    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_use_double_quant=True,
                             bnb_4bit_compute_dtype=torch.float16)
    print(f"[load] {args.base} (4bit)")
    model = AutoModelForCausalLM.from_pretrained(
        args.base, quantization_config=bnb, device_map="auto",
        torch_dtype=torch.float16, trust_remote_code=True, local_files_only=True)
    tokenizer = AutoTokenizer.from_pretrained(args.base, trust_remote_code=True, local_files_only=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = prepare_model_for_kbit_training(model)

    lora = LoraConfig(r=LORA_R, lora_alpha=LORA_ALPHA, lora_dropout=0.05,
                      bias="none", task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                      "gate_proj", "up_proj", "down_proj"])
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    items = [json.loads(l) for l in open(args.data, encoding="utf-8") if l.strip()]
    ds = Dataset.from_list([{"messages": it["messages"]} for it in items])
    tasks = {}
    for it in items:
        tasks[it.get("task", "?")] = tasks.get(it.get("task", "?"), 0) + 1
    print(f"[data] {len(items)} 条 messages | 任务分布: {tasks}")

    # response-only loss masking: 只学 assistant 回复
    response_template = "<|im_start|>assistant"
    data_collator = DataCollatorForCompletionOnlyLM(response_template, tokenizer=tokenizer)

    def fmt(example):
        return tokenizer.apply_chat_template(
            example["messages"], tokenize=False, add_generation_prompt=False)

    train_args = TrainingArguments(
        output_dir=run_dir,
        per_device_train_batch_size=BATCH,
        gradient_accumulation_steps=GRAD_ACC,
        learning_rate=LR,
        num_train_epochs=args.epochs,
        fp16=True,
        logging_steps=5,
        save_strategy="epoch",
        save_total_limit=2,
        gradient_checkpointing=True,
        optim="paged_adamw_8bit",
        report_to="none",
        warmup_ratio=0.05,
        lr_scheduler_type="cosine",
    )
    trainer = SFTTrainer(
        model=model, tokenizer=tokenizer, args=train_args,
        train_dataset=ds, formatting_func=fmt,
        data_collator=data_collator, max_seq_length=args.max_seq_len,
    )
    trainer.train()
    trainer.save_model(os.path.join(run_dir, "adapter"))
    tokenizer.save_pretrained(os.path.join(run_dir, "adapter"))
    print(f"[save] adapter -> {run_dir}/adapter")

    # 评估: 微调后 summarize JSON valid rate
    print("\n[test] 微调后 summarize JSON valid rate:")
    model.eval()
    ok_n = 0
    for i, q in enumerate(TEST_SUMMARIZE):
        msgs = [
            {"role": "system", "content": "你是文本摘要器。只回复JSON，不解释。"},
            {"role": "user", "content":
             f"用中文一句话总结以下内容（不超过40字）：\n\n{q}\n\n回复 JSON: {{\"summary\": \"一句话摘要\"}}"},
        ]
        text = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        inp = tokenizer(text, return_tensors="pt").to(model.device)
        with torch.no_grad():
            out = model.generate(**inp, max_new_tokens=256, do_sample=False,
                                 pad_token_id=tokenizer.eos_token_id)
        ans = tokenizer.decode(out[0][inp["input_ids"].shape[1]:], skip_special_tokens=True).strip()
        try:
            obj = json.loads(ans)
            ok = "summary" in obj
        except json.JSONDecodeError:
            ok = False
        ok_n += ok
        print(f"  [{i}] {'OK' if ok else 'FAIL'} {ans[:90]!r}")
    print(f"\n[result] summarize valid = {ok_n}/{len(TEST_SUMMARIZE)}")


if __name__ == "__main__":
    main()
