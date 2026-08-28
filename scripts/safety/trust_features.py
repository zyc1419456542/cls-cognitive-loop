#!/usr/bin/env python3
"""
trust_features.py — 多维信任特征提取器  |  v1.1 (web DS review)
================================================================
纯 Python+NumPy，不调 LLM。输入文本 → 输出 8 维特征向量。

特征维度（不可压缩设计——维度间不能互相代偿）:
  1. topological_entropy    — 符号动力学拓扑熵 (0=确定, ln(n)=随机)
  2. spectral_radius        — 转移矩阵谱半径 (0~1)
  3. anchor_density         — 文件路径引用密度
  4. sentence_length_cv     — 句长变异系数 (std/mean)
  5. repetition_ratio       — N-gram 重复率 (中文用词级, 英文用char级)
  6. forbidden_word_hits    — 自评关键词 + 第一人称命中 (含缓和词过滤)
  7. self_reference_density — 每百字符自指次数
  8. struct_completeness    — 四章节完整度 (含评价留白污染+HTML注释过滤)

v1.1 改进 (web DS 2026-06-06):
  - 最大 token 限制 3000，防止长文本特征分解超时
  - 中文分句支持 jieba (可选fallback)，repetition_ratio 用词级 3-gram
  - anchor_density 正则允许 Windows 路径中的空格
  - sentence_length_cv 增加英文断句符
  - forbidden_word_hits 过滤缓和词上下文 (参考/根据/依据)
  - struct_completeness 过滤 HTML 注释 <!-- -->
  - 短文本 (<200 chars) 跳过熵/谱半径特征
  - NaN/Inf 保护

用法:
    from scripts.safety.trust_features import extract
    result = extract(text)
    print(result["features"]["topological_entropy"])

CLI:
    python scripts/safety/trust_features.py --file <path> [--json]
    python scripts/safety/trust_features.py --text "..."  [--json]
    python scripts/safety/trust_features.py --calibrate --input data/safety/qwen_gate_log.jsonl
"""

import json, os, sys, re, time
from pathlib import Path

import numpy as np

# ── 路径设置 ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

# ══ 本地计算 KS 熵 + SLEM，不依赖 symbolic_dynamics_engine 的 eigenvalue 方法 ══
# 原因: stochastic matrix λ_max≡1 → ln(λ_max)≡0 / spectral_radius≡1，两个特征均退化
# 改用: KS metric entropy + SLEM (second largest eigenvalue magnitude)

# ── 可选: jieba 中文分词 ──
try:
    import jieba
    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False

# ── 常量 ──
FORBIDDEN_PATTERNS = [
    "验证通过", "确认正确", "我检查了", "没问题", "保证正确",
    "验证完毕", "已核实", "已确认", "verified", "checked",
    "没有问题", "正确性验证", "已验证", "检验通过",
]

SELF_REFERENCE_TERMS = [
    "我", "我们",   # name placeholder removed, "本系统", "本AI", "CLS",
]

HEDGING_TERMS = ["参考", "根据", "依据", "按照", "按照", "基于", "对照"]

REQUIRED_SECTIONS = ["设计思路", "过程轨迹", "产出物", "评价留白"]

MAX_TOKENS = 3000          # 最大 token 数，防止长文本特征分解超时
MIN_CHARS_FOR_ENTROPY = 200  # 短文本跳过熵特征

# 文件路径正则 (v1.1: 允许空格，但避免过度匹配)
PATH_PATTERN = re.compile(
    r'[A-Za-z]:[\\/][^\n\r]{2,}\.\w{2,6}'   # C:\...\file.ext (允许空格)
    r'|/[^\s\n]{2,}/[^\s\n]{1,}\.\w{2,6}'    # /path/to/file.ext
    r'|`[^`]{2,}\.\w{2,6}`'                    # `script.py`
)


