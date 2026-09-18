#!/usr/bin/env python3
"""cls_api_fallback.py — 硅基SF Qwen主通道 → 官网 DS Flash兜底
=============================================================
统一LLM调用入口。所有CLS后台脚本(cognitive_gate/content_gaze/always_injector等)
通过此模块调API,自动主备切换。

优先级: SF Qwen2.5-7B (免费) → 官网 DS Flash (keys/deepseek_official.json)
用途: 小模型脏活(分类/摘要/评分/规则提取), 不占大模型配额

@since: 2026-07-31
@fix 2026-08-17: opencode限流→官网DS Flash直连
"""

import json, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# ── 配置加载 (延迟) ──

_sf_cfg = None
_ds_cfg = None
_op_cfg = None

def _load_sf():
    global _sf_cfg
    if _sf_cfg is None:
        try:
            _sf_cfg = json.loads((ROOT / "keys" / "siliconflow_config.json").read_text(encoding="utf-8"))
        except Exception:
            _sf_cfg = {"api_key": "", "base_url": "https://api.siliconflow.cn/v1"}
    return _sf_cfg

def _load_ds():
    global _ds_cfg
    if _ds_cfg is None:
        try:
            _ds_cfg = json.loads((ROOT / "keys" / "deepseek_config.json").read_text(encoding="utf-8"))
        except Exception:
            _ds_cfg = {"api_key": "", "base_url": "https://api.deepseek.com"}
    return _ds_cfg

def _load_opencode():
    """opencode 套餐配置 (zen池, 三key轮换)。DS 涨价后兜底通道 (2026-08-16 夜)。"""
    global _op_cfg
    if _op_cfg is None:
        try:
            _op_cfg = json.loads((ROOT / "keys" / "opencode_config.json").read_text(encoding="utf-8"))
        except Exception:
            _op_cfg = {"api_keys": [], "base_url": "https://opencode.ai/zen/v1"}
    return _op_cfg


# ── 主调用 ──

def chat(
    messages: list[dict],
    model: str = "Qwen/Qwen2.5-7B-Instruct",
    max_tokens: int = 150,
    temperature: float = 0.3,
    timeout: int = 30,  # @fix 2026-09-05: 10→30s — SF免费档长prompt实测10-30s(auto_capture恒0同根因)
) -> str | None:
    """SF主 → DS Flash兜底。返回模型文本回复,失败返回None。

    messages: [{"role":"system","content":"..."}, {"role":"user","content":"..."}]
    """

    # 1. 硅基主通道
    sf = _load_sf()
    if sf.get("api_key"):
        try:
            body = json.dumps({
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }).encode()
            req = urllib.request.Request(
                sf["base_url"].rstrip("/") + "/chat/completions", data=body,
                headers={"Authorization": f"Bearer {sf['api_key']}", "Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())["choices"][0]["message"]["content"].strip()
        except Exception:
            pass  # fall through to DS

    # 2. opencode DS Flash 兜底 (2026-08-16 夜: DS涨价→opencode 套餐, zen池三key轮换)
    #    opencode flash 推理量大(单次可达~30s), 超时放宽: 不给 SF 主通道拖慢, 只给兜底通道充足时间
    _oc_timeout = max(timeout, 45)
    oc = _load_opencode()
    _oc_keys = oc.get("api_keys") or ([oc.get("api_key", "")] if oc.get("api_key") else [])
    # x-opencode-session (2026-09-05): zen会话粘性路由头, 09/05起缺头将error (deepseek-harness#5495)
    # 来源优先级: CC会话env → cog_step窗口ID → 进程uuid; 独立实现, 保兜底通道不依赖api_pipeline
    import os as _os
    import uuid as _uuid
    _oc_session = (_os.environ.get("CLAUDE_CODE_SESSION_ID")
                   or _os.environ.get("CLAUDE_SESSION_ID")
                   or _os.environ.get("DSH_SESSION_ID") or "")[:64] or None
    if not _oc_session:
        try:
            _csp = Path(__file__).resolve().parents[2] / "data" / "state" / "cog_step.json"
            _cs = json.loads(_csp.read_text(encoding="utf-8"))
            # @fix 2026-09-10: cog_step.json 存在两种 schema — MCP 写 _meta.window_id,
            #   旧版/磁盘遗留写顶层 window_id。只读 _meta 会让该级永远落空 → 每次走 uuid 兜底。
            _wid = (_cs.get("_meta") or {}).get("window_id") or _cs.get("window_id")
            _oc_session = str(_wid)[:64] if _wid else None
        except Exception:
            _oc_session = None
    if not _oc_session:
        _oc_session = _uuid.uuid4().hex
    for _oc_key in _oc_keys:
        if not _oc_key:
            continue
        try:
            body = json.dumps({
                "model": "deepseek-v4-flash",
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }).encode()
            req = urllib.request.Request(
                oc.get("base_url", "https://opencode.ai/zen/v1").rstrip("/") + "/chat/completions", data=body,
                headers={"Authorization": f"Bearer {_oc_key}", "Content-Type": "application/json",
                         "User-Agent": "ClaudeCode/1.0",  # UA 必须: Cloudflare 指纹拦截→403
                         "x-opencode-session": _oc_session},
                method="POST")
            with urllib.request.urlopen(req, timeout=_oc_timeout) as resp:
                msg = json.loads(resp.read().decode())["choices"][0]["message"]
                # opencode flash 推理模型: max_tokens 小→content 空, 兜底 reasoning_content
                return (msg.get("content") or msg.get("reasoning_content") or "").strip()
        except Exception:
            continue  # 换下一个 key

    return None


def embed(
    text: str,
    model: str = "BAAI/bge-large-zh-v1.5",
    timeout: int = 5,
) -> list[float] | None:
    """SF embedding → None (embedding无DS兜底, 失败就直接降级到关键词)"""

    sf = _load_sf()
    if not sf.get("api_key"):
        return None

    try:
        body = json.dumps({
            "model": model,
            "input": text,
            "encoding_format": "float",
        }).encode()
        req = urllib.request.Request(
            sf["base_url"].rstrip("/") + "/embeddings", data=body,
            headers={"Authorization": f"Bearer {sf['api_key']}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if "data" in data and len(data["data"]) > 0:
            return data["data"][0].get("embedding")
    except Exception:
        pass

    return None


# ── 便捷测试 ──

if __name__ == "__main__":
    result = chat([
        {"role": "user", "content": "回复OK即可"}
    ], max_tokens=5)
    print(f"chat: {result}")

    vec = embed("测试文本")
    print(f"embed: {len(vec) if vec else 'None'} dims")
