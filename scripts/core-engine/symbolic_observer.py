#!/usr/bin/env python3
"""
Symbolic Observer — event-to-symbol mapping for cognitive loop monitoring.

Maps discrete events (tool calls, step transitions, gate verdicts) to
symbol sequences over a finite alphabet.  Computes entropy rates and
stability metrics that reveal whether the system is converging or diverging.

Core concept: raw event streams carry 50K+ tokens of context.  By mapping
them to a small finite alphabet and tracking symbol frequencies, we can
detect behavioral regime changes (divergence / freezing / healthy oscillation)
in O(1) space.

Usage:
  python scripts/core-engine/symbolic_observer.py --observe <event_json>
  python scripts/core-engine/symbolic_observer.py --stats
  python scripts/core-engine/symbolic_observer.py --test
  python scripts/core-engine/symbolic_observer.py --history
"""

from __future__ import annotations

import json
import math
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── project root ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ─── symbol alphabet ─────────────────────────────────────────────

# Finite alphabet for cognitive domain events.
# Each domain has a small set of possible symbols.
DOMAIN_ALPHABET: dict[str, list[str]] = {
    "dialogue": ["ASK", "INFO", "ACK", "CORRECT", "CONFUSED", "CONFIRM"],
    "cad":       ["CREATE", "MODIFY", "VALIDATE", "EXPORT", "ERROR"],
    "quant":     ["OPEN", "EVAL", "MUTATE", "CLOSE", "STOP"],
    "hook":      ["PASS", "BLOCK", "WARN", "TRIP", "TIMEOUT"],
    "window":    ["FOCUS", "BLUR", "RESIZE", "CLOSE", "CREATE"],
    "pic":       ["INIT", "STEP", "CRASH", "CONVERGE", "ANALYZE"],
    "general":   ["CYCLE_START", "CYCLE_END", "ERROR", "RECOVER", "CHECKPOINT"],
}

# Default domain when no mapping matches
DEFAULT_DOMAIN = "general"
DEFAULT_SYMBOL = "UNKNOWN"


# ─── event-to-symbol mapper ──────────────────────────────────────

def observe(event: dict) -> str:
    """Map a discrete event dict to its domain symbol.

    The event dict should have at minimum a "type" or "domain" key.
    Additional keys ("action", "status", "step") improve resolution.

    Returns a dot-joined symbol string: "<domain>.<symbol>"
    Example:  "cad.CREATE"  or  "general.ERROR"

    Args:
        event: Event dict with keys like type, domain, action, status.

    Returns:
        Symbol string in the form "domain.symbol".
    """
    event_type = str(event.get("type", "")).lower()
    event_domain = str(event.get("domain", "")).lower()
    event_action = str(event.get("action", "")).upper()
    event_status = str(event.get("status", "")).upper()

    # Determine domain
    domain = DEFAULT_DOMAIN
    for d in DOMAIN_ALPHABET:
        if d in event_type or d in event_domain:
            domain = d
            break

    # Map within domain
    symbols = DOMAIN_ALPHABET.get(domain, DOMAIN_ALPHABET[DEFAULT_DOMAIN])

    # Try to match action or status to a known symbol
    for sym in symbols:
        if sym in event_action or sym in event_status:
            return f"{domain}.{sym}"

    # Fallback: use first character of action or a default
    if event_action and event_action in symbols:
        return f"{domain}.{event_action}"
    if event_status and event_status in symbols:
        return f"{domain}.{event_status}"

    # Last resort
    return f"{domain}.{DEFAULT_SYMBOL}"


# ─── entropy computation ─────────────────────────────────────────

def compute_entropy(symbols: list[str]) -> float:
    """Compute Shannon entropy over a sequence of symbols.

    H = -sum(p_i * log2(p_i))  where p_i is the frequency of symbol i.

    Higher entropy → more uniform distribution → potential divergence.
    Lower entropy → concentrated distribution → potential freezing.
    Healthy systems typically operate in the middle range.

    Args:
        symbols: List of observed symbol strings.

    Returns:
        Entropy value in bits (0.0 to log2(N) where N = unique symbols).
    """
    if not symbols:
        return 0.0

    n = len(symbols)
    counter = Counter(symbols)
    entropy = 0.0
    for count in counter.values():
        p = count / n
        if p > 0:
            entropy -= p * math.log2(p)
    return round(entropy, 4)


# ─── stability check ─────────────────────────────────────────────

