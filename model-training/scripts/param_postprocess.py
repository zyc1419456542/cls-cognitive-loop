"""
param_postprocess.py — 参数提取后处理清洗
==========================================
模型输出 JSON → 规则清洗:
  1. mode 中英映射 (聚焦/发散/过渡 → focused/diffuse/transition)
  2. 字段白名单 (删除伪造字段)
  3. 原文补 mode (模型漏提时从原文正则补)

用于生产: 模型负责格式与数值, 规则负责领域约束。两者互补。

用法: from param_postprocess import postprocess_param_json
"""
import re

# 已知 EP 参数字段白名单 (模型不得输出以外的字段)
ALLOWED_FIELDS = {
    "flow", "Ib", "B", "keeper", "eff_dim", "mid_E", "low_E",
    "Ne", "n_e", "Vp", "Vp_diff", "Vd", "mode", "electron_temp",
    "double_temp_ratio", "n_ion",
}

# 数值域约束 (超范围 = 伪造, 删除字段)
RANGES = {
    "flow": (0, 10), "Ib": (0, 5), "B": (0, 500), "keeper": (0, 5),
    "eff_dim": (0, 50), "mid_E": (0, 1), "low_E": (0, 1),
    "Ne": (1e6, 1e20), "n_e": (1e6, 1e20), "Vp": (0, 1000), "Vp_diff": (0, 100),
    "Vd": (0, 1000), "electron_temp": (0, 50),
    "double_temp_ratio": (0, 5), "n_ion": (1e6, 1e20),
}

# mode 中英映射 (覆盖: X放电 / X态 / X / X模式 / X状态)
MODE_MAP = {
    "聚焦放电": "focused", "聚焦态": "focused", "聚焦": "focused",
    "聚焦模式": "focused", "聚焦状态": "focused",
    "发散放电": "diffuse", "发散态": "diffuse", "发散": "diffuse",
    "发散模式": "diffuse", "发散状态": "diffuse",
    "过渡态": "transition", "过渡放电": "transition", "过渡": "transition",
    "过渡模式": "transition", "过渡状态": "transition",
}
# 按长度降序匹配 (优先匹配长词)
_MODE_ITEMS = sorted(MODE_MAP.items(), key=lambda kv: -len(kv[0]))


def _mode_from_text(text):
    """从原文提取 mode (中文 → 英文)"""
    if not text:
        return None
    for zh, en in _MODE_ITEMS:
        if zh in text:
            return en
    m = re.search(r"(focused|diffuse|transition)", text, re.I)
    return m.group(1).lower() if m else None


def postprocess_param_json(obj, text=""):
    """清洗参数提取模型输出。

    Args:
        obj: 模型输出的 dict (已 json.loads)
        text: 原始<传感器>文本 (用于 mode 补全)

    Returns:
        清洗后 dict
    """
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k, v in obj.items():
        if k not in ALLOWED_FIELDS:
            continue  # 白名单外字段删除
        if k == "mode":
            if isinstance(v, str):
                en = _mode_from_text(v) or _mode_from_text(text)
                if en:
                    out[k] = en
            continue
        # 数值字段必须是数字且在物理域内
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        lo, hi = RANGES.get(k, (None, None))
        if lo is not None and not (lo <= v <= hi):
            continue
        out[k] = v
    # 原文含 mode 描述但模型没输出 → 补全
    if "mode" not in out:
        en = _mode_from_text(text)
        if en:
            out["mode"] = en
    return out
