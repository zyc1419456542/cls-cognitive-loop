#!/usr/bin/env python3
"""
CLS Cognitive Loop — Live Demonstration
=======================================
Runs the complete 6-step cognitive cycle on a sample task.
Shows each step's input, processing, and output with clear delimiters.

Usage:
  python demo.py              # Run with built-in sample task
  python demo.py --task "..." # Run with custom task description
  python demo.py --quick      # Fast mode: skip detailed output
  python demo.py --benchmark  # Run 10 cycles and report timing stats
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import time
from pathlib import Path

# Allow imports from scripts/ subdirectory
_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Import from core-engine (directory has a hyphen, requires importlib)
def _import_module(module_name: str, file_path: Path):
    """Import a Python module by file path (supports hyphenated directory names)."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod

_core_engine = _SCRIPTS_DIR / "core-engine"
_cognitive_core_loop = _import_module(
    "cognitive_core_loop",
    _core_engine / "cognitive_core_loop.py",
)
_premise_check = _import_module(
    "premise_check",
    _core_engine / "premise_check.py",
)
_symbolic_observer = _import_module(
    "symbolic_observer",
    _core_engine / "symbolic_observer.py",
)

CognitiveLoop = _cognitive_core_loop.CognitiveLoop
LoopState = _cognitive_core_loop.LoopState
check_file_exists = _premise_check.check_file_exists
check_state_fresh = _premise_check.check_state_fresh
verify_preconditions = _premise_check.verify_preconditions
observe = _symbolic_observer.observe
check_stability = _symbolic_observer.check_stability
compute_entropy = _symbolic_observer.compute_entropy

# ─── project root ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent

# ─── terminal colors (cross-platform ANSI) ───────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"

def _c(color: str, text: str) -> str:
    """Wrap text in ANSI color codes.  No-op if output is not a TTY."""
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{RESET}"

# ─── pre-flight checks ──────────────────────────────────────────

def run_preflight(verbose: bool = True) -> dict:
    """Run all pre-flight checks before the cognitive loop.

    Returns a dict with keys: ok, checks, errors.
    """
    checks: list[dict] = []
    errors: list[str] = []

    # Check 1: project state files exist
    data_dir = PROJECT_ROOT / "data"
    if data_dir.exists():
        checks.append({"name": "data directory", "status": "PASS", "detail": str(data_dir)})
    else:
        errors.append(f"data directory missing: {data_dir}")
        checks.append({"name": "data directory", "status": "FAIL", "detail": str(data_dir)})

    # Check 2: key config files
    config_files = [
        "data/safety-configs/fuses_config.json",
        "data/safety-configs/compact_health_config.json",
    ]
    for cf in config_files:
        if check_file_exists(cf):
            checks.append({"name": cf, "status": "PASS"})
        else:
            checks.append({"name": cf, "status": "WARN", "detail": "file not found"})

    # Check 3: core scripts exist
    script_files = [
        "scripts/core-engine/cognitive_core_loop.py",
        "scripts/core-engine/fuse_board.py",
        "scripts/core-engine/premise_check.py",
        "scripts/core-engine/symbolic_observer.py",
    ]
    script_result = verify_preconditions(script_files)
    for f in script_result["verified"]:
        checks.append({"name": f, "status": "PASS"})
    for f in script_result["missing"]:
        checks.append({"name": f, "status": "FAIL", "detail": "missing"})
        errors.append(f"Missing: {f}")

    # Synthesize
    all_pass = all(c["status"] == "PASS" for c in checks) and len(errors) == 0

    if verbose:
        print(f"\n  {_c(BOLD, 'Pre-flight Checks')}")
        print(f"  {'─' * 50}")
        for c in checks:
            icon = _c(GREEN, "[PASS]") if c["status"] == "PASS" else (_c(YELLOW, "[WARN]") if c["status"] == "WARN" else _c(RED, "[FAIL]"))
            detail = f"  ({c['detail']})" if "detail" in c else ""
            print(f"  {icon} {c['name']}{detail}")
        if not all_pass:
            print(f"\n  {_c(RED, 'Pre-flight FAILED')} — {len(errors)} error(s)")
        else:
            print(f"\n  {_c(GREEN, 'Pre-flight PASSED')} — all systems nominal")

    return {"ok": all_pass, "checks": checks, "errors": errors}


