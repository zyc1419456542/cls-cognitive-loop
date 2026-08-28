# Training Runs (production log)

| Run | Date | Product | Notes |
|-----|------|---------|-------|
| 2026-08-08_json | 2026-08-08 | ep-json (GGUF f16) | structured JSON output; JSON legality 100% after tuning |
| 2026-08-15_mem | 2026-08-15 | ep-mem-v2 (GGUF f16) | 3-field memory summary (topic / key_points / decisions) |
| 2026-08-16_param | 2026-08-16 | ep-param-v4 (GGUF f16) | domain parameter extraction from experiment text |
| 2026-08-21_injection | 2026-08-21 | TRL SFT adapter | injection-pipeline task |

Weights (GGUF ~2.9GB each, LoRA adapters, checkpoints) are NOT in this repo.
Reproduce them with `scripts/run_train.ps1` + `scripts/convert_hf_to_gguf.py` + the
Modelfiles under `scripts/`.
