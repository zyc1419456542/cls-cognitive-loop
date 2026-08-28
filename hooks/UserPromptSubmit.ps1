# UserPromptSubmit.ps1 — cognitive_gate + 变更感知注入 (@since 2026-07-31 二号融合)
# 每次用户消息前: ①cognitive_gate四段式注入(复杂度判定+状态+建议) ②KG变更通知
# (2026-08-04: 移除长链笔记本时间记录 — v2 改用周期锚定, 不依赖人类在场检测)

$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $PROJECT_ROOT) { $PROJECT_ROOT = $env:CLS_ROOT }

# ═══════════════════════════════════════════════════
# ① cognitive_gate 四段式注入 (复杂度判定 → 注入决策 → 详细引导)
# ═══════════════════════════════════════════════════
# cognitive_gate stdin 读取: CC 通过管道传入 JSON, PS 脚本顶层用 $input 自动变量
$rawStdin = $null
try {
    $autoInput = @($input)
    if ($autoInput.Count -gt 0) {
        $rawStdin = $autoInput -join "`n"
    }
} catch { }
if (-not $rawStdin) { $rawStdin = "" }

if ($rawStdin.Trim()) {
    try {
        $gateScript = "$PROJECT_ROOT\scripts\wheels\cognitive_gate.py"
        if (Test-Path $gateScript) {
            $gateResult = $rawStdin | & pythonw $gateScript 2>$null
            if ($gateResult) {
                Write-Output $gateResult
            }
        }
    } catch { }
}

# SHA256变更检测: 仅内容变化时注入
$chronicleFile = "$PROJECT_ROOT\data\state\.cls_chronicle.json"
$hashFile = "$PROJECT_ROOT\data\state\.injection_hash"

try {
    $chronicleHash = (Get-FileHash $chronicleFile -Algorithm SHA256).Hash
    $kgFile = "$PROJECT_ROOT\data\state\.knowledge_graph.json"
    $kgHash = if (Test-Path $kgFile) { (Get-FileHash $kgFile -Algorithm SHA256).Hash } else { "" }
    $currentHash = "$chronicleHash|$kgHash"

    $lastHash = ""
    if (Test-Path $hashFile) { $lastHash = (Get-Content $hashFile -Raw).Trim() }

    if ($currentHash -ne $lastHash) {
        $currentHash | Out-File $hashFile -Encoding UTF8 -NoNewline

        if (Test-Path $chronicleFile) {
            $cRaw = Get-Content $chronicleFile -Raw -Encoding UTF8
            $c = $cRaw | ConvertFrom-Json -ErrorAction SilentlyContinue
            if ($c) {
                $domains = $c.domains
                if ($domains) {
                    # 找到所有领域中最新的里程碑
                    $latestDate = ""
                    $latestEvent = ""
                    foreach ($dname in $domains.Keys) {
                        $ms = $domains[$dname].milestones
                        if ($ms -and $ms.Count -gt 0) {
                            $last = $ms[$ms.Count - 1]
                            if ($last.date -gt $latestDate) {
                                $latestDate = $last.date
                                $latestEvent = "[$dname] " + $last.event
                            }
                        }
                    }
                    if ($latestEvent) {
                        Write-Output ("[CLS] " + $latestEvent)
                    }
                }
            }
        }
    }
} catch { }

# ═══════════════════════════════════════════════════
# ② drift_v2 锚点管理 (2026-08-18)
# ═══════════════════════════════════════════════════
# 人类每次输入时: 先clear旧框架, 再捕获新锚点
try {
    $driftScript = "$PROJECT_ROOT\scripts\wheels\drift_v2.py"
    if (Test-Path $driftScript) {
        # 清理旧框架 (人类回来了, 监控结束)
        & pythonw $driftScript clear 2>$null
        # 捕获新锚点 (记录本次人类输入)
        $driftResult = & pythonw $driftScript anchor 2>$null
        # 不注入 — 锚点捕获是后台行为, 不干扰对话
    }
} catch { }

# ── anchor_crawler 轻量检查 (2026-08-21) ──
# 每次用户输入时检查 ANCHOR 变化（内容变化驱动，无变化零消耗）
try {
    $acScript = "$PROJECT_ROOT\scripts\wheels\anchor_crawler.py"
    if (Test-Path $acScript) {
        & pythonw $acScript check 2>$null
    }
} catch { }

exit 0