# ─── built-in sample task ────────────────────────────────────────

SAMPLE_TASK = (
    "Validate safety configuration integrity: "
    "check fuses_config.json freshness, verify gate scripts exist, "
    "associate findings with system-health domain, "
    "abstract reusable health-check pattern, "
    "persist checkpoint, update trajectory."
)

# ─── step display helpers ────────────────────────────────────────

STEP_NAMES = {
    1: "Situational Awareness",
    2: "Task Execution",
    3: "Associative Learning",
    4: "Abstract Generalization",
    5: "Context Persistence",
    6: "Trajectory Update",
}

BREATH_MAP = {
    1: "SYSTOLE  ",  # contraction — focus
    2: "SYSTOLE  ",
    3: "DIASTOLE ",  # expansion — connect
    4: "DIASTOLE ",
    5: "SYSTOLE  ",
    6: "DIASTOLE ",
}


def display_step_header(step: int, state: LoopState) -> None:
    """Print a formatted header for a cognitive loop step."""
    name = STEP_NAMES.get(step, f"Step {step}")
    breath = BREATH_MAP.get(step, "         ")
    print(f"\n  {_c(BOLD, '╔' + '═' * 58 + '╗')}")
    print(f"  ║ {_c(CYAN, f'Step {step}: {name}')}  [{breath}]" + " " * (24 - len(name)) + "║")
    print(f"  {_c(BOLD, '╚' + '═' * 58 + '╝')}")


def display_step_summary(step: int, state: LoopState, elapsed: float) -> None:
    """Print a compact summary after a step completes."""
    details = ""
    if step == 1:
        details = f"freshness={state.meta.get('data_freshness_seconds', 'N/A')}s"
    elif step == 2:
        details = f"errors={len(state.errors)}, complexity={state.meta.get('complexity', '?')}"
    elif step == 3:
        details = f"{len(state.associations)} associations, {len(state.knowledge_hits)} hits"
    elif step == 4:
        details = f"{len(state.abstractions)} abstractions"
    elif step == 5:
        details = f"checkpoint={state.meta.get('last_checkpoint', '?')}"
    elif step == 6:
        details = f"stability={state.stability_status}, delta_m={state.mass - 1.0:.3f}"

    print(f"  {_c(DIM, '└─')} {details}  {_c(DIM, f'({elapsed:.3f}s)')}")


# ─── symbolic observer integration ───────────────────────────────

def run_symbolic_snapshot(loop: CognitiveLoop) -> dict:
    """Take a symbolic snapshot at the end of a cycle.

    Maps the loop's internal state markers to symbolic observations,
    then computes entropy and stability.
    """
    events = [
        {"domain": "general", "action": "CYCLE_START"},
    ]
    # Map each step to a symbolic event
    step_actions = {
        1: "INFO", 2: "ACK", 3: "ACK", 4: "ACK", 5: "CHECKPOINT", 6: "CYCLE_END",
    }
    for step in range(1, 7):
        action = step_actions.get(step, "UNKNOWN")
        events.append({"domain": "general", "action": action})

    if loop.state.errors:
        events.append({"domain": "general", "action": "ERROR"})

    symbols = [observe(e) for e in events]
    entropy = compute_entropy(symbols)
    stability = check_stability(symbols)

    return {
        "symbols": symbols,
        "entropy": entropy,
        "stability": stability,
        "unique_count": len(set(symbols)),
    }


# ─── main demo ───────────────────────────────────────────────────

