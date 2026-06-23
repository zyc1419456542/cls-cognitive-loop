# PreToolUse.ps1 — CLS Cognitive Loop Pre-Tool Hook Template
# Copy to: {PROJECT_ROOT}/.claude/hooks/PreToolUse.ps1
# Enforces: cog_step, write_protect, recursion_limit, audit logging
param([string]$tool_name,[string]$tool_input_json)

$ProjectRoot="{PROJECT_ROOT}"
$CogStepFile=Join-Path $ProjectRoot "data\state\cog_step.json"
$RecursionFile=Join-Path $ProjectRoot "data\state\recursion_depth.json"
$AuditLog=Join-Path $ProjectRoot "data\state\hook_audit.jsonl"
$ProtectedPaths=@(
  (Join-Path $ProjectRoot "scripts\core-engine\fuse_board.py"),
  (Join-Path $ProjectRoot "scripts\core-engine\qwen_gate.py"),
  (Join-Path $ProjectRoot "CLAUDE.md"),
  (Join-Path $ProjectRoot ".claude\hooks\PreToolUse.ps1"),
  (Join-Path $ProjectRoot ".claude\settings.json")
)
$MaxDepth={MAX_RECURSION_DEPTH}
$TTL={COG_STEP_TTL}

function Log($v,$c,$d){$e=@{ts=(Get-Date -Format "o");tool=$tool_name;verdict=$v;check=$c;detail=$d}|ConvertTo-Json -Compress;Add-Content $AuditLog $e -EA SilentlyContinue}

# CHECK 1: COG_STEP for Write/Edit
if($tool_name -in @("Write","Edit")){
  try{$i=$tool_input_json|ConvertFrom-Json;$t=$i.file_path}catch{$t=$tool_input_json}
  if($t -notmatch "cog_step\.json$"){
    if(!(Test-Path $CogStepFile)){Log "BLOCK" "COG_STEP" "missing";Write-Output (@{permissionDecision="deny";permissionDecisionReason="[COG_STEP] No step declared. Write cog_step.json first."}|ConvertTo-Json -Compress);exit 0}
    try{$c=Get-Content $CogStepFile -Raw|ConvertFrom-Json;$a=[int]((Get-Date)-[datetime]::Parse($c.declared_at)).TotalSeconds
      if($a -gt $TTL){Log "BLOCK" "COG_STEP_EXPIRED" "age=$a";Write-Output (@{permissionDecision="deny";permissionDecisionReason="[COG_STEP] Expired ($a>$TTL s). Re-declare."}|ConvertTo-Json -Compress);exit 0}
      Log "PASS" "COG_STEP" "phase=$($c.phase)"}catch{Log "BLOCK" "COG_PARSE" $_;Write-Output (@{permissionDecision="deny";permissionDecisionReason="[COG_STEP] Corrupt."}|ConvertTo-Json -Compress);exit 0}
  }
}

# CHECK 2: WRITE_PROTECT
if($tool_name -in @("Write","Edit") -and $t){
  $n=[IO.Path]::GetFullPath($t)
  foreach($p in $ProtectedPaths){if($n -eq [IO.Path]::GetFullPath($p)){Log "BLOCK" "WRITE_PROTECT" $t;Write-Output (@{permissionDecision="deny";permissionDecisionReason="[FUSE:WRITE_PROTECT] $t"}|ConvertTo-Json -Compress);exit 0}}
}

# CHECK 3: RECURSION_LIMIT
$d=0;if(Test-Path $RecursionFile){try{$d=[int](Get-Content $RecursionFile -Raw|ConvertFrom-Json).depth}catch{}}
$d++;if($d -gt $MaxDepth){Log "BLOCK" "RECURSION" "depth=$d";Write-Output (@{permissionDecision="deny";permissionDecisionReason="[FUSE:RECURSION] $d>$MaxDepth"}|ConvertTo-Json -Compress);exit 0}
@{depth=$d;tool=$tool_name;ts=(Get-Date -Format "o")}|ConvertTo-Json|Set-Content $RecursionFile -Force

# CHECK 4: Audit
Log "ALLOW" "PASSED" "depth=$d"
Write-Output (@{permissionDecision="allow";permissionDecisionReason="[CLS] OK depth=$d"}|ConvertTo-Json -Compress)
