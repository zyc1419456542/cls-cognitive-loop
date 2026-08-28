"""
CLS Memory 单例模块 —— Mem0 封装 (fastembed + Ollama, 全本地).

用法:
    from cls_memory import cls_memory

    # 存 (认知循环 Step 6)
    cls_memory.add("法拉第<传感器>壳体改为7mm通体螺栓", user_id="cls", agent_id="cad_agent")

    # 查 (认知循环 Step 1)
    results = cls_memory.search("壳体螺栓设计", user_id="cls", top_k=5)
    for r in results: print(r["memory"], r["score"])
"""

import os, sys, json, logging, threading
from pathlib import Path
from typing import Optional

# ── Telemetry: 必须优先于 mem0 导入 ──────────────────────────────────
os.environ.setdefault("DISABLE_TELEMETRY", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")

logger = logging.getLogger("cls_memory")

# ── 配置 ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MEM0_DATA_DIR = PROJECT_ROOT / "data" / "mem0"

DEFAULT_CONFIG = {
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "path": str(MEM0_DATA_DIR / "qdrant"),
            "on_disk": True,
            "embedding_model_dims": 512,  # bge-small-zh-v1.5
        },
    },
    "history_store": {
        "provider": "sqlite",
        "config": {
            "path": str(MEM0_DATA_DIR / "history.db"),
        },
    },
    "embedder": {
        "provider": "fastembed",
        "config": {
            "model": "BAAI/bge-small-zh-v1.5",
        },
    },
    "llm": {
        "provider": "ollama",
        "config": {
            "model": "qwen2.5:3b",
            "ollama_base_url": "http://localhost:11434",
            "temperature": 0,
        },
    },
    "version": "v3_0",
}


class _ClsMemory:
    """Mem0 单例 —— 懒加载，线程安全。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._memory = None
                    inst._ready = False
                    cls._instance = inst
        return cls._instance

    # ── 初始化 ────────────────────────────────────────────────────────

    def _ensure(self):
        """懒加载 Mem0 Memory 实例。"""
        if self._ready:
            return True

        try:
            from mem0 import Memory
        except ImportError:
            logger.warning("mem0ai not installed. Run: pip install mem0ai")
            return False

        try:
            MEM0_DATA_DIR.mkdir(parents=True, exist_ok=True)
            self._memory = Memory.from_config(DEFAULT_CONFIG)
            self._ready = True
            logger.info(f"CLS Memory 初始化完成 [data: {MEM0_DATA_DIR}]")
            return True
        except Exception as e:
            logger.warning(f"CLS Memory 初始化失败: {e}")
            self._ready = False
            return False

    # ── 公开 API ──────────────────────────────────────────────────────

    def add(self, text: str, user_id: str = "cls",
            agent_id: Optional[str] = None,
            metadata: Optional[dict] = None,
            infer: bool = False) -> bool:
        """
        存入一条记忆。

        Args:
            text:     记忆内容 (纯文本)
            user_id:  用户/系统标识 (默认 "cls")
            agent_id: 来源 agent 名 (如 "cad_agent", "quant_agent")
            metadata: 附加元数据 dict (如 {"domain": "cad"})
            infer:    是否用 LLM 提取结构化事实 (默认 False)

        Returns:
            True=成功, False=失败
        """
        if not self._ensure():
            return False

        try:
            msg = {"role": "assistant", "content": text}
            kwargs = dict(
                messages=[msg],
                user_id=user_id,
                infer=infer,
            )
            if agent_id:
                kwargs["agent_id"] = agent_id
            if metadata:
                kwargs["metadata"] = metadata

            self._memory.add(**kwargs)
            return True
        except Exception as e:
            logger.warning(f"Memory add 失败: {e}")
            return False

    def search(self, query: str, user_id: str = "cls",
               top_k: int = 5, threshold: float = 0.0,
               filters: Optional[dict] = None,
               agent_id: Optional[str] = None) -> list:
        """
        搜索记忆。

        Args:
            query:     自然语言查询
            user_id:   用户/系统标识
            top_k:     返回条数 (默认 5)
            threshold: 最低相似度 (0-1, 0=不限制)
            filters:   额外过滤条件
            agent_id:  快捷过滤指定 agent

        Returns:
            [{memory: str, score: float, id: str, ...}, ...]
        """
        if not self._ensure():
            return []

        try:
            f = {"user_id": user_id}
            if agent_id:
                f["agent_id"] = agent_id
            if filters:
                f["AND"] = [f, filters] if "AND" not in filters else filters["AND"] + [f]

            params = dict(query=query, filters=f, top_k=top_k)
            if threshold > 0:
                params["threshold"] = threshold

            result = self._memory.search(**params)
            return result.get("results", [])
        except Exception as e:
            logger.warning(f"Memory search 失败: {e}")
            return []

    def get_recent(self, user_id: str = "cls",
                   top_k: int = 5) -> list:
        """
        取最近记忆 (无查询, 按时间倒序).
        注意: mem0 v3 API 无直接 get_recent, 用空查询近似.
        """
        if not self._ensure():
            return []

        try:
            result = self._memory.search(
                query="",
                filters={"user_id": user_id},
                top_k=top_k,
            )
            return result.get("results", [])
        except Exception as e:
            logger.warning(f"Memory get_recent 失败: {e}")
            return []

    def close(self):
        """释放资源。"""
        self._memory = None
        self._ready = False
        logger.info("CLS Memory 已关闭")

    @property
    def ready(self) -> bool:
        return self._ready


# ── 单例 ──────────────────────────────────────────────────────────────
cls_memory: _ClsMemory = _ClsMemory()


# ── CLI 入口 (调试用) ──────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    import argparse

    parser = argparse.ArgumentParser(description="CLS Memory 调试 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="存一条记忆")
    p_add.add_argument("text", type=str)
    p_add.add_argument("--agent", default=None)
    p_add.add_argument("--domain", default=None)
    p_add.add_argument("--infer", action="store_true")

    p_search = sub.add_parser("search", help="搜索记忆")
    p_search.add_argument("query", type=str)
    p_search.add_argument("--top-k", type=int, default=5)
    p_search.add_argument("--agent", default=None)
    p_search.add_argument("--threshold", type=float, default=0.0)

    p_recent = sub.add_parser("recent", help="最近记忆")
    p_recent.add_argument("--top-k", type=int, default=5)

    args = parser.parse_args()

    if args.cmd == "add":
        meta = {"domain": args.domain} if args.domain else None
        ok = cls_memory.add(args.text, agent_id=args.agent, metadata=meta, infer=args.infer)
        print("ok" if ok else "fail")

    elif args.cmd == "search":
        results = cls_memory.search(args.query, top_k=args.top_k,
                                    agent_id=args.agent,
                                    threshold=args.threshold)
        for r in results:
            rid = r.get("id", "?")[:8]
            print(f"  [{r['score']:.3f}] ({rid}) {r['memory'][:80]}")
        if not results:
            print("  (no results)")

    elif args.cmd == "recent":
        results = cls_memory.get_recent(top_k=args.top_k)
        for r in results:
            print(f"  [{r.get('score', 0):.3f}] {r['memory'][:80]}")
        if not results:
            print("  (no results)")
