# 符号动力学 × AI 论文可行性分析

> 2026-06-24 | 搜索覆盖: 最新论文 + 潜在竞争 + 合适venue

---

## 一、已有竞争论文（最接近的3篇）

### 1. "Your Agent Has a Genome" (Deng, arXiv:2606.15579, 2026.04)
**最接近的竞争者。** 把agent行为编码为4符号序列(X/E/P/V)，用n-gram+Markov做异常检测。

| 维度 | 他 | CLS |
|------|----|-----|
| 符号字母表 | 4符号 (X/E/P/V) | 8域，每域8-10符号 |
| 数学工具 | n-gram + Markov + 点双列相关 | Shannon熵 + 拓扑熵 + 谱半径 + 禁止词(SFT) |
| 检测方式 | 后验分析 + 规则引擎 | **实时** (200ms/次，PreToolUse hook) |
| 理论框架 | 统计模式挖掘 | **子移位(SFT)** — 定义合法行为空间 |
| 领域 | SWE-bench (代码agent) | **通用** (CAD/量化/教辅/科研...) |
| 成本 | 离线分析 | **零API成本**，纯计算 |

**CLS超越的部分**: ①实时而非后验 ②拓扑熵+禁止词比纯n-gram深 ③SFT理论框架 ④跨域通用

### 2. ProbGuard/Pro²Guard (2025)
用 **离散时间马尔可夫链(DTMC)** 建模agent状态转移，提前38秒预测违规。PAC理论保证。

**CLS超越的部分**: DTMC是概率加权的转移 → 符号动力学直接用**拓扑**转移（有/无，不加权），对"新行为出现"更敏感。ProbGuard适合已知危险模式的预测，CLS适合**未知异常**的检测。

### 3. Michels (2025) — "Subliminal Learning... with Quantitative Symbolic Dynamics"
用了"符号动力学"这个名字，但是完全不同的含义——符号重力势Ψ(x;C)、吸引子盆地、CT共振。偏物理学隐喻，不涉及实际agent工具调用的符号序列分析。

---

## 二、CLS的核心创新点（论文可强调的）

### 可以发表的内容：

**1. 子移位(SFT)首次应用于LLM Agent行为约束**
- 没有人把SFT理论（禁止词→合法序列空间）用于定义agent的"安全行为边界"
- 这是理论计算机科学中成熟的概念，但在LLM监控领域是第一次

**2. 三维度实时稳定性评分**
- Shannon熵 (频率分布) + 拓扑熵 (转移结构) + 禁止词计数 (已知危险)
- 三者覆盖 "太死板/太混乱/已知坏" 三个维度
- Entropy Sentinel只有一维熵，Genome只有n-gram

**3. 跨域独立监控架构**
- 8个域各有自己的字母表、转移矩阵、禁止词列表
- 一个域异常不影响其他域——这个架构设计是新的

**4. 黑盒+实时+零成本**
- 不需要模型内部状态
- 200ms/次，零API调用
- 可以嵌入PreToolUse hook做实时拦截

**5. 行为vs内容的区分**
- 强调CLS监控agent"做什么"而非"说什么"
- 这个区分本身在文献中不常见

### 不能声称的内容：
- "熵检测幻觉" ← Farquhar 2024已做
- "用马尔可夫链分析agent" ← ProbGuard 2025已做  
- "符号序列编码agent行为" ← Genome 2026.04已做
- 所以论文必须强调 **SFT+拓扑熵+多域实时组合** 这个独特配方

---

## 三、推荐的发表路径

### 不高但正经的venue（按推荐顺序）

| Venue | 难度 | 周期 | 适合原因 |
|-------|------|------|---------|
| **arXiv preprint** | 零门槛 | 即时 | 先占坑建立优先权，论文可以后续改 |
| **ACL 2027 Workshop** (如KnowFM) | 低 | 6-8个月 | 2-4页短文，主题切合(AI安全/审计/监控) |
| **AAAI 2027 Workshop** (如TrustAgent) | 低-中 | 6-8个月 | AI信任/安全方向，适合agent监控 |
| **NeurIPS 2026 Workshop** (如SafeGenAI) | 中 | 3-4个月 | 安全+生成AI，竞争稍大但短文也可以 |
| **EMNLP 2027** (短文/demo track) | 中-高 | ~1年 | 4页短文，适合系统+方法 |
| **JAIR / TMLR** (期刊) | 中 | 无限 | 可以慢慢投，不赶时间 |

### 最现实的路径

```
Phase 1: arXiv preprint (本周末)
  → 4-6页短文，聚焦 SFT+拓扑熵+实时监测
  → 建立优先权，可以被引用

Phase 2: Workshop (下一个deadline)
  → 根据反馈修改，投ACL/AAAI/NeurIPS workshop
  → 2-4页短文 + poster

Phase 3: 期刊或Conf短文 (不着急)
  → 积累引用和实验后，扩写到8-10页
```

---

## 四、建议的论文结构 (4-6页)

```
标题: Subshift-Constrained Agent Monitoring: Real-Time Behavioral 
      Anomaly Detection via Symbolic Dynamics

1. Introduction (0.5页)
   - 问题: agent长任务漂移，语义方法只能检测内容，不能检测行为
   - 方案: 符号动力学→离散符号序列→实时异常检测
   - 贡献: ①SFT框架 ②三维度评分 ③跨域架构

2. Related Work (0.5页)
   - Semantic Entropy (Farquhar 2024)
   - Agent Genome (Deng 2026) — 最接近，但只有4符号+n-gram
   - ProbGuard (2025) — DTMC，但需要已知危险模式
   → 我们的定位: 实时+拓扑熵+SFT+黑盒，填补中间空白

3. Method (1.5页)
   3.1 事件→符号映射 (observer)
   3.2 Shannon熵 (频率分布)
   3.3 拓扑熵 + 谱半径 (转移结构)
   3.4 禁止词 + SFT (合法行为空间)
   3.5 稳定性评分 (三维度组合)

4. Architecture (0.5页)
   - 8域独立监控
   - PreToolUse hook集成
   - 实时决策流水线

5. Experiments (1页)
   - 104次真实激活的符号序列统计
   - 异常检测案例 (死循环/漂移/禁止词命中)
   - 与纯规则方法的对比 (熵检测到规则遗漏的异常)

6. Discussion & Limitations (0.5页)
   - 依赖符号映射的质量
   - 需要baseline校准
   - 未来: 禁止词自动发现

References (0.5页)
```

---

## 五、要不要发？

**建议：发。**

理由：
1. SFT+拓扑熵的组合在文献中确实是空白（Genome有n-gram但没有拓扑熵，Michels有符号动力学但不是实际agent监控）
2. 104次激活是真实生产数据，不是模拟——这个对审稿人有说服力
3. arXiv先占坑成本几乎为零，风险可控
4. Workshop审稿门槛低，短文形式足够
5. 即使只发arXiv不被会议接收，也可以作为CLS开源的理论背书——"我们不仅有代码，还有理论框架"

风险：
- Genome (2026.04) 非常新，可能会被审稿人拿来比较
- 需要在Related Work里明确区分
- 实验部分可能偏弱（单机单系统，无benchmark对比）

**一句话**: 发。先arXiv占坑，然后投workshop。不需要Nature/NeurIPS main conference级别，普通的ACL workshop短文即可，2-4页，聚焦 "SFT+拓扑熵" 这个独特卖点。