def run_demo(task: str, quick: bool = False) -> None:
    """Run the full cognitive loop demonstration."""

    print(f"\n  {_c(BOLD, 'CLS Cognitive Loop — Live Demonstration')}")
    print(f"  {'═' * 50}")
    print(f"  Project root: {PROJECT_ROOT}")
    print(f"  Task: {task[:100]}...")

    # ── pre-flight ────────────────────────────────────────────
    if not quick:
        preflight = run_preflight(verbose=True)
        if not preflight["ok"] and len(preflight["errors"]) > 0:
            print(f"\n  {_c(YELLOW, 'Warning:')} Some pre-flight checks failed. "
                  f"Proceeding with available resources.")

    # ── initialize loop ───────────────────────────────────────
    loop = CognitiveLoop()
    print(f"\n  {_c(DIM, 'Session ID:')} {loop.state.session_id}")

    # ── run 6 steps with display ──────────────────────────────
    total_start = time.time()

    # Step 1
    display_step_header(1, loop.state)
    t0 = time.time()
    loop._step_1_awareness(task)
    if not quick:
        print(f"  Scanned project metadata")
        print(f"  Data directory exists: {loop.state.meta.get('project_data_exists', False)}")
        if loop.state.meta.get("newest_data_file"):
            print(f"  Newest data file: {loop.state.meta['newest_data_file']}")
    display_step_summary(1, loop.state, time.time() - t0)

    # Step 2
    display_step_header(2, loop.state)
    t0 = time.time()
    loop._step_2_execute(task)
    if not quick:
        comp = loop.state.meta.get("complexity", "?")
        print(f"  Complexity: {comp}")
        print(f"  Output summary: {loop.state.output_summary}")
        if loop.state.errors:
            for e in loop.state.errors:
                print(f"  {_c(RED, '[ERROR]')} {e.get('type')}: {e.get('reason', '')}")
    display_step_summary(2, loop.state, time.time() - t0)

    # Step 3
    display_step_header(3, loop.state)
    t0 = time.time()
    loop._step_3_associate()
    if not quick:
        for a in loop.state.associations[:5]:
            mark = " *" if a["strength"] >= 0.7 else "  "
            print(f"  {mark} {a['term']} → {a['domain']} ({a['strength']:.2f})")
        if len(loop.state.associations) > 5:
            print(f"  ... and {len(loop.state.associations) - 5} more")
    display_step_summary(3, loop.state, time.time() - t0)

    # Step 4
    display_step_header(4, loop.state)
    t0 = time.time()
    loop._step_4_abstract()
    if not quick:
        for ab in loop.state.abstractions:
            print(f"  {_c(GREEN, '◆')} {ab}")
    display_step_summary(4, loop.state, time.time() - t0)

    # Step 5
    display_step_header(5, loop.state)
    t0 = time.time()
    loop._step_5_persist()
    if not quick:
        ckpt = loop.state.meta.get("last_checkpoint", "unknown")
        size = loop.state.meta.get("checkpoint_size_bytes", 0)
        print(f"  Checkpoint written: {ckpt} ({size} bytes)")
    display_step_summary(5, loop.state, time.time() - t0)

    # Step 6
    display_step_header(6, loop.state)
    t0 = time.time()
    loop._step_6_trajectory()
    if not quick:
        print(f"  Trajectory file: {loop.state.meta.get('trajectory_file', 'unknown')}")
        print(f"  Delta position: {loop.state.position:.4f}")
        print(f"  Delta mass:     {loop.state.mass - 1.0:.4f}")
        print(f"  Delta momentum: {loop.state.momentum:.4f}")
    display_step_summary(6, loop.state, time.time() - t0)

    total_elapsed = time.time() - total_start

    # ── symbolic snapshot ─────────────────────────────────────
    snap = run_symbolic_snapshot(loop)
    if not quick:
        print(f"\n  {_c(BOLD, 'Symbolic Observer Snapshot')}")
        print(f"  {'─' * 40}")
        print(f"  Symbols in cycle: {len(snap['symbols'])}")
        print(f"  Unique symbols:   {snap['unique_count']}")
        print(f"  Entropy:          {snap['entropy']:.4f} bits")
        print(f"  Stability:        {snap['stability']}")

    # ── final summary ─────────────────────────────────────────
    print(f"\n  {_c(BOLD, '╔' + '═' * 58 + '╗')}")
    print(f"  ║  {_c(CYAN, 'CYCLE COMPLETE')}" + " " * 42 + "║")
    print(f"  {_c(BOLD, '╠' + '═' * 58 + '╣')}")
    print(f"  ║  Session:     {loop.state.session_id:<30s} ║")
    print(f"  ║  Stability:   {loop.state.stability_status:<30s} ║")
    print(f"  ║  Errors:      {len(loop.state.errors):<30d} ║")
    print(f"  ║  Mass:        {loop.state.mass:<30.4f} ║")
    print(f"  ║  Momentum:    {loop.state.momentum:<30.4f} ║")
    print(f"  ║  Position:    {loop.state.position:<30.4f} ║")
    print(f"  ║  Elapsed:     {total_elapsed:<30.3f}s ║")
    print(f"  {_c(BOLD, '╚' + '═' * 58 + '╝')}")


