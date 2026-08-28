#!/usr/bin/env python3
"""
fuse_board.py — 熔断板：独立于认知系统的硬编码安全层
============================================

核心理念：
  自指系统无法从内部约束自己。熔断板是在认知系统之外运行的硬编码层。
  它不聪明，但它不参与思考。它只做一件事：在越界时拉闸。

关键设计纪律：
  1. 在认知系统加载之前执行
  2. 不 import 任何认知系统模块（纯 stdlib）
  3. 配置不可自改（认知系统无 fuses_config.json 写入权限）
  4. 日志独立（fuse_log.jsonl 由本模块独占写入）
  5. 硬件迁移时只换后端一行代码

用法:
  from scripts.core_engine.fuse_board import fuse_board

  # 操作前查询
  if not fuse_board.check("WRITE_PROTECT", {"path": "some/file.py"}):
      print("操作被熔断拦截")
      return

  # 熔断事件记录
  fuse_board.trip("RECURSION_LIMIT", "自指深度超过5层", {"depth": 7})

  # 查询状态
  status = fuse_board.status()

  # 重置（仅人工操作）
  fuse_board.reset("TOKEN_BUDGET")
"""

from __future__ import annotations

import enum
import fnmatch
import json
import sys
import uuid

# 确保 stdout 可以输出中文（Windows GBK 终端兼容）
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ─── paths ────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "data" / "safety" / "fuses_config.json"
LOG_PATH = PROJECT_ROOT / "data" / "safety" / "fuse_log.jsonl"
STATE_PATH = PROJECT_ROOT / "data" / "safety" / "fuse_state.json"


# ─── fuse type enum ───────────────────────────────────────────

class FuseType(str, enum.Enum):
    """熔断器类型 — 每个熔断器对应一次历史事故教训"""
    WRITE_PROTECT = "WRITE_PROTECT"              # 自指修改核心依赖事件：自指修改核心依赖
    RECURSION_LIMIT = "RECURSION_LIMIT"          # Token暴涨：自指无上限嵌套
    TOKEN_BUDGET = "TOKEN_BUDGET"                # 烧钱：无预算限制的API调用
    PARALLEL_CAP = "PARALLEL_CAP"                # 力大转飞：同时推太多变更
    CHECKPOINT_REQUIRED = "CHECKPOINT_REQUIRED"  # 无回滚点的痛苦
    PROXY_PURITY = "PROXY_PURITY"                # P07崩溃：代理层做语义变换
    NUMERIC_COMPUTATION = "NUMERIC_COMPUTATION"  # 数字不进推理流：所有数值走本地轮子
    SELF_EVALUATION_PROHIBITED = "SELF_EVALUATION_PROHIBITED"  # 禁止自我评价：创造层不碰评价层，核心设计原则，2026-06-03
    DUAL_AI_GATE = "DUAL_AI_GATE"                              # 双AI闸门：所有产出/学习必须过Qwen独立验证，核心设计原则，2026-06-03
    QWEN_DOWN_TOO_LONG = "QWEN_DOWN_TOO_LONG"                  # Qwen长时间离线: >30min阻断需验证的领域知识产出, 2026-06-05


# ─── fuse type 的允许字符串值（给外部调用用）───────────────────

FUSE_TYPES = {ft.value for ft in FuseType}


def _validate_fuse_type(ft: str) -> str:
    """验证熔断器类型字符串，非法则抛 ValueError。"""
    if ft not in FUSE_TYPES:
        raise ValueError(
            f"未知熔断器类型: {ft}。可用: {', '.join(sorted(FUSE_TYPES))}"
        )
    return ft


# ─── 抽象接口层 (硬件迁移的协议) ──────────────────────────────

