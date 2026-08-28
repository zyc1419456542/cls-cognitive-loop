# 自产模型训练启动器（阶段 0: QLoRA）
# 关键: PYTHONNOUSERSITE=1 隔离用户 site-packages —— 否则 botocore/request 等 user 包污染 transformers
# 用法: powershell -ExecutionPolicy Bypass -File run_train.ps1 [--epochs 3] [--max-seq-len 1024]
# 产物: model-training/runs/<date>/adapter/

$env:PYTHONNOUSERSITE = "1"
$env:HF_HOME = "<HF_CACHE>"
$env:TRANSFORMERS_CACHE = "<HF_CACHE>"

& "<ANACONDA>\envs\training\python.exe" "<REPO_ROOT>\model-training\scripts\train_qlora.py" @args