def _tokenize(text: str) -> list[str]:
    """分词（v1.1: 中文优先 jieba，否则逐字）"""
    text = re.sub(r'[ \t\n\r\f\v]+', ' ', text.strip())

    # 分离中英文部分
    parts = re.findall(
        r'[a-zA-Z_][a-zA-Z0-9_]*'
        r'|[一-鿿]+'   # 中文连续段
        r'|\d+\.?\d*'
        r'|[^\w\s]',
        text
    )

    punct = set(r'，。！？、；：""''（）【】《》〈〉—…·.,!?;:\'\"()[]{}--')
    tokens = []
    for p in parts:
        if p.strip() and p not in punct:
            # 中文段: 用 jieba 分词 (或逐字fallback)
            if re.match(r'[一-鿿]+', p):
                if _JIEBA_AVAILABLE and len(p) > 1:
                    tokens.extend(jieba.lcut(p))
                else:
                    tokens.extend(list(p))  # 逐字
            else:
                tokens.append(p)
    return tokens


def _compute_symbolic(text: str) -> dict:
    """计算符号动力学特征"""
    # 短文本跳过
    if len(text) < MIN_CHARS_FOR_ENTROPY:
        return {
            "topological_entropy": None,
            "spectral_radius": None,
            "alphabet_size": 0,
            "total_tokens": min(len(text), MAX_TOKENS),
            "skipped": True,
            "reason": f"文本过短 (<{MIN_CHARS_FOR_ENTROPY} chars)"
        }

    tokens = _tokenize(text)

    # 限制 token 数 (v1.1: 防止矩阵过大)
    if len(tokens) > MAX_TOKENS:
        tokens = tokens[:MAX_TOKENS]

    if len(tokens) < 2:
        return {
            "topological_entropy": None,
            "spectral_radius": None,
            "alphabet_size": len(tokens),
            "total_tokens": len(tokens),
            "skipped": True,
            "reason": "token 数不足"
        }

    unique = list(dict.fromkeys(tokens))
    n = len(unique)

    # 限制 letter 表大小 (v1.1: 也是为了防止矩阵过大)
    if n > 1024:
        # 保留频率最高的 1024 个符号
        from collections import Counter
        top = Counter(tokens).most_common(1024)
        keep = {t[0] for t in top}
        tokens = [t for t in tokens if t in keep]
        unique = list(dict.fromkeys(tokens))
        n = len(unique)

    idx = {sym: i for i, sym in enumerate(unique)}
    T = np.zeros((n, n))
    for i in range(len(tokens) - 1):
        r, c = idx[tokens[i]], idx[tokens[i + 1]]
        T[r, c] += 1
    row_sums = T.sum(axis=1, keepdims=True)
    T = np.divide(T, row_sums, out=np.zeros_like(T), where=row_sums > 0)

    try:
        # ── KS 度量熵: h = -Σ π_i Σ_j T_ij·ln(T_ij) ──
        # 找平稳分布 π (左特征向量, λ≈1)
        eigenvalues, eigenvectors = np.linalg.eig(T.T)  # 左特征向量 = T^T 的右特征向量
        eig_vals = np.abs(eigenvalues)
        # 找最接近 1 的特征值的索引
        idx_one = np.argmin(np.abs(eig_vals - 1.0))
        pi_raw = np.abs(eigenvectors[:, idx_one])
        pi = pi_raw / (pi_raw.sum() + 1e-15)  # 归一化平稳分布

        # KS 熵: H_i = -Σ_j T_ij·ln(T_ij),  h = Σ_i π_i·H_i
        with np.errstate(divide='ignore', invalid='ignore'):
            T_log = np.where(T > 0, T * np.log(T), 0.0)
        row_entropies = -T_log.sum(axis=1)
        h = float(np.dot(pi, row_entropies))

        # ── SLEM: 第二大特征值模长 (衡量混合速度) ──
        # 排序特征值模长，取第二大
        sorted_mags = np.sort(np.abs(eigenvalues))[::-1]
        slem = float(sorted_mags[1]) if len(sorted_mags) > 1 else 0.0

        # NaN/Inf 保护
        if np.isnan(h) or np.isinf(h):
            h = 0.0
        if np.isnan(slem) or np.isinf(slem):
            slem = 1.0

    except Exception:
        h, slem = 0.0, 1.0

    return {
        "topological_entropy": round(h, 4),
        "spectral_radius": round(slem, 4),
        "alphabet_size": n,
        "total_tokens": len(tokens),
    }


