"""
eval_json_baseline.py — 小模型 JSON 输出稳定性基线测量
======================================================
测量 classify / summarize 任务的 JSON valid rate（可解析率）和内容命中率。
用 CLS 实际 prompt 格式（small_model.py 同款），保证基线可对比。

用法:
  python eval_json_baseline.py --backend local     # 本地 ollama qwen2.5:1.5b
  python eval_json_baseline.py --backend silicon   # 硅基流动 Qwen2.5-7B (需 key)
  python eval_json_baseline.py --backend local --model qwen2.5:1.5b --task all

指标:
  valid_rate   = 输出可 json.loads 的比例
  schema_rate  = 含目标 key 的比例
  hit_rate     = 分类/摘要内容匹配率 (classify 需 category in categories)
  errors       = 错误类型分布 (非JSON/缺key/多token)
"""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

OLLAMA_URL = "http://localhost:11434"
SF_URL = "https://api.siliconflow.cn/v1/chat/completions"

# ── CLS 真实样本（材料/EP/文档/代码 混合）──────────────────
CLASSIFY_SAMPLES = [
    {"text": "BN-Si3N4 陶瓷，耐温500°C，适合<DOMAIN设备>通道壁", "cats": ["陶瓷", "金属", "塑料"]},
    {"text": "铝合金6061，密度2.7g/cm3，导热系数167W/mK", "cats": ["陶瓷", "金属", "塑料"]},
    {"text": "聚四氟乙烯PTFE，摩擦系数0.04，耐温260°C", "cats": ["陶瓷", "金属", "塑料"]},
    {"text": "这是<DOMAIN>推力器的点火时序说明文档", "cats": ["技术文档", "实验数据", "代码"]},
    {"text": "<DOMAIN设备><部件B>放电电流随磁场变化的实验记录", "cats": ["技术文档", "实验数据", "代码"]},
    {"text": "def thruster_model(B, mdot): return alpha*B**2/mdot", "cats": ["技术文档", "实验数据", "代码"]},
    {"text": "该推力器采用钡钨<部件A>，发射体寿命约500小时", "cats": ["<部件A>", "<部件B>", "磁场"]},
    {"text": "<部件B>恒压300V，励磁电流从2.3A扫描至0A", "cats": ["<部件A>", "<部件B>", "磁场"]},
    {"text": "呼吸模频率10-30kHz，由predator-prey机制驱动", "cats": ["<部件A>", "<部件B>", "磁场"]},
    {"text": "真空罐背压影响呼吸模强度，跨罐对比需标注背压", "cats": ["诊断技巧", "实验设置", "物理模型"]},
    {"text": "先用Welch方法对频谱多段平均降噪", "cats": ["诊断技巧", "实验设置", "物理模型"]},
    {"text": "电离深度假说：种子电子调控<部件B>电离深度", "cats": ["诊断技巧", "实验设置", "物理模型"]},
    {"text": "这份报告总结了66组工况的EEDF分析结果", "cats": ["分析报告", "论文", "会议纪要"]},
    {"text": "关于PLUME模式分类判据的学术论文摘要", "cats": ["分析报告", "论文", "会议纪要"]},
    {"text": "项目周会记录：下周进行PIC仿真验证", "cats": ["分析报告", "论文", "会议纪要"]},
]

SUMMARIZE_SAMPLES = [
    "<部件A>流量从3.0降到1.5 sccm时，<传感器>测到的电子密度上升而EEDF复杂度下降，这是电离深度变化的结果。",
    "真空罐背压对<DOMAIN设备>呼吸模强度有显著影响，背压升高呼吸模振幅增大，跨罐对比时必须标注背压差异。",
    "BN-Si3N4陶瓷具有优异的耐高温性能，在500°C下仍能保持结构稳定，适合用作<DOMAIN设备>通道壁材料。",
    "呼吸振荡不是寄生噪声，而是放电自持的功能标志，无呼吸代表反馈环断裂。",
    "<sensor><传感器>直接测量局域EEDF，但不能测量总电离量，<传感器>位置决定采样环节。",
    "Eff_dim是EEDF的FFT频谱有效维度，大表示多尺度结构，小表示近Maxwell分布。",
    "聚焦放电只需要足够强的B场，与<部件A>流量无关，flow=1.5也能出现聚焦态。",
    "双温崩塌发生在flow=2.0，此流量以下电子群温度结构不可逆变化。",
    "keeper电流控制<部件A>双层强度，流量只影响散射抹平，不改变EEDF基本框架。",
    "K=0.5A时双温比值被锁在1.12，0-D模型用双鞘和束电子贡献解释。",
    "Vp_diff大于7V可以排除发散放电，是<传感器>最有用的单指标信息。",
    "<部件A>流量通过回流传导调控<部件B>电离深度，是EEDF复杂度的主控变量。",
    "全息投影技术在航天器装配中的应用前景广阔，可提高装配精度。",
    "量化交易策略需要经过严格的回测才能上线，避免过拟合。",
    "这篇论文综述了<DOMAIN设备>近二十年的研究进展。",
]