def check_stability(symbols: list[str]) -> str:
    """Evaluate stability of a symbol sequence.

    Classification rules (heuristic, empirically tuned):
      - "diverging": entropy > 2.5 (near-uniform across large alphabet)
      - "critical":  entropy > 2.0 (elevated, watch closely)
      - "warn":      entropy > 1.5 (above normal, needs attention)
      - "frozen":    entropy < 0.3 (only one symbol, possible stall)
      - "ok":        otherwise (healthy oscillation)

    Also checks the most recent symbols for trend direction:
      - If the last 5 symbols are all the same → "frozen"
      - If the last 10 symbols have > 7 unique → "warn" or higher

    Args:
        symbols: Chronological list of observed symbols.

    Returns:
        One of: "ok", "warn", "critical", "diverging", "frozen"
    """
    if not symbols:
        return "ok"

    entropy = compute_entropy(symbols)
    n_unique = len(set(symbols))

    # Frozen detection: very low entropy or recent repetition
    if entropy < 0.3 or n_unique == 1:
        return "frozen"

    # Check trailing window
    recent_10 = symbols[-10:] if len(symbols) >= 10 else symbols
    recent_unique = len(set(recent_10))

    if recent_unique == 1 and len(recent_10) >= 5:
        return "frozen"  # trailing stall

    # Divergence / critical based on entropy thresholds
    if entropy > 2.5 and n_unique >= 6:
        return "diverging"
    if entropy > 2.0:
        return "critical"
    if entropy > 1.5 or recent_unique > 7:
        return "warn"

    return "ok"


def compute_domain_entropies(symbols: list[str]) -> dict[str, float]:
    """Compute per-domain entropy from a mixed sequence of symbols.

    Symbols are expected in "domain.symbol" format.
    Returns a dict mapping domain name to its entropy value.
    """
    domain_symbols: dict[str, list[str]] = {}
    for s in symbols:
        domain = s.split(".")[0] if "." in s else DEFAULT_DOMAIN
        domain_symbols.setdefault(domain, []).append(s)

    return {
        domain: compute_entropy(seq)
        for domain, seq in sorted(domain_symbols.items())
    }


# ─── observer state (in-memory ring buffer) ──────────────────────

