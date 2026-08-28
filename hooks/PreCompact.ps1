# PreCompact.ps1 — 压缩前状态保存  |  v1.0
# ===============================================
# CC自动压缩(512K阈值)前触发，保存当前认知状态和焦点。
# fail-open: 从不阻止CC的compact操作。
#
# 触发: CC的 PreCompact 事件
# 替代: 认知循环⑤手动msgs检查 → 现在由CC原生+此hook自动处理

# 0. 控制台编码 (必须在最前, 否则 python 输出中文乱码 — 2026-08-15 修复)
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8

$ErrorActionPreference = 'Continue'
$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = $env:CLS_ROOT }

# ── 0. 应急门快速绕过 ─────────────────────────────────────────
if (Test-Path "$PROJECT_ROOT\data\state\emergency_bypass.flag") {
    exit 0
}

# ── 0b. CLS 压缩前保存锚点 (2026-07-27, 二号融合保留; @fix 2026-08-16 一号融合: 原块误放 emergency_bypass if 内 → 应急门不存在时从不执行, 移出) ──
try {
    Copy-Item "$PROJECT_ROOT\data\state\drift_anchor_*.json" "$PROJECT_ROOT\data\state\precompact_anchor_bak.json" -Force -ErrorAction SilentlyContinue
    Copy-Item "$PROJECT_ROOT\data\state\ops_health.json" "$PROJECT_ROOT\data\state\precompact_ops_bak.json" -Force -ErrorAction SilentlyContinue
} catch {}

$pythonExe = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) {
    $pythonExe = "pythonw"
}

# ── 1. 写预压缩checkpoint (记录压缩发生时间) ─────────────────
$checkpoint = @{
    ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffzzz")
    event = "pre_compact"
    reason = "CC auto-compact at 512K threshold"
}
$checkpointDir = "$PROJECT_ROOT\data\state"
if (-not (Test-Path $checkpointDir)) {
    New-Item -ItemType Directory -Path $checkpointDir -Force | Out-Null
}
try {
    $checkpoint | ConvertTo-Json -Compress | Set-Content -Path "$checkpointDir\compact_checkpoint.json" -Encoding UTF8
} catch { }

# ── 2. 保存当前焦点状态 (current_focus) ───────────────────────
$focusFile = "$PROJECT_ROOT\data\state\current_focus.json"
if (Test-Path $focusFile) {
    # 焦点文件存在则追加时间戳作为备份
    $focusBackup = "$PROJECT_ROOT\data\state\current_focus_precompact_bak.json"
    try {
        Copy-Item $focusFile $focusBackup -Force
    } catch { }
}

# ── 3. 长链快照 (2026-08-04 v2): compact 前把当前焦点存进笔记本 goal.txt ──
# 供 PostCompact 注入 + longchain_guard 目标锚定。模型 compact 后据此接上。
try {
    $lcDir = "$PROJECT_ROOT\data\longchain\_state"
    New-Item -ItemType Directory -Force -Path $lcDir | Out-Null
    $wid = if ($env:CLAUDE_CODE_SESSION_ID) { $env:CLAUDE_CODE_SESSION_ID.Substring(0, [Math]::Min(12, $env:CLAUDE_CODE_SESSION_ID.Length)) } else { "unknown" }
    $today = Get-Date -Format "yyyyMMdd"
    $nbFile = "$PROJECT_ROOT\data\longchain\$today\$wid.md"
    # 若已有笔记本, 末尾追加 compact 标记 + 写入 goal.txt 指向笔记本
    # @fix 2026-08-16 窗口隔离(maintainer): 首行加 sid 前缀, 消费方(unified_inject)校验后只认本窗口 goal
    $goalHint = "sid:$($wid.Substring(0, 8))`ncompact 前最后状态: 见 data/longchain/$today/$wid.md (若存在). 长链任务请先 Read 笔记本再接续."
    [System.IO.File]::WriteAllText("$lcDir\goal.txt", $goalHint, [System.Text.Encoding]::UTF8)
} catch { }

# ── 4. 更新跨窗口上下文 (确保多窗口状态一致) ─────────────────
& $pythonExe "$PROJECT_ROOT\scripts\wheels\cross_window_hook.py" auto_update 2>&1 | Out-Null

# ── 4. 记录已压缩事件 (供认知循环恢复参考) ────────────────────
$compactLog = "$PROJECT_ROOT\data\pipeline\compact_log.jsonl"
$logEntry = @{
    ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffzzz")
    event = "pre_compact"
    source = "CC_PreCompact_hook"
} | ConvertTo-Json -Compress

try {
    Add-Content -Path $compactLog -Value $logEntry -Encoding UTF8
} catch { }

# ── 5. 编年史+知识图谱快照 (compact后恢复用, @added 2026-07-29) ──
$chronicleFile = "$PROJECT_ROOT\data\state\.cls_chronicle.json"
$chronicleSnap = "$PROJECT_ROOT\data\state\.cls_chronicle_precompact.json"
if (Test-Path $chronicleFile) {
    try { Copy-Item $chronicleFile $chronicleSnap -Force } catch { }
}
$kgFile = "$PROJECT_ROOT\data\state\.knowledge_graph.json"
$kgSnap = "$PROJECT_ROOT\data\state\.knowledge_graph_precompact.json"
if (Test-Path $kgFile) {
    try { Copy-Item $kgFile $kgSnap -Force } catch { }
}

