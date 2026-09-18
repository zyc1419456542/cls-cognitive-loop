﻿# PostToolUse.ps1 — 符号动力学信任闸门  |  v1.0
# ===============================================
# 每次 Write/Edit 后对 .md 交付文件执行多维门限检查。
# 失败时写入告警日志但不阻塞操作 (fail-open 设计)。
#
# 门限裁决: pass(放行) / flag(标记) / fail(告警+阻止后续)
# 紧急绕过: 存在 data/state/emergency_bypass.flag → 全部跳过

$ErrorActionPreference = 'Continue'
# 🔧 显式 UTF-8 输出编码 — 修复工具输出含中文时的 GBK/UTF-8 乱码
[Console]::OutputEncoding = [Text.Encoding]::UTF8
# 自动推导项目根: .claude/hooks/ → .claude/ → PROJECT_ROOT
$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
# 降级: PSScriptRoot 为空时(罕见)使用环境变量
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = $env:CLS_ROOT }
$TRUST_GATE = "$PROJECT_ROOT\scripts\wheels\trust_gate.py"
$ALERT_LOG = "$PROJECT_ROOT\assistant交付\🔍 符号动力学审计\hooks_alert.jsonl"

# ── 0a. 脱敏辅助函数（捕获错误前先洗掉密钥/Token）────────────────
function _SanitizeInbox {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return $Text }
    # API Key / Token 常见模式
    $Text = $Text -replace '(?i)(sk-[A-Za-z0-9_\-]{20,})', 'sk-***REDACTED***'
    $Text = $Text -replace '(?i)(Bearer\s+)[A-Za-z0-9._\-]{20,}', '${1}***REDACTED***'
    $Text = $Text -replace '(?i)(api[_-]?key[=:]["'']?)[A-Za-z0-9_\-]{16,}', '${1}***REDACTED***'
    $Text = $Text -replace '(?i)(token[=:]["'']?)[A-Za-z0-9_\-]{16,}', '${1}***REDACTED***'
    # 密钥文件路径
    $Text = $Text -replace '(?i)(keys[/\\]\S+)', 'keys/***REDACTED***'
    return $Text
}

# ── 0. 应急门快速绕过（限30分钟内新鲜flag）─────────────────────────
$EMERGENCY_FLAG = "$PROJECT_ROOT\data\state\emergency_bypass.flag"
if (Test-Path $EMERGENCY_FLAG) {
    try {
        $flagAge = (Get-Date) - (Get-Item $EMERGENCY_FLAG).LastWriteTime
        if ($flagAge.TotalMinutes -lt 30) {
            exit 0  # 新鲜flag（<30min）→ 确实需要绕过
        }
        # 旧flag（≥30min）→ 视为残留，继续执行
    } catch {
        # 读取异常 → 安全放行
        exit 0
    }
}

# ── 1. 读取 CC 传入的工具操作 JSON ─────────────────────────────
# 诊断: 记录hook是否被调用
try { [System.IO.File]::AppendAllText("$PROJECT_ROOT\state\posttooluse_diag.log", "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') HOOK_INVOKED`n") } catch {}
$inputJson = ""
try {
    # @fix 2026-08-22 (incident-log#12/#17族): 原 `$input | Out-String` 走控制台输入码页(GBK)解码,
    # CC 传入的 UTF-8 中文经此变为乱码并写入 trajectory summary。改为字节流+UTF8 显式解码。
    $_stdinReader = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), [System.Text.Encoding]::UTF8)
    $inputJson = $_stdinReader.ReadToEnd()
    $_stdinReader.Close()
    if ([string]::IsNullOrWhiteSpace($inputJson)) {
        # 可能通过命令行参数传递
        if ($args -and $args[0]) {
            try { [System.IO.File]::AppendAllText("$PROJECT_ROOT\state\posttooluse_diag.log", "$(Get-Date -Format 'HH:mm:ss') via args: $($args[0].Substring(0,[Math]::Min(80,$args[0].Length)))`n") } catch {}
            $inputJson = $args[0]
        } else {
            try { [System.IO.File]::AppendAllText("$PROJECT_ROOT\state\posttooluse_diag.log", "$(Get-Date -Format 'HH:mm:ss') EMPTY stdin`n") } catch {}
            exit 0
        }
    }
} catch {
    try { [System.IO.File]::AppendAllText("$PROJECT_ROOT\state\posttooluse_diag.log", "$(Get-Date -Format 'HH:mm:ss') stdin error: $_`n") } catch {}
    exit 0
}