class SymbolHistory:
    """In-memory ring buffer for symbol observation history.

    Persists to a JSONL file so observations survive process restarts.
    """

    def __init__(self, max_size: int = 200, persist: bool = True):
        self._buffer: deque[str] = deque(maxlen=max_size)
        self._max_size = max_size
        self._persist = persist
        self._log_path = PROJECT_ROOT / "data" / "symbolic_dynamics" / "observer_history.jsonl"

    def record(self, symbol: str) -> None:
        """Append a symbol to the buffer and optionally persist."""
        self._buffer.append(symbol)
        if self._persist:
            self._write_line(symbol)

    def history(self) -> list[str]:
        """Return the full history as a list (oldest first)."""
        return list(self._buffer)

    def recent(self, n: int = 20) -> list[str]:
        """Return the most recent n symbols."""
        items = list(self._buffer)
        return items[-n:] if len(items) >= n else items

    def load(self) -> int:
        """Load history from the persisted log file.  Returns count loaded."""
        if not self._log_path.exists():
            return 0
        count = 0
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                sym = entry.get("symbol", "")
                if sym:
                    self._buffer.append(sym)
                    count += 1
            except json.JSONDecodeError:
                continue
        return count

    def _write_line(self, symbol: str) -> None:
        """Write a single observation line to the JSONL log."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def __len__(self) -> int:
        return len(self._buffer)


# Module-level singleton
_history = SymbolHistory()


# ─── self-test ───────────────────────────────────────────────────

def self_test() -> bool:
    """Run internal self-tests.  Returns True if all pass."""
    import tempfile

    passed = 0
    total = 0

    # Test 1: observe basic event
    total += 1
    sym = observe({"type": "cad", "action": "CREATE"})
    if sym == "cad.CREATE":
        passed += 1
    else:
        print(f"  FAIL: observe(cad.CREATE) → {sym}", file=sys.stderr)

    # Test 2: observe with status
    total += 1
    sym = observe({"domain": "hook", "status": "TRIP"})
    if sym == "hook.TRIP":
        passed += 1
    else:
        print(f"  FAIL: observe(hook.TRIP) → {sym}", file=sys.stderr)

    # Test 3: observe unknown event
    total += 1
    sym = observe({"type": "bogus"})
    if sym == "general.UNKNOWN":
        passed += 1
    else:
        print(f"  FAIL: observe(bogus) → {sym}", file=sys.stderr)

    # Test 4: compute_entropy uniform distribution
    total += 1
    h = compute_entropy(["A", "B", "C", "D"])
    if abs(h - 2.0) < 0.01:  # 4 equally likely symbols
        passed += 1
    else:
        print(f"  FAIL: compute_entropy([A,B,C,D]) → {h} (expected ~2.0)", file=sys.stderr)

    # Test 5: compute_entropy single symbol
    total += 1
    h = compute_entropy(["X", "X", "X"])
    if abs(h - 0.0) < 0.001:
        passed += 1
    else:
        print(f"  FAIL: compute_entropy([X,X,X]) → {h} (expected 0.0)", file=sys.stderr)

    # Test 6: compute_entropy empty
    total += 1
    h = compute_entropy([])
    if h == 0.0:
        passed += 1
    else:
        print(f"  FAIL: compute_entropy([]) → {h}", file=sys.stderr)

    # Test 7: check_stability frozen
    total += 1
    status = check_stability(["cad.CREATE"] * 20)
    if status == "frozen":
        passed += 1
    else:
        print(f"  FAIL: check_stability(frozen) → {status}", file=sys.stderr)

    # Test 8: check_stability diverging
    total += 1
    status = check_stability([
        "dialogue.ASK", "cad.CREATE", "quant.OPEN", "hook.TRIP",
        "window.FOCUS", "pic.INIT", "dialogue.INFO", "cad.MODIFY",
        "quant.EVAL", "hook.BLOCK", "window.BLUR", "pic.STEP",
        "dialogue.ACK", "cad.VALIDATE", "quant.MUTATE", "hook.WARN",
    ])
    if status in ("diverging", "critical", "warn"):
        passed += 1
    else:
        print(f"  FAIL: check_stability(high entropy) → {status}", file=sys.stderr)

    # Test 9: check_stability ok (few unique symbols, low entropy, moderate repetition)
    total += 1
    status = check_stability([
        "dialogue.ASK", "dialogue.INFO", "dialogue.ASK", "dialogue.INFO",
        "dialogue.ASK", "dialogue.INFO", "dialogue.ASK", "dialogue.INFO",
    ])
    if status == "ok":
        passed += 1
    else:
        print(f"  FAIL: check_stability(ok) → {status}", file=sys.stderr)

    # Test 10: compute_domain_entropies
    total += 1
    de = compute_domain_entropies(["cad.CREATE", "cad.MODIFY", "hook.TRIP", "hook.PASS"])
    if "cad" in de and "hook" in de:
        passed += 1
    else:
        print(f"  FAIL: compute_domain_entropies → {de}", file=sys.stderr)

    # Report
    print(f"\n  Self-test results: {passed}/{total} passed", file=sys.stderr)
    if passed == total:
        print("  ALL TESTS PASSED", file=sys.stderr)
        return True
    else:
        print(f"  {total - passed} test(s) FAILED", file=sys.stderr)
        return False


# ─── CLI ─────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Symbolic Observer — event-to-symbol mapping and entropy monitoring.",
    )
    parser.add_argument(
        "--observe",
        type=str,
        default=None,
        help="Observe a JSON event string and print its symbol.",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Read events from a JSONL file and compute aggregate stats.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print stability stats for the current history buffer.",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Print the full symbol history.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run internal self-tests.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear the in-memory history buffer.",
    )
    args = parser.parse_args()

    # Load persisted history
    _history.load()

    # Self-test mode
    if args.test:
        ok = self_test()
        sys.exit(0 if ok else 1)

    # Clear history
    if args.clear:
        _history._buffer.clear()
        print("History cleared.", file=sys.stderr)
        sys.exit(0)

    # Observe a single event
    if args.observe:
        try:
            event = json.loads(args.observe)
        except json.JSONDecodeError as e:
            print(f"ERROR: invalid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        symbol = observe(event)
        _history.record(symbol)
        print(symbol)
        sys.exit(0)

    # Process a JSONL file
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"ERROR: file not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        symbols: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                symbols.append(observe(event))
            except json.JSONDecodeError:
                continue

        entropy = compute_entropy(symbols)
        stability = check_stability(symbols)
        domain_ent = compute_domain_entropies(symbols)

        print(f"  File: {args.file}")
        print(f"  Events processed: {len(symbols)}")
        print(f"  Unique symbols: {len(set(symbols))}")
        print(f"  Shannon entropy: {entropy:.4f} bits")
        print(f"  Stability: {stability}")
        print(f"  Per-domain entropy:")
        for domain, h in domain_ent.items():
            print(f"    {domain}: {h:.4f}")
        sys.exit(0)

    # Stats mode
    if args.stats:
        symbols = _history.history()
        entropy = compute_entropy(symbols)
        stability = check_stability(symbols)
        domain_ent = compute_domain_entropies(symbols)

        print(f"  History length: {len(symbols)}")
        print(f"  Unique symbols: {len(set(symbols))}")
        print(f"  Shannon entropy: {entropy:.4f} bits")
        print(f"  Stability: {stability}")
        if domain_ent:
            print(f"  Per-domain entropy:")
            for domain, h in domain_ent.items():
                print(f"    {domain}: {h:.4f}")
        sys.exit(0)

    # History mode
    if args.history:
        symbols = _history.history()
        if not symbols:
            print("No history recorded.")
        else:
            for i, sym in enumerate(symbols):
                print(f"  [{i:04d}] {sym}")
        sys.exit(0)

    # No action specified
    parser.print_help()
    sys.exit(0)


if __name__ == "__main__":
    main()