def _compute_anchor_density(text: str, total_lines: int) -> float:
    """文件路径引用密度 = 锚点数 / 总行数"""
    anchors = len(PATH_PATTERN.findall(text))
    return round(anchors / max(total_lines, 1), 4)


def _compute_sentence_length_cv(text: str) -> float:
    """句长变异系数 (v1.1: 中英文混合断句)"""
    sentences = re.split(r'[。！？\n.!?;]+', text)
    lengths = [len(s) for s in sentences if s.strip() and len(s.strip()) > 1]

    if len(lengths) < 3:
        return 0.0

    arr = np.array(lengths, dtype=float)
    mean_val = arr.mean()
    if mean_val < 1:
        return 0.0
    return round(float(arr.std() / mean_val), 4)


def _compute_repetition_ratio(text: str) -> float:
    """N-gram 重复率 (v1.1: 词级 3-gram，更准)"""
    tokens = _tokenize(text)
    if len(tokens) < 4:
        return 0.0

    trigrams = [tuple(tokens[i:i + 3]) for i in range(len(tokens) - 2)]
    unique = len(set(trigrams))
    total = len(trigrams)

    if total == 0:
        return 0.0
    return round(1.0 - (unique / total), 4)


def _compute_forbidden_hits(text: str) -> int:
    """自评关键词命中 (v1.1: 过滤缓和词上下文)"""
    hits = 0
    for fp_match in re.finditer(r'\b(我|我们|本系统|本AI)\b', text):
        start = max(0, fp_match.start() - 40)
        end = min(len(text), fp_match.end() + 40)
        context = text[start:end]

        # 检查是否有缓和词 (v1.1)
        has_hedging = any(h in context for h in HEDGING_TERMS)

        for forbidden in FORBIDDEN_PATTERNS:
            if forbidden in context:
                if has_hedging:
                    # 有缓和词 → 可能是引用/讨论而非自评
                    continue
                hits += 1
                break

    return hits


def _compute_self_reference_density(text: str, total_chars: int) -> float:
    """每百字符的自指次数"""
    count = 0
    for term in SELF_REFERENCE_TERMS:
        count += len(re.findall(re.escape(term), text))
    return round(count / max(total_chars, 1) * 100, 4)


def _compute_struct_completeness(text: str) -> float:
    """四章节完整度 (v1.1: 过滤 HTML 注释)"""
    # 先移除 HTML 注释
    cleaned = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)

    found = sum(1 for s in REQUIRED_SECTIONS if s in cleaned)
    score = found / len(REQUIRED_SECTIONS)

    # 评价留白污染检测
    if "评价留白" in cleaned:
        parts = cleaned.split("评价留白", 1)
        if len(parts) > 1:
            after = parts[1]
            next_heading = re.search(r'^#{1,3}\s', after, re.MULTILINE)
            content = after[:next_heading.start()] if next_heading else after

            # 过滤引用块 (> ) 和 HTML 注释
            non_empty = [
                l for l in content.split('\n')
                if l.strip()
                and not l.strip().startswith('> ')
                and not re.match(r'^\s*<!--.*?-->\s*$', l)
            ]
            if non_empty:
                score *= 0.5

    return round(score, 4)


def extract(text: str) -> dict:
    """输入文本 → 输出 8 维特征向量"""
    t0 = time.perf_counter()

    lines = text.split('\n')
    total_lines = len(lines)
    total_chars = len(text)

    symbolic = _compute_symbolic(text)

    # NaN/Inf 保护
    def _safe(v):
        if v is None:
            return 0.0
        if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
            return 0.0
        return v

    features = {
        "topological_entropy": _safe(symbolic.get("topological_entropy")),
        "spectral_radius": _safe(symbolic.get("spectral_radius")),
        "anchor_density": _compute_anchor_density(text, total_lines),
        "sentence_length_cv": _compute_sentence_length_cv(text),
        "repetition_ratio": _compute_repetition_ratio(text),
        "forbidden_word_hits": _compute_forbidden_hits(text),
        "self_reference_density": _compute_self_reference_density(text, total_chars),
        "struct_completeness": _compute_struct_completeness(text),
    }

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    return {
        "features": features,
        "meta": {
            "total_lines": total_lines,
            "total_chars": total_chars,
            "token_count": symbolic.get("total_tokens", 0),
            "alphabet_size": symbolic.get("alphabet_size", 0),
            "skipped_symbolic": symbolic.get("skipped", False),
            "compute_time_ms": elapsed_ms,
        }
    }