try {
    $op = $inputJson | ConvertFrom-Json
    # 诊断: 记录 tool_name 原始类型 (2026-08-18)
    try { [System.IO.File]::AppendAllText("$PROJECT_ROOT\state\posttooluse_diag.log", "$(Get-Date -Format 'HH:mm:ss') RAW tool_name=$($op.tool_name) type=$($op.tool_name.GetType().Name)`n") } catch {}
} catch {
    # CC 旧版格式——无 stdin，不触发
    exit 0
}

# ── 2a. 认知循环遥测（全工具调用记录）─────────────────────────
# @added 2026-06-30: 记录每次工具调用的基本信息到 cog_telemetry.jsonl
# 纯追加模式，不阻塞主线。用于统计认知循环触发次数/场景/耗时。
# 2026-08-18 修复: PowerShell -or 返回布尔值, 不是左操作数
if ($op.tool_name) { $toolName = $op.tool_name.ToString().ToLower() } else { $toolName = "" }
$toolInput = $op.tool_input
try {
    $telemetryDir = "$PROJECT_ROOT\data\state"
    if (-not (Test-Path $telemetryDir)) { New-Item -ItemType Directory -Force -Path $telemetryDir | Out-Null }
    $telemetryFile = "$telemetryDir\cog_telemetry.jsonl"
    $toolSummary = ""
    if ($toolInput) {
        if ($toolInput.file_path) { $toolSummary = $toolInput.file_path.Substring(0, [Math]::Min(80, $toolInput.file_path.Length)) }
        elseif ($toolInput.query) { $toolSummary = $toolInput.query.Substring(0, [Math]::Min(60, $toolInput.query.Length)) }
        elseif ($toolInput.description) { $toolSummary = $toolInput.description.Substring(0, [Math]::Min(60, $toolInput.description.Length)) }
        else { $toolSummary = $toolName }
    }
    $telemetryEntry = @{
        ts = [DateTime]::UtcNow.ToString("o")
        event_id = [guid]::NewGuid().ToString().Substring(0, 12)
        source = "post-tool-use"
        tool = $toolName
        summary = $toolSummary
        session_id = if ($env:CLAUDE_CODE_SESSION_ID) { $env:CLAUDE_CODE_SESSION_ID.Substring(0, 16) } else { "unknown" }
    } | ConvertTo-Json -Compress
    Add-Content -Path $telemetryFile -Value $telemetryEntry -Encoding UTF8 -ErrorAction SilentlyContinue
} catch {
    # 遥测失败不阻塞主线
}

