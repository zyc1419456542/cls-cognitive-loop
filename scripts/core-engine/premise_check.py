#!/usr/bin/env python3
"""
Premise Check — verify preconditions before operations.

Three-layer fact anchoring system (Layer 1):
  1. File existence: check that referenced files actually exist
  2. Path resolution: resolve relative paths against project root
  3. State validation: check that prerequisite state files are fresh

The principle: never proceed with an operation whose preconditions are
unmet.  Every external assertion about system state must be independently
verified before dependent logic runs.

Usage:
  python scripts/core-engine/premise_check.py --file <path>
  python scripts/core-engine/premise_check.py --state <state_key>
  python scripts/core-engine/premise_check.py --test        # self-test
  python scripts/core-engine/premise_check.py --verify <file1> <file2> ...
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── project root ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ─── core functions ──────────────────────────────────────────────

def resolve_path(filepath: str | Path, root: Optional[Path] = None) -> Path:
    """Resolve a potentially relative path against the project root.

    Args:
        filepath: Absolute or relative path string.
        root: Base directory for resolution (defaults to PROJECT_ROOT).

    Returns:
        Resolved absolute Path object.
    """
    if root is None:
        root = PROJECT_ROOT
    p = Path(filepath)
    if p.is_absolute():
        return p.resolve()
    return (root / p).resolve()


def check_file_exists(filepath: str | Path) -> bool:
    """Check whether a file exists on disk.

    Resolves relative paths against PROJECT_ROOT first.

    Args:
        filepath: Path to check (absolute or relative to project root).

    Returns:
        True if the file exists and is a regular file.
    """
    p = resolve_path(filepath)
    return p.is_file()


def check_state_fresh(
    state_file: str | Path,
    max_age_seconds: int = 300,
) -> bool:
    """Check whether a state file exists and is recent enough.

    "Fresh" means the file's modification time is within max_age_seconds.

    Args:
        state_file: Path to the state file.
        max_age_seconds: Maximum acceptable age in seconds (default 300 = 5 min).

    Returns:
        True if the file exists and its mtime is within the threshold.
    """
    p = resolve_path(state_file)
    if not p.is_file():
        return False
    age = time.time() - p.stat().st_mtime
    return age <= max_age_seconds


def get_file_age_seconds(state_file: str | Path) -> Optional[float]:
    """Return the age of a file in seconds, or None if it does not exist."""
    p = resolve_path(state_file)
    if not p.is_file():
        return None
    return time.time() - p.stat().st_mtime


def verify_preconditions(required_files: list[str], root: Optional[Path] = None) -> dict:
    """Verify that a list of required files exist and are healthy.

    Args:
        required_files: List of file paths (relative or absolute).
        root: Optional project root override.

    Returns:
        A dict with keys:
          - ok (bool): True if all preconditions pass.
          - missing (list[str]): Files that do not exist.
          - stale (list[dict]): Files that exist but are older than threshold,
            each with "path" and "age_seconds".
          - verified (list[str]): Files that passed all checks.
    """
    result: dict = {
        "ok": True,
        "missing": [],
        "stale": [],
        "verified": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    STALE_THRESHOLD = 300  # 5 minutes default for general files

    for f in required_files:
        p = resolve_path(f, root)

        if not p.is_file():
            result["ok"] = False
            result["missing"].append(str(f))
            continue

        age = time.time() - p.stat().st_mtime
        if age > STALE_THRESHOLD:
            result["ok"] = False
            result["stale"].append({
                "path": str(f),
                "age_seconds": round(age, 1),
                "threshold_seconds": STALE_THRESHOLD,
            })
        else:
            result["verified"].append(str(f))

    return result


# ─── self-test ───────────────────────────────────────────────────

def self_test() -> bool:
    """Run internal self-tests.  Returns True if all pass."""
    import tempfile

    passed = 0
    total = 0

    # Test 1: resolve_path with relative path
    total += 1
    p = resolve_path("README.md")
    if p == PROJECT_ROOT / "README.md":
        passed += 1
    else:
        print(f"  FAIL: resolve_path('README.md') → {p}", file=sys.stderr)

    # Test 2: resolve_path with absolute path
    total += 1
    abs_path = str(PROJECT_ROOT / "LICENSE")
    p = resolve_path(abs_path)
    if p == PROJECT_ROOT / "LICENSE":
        passed += 1
    else:
        print(f"  FAIL: resolve_path(abs) → {p}", file=sys.stderr)

    # Test 3: check_file_exists for a known file
    total += 1
    if check_file_exists("LICENSE"):
        passed += 1
    else:
        print("  FAIL: check_file_exists('LICENSE') returned False", file=sys.stderr)

    # Test 4: check_file_exists for a nonexistent file
    total += 1
    if not check_file_exists("nonexistent_file_xyz.abc"):
        passed += 1
    else:
        print("  FAIL: check_file_exists('nonexistent_file_xyz.abc') returned True", file=sys.stderr)

    # Test 5: check_state_fresh with a temp file
    total += 1
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        tf.write(b'{"test": true}')
        tmp_path = tf.name
    try:
        fresh = check_state_fresh(tmp_path, max_age_seconds=3600)
        if fresh:
            passed += 1
        else:
            print("  FAIL: check_state_fresh returned False for just-created file", file=sys.stderr)

        # Test 6: stale threshold
        total += 1
        stale = check_state_fresh(tmp_path, max_age_seconds=0)
        if not stale:
            passed += 1
        else:
            print("  FAIL: check_state_fresh with max_age=0 returned True", file=sys.stderr)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Test 7: verify_preconditions with mixed files (use fresh temp file to avoid staleness)
    total += 1
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        tf.write(b'{"test": true}')
        temp_abs = tf.name
    try:
        result = verify_preconditions([temp_abs, "nonexistent_xyz.abc"], root=Path(temp_abs).parent)
        ok = (not result["ok"]
              and "nonexistent_xyz.abc" in result["missing"]
              and any(temp_abs in v for v in result["verified"]))
        if ok:
            passed += 1
        else:
            print(f"  FAIL: verify_preconditions returned unexpected: {result}", file=sys.stderr)
    finally:
        Path(temp_abs).unlink(missing_ok=True)

    # Test 8: get_file_age_seconds
    total += 1
    age = get_file_age_seconds("LICENSE")
    if age is not None and age >= 0:
        passed += 1
    else:
        print(f"  FAIL: get_file_age_seconds('LICENSE') → {age}", file=sys.stderr)

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
        description="Premise Check — verify preconditions before operations.",
    )
    parser.add_argument(
        "--file", "-f",
        type=str,
        default=None,
        help="Check if a single file exists.",
    )
    parser.add_argument(
        "--state", "-s",
        type=str,
        default=None,
        help="Check if a state file is fresh (default max age: 300s).",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=300,
        help="Maximum age in seconds for --state check (default: 300).",
    )
    parser.add_argument(
        "--verify", "-v",
        nargs="+",
        default=None,
        help="Verify a list of required files and report status.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run internal self-tests.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON (for machine consumption).",
    )
    args = parser.parse_args()

    # Self-test mode
    if args.test:
        ok = self_test()
        sys.exit(0 if ok else 1)

    # Single file existence check
    if args.file:
        exists = check_file_exists(args.file)
        if args.json:
            print(json.dumps({"path": args.file, "exists": exists}))
        else:
            status = "EXISTS" if exists else "MISSING"
            print(f"[{status}] {args.file}")
        sys.exit(0 if exists else 1)

    # State freshness check
    if args.state:
        fresh = check_state_fresh(args.state, max_age_seconds=args.max_age)
        age = get_file_age_seconds(args.state)
        if args.json:
            print(json.dumps({
                "path": args.state,
                "fresh": fresh,
                "age_seconds": round(age, 1) if age is not None else None,
                "max_age_seconds": args.max_age,
            }))
        else:
            if age is None:
                print(f"[MISSING] {args.state}")
            elif fresh:
                print(f"[FRESH] {args.state} (age: {age:.1f}s, threshold: {args.max_age}s)")
            else:
                print(f"[STALE] {args.state} (age: {age:.1f}s, threshold: {args.max_age}s)")
        sys.exit(0 if fresh else 1)

    # Bulk verification
    if args.verify:
        result = verify_preconditions(args.verify)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"Precondition check: {'PASS' if result['ok'] else 'FAIL'}")
            if result["verified"]:
                print(f"  Verified ({len(result['verified'])}):")
                for f in result["verified"]:
                    print(f"    [OK] {f}")
            if result["missing"]:
                print(f"  Missing ({len(result['missing'])}):")
                for f in result["missing"]:
                    print(f"    [MISSING] {f}")
            if result["stale"]:
                print(f"  Stale ({len(result['stale'])}):")
                for s in result["stale"]:
                    print(f"    [STALE] {s['path']} (age: {s['age_seconds']}s)")
        sys.exit(0 if result["ok"] else 1)

    # No action specified
    parser.print_help()
    sys.exit(0)


if __name__ == "__main__":
    main()
