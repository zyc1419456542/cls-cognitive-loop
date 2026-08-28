"""
eval_mem_summary.py — 记忆摘要 3 字段验证
==========================================
测模型能否把 EP 文本压缩为 {topic, key_points[], decisions[]}。
检查: JSON valid + 三键存在 + 数组类型 + topic 长度。

用法: python eval_mem_summary.py [--model ep-mem]
"""
import argparse
import json
from urllib.request import Request, urlopen

OLLAMA = "http://localhost:11434"

# 训练数据外的测试文本 (泛化测试)
SAMPLES = [
    "<DOMAIN设备>运行时，B场增强会使放电从发散态转变为聚焦态，聚焦态下EEDF出现高能电子峰，推力效率提升约15%。",
    "真空罐背压高于1e-3 Pa时，呼吸模振幅显著增大，跨罐对比实验必须标注背压值，否则结论不可比。",
    "<传感器>数据采集正常完成，等待进一步分析。",
    "聚焦放电只要求足够强的B场，与<部件A>流量无关，因此低流量工况也能维持聚焦态。",
    "<sensor><传感器>直接测量局域EEDF，但不能测量总电离量，<传感器>位置决定采样环节。",
]


def call(model, prompt, system):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 400},
    }, ensure_ascii=False).encode("utf-8")
    req = Request(f"{OLLAMA}/api/chat", data=body,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="ep-mem")
    args = ap.parse_args()

    print(f"=== 记忆摘要 3 字段: {args.model} ===")
    valid_n = 0
    for i, text in enumerate(SAMPLES):
        user = f"把下面文本压缩为记忆摘要 JSON：\n{text}"
        raw = call(args.model, user,
                   "你是记忆摘要器。只回复 JSON 对象：topic(≤24字), key_points(数组), decisions(数组)。")
        try:
            obj = json.loads(raw)
            checks = []
            checks.append(("valid_json", True))
            checks.append(("topic", isinstance(obj.get("topic"), str)))
            checks.append(("topic_len<=30", len(obj.get("topic", "")) <= 30))
            checks.append(("key_points_list", isinstance(obj.get("key_points"), list)))
            checks.append(("decisions_list", isinstance(obj.get("decisions"), list)))
            kp = obj.get("key_points") or []
            checks.append(("key_points_nonempty", len(kp) >= 1))
            ok = all(c[1] for c in checks)
            if ok:
                valid_n += 1
            print(f"  [{i}] {'OK ' if ok else 'BAD'} " + " | ".join(
                f"{n}:{('✓' if v else '✗')}" for n, v in checks))
            print(f"       {raw[:160]}")
        except json.JSONDecodeError:
            print(f"  [{i}] FAIL(非JSON) {raw[:120]!r}")
    print(f"\n[result] valid={valid_n}/{len(SAMPLES)}")


if __name__ == "__main__":
    main()