# ── 2b-pre. 轨迹层级标签推断 (@added 2026-08-22 框架改动c·maintainer批准) ──
# 为 trajectory.jsonl 每条记录推断 layer ∈ {reflex, rhythm, strategy, human}。
# 只读现有证据文件(pre_tool_audit / autonomy_state / injection_log / active_context),
# 不加新模型不加新监控; 证据不足时返回 $null → 记录省略 layer 字段, 不猜。
# 优先级: reflex > human > rhythm > strategy (具体因果信号优先于宽泛信号)。
# 已知局限: pre_tool_audit/active_context 为跨窗口共享文件, 邻窗口信号可能串入(与 2c-bis 读全局 alerts.jsonl 同先例)。
function _GetTrajLayer {
    param([string]$ToolName)
    try {
        $nowLocal = Get-Date
        $sid8 = ""
        try { if ($env:CLAUDE_CODE_SESSION_ID) { $sid8 = $env:CLAUDE_CODE_SESSION_ID.Substring(0, 8) } } catch {}
        # reflex: 15s 内有闸门拦截(deny/ask) → 当前动作是对拦截的即时反应
        $auditFile = Join-Path $PROJECT_ROOT "assistant交付\🔍 符号动力学审计\pre_tool_audit.jsonl"
        if (Test-Path $auditFile) {
            $lastAudit = Get-Content $auditFile -Tail 1 -ErrorAction SilentlyContinue
            if ($lastAudit) {
                try {
                    $a = $lastAudit | ConvertFrom-Json
                    if ($a.decision -eq "deny" -or $a.decision -eq "ask") {
                        $ageS = ($nowLocal - [DateTime]::Parse($a.ts)).TotalSeconds
                        if ($ageS -ge 0 -and $ageS -le 15) { return "reflex" }
                    }
                } catch {}
            }
        }
        # human: 本窗口 120s 内有人话输入 (semantic_inject 状态机仅人类轮刷新 last_human_input_ts, 系统轮/cron 不刷)
        if ($sid8) {
            $autoStateFile = "$PROJECT_ROOT\data\state\autonomy_state_$sid8.json"
            if (Test-Path $autoStateFile) {
                try {
                    $st = Get-Content $autoStateFile -Raw -Encoding UTF8 | ConvertFrom-Json
                    $lhiTs = 0.0
                    if ($st.last_human_input_ts) { $lhiTs = [double]$st.last_human_input_ts }
                    if ($lhiTs -gt 0) {
                        $ageH = ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) - $lhiTs
                        if ($ageH -ge 0 -and $ageH -le 120) { return "human" }
                    }
                } catch {}
            }
        }
        # rhythm: 本窗口 30s 内有注入落盘 (人话轮已被 human 截获; 此处命中 = 系统轮/cron/注入建议驱动的动作)
        if ($sid8) {
            $injFile = "$PROJECT_ROOT\data\state\injection_log.jsonl"
            if (Test-Path $injFile) {
                $lastInj = Get-Content $injFile -Tail 1 -ErrorAction SilentlyContinue
                if ($lastInj) {
                    try {
                        $inj = $lastInj | ConvertFrom-Json
                        if ($inj.ts -and $inj.session -eq $sid8) {
                            $ageI = ($nowLocal - [DateTime]::Parse($inj.ts)).TotalSeconds
                            if ($ageI -ge 0 -and $ageI -le 30) { return "rhythm" }
                        }
                    } catch {}
                }
            }
        }
        # strategy: 态势/立场状态刚变更 — 当前工具即 cog-context/cog-trajectory, 或 active_context 60s 内被写过
        if ($ToolName -like "*cog-context*" -or $ToolName -like "*cog-trajectory*") { return "strategy" }
        $acFile = "$PROJECT_ROOT\state\active_context.json"
        if (Test-Path $acFile) {
            try {
                $ageA = ($nowLocal - (Get-Item $acFile).LastWriteTime).TotalSeconds
                if ($ageA -ge 0 -and $ageA -le 60) { return "strategy" }
            } catch {}
        }
    } catch {}
    return $null
}

# ── 2b. 轨迹记录 — 显著工具调用追加到 trajectory.jsonl ──────────
# @added 2026-07-05: 每次工具调用后按重要程度记录轨迹点
# 重要工具(Write/Edit/cog-*)必记，其余仅记录非读操作
# 输出到 state/trajectory.jsonl (JSONL 格式，纯追加无重写)
$isSignificant = $false
if ($toolName -in @("write", "edit", "create")) {
    $isSignificant = $true
} elseif ($toolName -like "cog-*") {
    $isSignificant = $true
} elseif ($toolName -in @("bash", "powershell", "execute_command")) {
    # 仅记录包含关键脚本的 bash 命令
    if ($toolSummary -match 'scripts/wheels|fast_commit|api_pipeline|self_activate') {
        $isSignificant = $true
    }
}
$isSignificant = $true  # 全量记录 (@fixed 2026-07-24)
if ($isSignificant) {
    try {
        $trajDir = "$PROJECT_ROOT\state"
        if (-not (Test-Path $trajDir)) { New-Item -ItemType Directory -Force -Path $trajDir | Out-Null }
        $trajFile = "$trajDir\trajectory.jsonl"
        $trajEntryHash = @{
            ts = [DateTime]::UtcNow.ToString("o")
            event_id = [guid]::NewGuid().ToString().Substring(0, 8)
            source = "post-tool-use"
            tool = $toolName
            summary = if ($toolSummary) { $toolSummary.Substring(0, [Math]::Min(120, $toolSummary.Length)) } else { "" }
            session_id = if ($env:CLAUDE_CODE_SESSION_ID) { $env:CLAUDE_CODE_SESSION_ID.Substring(0, 16) } else { "unknown" }
        }
        # 层级标签 (@added 2026-08-22 框架改动c·maintainer批准): 纯加字段不改行为, 判不出则省略
        $trajLayer = _GetTrajLayer -ToolName $toolName
        if ($trajLayer) { $trajEntryHash["layer"] = $trajLayer }
        $trajEntry = $trajEntryHash | ConvertTo-Json -Compress
        Add-Content -Path $trajFile -Value $trajEntry -Encoding UTF8 -ErrorAction Stop
    } catch {
        # 轨迹记录失败 -> 写诊断日志 (@fixed 2026-07-24)
	        try { [System.IO.File]::AppendAllText("$PROJECT_ROOT\state\posttooluse_diag.log", "$(Get-Date -Format 'HH:mm:ss') TRAJ_FAIL: $_`n") } catch {}
    }
}