class FuseBackend(ABC):
    """熔断后端抽象接口——软件/硬件共享同一协议。

    迁移到硬件时只换一行:
        backend = SoftwareFuseBackend()  # 现在
        backend = HardwareFuseBackend()  # 未来——接口不变
    """

    @abstractmethod
    def check(self, fuse_type: str, context: dict | None = None) -> bool:
        """查询：这个操作允许吗？返回 True=允许继续, False=熔断拦截"""
        ...

    @abstractmethod
    def trip(self, fuse_type: str, reason: str,
             context: dict | None = None) -> dict:
        """拉闸：记录熔断事件。返回事件记录字典"""
        ...

    @abstractmethod
    def status(self) -> dict:
        """查询所有熔断器当前状态"""
        ...

    @abstractmethod
    def reset(self, fuse_type: str | None = None) -> bool:
        """重置熔断器。fuse_type=None 时重置全部。返回是否成功"""
        ...


# ─── 软件后端 (当前实现) ──────────────────────────────────────

class SoftwareFuseBackend(FuseBackend):
    """软件熔断后端

    实现方式：
      - 配置：JSON 文件 (fuses_config.json)
      - 日志：JSONL 文件 (fuse_log.jsonl)
      - 状态：JSON 文件 (fuse_state.json) — 运行时计数器持久化
      - 写保护：路径模式匹配 + 文件只读属性尝试

    限制（与硬件后端对比）：
      - 可被绕过（自指系统可以选择不调用 check）
      - 文件权限可被用户/管理员覆盖
      - 没有皮秒级硬件判定速度
    """

    def __init__(self) -> None:
        self._config: dict = self._load_config()
        self._state: dict = self._load_state()
        self._log_path = LOG_PATH
        self._state_path = STATE_PATH
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    # ── 公共接口 (from FuseBackend) ──────────────────────────

    def check(self, fuse_type: str, context: dict | None = None) -> bool:
        ctx = context or {}
        fuse_cfg = self._get_fuse_config(fuse_type)
        if not fuse_cfg or not fuse_cfg.get("enabled", True):
            return True  # 未配置或未启用 → 放行

        try:
            if fuse_type == FuseType.WRITE_PROTECT.value:
                return self._check_write_protect(fuse_cfg, ctx)
            elif fuse_type == FuseType.RECURSION_LIMIT.value:
                return self._check_recursion_limit(fuse_cfg, ctx)
            elif fuse_type == FuseType.TOKEN_BUDGET.value:
                return self._check_token_budget(fuse_cfg, ctx)
            elif fuse_type == FuseType.PARALLEL_CAP.value:
                return self._check_parallel_cap(fuse_cfg, ctx)
            elif fuse_type == FuseType.CHECKPOINT_REQUIRED.value:
                return self._check_checkpoint_required(fuse_cfg, ctx)
            elif fuse_type == FuseType.PROXY_PURITY.value:
                return self._check_proxy_purity(fuse_cfg, ctx)
            elif fuse_type == FuseType.NUMERIC_COMPUTATION.value:
                return self._check_numeric_computation(fuse_cfg, ctx)
            elif fuse_type == FuseType.SELF_EVALUATION_PROHIBITED.value:
                return self._check_self_evaluation(fuse_cfg, ctx)
            elif fuse_type == FuseType.DUAL_AI_GATE.value:
                return self._check_dual_ai_gate(fuse_cfg, ctx)
            else:
                return True
        except Exception:
            self._emergency_log(fuse_type, "check_exception")
            return True  # 熔断板自己不能崩——异常时默认放行

    def trip(self, fuse_type: str, reason: str,
             context: dict | None = None) -> dict:
        ctx = context or {}
        _validate_fuse_type(fuse_type)

        event = {
            "event_id": uuid.uuid4().hex[:12],
            "fuse_type": fuse_type,
            "reason": reason,
            "action": self._get_fuse_config(fuse_type, {}).get("action", "block"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "context": ctx,
        }

        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

            if fuse_type not in self._state:
                self._state[fuse_type] = {"trip_count": 0, "last_trip": None}
            self._state[fuse_type]["trip_count"] += 1
            self._state[fuse_type]["last_trip"] = event["timestamp"]
            self._state[fuse_type]["last_reason"] = reason
            self._save_state()
        except Exception:
            self._emergency_log(fuse_type, "trip_write_failed")

        return event

    def status(self) -> dict:
        config = self._load_config()
        fuses = config.get("fuses", {})
        state = self._load_state()

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": config.get("version", 0),
            "fuses": {},
        }

        for ft in FUSE_TYPES:
            cfg = fuses.get(ft, {})
            st = state.get(ft, {})
            result["fuses"][ft] = {
                "enabled": cfg.get("enabled", False),
                "description": cfg.get("description", ""),
                "action": cfg.get("action", "block"),
                "trip_count": st.get("trip_count", 0),
                "last_trip": st.get("last_trip"),
                "last_reason": st.get("last_reason"),
            }

        return result

    def reset(self, fuse_type: str | None = None) -> bool:
        try:
            if fuse_type is None:
                self._state = {}
            else:
                _validate_fuse_type(fuse_type)
                self._state.pop(fuse_type, None)
            self._save_state()
            return True
        except Exception:
            return False

    # ── 写保护特别方法 ───────────────────────────────────────

    def get_protected_patterns(self) -> list[str]:
        """返回当前受保护路径模式列表。"""
        cfg = self._get_fuse_config(FuseType.WRITE_PROTECT.value, {})
        return cfg.get("protected_patterns", [])

    def get_trip_count(self, fuse_type: str) -> int:
        """查询指定熔断器的熔断次数。"""
        return self._state.get(fuse_type, {}).get("trip_count", 0)

    # ── 各熔断器检查逻辑 ─────────────────────────────────────

    def _check_write_protect(self, cfg: dict, ctx: dict) -> bool:
        raw_path = ctx.get("path", "")
        if not raw_path:
            return True

        # 统一为相对路径（绝对路径也截取 repo 内部分）
        norm_path = raw_path.replace("\\", "/")
        if ":/" in norm_path:
            # 绝对路径 → 只保留 repo 内相对部分
            parts = norm_path.split("/")
            try:
                idx = parts.index("cls-cognitive-loop")
                target_path = "/".join(parts[idx + 1:])
            except ValueError:
                target_path = raw_path.split("/")[-1]
        else:
            target_path = norm_path

        patterns = cfg.get("protected_patterns", [])
        for pattern in patterns:
            pat_norm = pattern.replace("\\", "/")
            if fnmatch.fnmatch(target_path, pat_norm):
                return False  # 匹配到受保护模式 → 拒绝

        return True

    def _check_recursion_limit(self, cfg: dict, ctx: dict) -> bool:
        max_depth = cfg.get("max_depth", 5)
        current_depth = ctx.get("depth", 0)
        if current_depth > max_depth:
            return False

        # 冷却期内反复触发的额外限制
        cooldown_min = cfg.get("cooldown_minutes", 30)
        ft = FuseType.RECURSION_LIMIT.value
        state_ft = self._state.get(ft, {})
        if state_ft.get("trip_count", 0) > 3:
            last_trip = state_ft.get("last_trip")
            if last_trip:
                elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_trip)
                if elapsed.total_seconds() < cooldown_min * 60:
                    return False

        return True

    def _check_token_budget(self, cfg: dict, ctx: dict) -> bool:
        session_limit = cfg.get("session_limit", 500_000)
        call_tokens = ctx.get("tokens", 0)

        ft = FuseType.TOKEN_BUDGET.value
        state_ft = self._state.get(ft, {})

        # 会话累计检查
        session_used = state_ft.get("session_used", 0)
        if session_used + call_tokens > session_limit:
            return False

        # 日累计检查（不做精确限制，依靠外部的 budget.json）
        daily_used = state_ft.get("daily_used", 0)
        daily_limit = cfg.get("daily_limit", 2_000_000)
        if daily_used + call_tokens > daily_limit:
            return False

        # 单次调用限制
        per_call = cfg.get("per_call_limit", 80_000)
        if call_tokens > per_call:
            return False

        return True

    def _check_parallel_cap(self, cfg: dict, ctx: dict) -> bool:
        max_parallel = cfg.get("max_parallel", 3)
        current_parallel = ctx.get("current_parallel", 0)
        return current_parallel < max_parallel

    def _check_checkpoint_required(self, cfg: dict, ctx: dict) -> bool:
        min_interval = cfg.get("min_interval_seconds", 300)
        ft = FuseType.CHECKPOINT_REQUIRED.value
        state_ft = self._state.get(ft, {})

        last_checkpoint = state_ft.get("last_checkpoint_time")
        if last_checkpoint:
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_checkpoint)
            if elapsed.total_seconds() < min_interval:
                return True  # 刚检查过，不需要再检查

        # 需要检查点但还没做 → 强制先保存
        return False

    def _check_proxy_purity(self, cfg: dict, ctx: dict) -> bool:
        operation = ctx.get("operation", "")
        allowed = cfg.get("allowed_operations", [])
        forbidden = cfg.get("forbidden_patterns", [])

        # 操作在白名单里 → 放行
        if operation in allowed:
            return True

        # 检查是否踩了禁止模式
        for pattern in forbidden:
            if fnmatch.fnmatch(operation, pattern):
                return False

        # 不在白名单也不在禁止名单 → 未知操作，默认拦截
        if operation and operation not in allowed:
            return False

        return True

    def _check_numeric_computation(self, cfg: dict, ctx: dict) -> bool:
        """数值计算熔断：所有数字必须来自已注册轮子。

        context 应包含:
            computation: str   — 计算名称（如 "trust_score"）
            wheel: str        — 来源脚本路径（如 "scripts/safety/audit_gate.py"）
            value: any        — 计算出的数值（可选，仅用于日志）

        检查逻辑：
            1. computation 必须在 registered_wheels 白名单内
            2. wheel 路径必须匹配白名单中对应的路径
            3. 无 computation 字段 → 默认放行（兼容未接入的旧代码）
        """
        comp_name = ctx.get("computation", "")
        wheel_path = ctx.get("wheel", "")

        # 未提供计算名 → 兼容旧代码，放行（同时记录警告）
        if not comp_name:
            return True

        registered = cfg.get("registered_wheels", {})
        if comp_name not in registered:
            # 计算不在白名单 → 拦截
            return False

        allowed_wheel = registered[comp_name]
        if not wheel_path:
            # 有计算名但轮子路径为空 → 拦截
            return False

        # 验证轮子路径匹配（允许路径末尾部分匹配）
        norm_wheel = wheel_path.replace("\\", "/")
        norm_allowed = allowed_wheel.replace("\\", "/")
        if not norm_wheel.endswith(norm_allowed) and norm_allowed not in norm_wheel:
            return False

        return True

    def _check_self_evaluation(self, cfg: dict, ctx: dict) -> bool:
        """自我评价熔断：创造层不得触碰评价层。

        核心设计原则 (2026-06-03):
          创造者只做创造和记录（设计思路、过程轨迹、产出物）。
          分析、裁决、验证、评价全部属于独立审核者。

        context 应包含:
            output_type: str  — 输出类型: "deliverable" | "analysis" | "verdict" | "process"
            content_hint: str — 内容概要（仅用于日志，不深入分析）

        检查逻辑:
            1. output_type="evaluation" / "verdict" / "analysis" → 直接拦截
            2. content_hint 匹配评价性关键词（"正确""验证通过""我检查了"等）→ 拦截
            3. output_type="deliverable" + 无评价性内容 → 放行
            4. 无output_type → 默认放行（兼容旧调用）
        """
        output_type = ctx.get("output_type", "")
        content_hint = ctx.get("content_hint", "")

        forbidden_types = {"evaluation", "verdict", "analysis", "self_check", "verification"}
        forbidden_patterns = cfg.get("forbidden_patterns", [
            "验证通过", "确认正确", "我检查了", "没有问题", "验证完毕",
            "正确性", "已核实", "已确认", "pass", "correct",
            "verified", "checked", "没问题", "保证正确",
        ])

        # 输出类型直接拦截
        if output_type in forbidden_types:
            return False

        # 内容摘要含评价性关键词 → 拦截
        if content_hint:
            for pat in forbidden_patterns:
                if pat.lower() in content_hint.lower():
                    return False

        return True

    def _check_dual_ai_gate(self, cfg: dict, ctx: dict) -> bool:
        """双AI闸门熔断：每件产出/学习必须通过独立模型验证。

        核心设计原则 (2026-06-03):
          双AI制度彻底落实 —— 从统计学上降低幻觉概率。
          所有产出和学习必须过独立验证闸门。

        context 应包含:
            output_type: str  — "cad_design" | "knowledge" | "pattern" | "deliverable"
            gate_name: str    — 设计/知识名称
            gate_result: str  — qwen_gate 返回的 verdict: "pass"|"fail"|"flag"
            gate_status: str  — "ok" | "unavailable" (API 不可用时)

        检查逻辑:
            1. gate_status="unavailable" → 默认放行 (不因基础设施故障阻塞工作)
            2. gate_result="pass" → 放行
            3. gate_result="fail" → 拦截
            4. gate_result="flag" → 根据阈值配置决定
            5. 缺少必要字段 → 放行 (兼容旧调用)
        """
        gate_result = ctx.get("gate_result", "")
        gate_status = ctx.get("gate_status", "ok")

        # API 不可用时不阻塞
        if gate_status == "unavailable":
            return True

        # fail → 拦截
        if gate_result == "fail":
            return False

        # flag → 看配置: 默认放行 (因为 flag 只是可疑不致命)
        if gate_result == "flag":
            flag_action = cfg.get("flag_action", "allow")
            if flag_action == "block":
                return False
            return True

        # pass 或未知 → 放行
        return True

    # ── 内部辅助 ─────────────────────────────────────────────

    def _get_fuse_config(self, fuse_type: str, default: dict | None = None) -> dict:
        if default is None:
            default = {"enabled": False}
        config = self._load_config()
        return config.get("fuses", {}).get(fuse_type, default)

    def _load_config(self) -> dict:
        try:
            if CONFIG_PATH.exists():
                return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"version": 0, "fuses": {}}

    def _load_state(self) -> dict:
        try:
            if STATE_PATH.exists():
                return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_state(self) -> None:
        try:
            self._state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _emergency_log(self, fuse_type: str, event: str) -> None:
        """应急日志——熔断板自己出问题时写纯文本"""
        try:
            log_dir = LOG_PATH.parent
            log_dir.mkdir(parents=True, exist_ok=True)
            emerg_path = log_dir / "fuse_emergency.log"
            line = f"[{datetime.now(timezone.utc).isoformat()}] {fuse_type} {event}\n"
            with open(emerg_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


# ─── FuseBoard 门面类（用户实际使用的接口） ──────────────────

class FuseBoard:
    """熔断板门面 — 持有后端，提供统一接口。

    用户代码用这个，不直接操作后端。
    """

    def __init__(self, backend: FuseBackend) -> None:
        self._backend = backend

    def check(self, fuse_type: str, context: dict | None = None) -> bool:
        """查询：这个操作允许吗？"""
        return self._backend.check(fuse_type, context)

    def trip(self, fuse_type: str, reason: str,
             context: dict | None = None) -> dict:
        """拉闸：记录熔断事件。"""
        return self._backend.trip(fuse_type, reason, context)

    def status(self) -> dict:
        """查询所有熔断器当前状态。"""
        return self._backend.status()

    def reset(self, fuse_type: str | None = None) -> bool:
        """重置熔断器。人工操作。"""
        return self._backend.reset(fuse_type)

    @property
    def backend(self) -> FuseBackend:
        return self._backend


# ─── module-level 单例 ────────────────────────────────────────

_fuse_board_instance: FuseBoard | None = None


def get_board() -> FuseBoard:
    """获取熔断板单例。首次调用时自动初始化。"""
    global _fuse_board_instance
    if _fuse_board_instance is None:
        _fuse_board_instance = FuseBoard(SoftwareFuseBackend())
    return _fuse_board_instance


# 主入口：
#   from scripts.core_engine.fuse_board import fuse_board
fuse_board: FuseBoard = get_board()


# ─── 便利函数（操作 fuse_board 单例） ────────────────────────

def mark_checkpoint_done() -> None:
    """标记检查点已保存。CHECKPOINT_REQUIRED 熔断器用。"""
    backend = fuse_board.backend
    if isinstance(backend, SoftwareFuseBackend):
        ft = FuseType.CHECKPOINT_REQUIRED.value
        if ft not in backend._state:
            backend._state[ft] = {}
        backend._state[ft]["last_checkpoint_time"] = (
            datetime.now(timezone.utc).isoformat()
        )
        backend._save_state()


def record_token_usage(tokens: int) -> None:
    """记录本次调用的 token 消耗量。"""
    backend = fuse_board.backend
    if isinstance(backend, SoftwareFuseBackend):
        ft = FuseType.TOKEN_BUDGET.value
        if ft not in backend._state:
            backend._state[ft] = {"session_used": 0, "daily_used": 0}
        backend._state[ft]["session_used"] = (
            backend._state[ft].get("session_used", 0) + tokens
        )
        backend._state[ft]["daily_used"] = (
            backend._state[ft].get("daily_used", 0) + tokens
        )
        backend._save_state()


def get_protected_patterns() -> list[str]:
    """返回受保护路径模式列表。"""
    backend = fuse_board.backend
    if isinstance(backend, SoftwareFuseBackend):
        return backend.get_protected_patterns()
    return []


# ─── 自检测试 ─────────────────────────────────────────────────

def self_test() -> bool:
    """运行熔断板自检。返回 True=全部通过。"""
    print("=" * 52)
    print("  熔断板自检 (Fuse Board Self-Test)")
    print("=" * 52)

    board = get_board()
    passed = 0
    failed = 0

    tests = [
        ("[1]  状态查询: 9个熔断器",
         lambda: (len(board.status()["fuses"]) == 9, "预期9个")),  # 2026-06-05 修复: 8→9 (新增NUMERIC_COMPUTATION/SELF_EVALUATION_PROHIBITED/DUAL_AI_GATE)

        ("[2]  写保护-核心文件: 拒绝",
         lambda: (board.check("WRITE_PROTECT", {"path": "scripts/core-engine/fuse_board.py"}) is False,
                  "预期拒绝")),

        ("[3]  写保护-普通文件: 放行",
         lambda: (board.check("WRITE_PROTECT", {"path": "data/todos/test.txt"}) is True,
                  "预期放行")),

        ("[4]  递归上限 depth=7: 拒绝",
         lambda: (board.check("RECURSION_LIMIT", {"depth": 7}) is False,
                  "深度7>5，预期拒绝")),

        ("[5]  递归上限 depth=3: 放行",
         lambda: (board.check("RECURSION_LIMIT", {"depth": 3}) is True,
                  "深度3<=5，预期放行")),

        ("[6]  Token预算 100万: 拒绝",
         lambda: (board.check("TOKEN_BUDGET", {"tokens": 1_000_000}) is False,
                  "100万>会话50万，预期拒绝")),

        ("[7]  Token预算 1000: 放行",
         lambda: (board.check("TOKEN_BUDGET", {"tokens": 1000}) is True,
                  "1000<会话50万，预期放行")),

        ("[8]  Proxy-内容修改: 拒绝",
         lambda: (board.check("PROXY_PURITY", {"operation": "content_modification"}) is False,
                  "内容修改在禁止列表，预期拒绝")),

        ("[9]  Proxy-允许操作: 放行",
         lambda: (board.check("PROXY_PURITY", {"operation": "delete_field:thinking"}) is True,
                  "删除thinking在允许列表，预期放行")),

        ("[10] 数值计算-注册轮子: 放行",
         lambda: (board.check("NUMERIC_COMPUTATION",
                              {"computation": "trust_score",
                               "wheel": "scripts/safety/audit_gate.py",
                               "value": 0.75}) is True,
                  "trust_score 在注册列表，预期放行")),

        ("[11] 数值计算-未注册: 拒绝",
         lambda: (board.check("NUMERIC_COMPUTATION",
                              {"computation": "inline_confidence",
                               "wheel": "scripts/core-engine/unknown.py"}) is False,
                  "inline_confidence 不在注册列表，预期拒绝")),

        ("[12] 数值计算-无轮子路径: 拒绝",
         lambda: (board.check("NUMERIC_COMPUTATION",
                              {"computation": "trust_score"}) is False,
                  "有计算名但无轮子路径，预期拒绝")),

        ("[13] 数值计算-无计算名(旧代码兼容): 放行",
         lambda: (board.check("NUMERIC_COMPUTATION", {}) is True,
                  "无计算名时兼容放行，预期放行")),

        ("[14] 数值计算-轮子路径不匹配: 拒绝",
         lambda: (board.check("NUMERIC_COMPUTATION",
                              {"computation": "trust_score",
                               "wheel": "scripts/core-engine/unrelated.py"}) is False,
                  "路径不匹配，预期拒绝")),

        ("[15] 自我评价-评价性输出: 拒绝",
         lambda: (board.check("SELF_EVALUATION_PROHIBITED",
                              {"output_type": "verdict",
                               "content_hint": "验证通过"}) is False,
                  "verdict + 验证通过，预期拒绝")),

        ("[16] 自我评价-纯交付物: 放行",
         lambda: (board.check("SELF_EVALUATION_PROHIBITED",
                              {"output_type": "deliverable",
                               "content_hint": "设计思路和参数表"}) is True,
                  "纯交付物，预期放行")),

        ("[17] 自我评价-关键词拦截: 拒绝",
         lambda: (board.check("SELF_EVALUATION_PROHIBITED",
                              {"output_type": "deliverable",
                               "content_hint": "这个方案确认正确"}) is False,
                  "含确认正确关键词，预期拒绝")),
    ]

    for label, test_fn in tests:
        try:
            ok, msg = test_fn()
            if ok:
                print(f"  {label}: [OK]")
                passed += 1
            else:
                print(f"  {label}: [FAIL] {msg}")
                failed += 1
        except Exception as e:
            print(f"  {label}: [FAIL] 异常: {e}")
            failed += 1

    # 熔断事件记录测试（单独，因为 trip 会改状态）
    print()
    try:
        event = board.trip("RECURSION_LIMIT", "测试: 模拟递归溢出", {"depth": 10})
        assert "event_id" in event
        assert event["fuse_type"] == "RECURSION_LIMIT"
        s = board.status()
        assert s["fuses"]["RECURSION_LIMIT"]["trip_count"] > 0
        print(f"  [18] 熔断事件记录: [OK]  event_id={event['event_id']}")
        passed += 1
    except Exception as e:
        print(f"  [18] 熔断事件记录: [FAIL] {e}")
        failed += 1

    # 重置测试
    try:
        before = board.status()["fuses"]["RECURSION_LIMIT"]["trip_count"]
        ok = board.reset("RECURSION_LIMIT")
        after = board.status()["fuses"]["RECURSION_LIMIT"]["trip_count"]
        assert ok and after == 0, f"重置前={before}, 重置后={after}"
        print(f"  [19] 重置熔断器: [OK]")
        passed += 1
    except Exception as e:
        print(f"  [19] 重置熔断器: [FAIL] {e}")
        failed += 1

    # 未知熔断器类型容错
    try:
        board.check("NOT_A_FUSE", {})
        print(f"  [20] 未知类型(容错放行): [OK]")
        passed += 1
    except Exception as e:
        print(f"  [20] 未知类型(容错放行): [FAIL] {e}")
        failed += 1

    print(f"\n  结果: {passed}/{passed + failed} 通过, {failed} 失败")
    print("=" * 52)

    return failed == 0


# ─── 入口 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(0 if self_test() else 1)
    elif "--status" in sys.argv:
        import json as _json
        print(_json.dumps(get_board().status(), ensure_ascii=False, indent=2))
    else:
        print("熔断板已加载: FuseBoard(SoftwareFuseBackend)")
        print(f"  配置: {CONFIG_PATH}")
        print(f"  日志: {LOG_PATH}")
        print(f"  状态: {STATE_PATH}")
        print(f"  9个熔断器就绪")
        print(f"  --self-test  运行自检")
        print(f"  --status     查看状态")