# ─── benchmark mode ──────────────────────────────────────────────

def run_benchmark(task: str, cycles: int = 10) -> None:
    """Run multiple cognitive loop cycles and report timing statistics."""
    print(f"\n  {_c(BOLD, 'CLS Cognitive Loop — Benchmark')}")
    print(f"  {'═' * 50}")
    print(f"  Cycles: {cycles}")
    print(f"  Task: {task[:80]}...")
    print()

    timings: dict[str, list[float]] = {
        "total": [],
        "step_1": [],
        "step_2": [],
        "step_3": [],
        "step_4": [],
        "step_5": [],
        "step_6": [],
    }

    stability_counts = {"ok": 0, "warn": 0, "critical": 0, "diverging": 0, "frozen": 0}

    for i in range(cycles):
        loop = CognitiveLoop()

        t_start = time.time()
        loop.run(task)
        t_total = time.time() - t_start
        timings["total"].append(t_total)

        for step_key, step_num in [
            ("step_1", 1), ("step_2", 2), ("step_3", 3),
            ("step_4", 4), ("step_5", 5), ("step_6", 6),
        ]:
            timings[step_key].append(loop._step_timings.get(step_key, 0))

        stab = loop.state.stability_status
        stability_counts[stab] = stability_counts.get(stab, 0) + 1

        print(f"  [{i+1:2d}/{cycles}] {t_total:.4f}s  "
              f"mass={loop.state.mass:.3f}  stability={stab}  "
              f"errors={len(loop.state.errors)}")

    # Statistics
    print(f"\n  {_c(BOLD, 'Timing Statistics')} (seconds)")
    print(f"  {'─' * 50}")
    print(f"  {'Metric':<12} {'Min':>10} {'Max':>10} {'Mean':>10} {'Median':>10}")

    import statistics as st
    for label, values in timings.items():
        if values:
            print(f"  {label:<12} {min(values):>10.4f} {max(values):>10.4f} "
                  f"{st.mean(values):>10.4f} {st.median(values):>10.4f}")

    print(f"\n  {_c(BOLD, 'Stability Distribution')}")
    print(f"  {'─' * 30}")
    for status, count in sorted(stability_counts.items()):
        pct = count / cycles * 100
        bar = "█" * int(pct / 5)
        print(f"  {status:<12} {count:>3d} ({pct:5.1f}%) {bar}")

    # Overall assessment
    mean_total = st.mean(timings["total"])
    print(f"\n  Mean cycle time: {mean_total:.4f}s")
    total_errors = sum(
        stability_counts.get(s, 0)
        for s in ("warn", "critical", "diverging")
    )
    if total_errors > cycles * 0.2:
        print(f"  {_c(YELLOW, 'Note:')} {total_errors} cycles showed elevated instability")


# ─── CLI entry point ─────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="CLS Cognitive Loop — Live Demonstration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python demo.py                        # Default sample task
  python demo.py --task "analyze logs"  # Custom task
  python demo.py --quick                # Compact output
  python demo.py --benchmark            # 10-cycle timing benchmark
        """,
    )
    parser.add_argument(
        "--task",
        type=str,
        default=None,
        help="Custom task description (default: built-in sample).",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Fast mode: skip detailed per-step output.",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run multiple cycles and report timing statistics.",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=10,
        help="Number of cycles for --benchmark (default: 10).",
    )
    args = parser.parse_args()

    task = args.task or SAMPLE_TASK

    if args.benchmark:
        run_benchmark(task, args.cycles)
    else:
        run_demo(task, quick=args.quick)


if __name__ == "__main__":
    main()
