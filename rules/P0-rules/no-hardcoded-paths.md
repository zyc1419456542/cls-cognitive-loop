# 🔴 路径硬编码禁令 P0
> 来源: crash-lessons#22 (65处硬编码教训)

## 规则

| # | 原则 | 🚫禁止 | ✅ 方法 |
|---|------|--------|--------|
| 1 | 根路径自推导 | `E:\...` 等绝对路径 | `Path(__file__).parent.parent` (Py) / `Split-Path $PSScriptRoot` (PS) |
| 2 | 跨机配置 | 硬编码路径进代码 | `.claude/settings.local.json` 读 `CLS_ROOT` |
| 3 | JSON/MD 文件 | 绝对路径 | `scripts/`, `.claude/` 等相对路径 |
| 4 | CC 配置 | 手动写 settings.json | 安装脚本动态生成 |
| 5 | 新脚本自检 | — | 创建后搜 `E:\` / `C:\` |

## 跨机路径映射

| 机器 | 项目根 |
|------|--------|
| {MACHINE_A} | `E:\{ANONYMIZED_PATH_A}\claude_api\claude\` |
| {MACHINE_B} | `E:\{ANONYMIZED_PATH_B}\claude_api\claude\` |

## 文件锚点

### 1. 用 `__file__` 推导项目根

```python
# ✅ 允许
import os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
data_path = os.path.join(BASE, "data", "stats.json")
```

```powershell
# ❌ 禁止
$ProjectRoot = "E:\{ANONYMIZED_EXAMPLE_PATH}"

# ✅ 允许
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
```

### 2. 项目根路径只有一个入口

项目根路径从文件自身位置推导，不依赖外部变量。

| 语言 | 获取项目根的方法 |
|------|----------------|
| Python | `Path(__file__).resolve().parent.parent` |
| PowerShell | `Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)` |
| Batch | `%~dp0..` |
| JSON/MD | 用相对路径（`scripts/`, `.claude/`, `data/`） |

### 3. 配置文件中的路径

`.claude/settings.json` 等 CC 配置文件允许绝对路径（CC 需要），但必须由安装脚本动态生成，不能手动写死。

```json
// ✅ CC 配置允许绝对路径（CC 框架要求），但由安装脚本写入
{
    "CLS_ROOT": "E:/claude_api/claude",   // 安装脚本动态生成
    "CLAUDE_CODE_TMPDIR": "E:\\claude_api\\claude\\temp\\cc_tmp"
}
```

### 4. 跨机器路径

不同机器的项目路径可能不同：
- {MACHINE_A}: `E:\{ANONYMIZED_PATH_A}\claude_api\claude\`
- {MACHINE_B}: `E:\{ANONYMIZED_PATH_B}\claude_api\claude\`
- 笔记本: 待定

**如果必须引用跨机器路径**，用环境变量或配置文件：

```python
# ✅ 用配置文件
import json
with open(".claude/settings.local.json") as f:
    config = json.load(f)
project_root = config["env"]["CLS_ROOT"]
```

### 5. 新建脚本自检

新建任何脚本后，搜一遍有没有 `E:\` 或 `C:\` 开头的字符串。有就改。

---

## 融合影响

双机共享知识库时，硬编码路径会导致：
- 一号的脚本在二号上跑 → 写到错误位置或直接崩溃
- 融合仓库里的脚本引用了某台机器的路径 → 另一台拉下来不能用

**融合规则**：融合仓库里的所有脚本必须用相对路径或 `__file__` 推导。任何含绝对路径的脚本不进融合仓库。

---

## 历史债务

现有 65 处硬编码路径（主要在 CAD/PIC 脚本和已废弃目录中），分类处理：

| 类别 | 处理方式 |
|------|---------|
| 核心脚本 (scripts/, .claude/) | {MACHINE_ID}启动时全局替换 |
| CAD/freecad/知识库/学习资料/CAD标准件学习 | 需要时才修 |
| data/archive/, _attic/ | 不动（历史存档） |

**原则**：新代码零硬编码。旧代码按需修复。

---

_创建: 2026-06-10 | 关联: crash-lessons #22 | 影响: 融合系统设计_
