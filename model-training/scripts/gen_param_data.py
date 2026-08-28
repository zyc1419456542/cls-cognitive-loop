"""
gen_param_data.py — 参数提取训练数据生成 v3 (配比调整版)
=========================================================
从 fft_spectral_features.json 的 67 组真值，程序化生成
"<传感器>报告叙述文本 → 参数 JSON" 训练样本（Qwen ChatML messages 格式）。

v3 配比调整 (2026-08-15, 根治白名单内伪造字段):
  - 全字段模板降为每 sweep 1 变体 (67 条) — 不再占 70%
  - 字段缺失扩到 12 条 (教会"只输出出现的字段")
  - mode 中英规范化扩到 15 条
  - 字段缺失+mode 组合 4 条
  - 科学计数法 8 条 (展开形式 + 指数 token)
  - 负样本 3 条

用法: python gen_param_data.py
输出: model-training/data/param_sft_messages.jsonl
"""
import json
import random
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FFT = Path(r"<REPO_ROOT>\assistant交付\🎨 assistant设计\02_domainX与<介质>\<DOMAIN>研究\20260714_domainX数字孪生阶段交付\03_<传感器>微观物理\<部件B><传感器>数据\EEDF_FFT分析\fft_spectral_features.json")
OUT = BASE / "data" / "param_sft_messages.jsonl"

# 全字段叙述模板 (每 sweep 1 变体)
TEMPLATES = [
    "flow={flow} sccm，励磁电流 Ib={Ib}A，EEDF 频谱有效维度 eff_dim={eff_dim}，中频能量占比 mid_E={mid_E}，低频占比 low_E={low_E}",
    "<部件A>流量 {flow} sccm，Ib 扫描至 {Ib}A，FFT 频谱复杂度 eff_dim={eff_dim}，中频占比 {mid_E}",
    "工况 flow={flow}、Ib={Ib}A：EEDF 结构复杂度 eff_dim={eff_dim}，mid_E={mid_E}，low_E={low_E}",
    "<传感器>分析：流量 {flow} sccm，励磁 {Ib}A，频谱维度 {eff_dim}，中频能量 {mid_E}",
    "flow={flow}，Ib={Ib}A，eff_dim={eff_dim}，mid_E={mid_E}，low_E={low_E}",
]


def make_params(d):
    return {
        "flow": float(d["flow"]),
        "Ib": float(d["Ib"]),
        "eff_dim": int(d["eff_dim"]),
        "mid_E": round(float(d["mid_E"]), 4),
        "low_E": round(float(d["low_E"]), 4),
    }