def call_ollama(prompt: str, system: str, model: str) -> str | None:
    body = json.dumps({
        "model": model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ], "stream": False,
        "options": {"temperature": 0.1, "num_predict": 256},
        "keep_alive": "5m",
    }).encode("utf-8")
    try:
        req = Request(f"{OLLAMA_URL}/api/chat", data=body,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("message", {}).get("content", "")
    except Exception as e:
        return f"__ERROR__ {e}"


def call_silicon(prompt: str, system: str, model: str) -> str | None:
    import os
    key = os.environ.get("SILICONFLOW_API_KEY", "")
    if not key:
        kf = Path(__file__).resolve().parent.parent.parent / "keys" / "siliconflow_config.json"
        if kf.exists():
            key = json.loads(kf.read_text(encoding="utf-8"))["api_key"]
    body = json.dumps({"model": model, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": prompt},
    ], "max_tokens": 256, "temperature": 0.1}, ensure_ascii=False).encode("utf-8")
    try:
        req = Request(SF_URL, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("Content-Type", "application/json")
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"__ERROR__ {e}"


def classify_prompt(text: str, cats: list[str]) -> tuple[str, str]:
    c = "\n".join(f"- {x}" for x in cats)
    prompt = (f"将以下内容分类到最合适的类别。只返回类别名和置信度。\n\n"
              f"内容: {text}\n\n候选类别:\n{c}\n\n"
              f"回复 JSON: {{\"category\": \"类别名\", \"confidence\": 0.0-1.0}}")
    return prompt, "你是文本分类器。只回复JSON，不解释。"


def summarize_prompt(text: str, max_words: int = 50) -> tuple[str, str]:
    prompt = (f"用中文一句话总结以下内容（不超过{max_words}字）：\n\n{text}\n\n"
              f"回复 JSON: {{\"summary\": \"一句话摘要\"}}")
    return prompt, "你是文本摘要器。只回复JSON，不解释。"


def extract_json(raw: str):
    """尝试解析 JSON，容忍 ```json 包裹和前后杂质。"""
    if not raw or raw.startswith("__ERROR__"):
        return None, "call_error"
    s = raw.strip()
    # 去掉 markdown 围栏
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
    # 定位第一个 { 和最后一个 }
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1:
        return None, "no_json"
    try:
        obj = json.loads(s[a:b + 1])
        return obj, "ok"
    except json.JSONDecodeError:
        return None, "json_decode_error"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["local", "silicon"], default="local")
    ap.add_argument("--model", default="qwen2.5:1.5b")
    ap.add_argument("--task", choices=["classify", "summarize", "all"], default="all")
    args = ap.parse_args()

    caller = call_ollama if args.backend == "local" else call_silicon
    tag = f"{args.backend}/{args.model}"

    print(f"=== JSON 稳定性基线: {tag} ===")
    summary = {}

    if args.task in ("classify", "all"):
        ok = 0; schema_ok = 0; hit = 0; errs = {}
        t0 = time.time()
        for i, s in enumerate(CLASSIFY_SAMPLES):
            prompt, system = classify_prompt(s["text"], s["cats"])
            raw = caller(prompt, system, args.model)
            obj, status = extract_json(raw)
            if obj is not None:
                ok += 1
                if "category" in obj and "confidence" in obj:
                    schema_ok += 1
                    if obj.get("category") in s["cats"]:
                        hit += 1
            errs[status] = errs.get(status, 0) + 1
            if i < 3 or obj is None:
                print(f"  [{i}] {status} | raw={raw[:90]!r}")
        n = len(CLASSIFY_SAMPLES)
        print(f"\n[classify] {n} 条 | valid={ok}/{n} ({ok/n*100:.0f}%) | "
              f"schema={schema_ok}/{n} | hit={hit}/{n} | 耗时{time.time()-t0:.0f}s")
        print(f"  错误分布: {errs}")
        summary["classify"] = {"n": n, "valid": ok, "schema": schema_ok, "hit": hit}

    if args.task in ("summarize", "all"):
        ok = 0; schema_ok = 0; errs = {}
        t0 = time.time()
        for i, s in enumerate(SUMMARIZE_SAMPLES):
            prompt, system = summarize_prompt(s)
            raw = caller(prompt, system, args.model)
            obj, status = extract_json(raw)
            if obj is not None:
                ok += 1
                if "summary" in obj:
                    schema_ok += 1
            errs[status] = errs.get(status, 0) + 1
            if i < 3 or obj is None:
                print(f"  [{i}] {status} | raw={raw[:90]!r}")
        n = len(SUMMARIZE_SAMPLES)
        print(f"\n[summarize] {n} 条 | valid={ok}/{n} ({ok/n*100:.0f}%) | "
              f"schema={schema_ok}/{n} | 耗时{time.time()-t0:.0f}s")
        print(f"  错误分布: {errs}")
        summary["summarize"] = {"n": n, "valid": ok, "schema": schema_ok}

    print(f"\n=== 汇总: {tag} ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
