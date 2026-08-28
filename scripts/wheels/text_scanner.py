#!/usr/bin/env python3
"""
text_scanner.py — 大规模文字检索轮子
=====================================
**零 API 成本，本地 GPU 建索引，CPU 检索。**

核心设计：
  - 扫文件 → 分块 → 去重 → 存 chunk
  - GPU embedding（子进程，不卡主窗口）
  - FAISS / numpy 向量检索（CPU，毫秒级）
  - 增量更新（文件哈希检测变更）

用法:
  text_scanner.py scan                   扫全项目文件，生成 chunks
  text_scanner.py index [--gpu]          对 chunks 生成 embedding
  text_scanner.py search "<query>" [--top N]  语义检索
  text_scanner.py update [--gpu]         增量扫描+索引
  text_scanner.py status                 索引统计

GPU 安全:
  --gpu 在子进程中执行，主进程不被 CUDA 初始化影响。
  如果子进程因 GPU 超时/崩溃退出，主进程不受影响，回退到 CPU。
"""

import json, os, sys, hashlib, subprocess, time, re, glob
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_DIR = ROOT / "data" / "text_index"
INDEX_DIR.mkdir(parents=True, exist_ok=True)

CHUNKS_FILE = INDEX_DIR / "chunks.jsonl"
VECTORS_FILE = INDEX_DIR / "vectors.npy"
CHUNK_IDS_FILE = INDEX_DIR / "chunk_ids.npy"
FILE_INDEX_FILE = INDEX_DIR / "file_index.json"
META_FILE = INDEX_DIR / "meta.json"

MODEL_NAME = "intfloat/multilingual-e5-small"
EMBED_DIM = 384

# ─── 扫描配置 ───
# 扫描目录使用相对路径（相对项目根），便于内部逻辑处理
_SCAN_DELIVERY_DESIGN = "assistant交付/🎨 assistant设计"

SCAN_DIRS = [
    "scripts", "knowledge", "data/cues", "data/memory",
    "data/workflows", "state", "学习计划",
    _SCAN_DELIVERY_DESIGN, "knowledge",
]
MAX_CHUNK_CHARS = 1000   # 约 250 token
CHUNK_OVERLAP = 100      # 块间重叠字符
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB 以上跳过

SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".claude",
    "archive", "spore_package", "_attic",
}
SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico",
             ".pyc", ".pyd", ".so", ".dll", ".exe",
             ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
             ".wav", ".mp3", ".mp4", ".avi",
             ".pdf", ".docx", ".xlsx", ".pptx",
             ".npy", ".bin", ".dat", ".plt"}

# ─── 工具函数 ───

def _is_text_file(path: Path) -> bool:
    """判断是否可读的文本文件"""
    if path.suffix.lower() in SKIP_EXTS:
        return False
    if path.stat().st_size > MAX_FILE_SIZE:
        return False
    # 尝试读取前几个字节检查是否为二进制
    try:
        with open(path, 'rb') as f:
            chunk = f.read(1024)
        return b'\0' not in chunk  # 含 null = 二进制
    except Exception:
        return False


def _chunk_text(text: str, source_path: str, file_hash: str) -> list[dict]:
    """将长文本分块，每块 ≈ MAX_CHUNK_CHARS 字符"""
    lines = text.split('\n')
    chunks = []
    current_lines = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current_len + line_len > MAX_CHUNK_CHARS and current_lines:
            chunk_text = '\n'.join(current_lines)
            chunk_id = hashlib.md5(f"{source_path}:{len(chunks)}".encode()).hexdigest()[:12]
            chunks.append({
                "id": chunk_id,
                "path": source_path,
                "text": chunk_text,
                "file_hash": file_hash,
                "chunk_idx": len(chunks),
                "n_chunks": 0,  # filled after
            })
            # overlap: 保留最后几行
            overlap_chars = 0
            overlap_lines = []
            for l in reversed(current_lines):
                if overlap_chars + len(l) + 1 > CHUNK_OVERLAP:
                    break
                overlap_chars += len(l) + 1
                overlap_lines.insert(0, l)
            current_lines = overlap_lines
            current_len = overlap_chars

        current_lines.append(line)
        current_len += line_len

    if current_lines:
        chunk_text = '\n'.join(current_lines)
        chunk_id = hashlib.md5(f"{source_path}:{len(chunks)}".encode()).hexdigest()[:12]
        chunks.append({
            "id": chunk_id,
            "path": source_path,
            "text": chunk_text,
            "file_hash": file_hash,
            "chunk_idx": len(chunks),
            "n_chunks": 0,
        })

    # 填 n_chunks
    for c in chunks:
        c["n_chunks"] = len(chunks)
    return chunks