# ── 2c. 符号动力学注入 — 全量工具调用喂观测（iter-050 落地）───
# @added 2026-07-06 | @updated 2026-07-07: 从仅显著工具扩展到全量工具
# 后台运行，不阻塞主线。CC 工具名 → 符号映射 → 域引擎。
# CPU only，零 GPU 占用。
$symbolicScript = "$PROJECT_ROOT\scripts\wheels\symbolic_observer.py"
if (Test-Path $symbolicScript) {
    # 工具名 → 符号映射（扩展自 iter-041 字母表）
    $toolSymbol = "unknown"
    switch -Regex ($toolName) {
        "^[Ww]rite$"            { $toolSymbol = "W" }
        "^[Ee]dit$"             { $toolSymbol = "E" }
        "^[Rr]ead$"             { $toolSymbol = "R" }
        "^[Bb]ash$"             { $toolSymbol = "B" }
        "^[Gg]rep$"             { $toolSymbol = "G" }
        "^[Gg]lob$"             { $toolSymbol = "G" }
        "^[Ww]eb[Ss]earch$"     { $toolSymbol = "S" }
        "^[Ww]eb[Ff]etch$"      { $toolSymbol = "S" }
        "^[Aa]gent$"            { $toolSymbol = "T" }
        "^[Tt]ask"              { $toolSymbol = "T" }
        "^[Ss]kill$"            { $toolSymbol = "T" }
        "^mcp__"                { $toolSymbol = "T" }
        "^[Tt]odo[Ww]rite$"     { $toolSymbol = "W" }
        default                 { $toolSymbol = "T" }
    }
    try {
        # @fix 2026-08-10 二号融合: Start-Process 隐藏窗口 — 不新建 conhost (incident-log#34 静默化)
        # @fix 2026-08-22: 移除 -NoNewWindow — 与 -WindowStyle Hidden 互斥, 同时指定必抛
        # InvalidOperationException, 跨窗口保活/symbolic_observer 实际从未被拉起(fail-open静默)
        Start-Process -WindowStyle Hidden -FilePath pythonw -ArgumentList $symbolicScript, "tool_call", $toolName, "$toolSymbol $toolSummary"
    } catch {
        # 符号注入失败不阻塞主线
    }
}

# ── 2c-bis. 小模型裁决器 (@added 2026-07-23) ──
# stdout JSON decision:block — CC唯一有效的上下文注入通道(GH#11224)
$judgeScript = "$PROJECT_ROOT\scripts\wheels\symbolic_judge.py"

# ── 2c. 面向过程注入 (2026-08-14 三层记忆架构 L3 新实现, 二号机融合) ──
# 每次工具调用后分析 → 匹配 CC MEMORY.md 索引 → 定向注入(记忆候选/强制回顾/修复循环告警)。
# tool_input 走临时 JSON 文件(stdin 管道给 pythonw 不可靠); temp/ 目录定期清理。
# @backport 2026-09-14 二号三热修回流(incident-log#84族): ①接住 stdout 转发(原丢弃, 注入
#   信封漏到控制台从未上屏) ②pythonw→python.exe(PS5.1 捕获不了 GUI 子系统 stdout,
#   继承 _run_hidden 的 CREATE_NO_WINDOW 祖先链不闪窗) ③一号原始位置已天然在闸门前, 无需搬移。
# process_inject 只在有注入时输出单个 hookSpecificOutput JSON, 无注入零输出 — 与主信封无冲突。
try {
    $piScript = "$PROJECT_ROOT\scripts\wheels\process_inject.py"
    if (Test-Path $piScript) {
        $piTmp = "$PROJECT_ROOT\temp\pi_input_$PID.json"
        ($toolInput | ConvertTo-Json -Depth 8 -Compress) | Out-File -FilePath $piTmp -Encoding UTF8
        $piOut = & python $piScript $toolName $piTmp 2>$null
        if ($piOut) { Write-Output ($piOut -join "`n") }
    }
} catch {}

