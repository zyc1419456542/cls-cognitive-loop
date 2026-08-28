# 阶段 0：第一次 QLoRA——从"能跑"到"跑通"

> 战略背景：maintainer 2026-08-07 定下自产模型战略（见 memory `expert-model-library-orchestration-idea`）。
> 本文档是本机侦察结论 + 最小可行方案，**无人值守期间只写方案不执行**（不装环境、不训练、不改系统）。
> 执行时机：maintainer醒来确认后。

## 一、本机条件（2026-08-07 实际侦察，非推测）

| 项 | 实测值 | 对训练的含义 |
|---|---|---|
| GPU | NVIDIA GeForce RTX 4060 **Laptop**，8GB VRAM，CUDA 13.3 | 8GB 是硬约束：**3B QLoRA 稳妥，7B QLoRA 需 unsloth 优化**（vanilla 要 10-14GB）|
| torch | 2.6.0+cu124，`cuda.is_available()=True`，1 设备 | 环境已就绪，无需重装 |
| 已装库 | transformers 4.57.1 / peft 0.14.0 / datasets 2.18.0 / accelerate 1.13.0 / bitsandbytes 0.49.2 / safetensors 0.8.0 | 训练主干齐了 |
| 缺 | **trl**（DPO/GRPO 用）、**unsloth**（显存/速度优化）、vllm、axolotl | 阶段 0 需补 trl + unsloth |
| Python | 3.11.5（Anaconda @ <ANACONDA>） | 用现有 env，装包前先验证兼容 |
| 磁盘 | C 剩 **13.8GB**（紧张）、E 剩 **137.8GB** | **HF 缓存必须设到 E 盘**（`HF_HOME`），权重也放 E |
| 现有模型资产 | ollama: qwen3-vl:4b / deepseek-r1:8b / qwen2.5:1.5b / Qwythos-9B / bge-m3 | **deepseek-r1:8b 可做评测基线对照** |

## 二、基座选择（8GB 显存档）

| 档位 | 模型 | 显存 | 用途 |
|---|---|---|---|
| **稳妥（推荐先跑）** | Qwen3-3B 或 Qwen2.5-3B-Instruct | QLoRA ~4-5GB | 先跑通管线，验证数据质量 |
| 进阶 | Qwen3-7B / DeepSeek-R1-Distill-Qwen-7B | QLoRA+unsloth ~5-6GB | 数据够后冲质量 |
| 基线对照（不训练） | ollama `deepseek-r1:8b`（已有） | — | 同一批评测问题跑一遍，对比领域差异 |

> 依据：QLoRA 7B 需 10-14GB，unsloth 优化后 5-6GB；8GB 卡跑 3B QLoRA 是行业常规配置（Spheron/dev.to 2026 实测）。

## 三、数据集——从 EP 资产造 SFT 样本（本阶段核心工作）

**原则：质量 > 数量。** QLoRA 只需几百到几千条高质量样本，不需要海量。但样本正确性不可妥协——**错误领域知识进权重 = 污染模型，比不训更糟**。

| 数据源（本机已有资产） | 可提取的知识 | 风险 |
|---|---|---|
| `ep-processing-tips` skill（42 条处理技巧：背压/罐体阻尼/EEPF双峰/Vd塌压/Simpson悖论…） | 技巧性知识，天然适合 QA 对 | 需区分**事实 vs 经验**（CLAUDE.md 结论分级）|
| EP 四层闸门 / PIC 分析结论（`<DOMAIN>电推数据库`） | 推理链 + 结论 | 长样本需剪裁到 ≤2048 token |
| `knowledge/` <DOMAIN>内容、<传感器>分析报告 | 概念定义 + 结论 | 需重写为问答形式 |
| `灵魂/expression_library.json`（可选） | 表达风格 | 阶段 0 不做，防人格数据进训练 |

**样本格式**（instruction-output，每条约 200-600 token）：
```json
{"instruction": "处理 EEPF 双峰时该注意什么？", "output": "……（分级标注：事实/经验）"}
```

