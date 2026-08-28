#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stance_read.py — active_context.json stance 档位只读器 (改动a 2026-08-22·maintainer批准)
====================================================================
四档: farming(常规,默认) / skirmish(修复循环保护) / teamfight(交付/核心文件) / retreat(incident-log诊断保护)

职责边界 (仓库铁律):
  - 本文件只读不写。stance 写入唯一路径 = scripts/mcp_cls_tools.py 的 cog-context(set-stance)
    → _cog_lock + _cog_cas_write(fencing版本CAS) + 原子替换。禁止 hook/脚本直写 active_context.json。
  - stdlib-only, 供 ops_monitor / cognitive_gate 每次调用零负担 import (不引入 FastMCP 重依赖)。

读取语义 (所有读取方必须一致):
  - 字段缺失 / JSON 损坏 / 读失败 → farming (fail-open)
  - 乱值(不在四档内, 含大小写变体) → farming
  - expires_at 已过 → farming (惰性 TTL 过期, 无需后台任务)
  - mode=farming 时行为与"无 stance 字段"逐字节等价

@since: 2026-08-22 改动a (三改动框架顺序 c→b→a 的最后一步)
"""

import json
import time
from pathlib import Path

STANCE_MODES = ("farming", "skirmish", "teamfight", "retreat")

_CTX_PATH = Path(__file__).resolve().parent.parent.parent / "state" / "active_context.json"


def read_stance(ctx_path=None) -> dict:
    """读 stance 档位。

    Returns:
        {"mode": str(必为 STANCE_MODES 之一), "expires_at": float|None,
         "set_by": str, "reason": str}
        任何异常情形 mode 一律回落 "farming"。
    """
    out = {"mode": "farming", "expires_at": None, "set_by": "", "reason": ""}
    path = Path(ctx_path) if ctx_path else _CTX_PATH
    try:
        if not path.exists():
            return out
        raw = json.loads(path.read_text(encoding="utf-8"))
        s = raw.get("stance") if isinstance(raw, dict) else None
        if isinstance(s, str):
            # 兼容纯字符串简写: 乱值 → farming
            out["mode"] = s if s in STANCE_MODES else "farming"
            return out
        if not isinstance(s, dict):
            return out
        mode = s.get("mode")
        if mode not in STANCE_MODES:
            return out
        exp = s.get("expires_at")
        if exp is not None:
            # TTL 字段存在但非数值 → 整体不可信 → farming (fail-safe, 防"过期时间损坏致档位永久卡死")
            if isinstance(exp, bool) or not isinstance(exp, (int, float)):
                return out
            if time.time() > float(exp):
                return out  # TTL 已过期 → farming
            out["expires_at"] = float(exp)
        out["mode"] = mode
        out["set_by"] = str(s.get("set_by", ""))
        out["reason"] = str(s.get("reason", ""))
        return out
    except Exception:
        return out  # fail-open: 读失败不影响主流程


if __name__ == "__main__":
    print(json.dumps(read_stance(), ensure_ascii=False))