# ── 2c-bis. 小模型裁决器 (续) ──
$alertsFile = "$PROJECT_ROOT\data\symbolic_dynamics\alerts.jsonl"
if ((Test-Path $judgeScript) -and (Test-Path $alertsFile)) {
    try {
        $latestAlertLine = Get-Content $alertsFile -Tail 1 -ErrorAction SilentlyContinue
        if ($latestAlertLine) {
            $latestAlert = $latestAlertLine | ConvertFrom-Json -ErrorAction SilentlyContinue
            if ($latestAlert -and $latestAlert.type -eq "forbidden_hit") {
                $alertAge = [DateTime]::UtcNow - [DateTime]::Parse($latestAlert.ts)
                if ($alertAge.TotalSeconds -lt 60) {
                    $domain = $latestAlert.domain
                    $ctxSnippet = "$toolName $toolSummary"
                    if ($ctxSnippet.Length -gt 300) { $ctxSnippet = $ctxSnippet.Substring(0, 300) }
                    $judgeResult = & pythonw $judgeScript judge $domain $toolName $ctxSnippet 2>$null
                    if ($judgeResult) {
                        $verdict = $judgeResult | ConvertFrom-Json -ErrorAction SilentlyContinue
                        if ($verdict -and $verdict.block -eq $true) {
                            $blockOut = @{decision="block";reason="[JUDGE] $($verdict.reason)";hookSpecificOutput=@{hookEventName="PostToolUse";additionalContext=$verdict.injection}} | ConvertTo-Json -Compress
                            [Console]::Out.WriteLine($blockOut)
                        } elseif ($verdict -and $verdict.injection -and $verdict.injection.Length -gt 0) {
                            $injectOut = @{decision="block";reason="[JUDGE INJECT] $($verdict.injection)";hookSpecificOutput=@{hookEventName="PostToolUse";additionalContext=$verdict.injection}} | ConvertTo-Json -Compress
                            [Console]::Out.WriteLine($injectOut)
                        }
                    }
                }
            }
        }
    } catch { }
}

# ── 2c-ter. cls_brain 统一脑区调度 ──────
try {
    $brainScript = "$PROJECT_ROOT\scripts\wheels\cls_brain.py"
    if (Test-Path $brainScript) {
        & pythonw $brainScript 2>&1 | Out-Null
    }
} catch {
    # 脑区调度失败不阻塞主线
}

# ── 2c-bis. 跨窗口保活（iter-050 修复）────────────────────────
# @added 2026-06-07 | @removed 2026-06-30 | @restored 2026-07-07
# 每次工具调用后更新 last_seen 和 focus。
# 原 CHECK 16 被移除后 cog-context 未接上导致跨窗口感知全死。
# 从 state/.cross_window_id 读取稳定窗口 ID，防止 Start-Job 子进程 PID 变化。
# 后台运行，CPU only，不阻塞主线。
$crossWindowScript = "$PROJECT_ROOT\scripts\wheels\cross_window_hook.py"
if (Test-Path $crossWindowScript) {
    try {
        $widFile = "$PROJECT_ROOT\state\.cross_window_id"
        $windowId = if (Test-Path $widFile) { Get-Content $widFile -Raw | ForEach-Object { $_.Trim() } } else { "" }
        # @fix 2026-08-10 二号融合: Start-Job 异步, 不阻塞主线
        Start-Job -ScriptBlock {
            param($script, $tool, $detail, $wid)
            if ($wid) {
                & pythonw $script --keepalive --wid $wid $tool $detail 2>$null
            } else {
                & pythonw $script --keepalive $tool $detail 2>$null
            }
        } -ArgumentList $crossWindowScript, $toolName, $toolSummary, $windowId | Out-Null
    } catch {
        # 跨窗口保活失败不阻塞主线
    }
}

# ── 2c-quater-2. knowledge_graph 3小时构建 (@added 2026-07-25, @fixed 3h interval) ──
# 每3小时触发一次图谱构建 (32B vLLM提取实体+关系, 时间戳防重复)
$kgScript = "$PROJECT_ROOT\scripts\wheels\knowledge_graph.py"
if (Test-Path $kgScript) {
    $kgTimerFile = "$PROJECT_ROOT\data\state\.kg_last_build"
    try {
        $shouldBuild = $true
        if (Test-Path $kgTimerFile) {
            $lastBuild = [datetime]::Parse((Get-Content $kgTimerFile -Raw).Trim())
            if (([DateTime]::UtcNow - $lastBuild).TotalHours -lt 3) { $shouldBuild = $false }
        }
        if ($shouldBuild) {
            [DateTime]::UtcNow.ToString("o") | Out-File $kgTimerFile -Encoding UTF8 -NoNewline
            & pythonw $kgScript build 2>$null
        }
    } catch { }
}