# ═══════════════════════════════════════════
# scan — 扫描文件
# ═══════════════════════════════════════════

def cmd_scan(args):
    """扫描项目目录 → 提取文本 → 分块 → 去重 → 保存"""
    force = "--force" in args or "-f" in args

    # 已有的 chunks 索引
    existing = {}
    if not force and CHUNKS_FILE.exists():
        with open(CHUNKS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        c = json.loads(line)
                        existing[c["id"]] = c
                    except json.JSONDecodeError:
                        continue

    # 旧的 file index
    file_index = {}
    if FILE_INDEX_FILE.exists():
        file_index = json.loads(FILE_INDEX_FILE.read_text(encoding='utf-8'))

    total_files = 0
    scanned_files = 0
    new_chunks = []
    changed_files = 0

    # 递归扫描
    for dir_path in SCAN_DIRS:
        p = ROOT / dir_path
        if not p.exists():
            continue
        for fpath in sorted(p.rglob('*')):
            if not fpath.is_file():
                continue
            # 跳过黑名单目录
            rel = fpath.relative_to(ROOT).as_posix()
            if any(skip in rel.split('/') for skip in SKIP_DIRS):
                continue
            total_files += 1

            if not _is_text_file(fpath):
                continue

            # 计算文件哈希
            try:
                content = fpath.read_bytes()
                file_hash = hashlib.md5(content).hexdigest()
            except Exception:
                continue

            rel_path = str(fpath.relative_to(ROOT).as_posix())

            # 检查文件是否变更
            old_entry = file_index.get(rel_path)
            if old_entry and old_entry.get("hash") == file_hash and not force:
                continue  # 没变，跳过

            # 读取文本
            try:
                text = content.decode('utf-8', errors='replace')
            except Exception:
                continue

            if not text.strip():
                continue

            # JSON/JSONL 文件提取纯文本
            if fpath.suffix in ('.json', '.jsonl'):
                text = _extract_json_text(text, rel_path)

            # 分块
            chunks = _chunk_text(text, rel_path, file_hash)

            # 去重 (按来源文件去重)
            new_chunks.extend(chunks)
            file_index[rel_path] = {
                "hash": file_hash,
                "size": len(content),
                "mtime": fpath.stat().st_mtime,
                "n_chunks": len(chunks),
            }
            changed_files += 1
            scanned_files += 1

    # 合并已有 + 新的
    if not force:
        # 保持未被覆盖的旧 chunks（只覆盖变更文件的）
        new_paths = {c["path"] for c in new_chunks}
        kept = [c for c in existing.values() if c["path"] not in new_paths]
        all_chunks = kept + new_chunks
    else:
        all_chunks = new_chunks

    # 写 chunks
    with open(CHUNKS_FILE, 'w', encoding='utf-8') as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # 写 file index
    FILE_INDEX_FILE.write_text(
        json.dumps(file_index, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    # 统计
    print(f"[Scan] 扫描: {scanned_files}/{total_files} 文件变更, "
          f"总计 {len(all_chunks)} 文本块")

    # 按目录统计
    dir_counts = {}
    for c in all_chunks:
        d = c["path"].split('/')[0]
        dir_counts[d] = dir_counts.get(d, 0) + 1
    for d in sorted(dir_counts, key=dir_counts.get, reverse=True)[:10]:
        print(f"   {d}: {dir_counts[d]} 块")


def _extract_json_text(text: str, path: str) -> str:
    """从 JSON/JSONL 提取有意义的纯文本"""
    parts = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            parts.append(line)
            continue

        if isinstance(obj, dict):
            # 提取常见的文本字段
            for key in ["text", "description", "content", "body", "advice",
                        "natural", "lesson", "summary", "why", "name",
                        "title", "message", "output", "rationale", "prompt"]:
                val = obj.get(key)
                if val and isinstance(val, str) and len(val) > 10:
                    parts.append(val.strip())
                elif val and isinstance(val, list):
                    for v in val:
                        if isinstance(v, str) and len(v) > 10:
                            parts.append(v.strip())
    return "\n".join(parts)


# ═══════════════════════════════════════════
# index — 生成 embedding
# ═══════════════════════════════════════════

def cmd_index(args):
    """对 chunks 生成 embedding 向量"""
    use_gpu = "--gpu" in args
    force = "--force" in args

    if not CHUNKS_FILE.exists():
        print("[x] 没有 chunks，先运行: text_scanner.py scan")
        return

    if not force and VECTORS_FILE.exists():
        print(f"[OK] 索引已存在 ({VECTORS_FILE})，用 --force 重建")
        return

    # 读取 chunks
    chunks = []
    with open(CHUNKS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    chunks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not chunks:
        print("[x] 没有文本块可索引")
        return

    chunk_ids = [c["id"] for c in chunks]
    # e5 模型需要 "passage: " 前缀
    texts = ["passage: " + c["text"] for c in chunks]
    print(f"[Embed] {len(chunks)} 文本块，开始 embedding...")

    if use_gpu:
        # GPU 子进程（不卡主窗口）
        _index_gpu_subprocess(chunks, chunk_ids, texts)
    else:
        # CPU 模式（在当前进程运行）
        _index_cpu(chunks, chunk_ids, texts)

    # 保存元数据
    META_FILE.write_text(json.dumps({
        "model": MODEL_NAME,
        "dim": EMBED_DIM,
        "n_chunks": len(chunks),
        "n_vectors": len(chunks),
        "mode": "gpu" if use_gpu else "cpu",
        "updated": time.strftime("%Y-%m-%d %H:%M"),
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    v_size = os.path.getsize(VECTORS_FILE) / 1024 / 1024 if VECTORS_FILE.exists() else 0
    print(f"[OK] 索引完成: {len(chunks)} 向量, {v_size:.1f} MB ({MODEL_NAME})")


def _encode_texts(texts: list[str], device: str = "cpu") -> np.ndarray:
    """使用 transformers 直接编码（比 sentence_transformers 启动快 5-10 倍）"""
    from transformers import AutoTokenizer, AutoModel
    import torch

    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, local_files_only=True)
    model = AutoModel.from_pretrained(MODEL_NAME, local_files_only=True)
    model = model.to(device)
    model.eval()

    all_vectors = []
    batch_size = 64 if device == "cuda" else 32  # 来源:经验值，GPU 可处理 64 样本/批，CPU 减半至 32

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        encoded = tokenizer(
            batch, padding=True, truncation=True, max_length=512,  # 来源:BERT 标准输入长度限制 (512 token)
            return_tensors="pt"
        )
        encoded = {k: v.to(device) for k, v in encoded.items()}

        with torch.no_grad():
            outputs = model(**encoded)

        # Mean pooling
        mask = encoded["attention_mask"].unsqueeze(-1).float()
        pooled = (outputs.last_hidden_state * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
        pooled = pooled / pooled.norm(dim=1, keepdim=True).clamp(min=1e-12)
        all_vectors.append(pooled.cpu().numpy())

    return np.concatenate(all_vectors).astype(np.float32)


def _index_cpu(chunks: list, chunk_ids: list, texts: list):
    """CPU 模式：直接在当前进程计算"""
    import numpy as np

    print(f"  CPU 推理 ({len(texts)} 条)...")
    t0 = time.time()

    vectors = _encode_texts(texts, device="cpu")

    elapsed = time.time() - t0
    np.save(str(VECTORS_FILE), vectors.astype(np.float32))
    np.save(str(CHUNK_IDS_FILE), np.array(chunk_ids, dtype='S64'))

    print(f"  CPU 耗时: {elapsed:.1f}s ({len(texts)/elapsed:.0f} 条/s)")
    print(f"  向量形状: {vectors.shape}")


def _index_gpu_subprocess(chunks: list, chunk_ids: list, texts: list):
    """GPU 子进程：fork 一个单独的 Python 进程跑 embedding

    使用 tempfile（纯 ASCII 路径）避免中文路径编码问题。
    """
    import tempfile

    # 数据文件扔到系统 temp 目录（纯 ASCII 路径）
    data_fd, data_path = tempfile.mkstemp(suffix=".json", prefix="gpu_batch_")
    with os.fdopen(data_fd, 'w', encoding='utf-8') as f:
        json.dump({
            "texts": texts,
            "chunk_ids": chunk_ids,
            "model": MODEL_NAME,
            "out_vectors": str(VECTORS_FILE),
            "out_ids": str(CHUNK_IDS_FILE),
        }, f, ensure_ascii=False)

    # 子进程脚本也写到系统 temp 目录
    worker_fd, worker_path = tempfile.mkstemp(suffix=".py", prefix="gpu_worker_")
    worker_path_obj = Path(worker_path)
    os.close(worker_fd)  # 关闭 fd，后面用 Path 写
    data_abs = data_path.replace('\\', '/')
    worker_path_obj.write_text(
        '# -*- coding: utf-8 -*-\n'
        'import json,sys,os,time\n'
        'data=json.load(open("' + data_abs + '",encoding="utf-8"))\n'
        'print("[GPU] init CUDA...")\n'
        't0=time.time()\n'
        'from transformers import AutoTokenizer,AutoModel\n'
        'import torch,numpy as np\n'
        'device="cuda"\n'
        'tok=AutoTokenizer.from_pretrained(data["model"],local_files_only=True)\n'
        'model=AutoModel.from_pretrained(data["model"],local_files_only=True)\n'
        'model=model.to(device);model.eval()\n'
        'txts=data["texts"];bs=64;all_v=[]\n'  # bs=64, max_length=512 来源:同上——GPU批次大小64，BERT最大长度512
        'for i in range(0,len(txts),bs):\n'
        '  b=txts[i:i+bs]\n'
        '  e=tok(b,padding=True,truncation=True,max_length=512,return_tensors="pt")\n'
        '  e={k:v.to(device) for k,v in e.items()}\n'
        '  with torch.no_grad():\n'
        '    o=model(**e)\n'
        '  m=e["attention_mask"].unsqueeze(-1).float()\n'
        '  p=(o.last_hidden_state*m).sum(1)/m.sum(1).clamp(min=1e-9)\n'
        '  p=p/p.norm(dim=1,keepdim=True).clamp(min=1e-12)\n'
        '  all_v.append(p.cpu().numpy())\n'
        'v=np.concatenate(all_v).astype(np.float32)\n'
        'el=time.time()-t0\n'
        'print("[GPU] shape=%s %.1fs (%.0f/s)"%(str(v.shape),el,len(txts)/el))\n'
        'np.save(data["out_vectors"],v)\n'
        'np.save(data["out_ids"],np.array(data["chunk_ids"],dtype="S64"))\n'
        'print("[GPU] saved")\n'
    )

    proc = subprocess.run(
        [sys.executable, str(worker_path_obj)],
        capture_output=True, text=True, timeout=None  # 无超时，全量索引可能>30分钟
    )

    # 清理临时文件
    try: os.unlink(data_path)
    except Exception: pass
    try: worker_path_obj.unlink()
    except Exception: pass

    if proc.returncode == 0 and VECTORS_FILE.exists():
        print(proc.stdout)
    else:
        print("[GPU] subprocess failed (exit=%d)" % proc.returncode)
        if proc.stderr:
            print("  stderr:", proc.stderr[:500])
        print("  => falling back to CPU...")
        _index_cpu(chunks, chunk_ids, texts)


# ═══════════════════════════════════════════
# search — 语义检索
# ═══════════════════════════════════════════

def cmd_search(args):
    """语义检索：CPU 模式，零 GPU 风险"""
    if not args:
        print("用法: text_scanner.py search \"<query>\" [--top N] [--dir <dir>]")
        return

    # 解析参数
    top_n = 10  # 来源:默认返回 top-10 结果，经验值平衡覆盖与噪声
    dir_filter = None
    query_parts = []
    skip_next = False
    stdin_query = None
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a == "--top" and i + 1 < len(args):
            top_n = int(args[i + 1])
            skip_next = True
        elif a == "--dir" and i + 1 < len(args):
            dir_filter = args[i + 1]
            skip_next = True
        elif a == "--stdin":
            # Windows 管道编码问题：用 buffer 读原始字节再 UTF-8 解码
            if hasattr(sys.stdin, 'buffer'):
                stdin_query = sys.stdin.buffer.read().decode('utf-8').strip()
            else:
                stdin_query = sys.stdin.read().strip()
        elif a.startswith("--top="):
            top_n = int(a.split("=", 1)[1])
        elif a.startswith("--dir="):
            dir_filter = a.split("=", 1)[1]
        else:
            query_parts.append(a)

    query = stdin_query or " ".join(query_parts)
    if not query:
        print("[x] 请输入查询关键词")
        return

    if not VECTORS_FILE.exists():
        print("[x] 没有索引，先运行: text_scanner.py index")
        return

    # 加载索引
    import numpy as np
    vectors = np.load(str(VECTORS_FILE))
    chunk_ids = np.load(str(CHUNK_IDS_FILE))

    # 加载 chunks
    chunks_map = {}
    if CHUNKS_FILE.exists():
        with open(CHUNKS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        c = json.loads(line)
                        chunks_map[c["id"]] = c
                    except json.JSONDecodeError:
                        continue

    # Embed 查询（CPU 安全，transformers 直接编码）
    t0 = time.time()
    q_vec = _encode_texts(["query: " + query], device="cpu")[0]
    embed_time = time.time() - t0

    # 余弦相似度 = dot product (归一化后)
    t0 = time.time()
    scores = np.dot(vectors, q_vec)
    search_time = time.time() - t0

    # 排序
    top_indices = np.argsort(scores)[::-1][:top_n]

    # 输出
    results = []
    for idx in top_indices:
        cid = chunk_ids[idx].decode() if isinstance(chunk_ids[idx], bytes) else str(chunk_ids[idx])
        chunk = chunks_map.get(cid)
        if not chunk:
            continue
        score = float(scores[idx])
        if score < 0.1:
            continue  # 太低的相关度跳过

        path = chunk.get("path", "?")
        if dir_filter and dir_filter not in path:
            continue

        text = chunk.get("text", "")
        # 找到匹配位置附近
        preview = _make_preview(text, query)
        results.append({
            "score": score,
            "path": path,
            "preview": preview,
            "chunk_idx": chunk.get("chunk_idx", 0),
            "n_chunks": chunk.get("n_chunks", 1),
        })

    if not results:
        print(f"[x] 未匹配: \"{query}\"")
        return

    print(f"\n[Search] {query}")
    print(f"   检索 {len(vectors)} 条 | {embed_time:.2f}s embed + {search_time*1000:.0f}ms search")
    print()

    for i, r in enumerate(results, 1):
        tag = f"[{r['chunk_idx']+1}/{r['n_chunks']}]" if r['n_chunks'] > 1 else ""
        print(f"{i}. [{r['score']:.3f}] {r['path']} {tag}")
        print(f"   {r['preview']}")
        print()


def _make_preview(text: str, query: str, context_chars: int = 80) -> str:
    """找到查询在文本中的位置，取上下文"""
    text_clean = text.replace('\n', ' ').strip()
    idx = text_clean.lower().find(query.lower())
    if idx < 0:
        return text_clean[:200] + ('…' if len(text_clean) > 200 else '')

    start = max(0, idx - context_chars)
    end = min(len(text_clean), idx + len(query) + context_chars)
    preview = text_clean[start:end]
    if start > 0:
        preview = "…" + preview
    if end < len(text_clean):
        preview = preview + "…"
    return preview


# ═══════════════════════════════════════════
# update — 增量更新
# ═══════════════════════════════════════════

def cmd_update(args):
    """增量扫描 + 重新索引变更文件"""
    use_gpu = "--gpu" in args
    print("[Update] 增量更新...")

    # Step 1: 增量扫描
    old_chunks_count = 0
    if CHUNKS_FILE.exists():
        old_chunks_count = sum(1 for _ in open(CHUNKS_FILE, 'r', encoding='utf-8') if _.strip())

    cmd_scan([a for a in args if a != "--gpu"])

    new_chunks_count = 0
    if CHUNKS_FILE.exists():
        new_chunks_count = sum(1 for _ in open(CHUNKS_FILE, 'r', encoding='utf-8') if _.strip())

    changed = new_chunks_count - old_chunks_count
    if changed == 0:
        print("[OK] 无变更，索引无需更新")
        return

    print(f"[Embed] {changed} 块变更 ({new_chunks_count} 总计)")

    # Step 2: 重建索引
    # 如果已有索引，只重新 embedding
    if VECTORS_FILE.exists():
        if abs(changed) > new_chunks_count * 0.3:
            # 变动太大，全量重建
            print("  变动 >30%，全量重建索引...")
            cmd_index(["--force", "--gpu" if use_gpu else ""])
        else:
            # 变动小，全量重建（增量 embedding 太复杂，小项目全量更快）
            print("  增量重建索引...")
            cmd_index(["--force", "--gpu" if use_gpu else ""])
    else:
        cmd_index(["--gpu" if use_gpu else ""])


# ═══════════════════════════════════════════
# status — 索引统计
# ═══════════════════════════════════════════

def cmd_status(args):
    """索引统计"""
    meta = {}
    if META_FILE.exists():
        meta = json.loads(META_FILE.read_text(encoding='utf-8'))

    n_chunks = 0
    disk_size = 0
    if CHUNKS_FILE.exists():
        with open(CHUNKS_FILE, 'r', encoding='utf-8') as f:
            n_chunks = sum(1 for _ in f if _.strip())
        disk_size = CHUNKS_FILE.stat().st_size

    n_vectors = 0
    vec_size = 0
    if VECTORS_FILE.exists():
        n_vectors = os.path.getsize(VECTORS_FILE) // (EMBED_DIM * 4)
        vec_size = VECTORS_FILE.stat().st_size

    n_files = 0
    if FILE_INDEX_FILE.exists():
        fi = json.loads(FILE_INDEX_FILE.read_text(encoding='utf-8'))
        n_files = len(fi)

    print(f"\n{'='*50}")
    print(f"[Status] 文字索引状态")
    print(f"{'='*50}")
    print(f"  源文件:   {n_files} 个")
    print(f"  文本块:   {n_chunks} 块 ({disk_size/1024:.1f} KB)")
    print(f"  向量:     {n_vectors} 条 ({vec_size/1024/1024:.1f} MB)")
    print(f"  模型:     {meta.get('model', '未索引')}")
    print(f"  维数:     {meta.get('dim', EMBED_DIM)}")
    print(f"  更新:     {meta.get('updated', '未索引')}")
    print(f"  模式:     {meta.get('mode', '未索引')}")
    print()

    if n_chunks > 0:
        # 按目录统计
        dir_counts = {}
        with open(CHUNKS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        c = json.loads(line)
                        d = c["path"].split('/')[0] if '/' in c["path"] else "root"
                        dir_counts[d] = dir_counts.get(d, 0) + 1
                    except json.JSONDecodeError:
                        continue
        print(f"  覆盖目录:")
        for d in sorted(dir_counts, key=dir_counts.get, reverse=True)[:10]:
            print(f"    {d}: {dir_counts[d]} 块")
    print()


# ═══════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════

def main():

    if len(sys.argv) < 2:
        print(__doc__.strip())
        return

    cmd = sys.argv[1]
    cmd_args = sys.argv[2:]

    cmds = {
        "scan": cmd_scan,
        "index": cmd_index,
        "search": cmd_search,
        "update": cmd_update,
        "status": cmd_status,
    }

    if cmd not in cmds:
        print(f"[TEXT] 未知命令: {cmd}")
        print(f"  可用: {', '.join(cmds.keys())}")
        return

    cmds[cmd](cmd_args)


if __name__ == "__main__":
    main()