# ── 6. ops_health快照 (@added 2026-07-29) ──
$opsFile = "$PROJECT_ROOT\data\symbolic_dynamics\alerts.jsonl"
if (Test-Path $opsFile) {
    try {
        $alertCount = (Get-Content $opsFile -Tail 100 | Select-String "forbidden_hit").Count
        $opsSnap = @{ts=(Get-Date -Format "o"); alert_count=$alertCount} | ConvertTo-Json -Compress
        $opsSnap | Set-Content -Path "$PROJECT_ROOT\data\state\ops_health_precompact.json" -Encoding UTF8
    } catch { }
}

# ── 7. 压缩恢复保存 (2026-08-15 修复): PreCompact 不接受 hookSpecificOutput ──
# CC 2.1.220 schema 不认 hookEventName="PreCompact" → JSON 整体被拒, 注入全丢
# + 每次 compact 报 hook failed (2026-08-14 暴毙教训, 见 hooks-reference skill)。
# 恢复注入挪到 SessionStart (compact 后 source=compact 重触发, 读本文件合并后删除)。
# 本段只写文件, stdout 零输出。
try {
    $anchor = "未知"
    $af = Get-ChildItem "$PROJECT_ROOT\data\state\drift_anchor_*.json" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($af) {
        $a = Get-Content $af.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        $anchor = $a.goal.Substring(0, [Math]::Min(60, $a.goal.Length))
    } elseif (Test-Path "$PROJECT_ROOT\data\longchain\_state\goal.txt") {
        $h = Get-Content "$PROJECT_ROOT\data\longchain\_state\goal.txt" -TotalCount 1
        $anchor = $h.Substring(0, [Math]::Min(60, $h.Length))
    }
    $opsInfo = "无数据"
    if (Test-Path "$PROJECT_ROOT\data\state\ops_health.json") {
        $oh = Get-Content "$PROJECT_ROOT\data\state\ops_health.json" -Raw -Encoding UTF8 | ConvertFrom-Json
        $opsInfo = "$($oh.total_session)次操作"
    }
    # 原 PostCompact 四段内容并入 (单文件一次注入, 由 SessionStart 带出)
    $extra = ""
    # 7a. CC 工具速查 (DS V4 召回率低, compact 后最需要)
    try {
        $reminderScript = "$PROJECT_ROOT\scripts\wheels\cc_tool_reminder.py"
        if (Test-Path $reminderScript) {
            $reminder = (& pythonw $reminderScript 2>$null | Out-String).Trim()
            if ($reminder) { $extra += "`n" + $reminder }
        }
    } catch {}
    # 7b. 认知循环步骤锚点
    $cogFile = "$PROJECT_ROOT\data\state\cog_step.json"
    if (Test-Path $cogFile) {
        $cog = Get-Content $cogFile -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($cog -and $cog.phase) { $extra += "`n[认知步骤] 步骤$($cog.phase): $($cog.label)" }
    }
    # 7c. KG 领域 (chronicle 快照 §5 已存)
    if (Test-Path "$PROJECT_ROOT\data\state\.cls_chronicle_precompact.json") {
        $c = Get-Content "$PROJECT_ROOT\data\state\.cls_chronicle_precompact.json" -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction SilentlyContinue
        $domains = ($c.domains.PSObject.Properties.Name | Select-Object -First 5) -join ","
        if ($domains) { $extra += "`n[KG领域] $domains" }
    }
    # 7d. 长链笔记本尾部 + compact 前锚点
    $goalHint = ""
    if (Test-Path "$PROJECT_ROOT\data\longchain\_state\goal.txt") {
        $goalHint = (Get-Content "$PROJECT_ROOT\data\longchain\_state\goal.txt" -Raw -Encoding UTF8 -ErrorAction SilentlyContinue).Trim()
    }
    if ($goalHint) { $extra += "`n[compact前锚点] $goalHint" }
    $wid = if ($env:CLAUDE_CODE_SESSION_ID) { $env:CLAUDE_CODE_SESSION_ID.Substring(0, [Math]::Min(12, $env:CLAUDE_CODE_SESSION_ID.Length)) } else { "unknown" }
    $today = Get-Date -Format "yyyyMMdd"
    $nbFile = "$PROJECT_ROOT\data\longchain\$today\$wid.md"
    if (Test-Path $nbFile) {
        $nbContent = Get-Content $nbFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
        if ($nbContent.Length -gt 2000) { $nbContent = $nbContent.Substring($nbContent.Length - 2000) }
        if ($nbContent) { $extra += "`n[推理链摘要] " + $nbContent }
    }
    $recovery = @{
        anchor = $anchor
        ops = $opsInfo
        extra = $extra
        ts = (Get-Date -Format "o")
    }
    $recovery | ConvertTo-Json -Compress | Set-Content -Path "$PROJECT_ROOT\data\state\precompact_recovery.json" -Encoding UTF8
} catch {}


exit 0
