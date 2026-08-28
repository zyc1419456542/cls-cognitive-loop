"""
eval_multi_field.py — 验证 ep-json 的多字段泛化
=================================================
测试模型能否从单字段(schema {"summary"}) 扩展到多字段结构化摘要
({"topic", "key_points", "decisions"}) —— 决定记忆摘要的数据量设计。

用法: python eval_multi_field.py [--model ep-json]
"""
import argparse
import json
from urllib.request import Request, urlopen

OLLAMA = "http://localhost:11434"

SAMPLES = [
    "<部件A>流量从3.0降到1.5 sccm时，<传感器>测到的电子密度上升而EEDF复杂度下降，这是电离深度变化的结果。",
    "keeper电流控制<部件A>双层强度，流量只影响散射抹平，不改变EEDF基本框架。",
    "全息投影技术在航天器装配中的应用前景广阔，可提高装配精度。",
    "双温崩塌发生在flow=2.0，此流量以下电子群温度结构不可逆变化。",
    "这篇论文综述了<DOMAIN设备>近二十年的研究进展。",
    "聚焦放电只需要足够强的B场，与<部件A>流量无关，flow=1.5也能出现聚焦态。",
]

SCHEMA = {"topic", "key_points", "decisions"}


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="ep-json")
    args = ap.parse_args()

    print(f"=== 多字段泛化测试: {args.model} ===")
    valid = full = 0
    for i, text in enumerate(SAMPLES):
        user = (f"把下面内容整理成结构化摘要：\n\n{text}\n\n"
                f"回复 JSON: {{\"topic\": \"主题\", \"key_points\": [\"要点1\", \"要点2\"], \"decisions\": [\"决策1\"]}}")
        raw = call(args.model, user, "你是结构化摘要器。只回复JSON，不解释。")
        try:
            obj = json.loads(raw)
            has = all(k in obj for k in SCHEMA)
            valid += 1
            full += has
            print(f"  [{i}] {'FULL' if has else 'PARTIAL'} {raw[:130]}")
        except json.JSONDecodeError:
            print(f"  [{i}] FAIL {raw[:130]!r}")
    print(f"\n[result] valid={valid}/{len(SAMPLES)} | schema全字段={full}/{len(SAMPLES)}")


if __name__ == "__main__":
    main()