# ── 2c-quater-3. cls_chronicle 3小时构建 (@added 2026-07-28) ──
$chronicleScript = "$PROJECT_ROOT\scripts\wheels\cls_chronicle.py"
if (Test-Path $chronicleScript) {
    $chronicleTimer = "$PROJECT_ROOT\data\state\.chronicle_last_build"
    try {
        $shouldBuildChron = $true
        if (Test-Path $chronicleTimer) {
            $lastChron = [datetime]::Parse((Get-Content $chronicleTimer -Raw).Trim())
            if (([DateTime]::UtcNow - $lastChron).TotalHours -lt 3) { $shouldBuildChron = $false }
        }
        if ($shouldBuildChron) {
            [DateTime]::UtcNow.ToString("o") | Out-File $chronicleTimer -Encoding UTF8 -NoNewline
            & pythonw $chronicleScript build 2>$null
        }
    } catch { }
}

# ── 2c-quater. auto_capture 认知循环③⑤自动化 (@added 2026-07-24) ──
# 每10轮: 硅基Qwen2.5-7B提取知识 + 增量session_memory (替代不可靠SessionEnd)
# 设计经Fable5+GPT5.5双审: 异步非阻塞,合并③⑤,去重+冷却,1个本地模型兜底
$captureScript = "$PROJECT_ROOT\scripts\wheels\auto_capture.py"
if (Test-Path $captureScript) {
    try {
        & pythonw $captureScript run 2>$null
    } catch { }
}

# ── 2d. 错误自动捕获 → hunt 围猎 inbox ───────────────────────
# 检测 Bash/Write/Edit 等工具的错误输出，写入 inbox 供 hunt.py 异步处理
$result = $op.result
if ($result) {
    $hasError = $false
    $errMsg = ""
    if ($toolName -eq "bash" -and $result.exit_code -and $result.exit_code -ne 0) {
        $hasError = $true
        $errMsg = "exit=$($result.exit_code) "
    }
    if ($result.stderr) {
        $stderrText = ($result.stderr -replace "`n", " " -replace "`r", " ").Trim()
        if ($stderrText.Length -gt 0) {
            $hasError = $true
            if ($stderrText.Length -gt 300) { $stderrText = $stderrText.Substring(0, 300) + "..." }
            $errMsg += $stderrText
        }
    }
    # 2026-08-04: Bash 失败按原因分类(NEW_TOOL/LOCAL), 供 PreToolUse 分流判定
    # NEW_TOOL: 本地没这个包/命令 → 必须 Web 官方文档; LOCAL: 本地能解决 → Read 即可
    if ($hasError -and $toolName -eq "bash") {
        try {
            $errLow = ($errMsg + $stderrText).ToLower()
            $cls = if ($errLow -match "no module named|modulenotfounderror|command not found|unknown command|not recognized|requires |importerror|no such option") { "NEW_TOOL" }
                  elseif ($errLow -match "permission denied|no such file|filenotfounderror|syntaxerror|traceback|undefined reference|ld returned|error c[0-9]") { "LOCAL" }
                  else { "" }
            if ($cls) {
                $bfFile = "$PROJECT_ROOT\data\state\bash_fail_class.json"
                $bf = @{ cls = $cls; ts = (Get-Date -Format "o"); err = $errMsg.Substring(0, [Math]::Min(80, $errMsg.Length)) } | ConvertTo-Json -Compress
                [System.IO.File]::WriteAllText($bfFile, $bf, [System.Text.Encoding]::UTF8)
            }
        } catch { }
    }
    if ($hasError) {
        try {
            $huntDir = Join-Path $PROJECT_ROOT "data\hunt"
            if (-not (Test-Path $huntDir)) { New-Item -ItemType Directory -Force -Path $huntDir | Out-Null }
            $inboxFile = Join-Path $huntDir "inbox.jsonl"
            $summary = ""
            if ($toolInput) {
                if ($toolInput.file_path) { $summary = $toolInput.file_path }
                elseif ($toolInput.command) { $summary = $toolInput.command.Substring(0, [Math]::Min(80, $toolInput.command.Length)) }
            }
            if ($errMsg.Length -gt 500) { $errMsg = $errMsg.Substring(0, 500) }
            $errMsg = _SanitizeInbox $errMsg
            $entry = @{
                ts = (Get-Date -Format "o")
                tool = $toolName
                error = $errMsg
                summary = $summary
            } | ConvertTo-Json -Compress
            Add-Content -Path $inboxFile -Value $entry -Encoding UTF8 -ErrorAction SilentlyContinue
        } catch {}
    }
}

