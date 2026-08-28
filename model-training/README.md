# 本地小模型：自产微调（ep 系列）

> 在一台 RTX 4060 Laptop（8GB 显存）的笔记本上，用 QLoRA 微调 3B 级开源模型，产出 CLS 系统日常使用的小模型。训练数据来自系统自身的知识库与运行记录——数据飞轮的第一次兑现。

---

## 一、本地模型的作用

CLS 的注入管线、分类、摘要、参数提取原来全部调用云端小模型。微调后的本地模型替换其中三类高频任务：

| 模型 | 任务 | 效果 |
|------|------|------|
| ep-json | 文本 → 严格 JSON（摘要/分类） | 微调后 JSON 合法率 100%（基座模型有概率输出解释文字或尾随逗号） |
| ep-mem | 对话 → 三字段记忆摘要（主题/要点/决策） | 会话记忆压缩，格式稳定 |
| ep-param | 领域实验文本 → 参数提取（流量/电流/磁场等） | 领域术语识别准确率高于通用基座 |

收益：零 API 成本、离线可用、无速率限制、数据不出本机。上线方式：转换为 GGUF 后经 Ollama 加载（`Modelfile.ep-json` 等见 scripts/），按需加载用完即卸（`KEEP_ALIVE=0`），不常驻显存。

## 二、训练过程（8GB 显存全流程）

```
知识库文本 ──造数据──▶ SFT 样本(jsonl, ChatML格式)
                          │  ①用生产环境同款云端小模型批量生成
                          │  ②每条带 source 溯源 + grade 分级(fact/experience/hypothesis)
                          ▼
QLoRA 微调(3B基座, 8GB配方) ──▶ LoRA adapter
                          │
                     merge 回基座 ──▶ GGUF 量化(f16) ──▶ Ollama Modelfile ──▶ 上线替换云端调用
                          │
                     前后对照评测(eval_*_baseline vs eval_*_finetuned)
```

8GB 显存配方（`scripts/train_qlora.py`）：

- 4-bit NF4 量化（bitsandbytes，double quant）
- LoRA rank 16，覆盖全部 7 个 linear 层（q/k/v/o/gate/up/down）
- batch=1 × 梯度累积 8（等效 batch 8）
- gradient checkpointing + fp16，序列长度 1024
- 学习率 2e-4，1-3 个 epoch

数据量：**几百条就够**（124 条 JSON / 195 条记忆摘要 / 147 条参数）。QLoRA 对数据质量远比数量敏感——每条样本的结论必须有源文件可溯源，无源的不进数据集。

## 三、有用的技巧（全部实战验证）

1. **训练格式 = 服务格式。** 造数据直接用生产环境调用的 ChatML messages 格式、生产环境的真实类别池，微调后模型对上线接口零适配成本。
2. **用生产同款云端模型造数据。** 数据由与线上任务同族的 Qwen2.5-7B 生成——分布一致，小基座学得快。
3. **事实分级进数据。** 每条样本带 `grade: fact|experience|hypothesis` 字段，未锚定的事实不进权重。错误领域知识进权重比不训练更糟。
4. **`PYTHONNOUSERSITE=1` 隔离用户包。** 不隔离的话，用户目录里的 botocore 等旧包会污染 transformers 环境导致诡异报错（排查半天的那种）。
5. **HF 缓存放大盘。** `HF_HOME` 指向大盘路径，系统盘很容易被模型缓存塞满。
6. **先 dry-run 造数。** `--limit 20 --dry-run` 抽查样本质量再全量生成，坏样本进数据集的代价比晚跑半天高。
7. **上线前后各评一次。** `eval_*_baseline` 与 `eval_*_finetuned` 跑同一批评测问题，"JSON 合法率 100%"这类结论必须前后对照得出，不能只看微调后。
8. **GGUF f16 起步。** 转换用 f16 保精度，8GB 卡推理 3B f16 量级正好；先跑通再考虑更激进的量化。

## 目录

```
scripts/    全流程脚本: 造数据(gen_*) → 训练(train_qlora) → 合并(merge_adapter)
            → 转GGUF(convert_hf_to_gguf) → 评测(eval_*) → Modelfile ×3
data/       训练数据(jsonl, 含溯源与分级字段) — 即数据飞轮的实物
docs/       阶段0训练计划(硬件侦察/基座选择/数据策略原始文档)
RUNS.md     四轮训练 run 清单与产物(权重不入库)
```

复现：`scripts/run_train.ps1`（占位符路径按 `GLOSSARY.md` 替换）。
