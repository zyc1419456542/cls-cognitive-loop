#!/usr/bin/env python3
"""stuck_detector.py — behavior-based stuck detection (CLS v3, sanitized standalone).

Detects "repeated attempts on one target without progress" from a categorized
ops log (one JSON line per tool call: {tool, category, input_preview}).

Three signals (deterministic, zero model calls):
  S1  same file mutated >= N times in window   (Edit/Write OR bash mutation:
      sed -i / output redirection / tee / Set-Content — recall over tool names)
  S2  mutate->verify oscillation >= 2 pairs    (verify = Read tool OR running
      tests / the mutated file — real agents verify by running, not re-reading)
  S3  fix loop: mutate->verify pairs >= 3      (tier-2 threshold -> escalation)

Intended wiring:
  S2 hit  -> skirmish gear + forced-strategy-change injection (once/session)
  S3 hit  -> deny becomes consultation entry (see DIFFICULTY_LADDER.md)

CLI:
    python stuck_detector.py analyze ops.jsonl
    python stuck_detector.py selftest
"""
import json
import re
import sys

WINDOW = 12
STUCK_FILE_N = 3
PRECURSOR_PAIRS = 2
FIXLOOP_PAIRS = 3

# exec-class commands that directly mutate files (stderr redirects like 2>/dev/null excluded)
BASH_MUTATE_RE = re.compile(
    r"\bsed\b[^|;&]*\s-i"                                  # sed -i
    r"|(?<![0-2>])>{1,2}\s*[^\s|>&]"                       # > file / >> file
    r"|\btee\b\s+-?\w*\s*[^\s|]"                           # tee file
    r"|\bSet-Content\b|\bAdd-Content\b|\bOut-File\b"       # PowerShell writers
)
MUTATE_TOOLS = {"write", "edit", "str_replace_editor", "Write", "Edit"}
READ_TOOLS = {"read", "Read"}


def categorize(tool: str, input_preview: str = "") -> str:
    if tool in MUTATE_TOOLS:
        return "mutate"
    if tool in READ_TOOLS:
        return "explore"
    if tool in ("bash", "pwsh", "Bash", "PowerShell"):
        if input_preview and BASH_MUTATE_RE.search(input_preview[:400]):
            return "mutate"
        return "exec"
    return "other"


def file_hint(entry: dict) -> str:
    """File identity as basename (window is small; same-name collisions acceptable)."""
    prev = entry.get("input_preview", "") or ""
    m = re.search(r'file_path"?\s*[:=]\s*"?([^",\s]+)', prev)
    if m:
        raw = m.group(1)
    elif entry.get("category") == "mutate":
        m2 = re.search(r"(?:\bsed\b[^|;&]*?|\btee\s+\S+\s+)(\S+\.\w+)", prev)
        if not m2:
            m2 = re.search(r"(?<![0-2>])>{1,2}\s*([^\s|;&]+)", prev)
        raw = m2.group(1) if m2 else ""
    else:
        raw = ""
    return re.split(r"[\\/]", raw)[-1] if raw else ""


def is_verify(entry: dict, mutated_file: str = "") -> bool:
    """Write-then-verify: Read tool, or exec running tests / the mutated file."""
    if entry.get("tool") in READ_TOOLS:
        return True
    if entry.get("category") == "exec":
        cmd = entry.get("input_preview", "") or ""
        if re.search(r"\bpython[\w.]*\b|\bpytest\b|\bnpm\s+test\b|\bmake\s+test\b", cmd):
            if "test" in cmd.lower() or (mutated_file and mutated_file in cmd):
                return True
    return False


def analyze(entries: list) -> list:
    """Return alert strings. S1/S2 are tier-1 (inject); S3 is tier-2 (consult entry)."""
    alerts = []
    recent = entries[-WINDOW:]
    # S1 same-file mutation count
    counts: dict[str, int] = {}
    for e in recent:
        if e.get("category") == "mutate":
            h = file_hint(e)
            if h:
                counts[h] = counts.get(h, 0) + 1
    for fname, n in counts.items():
        if n >= STUCK_FILE_N:
            alerts.append(f"stuck: {fname} mutated {n}x without passing")
    # S2/S3 mutate->verify pairs
    pairs = sum(
        1
        for i in range(len(recent) - 1)
        if recent[i].get("category") == "mutate" and is_verify(recent[i + 1], file_hint(recent[i]))
    )
    if pairs >= FIXLOOP_PAIRS:
        alerts.append(f"fix-loop: {pairs} write->verify cycles — open consultation")
    elif pairs >= PRECURSOR_PAIRS:
        alerts.append(f"stuck-precursor: {pairs} write->verify oscillations")
    return alerts


def _mk(tool, preview, cat=None):
    return {"tool": tool, "category": cat or categorize(tool, preview), "input_preview": preview}


def selftest() -> None:
    # S1: same file edited 3x (via Edit + bash sed — recall check)
    s1 = [
        _mk("Edit", '{"file_path": "/proj/parser.py", "old_string": "a"'),
        _mk("bash", "sed -i 's/a/b/' parser.py"),
        _mk("bash", "cd /proj && sed -i 's/c/d/' parser.py"),
    ]
    a1 = analyze(s1)
    assert any("parser.py" in a and "3x" in a for a in a1), a1
    # S2: 2 pairs edit->test-run -> precursor (not yet fix-loop)
    s2 = []
    for _ in range(2):
        s2 += [
            _mk("Edit", '{"file_path": "/proj/x.py", "old_string": "a"'),
            _mk("bash", "cd /proj && python test_x.py"),
        ]
    a2 = analyze(s2)
    assert any("precursor" in a for a in a2) and not any("fix-loop" in a for a in a2), a2
    # S3: 3 pairs -> fix-loop
    s3 = []
    for _ in range(3):
        s3 += [
            _mk("Edit", '{"file_path": "/proj/x.py", "old_string": "a"'),
            _mk("Read", '{"file_path": "/proj/x.py"}'),
        ]
    a3 = analyze(s3)
    assert any("fix-loop" in a for a in a3), a3
    # negative: different files, non-verify exec between
    n = []
    for f in ("a.py", "b.py", "c.py"):
        n += [_mk("Edit", f'{{"file_path": "/proj/{f}"'), _mk("bash", "git status")]
    assert analyze(n) == [], analyze(n)
    # negative: running unrelated python is not verify
    assert not is_verify(_mk("bash", 'python -c "print(1)"'), "")
    # negative: stderr redirect is not a file write
    assert categorize("bash", "python x.py 2>/dev/null") == "exec"
    print("stuck_detector selftest: ALL PASS (S1 recall/S2 precursor/S3 fixloop/negatives)")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "selftest":
        selftest()
    elif sys.argv[1] == "analyze":
        rows = []
        with open(sys.argv[2], encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        for a in analyze(rows):
            print(a)
    else:
        print(__doc__)