# ── 2e. capture→hunt 桥接：knowledge写入含错误关键词 → 自动写 hunt inbox ──
if ($toolName -eq "write" -and $toolInput.file_path -match "knowledge|knowledge") {
    $captureContent = $toolInput.content
    if ($captureContent) {
        $errorKeywords = @("error", "bug", "fix", "crash", "exception", "解决", "修复", "错误")
        $matched = $null
        foreach ($kw in $errorKeywords) {
            if ($captureContent -match $kw) { $matched = $kw; break }
        }
        if ($matched) {
            try {
                $huntDir = Join-Path $PROJECT_ROOT "data\hunt"
                if (-not (Test-Path $huntDir)) { New-Item -ItemType Directory -Force -Path $huntDir | Out-Null }
                $inboxFile = Join-Path $huntDir "inbox.jsonl"
                $snippet = $captureContent.Substring(0, [Math]::Min(300, $captureContent.Length)) -replace "`n", " " -replace "`r", ""
                $snippet = _SanitizeInbox $snippet
                $entry = @{
                    ts = (Get-Date -Format "o")
                    tool = "capture"
                    error = $snippet
                    summary = $toolInput.file_path
                } | ConvertTo-Json -Compress
                Add-Content -Path $inboxFile -Value $entry -Encoding UTF8 -ErrorAction SilentlyContinue
            } catch {}
        }
    }
}

# ── 2f. Read 后自总结提醒（iter-050 落地）───────────────────
# @added 2026-07-07: 每次 Read 文件后，注入上下文提醒要求assistant输出
# 一句关键发现（≤30字）作为 recency 锚。防止原文沉入 U 形谷底后丢失。
# 纯 prompt 层操作，不引入外部模型/GPU/API。
if ($toolName -eq "read") {
    $summary = $toolSummary
    if (-not $summary) { $summary = "file" }
    $ctx = @{
        additionalContext = "读完文件后，回复中先输出一句关键发现（≤30字）作为recency锚: 上面读的 $summary 的核心信息是什么？"
    } | ConvertTo-Json -Compress
    Write-Output $ctx
    exit 0
}

# ── 2g. 只处理 Write/Edit 操作 ─────────────────────────────────
if ($toolName -notin @("write", "edit")) {
    exit 0
}

# ── 3. 提取目标文件路径 ───────────────────────────────────────
$filePath = $null
if ($op.tool_input) {
    $filePath = $op.tool_input.file_path
    if (-not $filePath) {
        $filePath = $op.tool_input.path
    }
    if (-not $filePath) {
        $filePath = $op.tool_input.file
    }
}
if (-not $filePath) {
    exit 0
}

# 转成绝对路径
try {
    $filePath = [System.IO.Path]::GetFullPath($filePath)
} catch {
    exit 0
}

# ── 3b. temp 目录防护 (maintainer定 2026-08-21) ──────────────────────
# temp/ 只放临时产物; 长期轮子写里面 = 进不了 MCP 包装(维护铁律只认 scripts/wheels/)
# + 跨会话找不到 + 没人维护。不拦写入(测试脚本合法), 只注入警告。
$fpNorm = $filePath -replace "/", "\"
if ($fpNorm -match "(^|[\\])temp\\") {
    $warn = "【消息】temp 警告(CLS): 本次写入落在 temp/ 目录。【为什么】temp/ 只放临时产物和一次性脚本, 用完即删。长期运行的机制轮子写在里面 = 三重后果: 进不了 MCP 包装(维护铁律只认 scripts/wheels/)、跨会话找不到、没人维护会烂掉。【级别】警告 — 不拦写入(测试脚本放 temp 合法), 但长期轮子必须搬走。【内容】是测试 → 测完把要长期用的轮子转移到 scripts/wheels/ 或专用文件夹; 是一次性脚本 → 用完即删。"
    $ctx = @{ additionalContext = $warn } | ConvertTo-Json -Compress
    Write-Output $ctx
    exit 0
}

