#!/usr/bin/env python3
"""state_freshness.py -- State freshness checker for cognitive loop.
Runs from SessionStart hook. Checks if key state files are stale.
Stale -> injects warning into CC context (inform, don't deny).
3 consecutive unfixed -> writes escalation flag -> PreToolUse can read it to deny.
"""

import json, os, sys, time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent

CHECKS = [
    ("state/active_context.json", 86400,  "active_context(1)", "cog-context --action read"),
    ("state/session_memory.md",   86400,  "session_memory(5)", "write session summary"),
    ("data/state/cog_step.json",  600,    "cog_step(2)", "cog-step-declare"),
    ("state/trajectory.json",     43200,  "trajectory(6)", "cog-trajectory or /save"),
]

def check():
    now = time.time()
    stale = []
    for path, max_age, label, fix in CHECKS:
        fp = ROOT / path
        if not fp.exists():
            stale.append({"label":label,"status":"missing","age_h":"N/A","fix":fix})
            continue
        age_h = (now - fp.stat().st_mtime) / 3600
        if age_h * 3600 > max_age:
            stale.append({"label":label,"status":"stale","age_h":round(age_h,1),"fix":fix})
    return stale

def escalation(efile):
    if not os.path.exists(efile): return 0, False
    try:
        d = json.load(open(efile))
        c = d.get("warn_count",0)
        return c, (c >= 3)
    except: return 0, False

def update_escalation(efile, stale):
    if not stale:
        if os.path.exists(efile): os.remove(efile)
        return
    c = 1
    if os.path.exists(efile):
        try:
            d = json.load(open(efile))
            c = d.get("warn_count",0) + 1
        except: pass
    json.dump({"warn_count":c,"last_check":datetime.now().isoformat(),"stale":[s["label"] for s in stale],"escalated":(c>=3)}, open(efile,'w'), ensure_ascii=False, indent=2)

def main():
    efile = str(ROOT / "data" / "state" / "freshness_escalation.json")
    stale = check()
    update_escalation(efile, stale)

    if "--json" in sys.argv:
        print(json.dumps({"stale_count":len(stale),"items":stale,"escalated":escalation(efile)[1]}, ensure_ascii=False, indent=2))
        return

    if not stale:
        # All fresh - inject nothing
        return

    lines = ["[state_freshness]"]
    for s in stale:
        lines.append(f"  {s['label']} {s['status']} age={s['age_h']}h -> {s['fix']}")
    wc, esc = escalation(efile)
    if esc:
        lines.append(f"  ESCALATED ({wc}/3) - next: PreToolUse deny")
    else:
        lines.append(f"  ({wc}/3)")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
