"""
eval_param_extract.py — 参数提取器可行性小验证
=================================================
测 ep-json 能否从<传感器>报告文本提取参数 JSON（格式 + 数值双查）。

用法: python eval_param_extract.py [--model ep-json]
"""
import argparse
import json
from urllib.request import Request, urlopen

from param_postprocess import postprocess_param_json

OLLAMA = "http://localhost:11434"

SAMPLES = [
    {"text": "flow=3.0 sccm，Ib=2.3A，B=200Gs，eff_dim=17，放电呈聚焦态",
     "expect": {"flow": 3.0, "Ib": 2.3, "B": 200, "eff_dim": 17, "mode": "focused"}},
    {"text": "<部件A>流量2.0 sccm，励磁电流0.5A，eff_dim 仅2，双温崩塌",
     "expect": {"flow": 2.0, "Ib": 0.5, "eff_dim": 2}},
    {"text": "<传感器>测得 Vp=300V，Vp_diff=9V，Ne=8.7e9，模式为聚焦放电",
     "expect": {"Vp": 300, "Vp_diff": 9, "Ne": 8.7e9, "mode": "focused"}},
    {"text": "flow 1.5 时 B 降至 30Gs，呼吸峰消失，接近熄火",
     "expect": {"flow": 1.5, "B": 30}},
    {"text": "keeper=1A 强双层，mid_E=0.36，eff_dim=24.5，Vp_diff=9.5V",
     "expect": {"keeper": 1.0, "mid_E": 0.36, "eff_dim": 24.5, "Vp_diff": 9.5}},
    {"text": "flow 2.5 sccm，Ib 1.1A，eff_dim 6，处于过渡态",
     "expect": {"flow": 2.5, "Ib": 1.1, "eff_dim": 6, "mode": "transition"}},
]


def call(model, prompt, system):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 256},
    }, ensure_ascii=False).encode("utf-8")
    req = Request(f"{OLLAMA}/api/chat", data=body,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"].strip()


def near(a, b, tol=0.05):
    # 字符串期望值 (mode 等) 精确比较, 不做数值转换
    if isinstance(b, str):
        return isinstance(a, str) and a == b
    try:
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))
    except (TypeError, ValueError):
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="ep-json")
    args = ap.parse_args()

    print(f"=== 参数提取可行性: {args.model} ===")
    valid_n = key_n = val_n = 0
    for i, s in enumerate(SAMPLES):
        user = (f"从下面<传感器>实验文本提取参数，只输出JSON（数值保持原样，mode用英文）：\n\n"
                f"{s['text']}\n\n回复 JSON: 参数对象")
        raw = call(args.model, user, "你是实验参数提取器。只回复JSON，不解释。")
        try:
            obj = postprocess_param_json(json.loads(raw), s["text"])
            valid_n += 1
            # 检查 expect 字段
            keys_hit = 0
            vals_hit = 0
            for k, v in s["expect"].items():
                if k in obj:
                    keys_hit += 1
                    if near(obj[k], v):
                        vals_hit += 1
            key_n += keys_hit
            val_n += vals_hit
            total = len(s["expect"])
            print(f"  [{i}] JSON OK | 字段 {keys_hit}/{total} | 数值 {vals_hit}/{total}")
            print(f"       got: {json.dumps(obj, ensure_ascii=False)[:120]}")
        except json.JSONDecodeError:
            print(f"  [{i}] FAIL {raw[:120]!r}")
    total_keys = sum(len(s["expect"]) for s in SAMPLES)
    print(f"\n[result] valid={valid_n}/{len(SAMPLES)} | 字段命中={key_n}/{total_keys} | 数值命中={val_n}/{total_keys}")


if __name__ == "__main__":
    main()
