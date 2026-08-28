#!/usr/bin/env python3
"""stance.py — operational stance gears with TTL (CLS v3, sanitized standalone).

Four stances modulate gate sensitivity top-down (events set the gear, never the
model's self-report):
    farming    default; all thresholds nominal
    skirmish   stuck detected; alerts fire earlier (e.g. fix-loop 3→2 rounds)
    teamfight  delivery in progress; writes to delivery paths get enhanced
               audit + numeric-assertion reminders
    retreat    incident just recorded; non-diagnostic writes require human ask
               (NEVER deny — the gear itself must not be able to deadlock)

Design laws:
  - chaos/missing/expired/corrupt state == farming (fail-open to nominal)
  - every non-farming stance auto-expires (TTL) — no permanent gears
  - single writer path with atomic replace (os.replace)

CLI:
    python stance.py get
    python stance.py set skirmish --ttl 600 --by ops_monitor --reason "fix loop"
Self-test:
    python stance.py selftest
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

VALID = ("farming", "skirmish", "teamfight", "retreat")
STATE_FILE = Path(tempfile.gettempdir()) / "cls_stance_demo.json"


def read_stance(path=None) -> dict:
    """Read stance; TTL-expired / invalid / missing all degrade to farming."""
    p = Path(path or STATE_FILE)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"mode": "farming", "expires_at": None, "set_by": None, "reason": None}
    mode = raw.get("mode")
    if mode not in VALID:
        mode = "farming"
    exp = raw.get("expires_at")
    if isinstance(exp, (int, float)) and time.time() > exp:
        mode = "farming"
        exp = None
    return {"mode": mode, "expires_at": exp, "set_by": raw.get("set_by"), "reason": raw.get("reason")}


def set_stance(mode: str, ttl_seconds: int = 0, set_by: str = "", reason: str = "", path=None) -> dict:
    if mode not in VALID:
        raise ValueError(f"unknown stance {mode!r}; valid: {VALID}")
    p = Path(path or STATE_FILE)
    payload = {
        "mode": mode,
        "set_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "set_by": set_by,
        "reason": reason,
        "expires_at": (time.time() + ttl_seconds) if ttl_seconds > 0 else None,
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)  # atomic
    return payload


def selftest() -> None:
    tf = Path(tempfile.gettempdir()) / "cls_stance_selftest.json"
    tf.unlink(missing_ok=True)
    assert read_stance(tf)["mode"] == "farming", "missing -> farming"
    set_stance("retreat", ttl_seconds=1, set_by="test", path=tf)
    assert read_stance(tf)["mode"] == "retreat"
    time.sleep(1.1)
    assert read_stance(tf)["mode"] == "farming", "TTL expiry -> farming"
    # chaos value: write garbage directly
    tf.write_text('{"mode": "banana", "expires_at": null}', encoding="utf-8")
    assert read_stance(tf)["mode"] == "farming", "chaos -> farming"
    try:
        set_stance("banana", path=tf)
        raise AssertionError("invalid stance should raise at write time")
    except ValueError:
        pass
    tf.unlink(missing_ok=True)
    print("stance selftest: ALL PASS (missing/TTL/chaos/write-reject)")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "selftest":
        selftest()
    elif sys.argv[1] == "get":
        print(json.dumps(read_stance(), ensure_ascii=False))
    elif sys.argv[1] == "set":
        mode = sys.argv[2]
        ttl = 0
        by = reason = ""
        args = sys.argv[3:]
        for i, a in enumerate(args):
            if a == "--ttl" and i + 1 < len(args):
                ttl = int(args[i + 1])
            elif a == "--by" and i + 1 < len(args):
                by = args[i + 1]
            elif a == "--reason" and i + 1 < len(args):
                reason = args[i + 1]
        print(json.dumps(set_stance(mode, ttl, by, reason), ensure_ascii=False))
    else:
        print(__doc__)
