# CLS 认知操作系统

[![DOI](https://zenodo.org/badge/1278055077.svg)](https://doi.org/10.5281/zenodo.20830497)

> LLM Agent 的生产级认知运行时：6步认知循环、4脑区调度器、3级异常裁决管线、潜意识知识捕获——全部运行在 Claude Code Hooks 上。2025年6月设计，2026年7月全面投产。

---

## CLS 实际做什么（2026年7月）

经过 5 周对 Claude Code Hooks 的逐字节调试，以下是运行在 Windows 桌面上的生产系统：

```
每次工具调用：
  PreToolUse Hook  → 15个deny闸门（LIFE_CLAIM, COG_STEP, FAKE_MODEL 等）
  PostToolUse Hook → trajectory.jsonl（工具调用审计日志，~140条/会话）
                   → symbolic_observer（禁止词检测）
                   → symbolic_judge（硅基 Qwen2.5-7B：正则命中→语义分析→block/inject）
                   → cls_brain.tick()（每10轮：新鲜度，4脑区竞价，符号健康）

每10次工具调用：
  auto_capture → 硅基 Qwen2.5-7B：提取可复用知识→写入memory → 追加session_memory.md

每次tick()调用：
  rotate_telemetry → 修剪9个JSONL日志文件
```

**生产数据：** trajectory 140+条/会话（修复前从未存在），brain_telemetry 每30分钟更新（之前死47h），symbolic_health 每10轮更新（之前死23天），auto_capture 24h内捕获3条知识。

---

## 架构（实际运行版）

```
Claude Code 会话
       │
  ┌────┼────┐
  ▼    ▼    ▼
SessionStart  PreToolUse   PostToolUse
(启动注入)   (15deny闸门)  (observer+judge+brain+capture)
  │           │              │
  ▼           ▼              ▼
guidance_  hookSpecific   trajectory.jsonl
injection  Output信封     operations/retrieval/alerts
           {permission    judge_log.jsonl
           Decision:deny} auto_capture_log.jsonl
                                │
                    ┌───────────┘
                    ▼
            cls_brain.tick() [每10轮]
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   freshness()  bidding()  _compute_symbolic_health()
```

### 三级异常裁决管线

```
工具调用 → PostToolUse
  ├─ 第一层: symbolic_observer (正则扫描, forbidden_words.json, <1ms)
  ├─ 第二层: symbolic_judge (硅基7B语义分析, <2s, 5层JSON容错)
  └─ 第三层: PreToolUse CHECK 15.3 (读取guidance_block, 10min窗口)
```

### 潜意识系统

主模型(DS V4 Pro)专注用户任务，后台模型(Qwen2.5-7B)观察trajectory、每10轮提取知识、静默写入memory。两个模型互不知道对方存在——防止自举污染。

---

## 6步认知循环（生产状态）

| 步骤 | 状态 | 机制 |
|------|------|------|
| ① 态势感知 | ⚠️ 手动 | active_context.json |
| ② 任务执行 | ✅ 自动 | cog_step.json + CHECK 15硬闸门 |
| ③ 关联学习 | ✅ 自动 | auto_capture每10轮→硅基7B→SHA-256去重 |
| ④ 抽象泛化 | ✅ 自动 | 每10次捕获→跨会话模式检测 |
| ⑤ 上下文持久化 | ✅ 自动 | 增量session_memory.md |
| ⑥ 轨迹更新 | ✅ 自动 | trajectory.jsonl每次工具调用 |

---

## 关键工程教训

**34天调试的根因：对CC Hooks的四个接口假设全部错误。**

| 假设 | 实际 | 修复 |
|------|------|------|
| JSON裸字段可用 | 必须嵌套hookSpecificOutput | 改_EmitDecision格式 |
| exit 2全拦截 | 只拦Bash不拦Write/Edit | 改用exit 0 |
| 写文件=CC读 | CC只读stdout JSON | 改用stdout |
| UTF-8通用 | PS 5.1中文Windows需BOM | 加\xEF\xBB\xBF |

**教训：** 外部模型不知道CC内部API。CC为Opus设计。开发CC基础设施必须搜官方文档+GitHub Issues。

---

## 对比

| 方案 | CLS增加 |
|------|--------|
| 裸LLM API | 6步循环+硬闸门+潜意识捕获+跨会话状态 |
| LangChain/CrewAI | 认知循环（非DAG）；双模型验证；stdlib熔断 |
| Claude Code内置 | 15deny闸门+3级裁决+潜意识记忆+脑区调度 |
| llm-wiki/claude-mem | 每10轮实时捕获（不依赖SessionEnd） |

---

## 设计原则

1. **接口>实现** — 认知循环固定协议；后端可替换
2. **约束在模型外** — 15条deny规则在.ps1文件中，模型不可修改
3. **双模型零互知** — 潜意识模型从未见过主模型上下文
4. **Fail-open+审计** — Hook失败写诊断日志但永不阻塞主线
5. **无常驻GPU** — 硅基免费API+本地Ollama按需调用

---

## 许可证

Apache 2.0. [LICENSE](LICENSE). Copyright 2026 The CLS Project Authors