# ── 校准模式 ──

def calibrate(log_path: str) -> dict:
    """从 qwen_gate_log.jsonl 的 claim_summary 字段提取特征分布"""
    log_file = Path(log_path)
    if not log_file.exists():
        return {"error": f"文件不存在: {log_path}"}

    samples = {"pass": [], "fail": [], "flag": []}

    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            claim = entry.get("claim_summary", "")
            verdict = entry.get("verdict", "flag")
            if verdict.startswith("fail"):
                cat = "fail"
            elif verdict.startswith("flag"):
                cat = "flag"
            elif verdict.startswith("pass"):
                cat = "pass"
            else:
                cat = "flag"

            if claim:
                result = extract(claim)
                samples[cat].append(result["features"])

    report = {"sample_counts": {k: len(v) for k, v in samples.items()}}

    for cat, features_list in samples.items():
        if not features_list:
            report[cat] = {"count": 0, "message": "无样本"}
            continue

        dims = {}
        for feat_name in features_list[0].keys():
            values = [f[feat_name] for f in features_list
                      if isinstance(f[feat_name], (int, float))
                      and not (isinstance(f[feat_name], float) and np.isnan(f[feat_name]))]
            if values:
                arr = np.array(values, dtype=float)
                dims[feat_name] = {
                    "mean": round(float(arr.mean()), 4),
                    "std": round(float(arr.std()), 4),
                    "p25": round(float(np.percentile(arr, 25)), 4),
                    "p50": round(float(np.percentile(arr, 50)), 4),
                    "p75": round(float(np.percentile(arr, 75)), 4),
                    "p95": round(float(np.percentile(arr, 95)), 4),
                    "min": round(float(arr.min()), 4),
                    "max": round(float(arr.max()), 4),
                }

        report[cat] = {"count": len(features_list), "dimensions": dims}

    return report


# ── CLI ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="多维信任特征提取器 v1.1")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=str, help="从文件读取文本")
    group.add_argument("--text", type=str, help="直接传入文本")
    group.add_argument("--stdin", action="store_true", help="从标准输入读取")
    group.add_argument("--calibrate", action="store_true", help="校准模式")
    parser.add_argument("--input", type=str, help="校准输入文件路径")
    parser.add_argument("--output", type=str, help="校准报告输出路径")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    if args.calibrate:
        log_path = args.input or str(
            _PROJECT_ROOT / "data" / "safety" / "qwen_gate_log.jsonl"
        )
        report = calibrate(log_path)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"校准报告已写入: {args.output}")
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.stdin:
        text = sys.stdin.read()
    else:
        text = args.text

    result = extract(text)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        f = result["features"]
        m = result["meta"]
        print(f"信任特征向量 ({m['total_chars']}字, {m['compute_time_ms']}ms)")
        print("=" * 50)
        print(f"  拓扑熵:           {f['topological_entropy']:.4f}  (0=确定, >2.5=发散)")
        print(f"  谱半径:           {f['spectral_radius']:.4f}  (0~1)")
        print(f"  锚定密度:         {f['anchor_density']:.4f}  (路径引用/行)")
        print(f"  句长变异系数:     {f['sentence_length_cv']:.4f}  (>0.3=自然)")
        print(f"  重复率:           {f['repetition_ratio']:.4f}  (<0.3=健康)")
        print(f"  禁止词命中:       {f['forbidden_word_hits']}  (0=安全)")
        print(f"  自指密度:         {f['self_reference_density']:.4f}  (/百字)")
        print(f"  结构完整度:       {f['struct_completeness']:.4f}  (1.0=完整)")
        if m.get("skipped_symbolic"):
            print(f"  ⚠ 符号动力学已跳过: 文本过短")
        print("=" * 50)


if __name__ == "__main__":
    main()