**造数据流程（需maintainer参与或授权）**：
1. 从 `ep-processing-tips` 提取 42 条 → 转成 QA 对初稿
2. 用 `api_pipeline.call('kimi')` 或大模型批量改写润色（生成初稿，**不是事实来源**）
3. **事实锚定**：每条样本的结论必须有源文件路径（`knowledge/xxx.md` 第 x 节），无源的不进数据集
4. maintainer冷读抽样 10-20 条 → 确认后才训练
5. 保留"可验证答案"倾向（数值/工况判定类），为阶段 2 RLVR 铺路

> 参照：CAD-Coder 用 110K 文本-CadQuery 对 + RL 几何奖励；ChipNeMo 靠 24B 领域 token 让小模型超 70B。领域数据的**质量+锚定性**是自产模型唯一的护城河。

## 四、训练脚本骨架（QLoRA，8GB 配置）

```python
# 关键参数（8GB VRAM 实测适配）
# model: 4bit bitsandbytes 量化
# LoRA: rank=16, 全 7 个 linear 层(q/k/v/o/gate/up/down)
# lr=2e-4, warmup 5%, epochs 1-3
# max_seq_len=2048, per_device_train_batch_size=1 + grad_accum=8 (等效 bs=8)
# gradient_checkpointing=True, fp16
```
- 训练器：`trl.SFTTrainer`（trl v1.0 已内置 unsloth 内核加速）
- 显存不够就降 `max_seq_len` 或换 3B，不要硬上 7B
- 训练日志、adapter 权重、评测结果存 `model-training/runs/<date>/`

## 五、验证标准（不自评，外部锚定）

1. **管线信号**：训练不崩、loss 下降、adapter 可加载推理
2. **领域对照**：同一批 5-10 个 EP 冷门问题，跑 `ollama deepseek-r1:8b` 做基线，逐条对比自产模型 vs 基线
3. **人类冷读**：maintainer醒来对输出抽样判断（规则 7：Qwen 冷读 / 规则 6：独立审稿，按需触发）
4. 阶段 0 不追求 SOTA——目标是**管线通 + 有对比信号**

## 六、执行清单（待maintainer确认，逐项低风险）

```bash
# 1. 设缓存到 E 盘（C 盘只剩 13.8GB）
setx HF_HOME "<HF_CACHE>"
# 2. 补装（先验证 unsloth 与 torch 2.6.0 兼容；不兼容则建独立 conda env 再装）
pip install trl unsloth
# 3. 造数据（见第三节，maintainer参与抽样）
# 4. 跑 3B QLoRA（预计 1-3 小时，本机电费级成本）
# 5. 评测对照 + 留档
```

## 七、成本/时间预估

| 项 | 数值 | 依据 |
|---|---|---|
| 3B QLoRA 训练时长 | 1-3 小时（本机 4060 笔记本） | 8GB 卡 3B QLoRA 行业常规 |
| 7B QLoRA（unsloth） | 3-8 小时，功耗/散热受限 | 4060 Laptop 性能档 |
| 电费 | 个位数元 | — |
| 云租备选（不推荐起步） | 4090 ~$0.44/h | Spheron 2026 |

## 八、阶段 0 之后（路线图 L1→L4）

- **L1**：EP 数据批量整理成更大 SFT 集（千条级）→ 重训
- **L2**：DPO + GRPO/RLVR（可验证奖励：EP 数值约束/工况判定）——TRL v1.0 单卡可跑
- **L3**：蒸馏（api_pipeline 大模型生成领域轨迹 → 小模型）
- **L4**：从零预训练 0.5-1B 领域 base model（$1500 量级可行，Sapient 实测）
- **嵌入**：产出模型 → `tier_router` 加"私有领域层"，EP 数据不出本机

## 卡点 & 风险

1. **数据正确性是最大风险**——错误领域知识进权重污染模型。对策：样本分级标注 + 源文件锚定 + maintainer抽样。
2. **8GB 显存硬约束**——7B 质量受限。对策：先用 3B 验证管线，质量提升靠数据而非模型大小。
3. **unsloth/torch 兼容**——装前验证，必要时独立 conda env，**不动现有 anaconda 主环境**。
4. **C 盘紧张**——所有缓存/权重/日志走 E 盘。