# ── 4. 检查文件是否在信任闸门管辖范围 ──────────────────────────
$filePathNorm = $filePath.Replace('\', '/')
$PROJECT_ROOT_NORM = $PROJECT_ROOT.Replace('\', '/')

# 必须在项目目录下
if (-not $filePathNorm.StartsWith($PROJECT_ROOT_NORM)) {
    exit 0
}

# 必须是 .md 文件
if (-not $filePathNorm.EndsWith('.md')) {
    exit 0
}

# 必须在交付目录下
$inDelivery = ($filePathNorm -match "assistant交付") -or ($filePathNorm -match "data/memory")
if (-not $inDelivery) {
    exit 0
}

# 跳过信任闸门自身文件
if ($filePathNorm -match "物料表逻辑链") {
    exit 0
}

# ── 5. 运行信任闸门 ──────────────────────────────────────────
$pythonExe = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) {
    $pythonExe = "pythonw.exe"
}

$gateJson = & $pythonExe $TRUST_GATE --file $filePath --json --force --report
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0 -or -not $gateJson) {
    exit 0  # 工具不可用 → 放行 (fail-open)
}

# ── 6. 解析裁决结果 ───────────────────────────────────────────
try {
    $result = $gateJson | Out-String | ConvertFrom-Json
} catch {
    # 解析失败 → 放行
    exit 0
}

$verdict = $result.verdict
$violations = $result.violations
$blockCount = $result.block_count
$warnCount = $result.warn_count
$elapsed = $result.elapsed_ms

# ── 7. 写入告警日志 ───────────────────────────────────────────
$alert = @{
    ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffzzz")
    tool = $toolName
    file = $filePath
    verdict = $verdict
    block_count = $blockCount
    warn_count = $warnCount
    elapsed_ms = $elapsed
    violations = @($violations | ForEach-Object {
        @{
            feature = $_.feature
            severity = $_.severity
            value = $_.value
            reason = if ($_.reason) { $_.reason } else { "$($_.description) ($($_.range))" }
        }
    })
    features = $result.features
}

$alertDir = Split-Path $ALERT_LOG -Parent
if (-not (Test-Path $alertDir)) {
    New-Item -ItemType Directory -Path $alertDir -Force | Out-Null
}

$alertJson = $alert | ConvertTo-Json -Depth 6 -Compress
Add-Content -Path $ALERT_LOG -Value $alertJson -Encoding UTF8

# ── 8. 输出 stderr (CC 会显示给用户) ───────────────────────────
if ($verdict -eq "fail") {
    $blockFeatures = ($violations | Where-Object { $_.severity -eq "block" } | ForEach-Object { $_.feature }) -join ", "
    Write-Host "`n🛑 信任闸门拦截: $filePath" -ForegroundColor Red
    Write-Host "   违规维度: $blockFeatures" -ForegroundColor Yellow
    Write-Host "   建议: 检查产出后再试，连续3次失败将进入冷却期`n" -ForegroundColor DarkYellow
} elseif ($verdict -eq "flag") {
    $warnFeatures = ($violations | Where-Object { $_.severity -eq "warn" } | ForEach-Object { $_.feature }) -join ", "
    Write-Host "`n⚠️ 信任闸门标记: $filePath" -ForegroundColor Yellow
    Write-Host "   标记维度: $warnFeatures" -ForegroundColor DarkYellow
    Write-Host "   文件已写入，但建议review`n" -ForegroundColor DarkYellow
}

# ── CLS content_gaze 内容凝视 (定期化) ──
# @fix 2026-08-10 二号融合 + 2026-08-02 张maintainer决策: 改定期脚本自动整理, 不再每次 Write spawn。
# 原因: ①每次 Write spawn python 进程=空转+弹窗(GAZE_TTL 600s 过期后永不激活, 死链)
#      ②自动激活依赖 ops_freq 最后30行, 写密集时 Write 被挤出窗口 → 永不激活。
# 现由定期脚本 `content_gaze.py --sweep` 主动扫描最近修改文件评估(见 data/state/ 对应任务)。
# 无内容凝视调用(省每次 Write 进程开销)。

# ── CLS unified_monitor Fast check (2026-07-27) ──
try { & pythonw scripts/wheels/unified_monitor.py --fast 2>$null } catch {}

# fail-open: 不阻止 CC 操作
exit 0