def main():
    data = json.load(open(FFT, encoding="utf-8"))
    print(f"[源] fft_spectral_features.json: {len(data)} sweep")

    samples = []
    rng = random.Random(42)

    # ── 全字段模板: 每 sweep 1 变体 (67 条) ──
    for i, d in enumerate(data):
        params = make_params(d)
        tpl = TEMPLATES[i % len(TEMPLATES)]
        text = tpl.format(**d)
        answer = json.dumps(params, ensure_ascii=False)
        samples.append({"task": "param_extract", "messages": [
            {"role": "system", "content": "你是实验参数提取器。只回复JSON，数值转数字，不解释。"},
            {"role": "user", "content":
                f"从下面<传感器>实验文本提取参数，只输出JSON（数值转数字，不含单位）：\n\n{text}\n\n回复 JSON: 参数对象"},
            {"role": "assistant", "content": answer}]})

    # ── 字段缺失: 12 条 (只输出出现的字段) ──
    partial_templates = [
        ([("flow", 3.0)], "flow={flow} sccm，其余参数未记录"),
        ([("flow", 2.0), ("Ib", 1.5)], "<部件A>流量 {flow} sccm，Ib={Ib}A"),
        ([("Ib", 0.8)], "励磁电流 Ib={Ib}A"),
        ([("eff_dim", 12)], "EEDF 频谱有效维度 eff_dim={eff_dim}"),
        ([("mid_E", 0.45), ("low_E", 0.31)], "中频占比 mid_E={mid_E}，低频 low_E={low_E}"),
        ([("flow", 1.5), ("eff_dim", 9)], "flow={flow} sccm，频谱维度 eff_dim={eff_dim}"),
        ([("flow", 2.5), ("mid_E", 0.5)], "flow={flow} sccm，中频占比 {mid_E}"),
        ([("Ib", 1.2), ("eff_dim", 15)], "Ib={Ib}A，eff_dim={eff_dim}"),
        ([("flow", 2.0), ("low_E", 0.28)], "flow={flow} sccm，低频占比 {low_E}"),
        ([("eff_dim", 20), ("Ib", 2.0)], "eff_dim={eff_dim}，Ib={Ib}A"),
        ([("mid_E", 0.33)], "中频能量占比 {mid_E}"),
        ([("low_E", 0.4)], "低频占比 {low_E}"),
    ]
    for fields, tpl in partial_templates:
        fmt = dict(fields)
        text = tpl.format(**fmt)
        answer = json.dumps({k: v for k, v in fields}, ensure_ascii=False)
        samples.append({"task": "param_extract", "messages": [
            {"role": "system", "content": "你是实验参数提取器。只输出文本中出现的参数，未出现的字段不要加，只回复JSON。"},
            {"role": "user", "content": f"提取参数（只含文本出现的字段）：{text} → JSON"},
            {"role": "assistant", "content": answer}]})

    # ── mode 中英规范化: 15 条 ──
    mode_variants = [
        ("放电呈聚焦态", "focused"), ("呈聚焦放电", "focused"), ("聚焦放电状态", "focused"),
        ("进入聚焦模式", "focused"), ("表现出聚焦特征", "focused"),
        ("放电呈发散态", "diffuse"), ("呈发散放电", "diffuse"), ("发散放电状态", "diffuse"),
        ("进入发散模式", "diffuse"), ("表现出发散特征", "diffuse"),
        ("处于过渡态", "transition"), ("呈过渡放电", "transition"), ("过渡放电状态", "transition"),
        ("进入过渡模式", "transition"), ("表现出过渡特征", "transition"),
    ]
    for i, (zh, en) in enumerate(mode_variants):
        flow = 1.5 + 0.25 * (i % 7)
        ib = 0.3 + 0.3 * (i % 5)
        text = f"flow={flow:.1f} sccm，Ib={ib:.1f}A，{zh}，人眼判读确认"
        answer = json.dumps({"mode": en, "flow": round(flow, 1), "Ib": round(ib, 1)}, ensure_ascii=False)
        samples.append({"task": "param_extract", "messages": [
            {"role": "system", "content": "你是实验参数提取器。mode字段必须用英文(focused/diffuse/transition)，只回复JSON。"},
            {"role": "user", "content": f"提取参数（mode用英文）：{text} → JSON"},
            {"role": "assistant", "content": answer}]})

    # ── 字段缺失 + mode 组合: 4 条 ──
    combo_templates = [
        ([("flow", 3.0), ("mode", "focused")], "flow={flow} sccm，{mode_text}"),
        ([("Ib", 1.5), ("mode", "transition")], "Ib={Ib}A，{mode_text}"),
        ([("eff_dim", 10), ("mode", "diffuse")], "eff_dim={eff_dim}，{mode_text}"),
        ([("flow", 2.0), ("Ib", 0.8), ("mode", "focused")], "flow={flow} sccm，Ib={Ib}A，{mode_text}"),
    ]
    mode_text_map = {"focused": "聚焦态", "transition": "过渡态", "diffuse": "发散态"}
    for fields, tpl in combo_templates:
        fd = dict(fields)
        fmt = {k: (mode_text_map[v] if k == "mode" else v) for k, v in fields}
        fmt["mode_text"] = mode_text_map[fd["mode"]]
        text = tpl.format(**fmt)
        answer = json.dumps({k: v for k, v in fields}, ensure_ascii=False)
        samples.append({"task": "param_extract", "messages": [
            {"role": "system", "content": "你是实验参数提取器。只输出文本中出现的参数，mode用英文(focused/diffuse/transition)，只回复JSON。"},
            {"role": "user", "content": f"提取参数（只含出现的字段，mode英文）：{text} → JSON"},
            {"role": "assistant", "content": answer}]})

    # ── 科学计数法: 展开形式 (4) + 指数 token (4) ──
    for flow, ne in [(3.0, 8.7e9), (2.0, 1.3e10), (1.5, 1.7e10), (2.5, 9.3e9)]:
        text = (f"flow={flow} sccm，<传感器>测得电子密度 Ne={ne:.1e} m^-3，"
                f"n_e 随电离深度变化")
        answer = json.dumps({"flow": flow, "Ne": ne, "n_e": ne}, ensure_ascii=False)
        samples.append({"task": "param_extract", "messages": [
            {"role": "system", "content": "你是实验参数提取器。只回复JSON，数值保留科学计数法，不解释。"},
            {"role": "user", "content": f"提取参数（Ne用科学计数法）：{text} → JSON"},
            {"role": "assistant", "content": answer}]})
    for flow, ne in [(3.0, 8.7e9), (2.0, 1.3e10), (1.5, 1.7e10), (2.5, 9.3e9)]:
        text = f"flow={flow} sccm，Ne={ne:.1e} m^-3（<sensor> <传感器>测得）"
        answer = '{"flow": %s, "Ne": %s}' % (int(flow), f"{ne:.1e}")
        samples.append({"task": "param_extract", "messages": [
            {"role": "system", "content": "你是实验参数提取器。Ne 必须保留科学计数法形式（如 8.7e9），只回复JSON。"},
            {"role": "user", "content": f"提取参数（Ne 保留指数）：{text} → JSON"},
            {"role": "assistant", "content": answer}]})

    # ── 负样本: 3 条 ──
    for text in ["<传感器>曲线平滑，无明显特征峰", "数据采集正常完成，等待进一步分析",
                 "该文本不包含实验参数信息"]:
        samples.append({"task": "param_extract", "messages": [
            {"role": "system", "content": "你是实验参数提取器。若无参数信息输出空对象 {}，只回复JSON。"},
            {"role": "user", "content": f"提取参数：{text} → JSON"},
            {"role": "assistant", "content": "{}"}]})

    # ── 增强 6: 真实报告负样本 (text_pool 无参数段 → {}, 教"大多数文本没参数") ──
    import re as _re
    TEXT_POOL = BASE / "data" / "text_pool.jsonl"
    pool = [json.loads(l)["text"] for l in open(TEXT_POOL, encoding="utf-8") if l.strip()]

    def _param_count(t):
        n = 0
        for pat in [r"flow\s*=\s*[\d.]+", r"eff[_ ]?dim\s*=\s*[\d.]+",
                    r"B\s*=\s*[\d.]+\s*Gs", r"Ib\s*=\s*[\d.]+"]:
            if _re.search(pat, t):
                n += 1
        return n

    rng.seed(11)
    no_param = [t for t in pool if _param_count(t) < 2 and 30 <= len(t) <= 300]
    neg_pool = rng.sample(no_param, min(30, len(no_param)))
    for text in neg_pool:
        samples.append({"task": "param_extract", "messages": [
            {"role": "system", "content": "你是实验参数提取器。文本若无明确参数信息，输出空对象 {}，不要编造。只回复JSON。"},
            {"role": "user", "content": f"提取参数：{text} → JSON"},
            {"role": "assistant", "content": "{}"}]})
    print(f"  [增强6] 真实负样本 {len(neg_pool)} 段 (无参数 → {{}})")

    # ── 增强 7: 真实格式正样本 (正则提取真值, 含 LaTeX 噪声和 mode) ──
    MODE_RE = {"聚焦": "focused", "发散": "diffuse", "过渡": "transition"}
    real_pos = 0
    for text in pool:
        found = {}
        m = _re.search(r"flow\s*=\s*([\d.]+)", text)
        if m:
            found["flow"] = float(m.group(1))
        m = _re.search(r"eff[_ ]?dim\s*=\s*([\d.]+)", text)
        if m:
            found["eff_dim"] = float(m.group(1))
        m = _re.search(r"B\s*=\s*([\d.]+)", text)
        if m:
            found["B"] = float(m.group(1))
        m = _re.search(r"Ib\s*=\s*([\d.]+)", text)
        if m:
            found["Ib"] = float(m.group(1))
        for zh, en in MODE_RE.items():
            if zh in text and "mode" not in found:
                found["mode"] = en
        if len(found) >= 2:
            answer = json.dumps(found, ensure_ascii=False)
            samples.append({"task": "param_extract", "messages": [
                {"role": "system", "content": "你是实验参数提取器。提取文本中明确出现的参数，mode用英文，只回复JSON。"},
                {"role": "user", "content": f"从下面实验记录文本提取参数，只输出JSON：\n\n{text}\n\n回复 JSON: 参数对象"},
                {"role": "assistant", "content": answer}]})
            real_pos += 1
    print(f"  [增强7] 真实正样本 {real_pos} 段 (正则提取真值)")

    # 去重: 按 user 叙述去重
    seen, uniq = set(), []
    for s in samples:
        k = s["messages"][1]["content"]
        if k not in seen:
            seen.add(k)
            uniq.append(s)

    # 配比统计
    full = sum(1 for s in uniq if "数值转数字" in s["messages"][0]["content"])
    print(f"[生成] {len(uniq)} 条 | 全字段模板 {full} 条 ({full/len(uniq)*100:.0f}%)")

    with open(OUT, "w", encoding="utf-8") as f:
        for s in uniq:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"       -> {OUT}")


if __name__ == "__main__":
    main()
