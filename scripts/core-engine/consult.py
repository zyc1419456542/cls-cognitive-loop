#!/usr/bin/env python3
"""consult.py — external-model consultation for stuck agents (CLS v3, standalone).

The tier-3 step of the difficulty ladder: the agent submits a 4-part
explanation (what tried / why failed / root-cause hypothesis / why this
attempt differs), a cheap external model reviews it AS A COLLEAGUE, and a
time-limited clearance pass is issued so write gates stand down.

Reviewer contract:
  first line = verdict keyword:  [proceed] [one-more-round] [change-approach]
  rest         = <=200 chars of advice, second person, no pleasantries

Fail-open law: if the reviewer is unreachable, issue a SHORTER pass tagged
reviewer_unavailable — the help channel must never become a deadlock source.

Config via env:
  CLS_REVIEW_URL   OpenAI-compatible endpoint (default: SiliconFlow)
  CLS_REVIEW_KEY   API key
  CLS_REVIEW_MODEL model id (default Qwen/Qwen2.5-7B-Instruct)

CLI:
  python consult.py "①tried... ②failed because... ③hypothesis... ④this time..."
  python consult.py --selftest        (offline: fail-open path + verdict parsing)
"""
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

PASS_FILE = Path("consult_clear.json")
PASS_TTL_S = 600          # reviewer answered
PASS_TTL_FALLBACK_S = 300  # reviewer unreachable (fail-open, shorter)

REVIEW_PROMPT = (
    "You are a senior engineer in a consultation with an AI programming agent "
    "stuck in a fix loop. Its self-report:\n\n{explanation}\n\n"
    "Speak to it directly in second person, like a colleague:\n"
    "1. Point out the most suspicious assumption or logic gap (if any)\n"
    "2. Give the most likely root-cause direction\n"
    "3. Recommend: proceed with retry, or verify something first\n"
    "FIRST LINE = verdict keyword only, one of [proceed] [one-more-round] "
    "[change-approach]; then a newline and your advice, under 200 words."
)

VERDICTS = ("proceed", "one-more-round", "change-approach")


def _parse_verdict(reply: str) -> tuple[str, str]:
    first = (reply or "").strip().splitlines()[0] if reply and reply.strip() else ""
    for kw in VERDICTS:
        if kw in first.lower():
            return kw, reply.strip()
    return "one-more-round", (reply or "").strip()  # unparseable -> conservative


def call_reviewer(explanation: str) -> tuple[str | None, str, str]:
    """Returns (verdict|None, opinion, source). Never raises."""
    url = os.environ.get("CLS_REVIEW_URL", "https://api.siliconflow.cn/v1/chat/completions")
    key = os.environ.get("CLS_REVIEW_KEY", "")
    model = os.environ.get("CLS_REVIEW_MODEL", "Qwen/Qwen2.5-7B-Instruct")
    if not key:
        return None, "", "no_key"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": REVIEW_PROMPT.format(explanation=explanation[:2000])}],
        "max_tokens": 400,
        "temperature": 0.5,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        reply = data["choices"][0]["message"]["content"]
        verdict, opinion = _parse_verdict(reply)
        return verdict, opinion, "api"
    except Exception:
        return None, "", "unreachable"


def consult(explanation: str) -> dict:
    if not explanation or len(explanation.strip()) < 20:
        return {"error": "explanation too short — use the 4-part structure"}
    verdict, opinion, source = call_reviewer(explanation)
    if verdict is None:
        verdict = "one-more-round"
        opinion = opinion or (
            "[reviewer unavailable — fail-open pass, shorter TTL]\n"
            "Self-report acknowledged. Proceed carefully; re-consult when the "
            "review channel is back."
        )
    now = time.time()
    ttl = PASS_TTL_S if source == "api" else PASS_TTL_FALLBACK_S
    clear = {
        "ts": now,
        "verdict": verdict,
        "opinion_brief": opinion[:120],
        "expires_at": now + ttl,
        "source": source,
        "explanation_brief": explanation[:200],
    }
    tmp = PASS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(clear, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PASS_FILE)
    return {"verdict": verdict, "opinion": opinion, "clear_seconds": ttl, "source": source}


def pass_valid() -> bool:
    try:
        c = json.loads(PASS_FILE.read_text(encoding="utf-8"))
        return time.time() < float(c.get("expires_at", 0))
    except Exception:
        return False


def selftest() -> None:
    # verdict parsing
    assert _parse_verdict("[change-approach]\nDo X")[0] == "change-approach"
    assert _parse_verdict("garbage")[0] == "one-more-round"
    # fail-open path (no key in env)
    os.environ.pop("CLS_REVIEW_KEY", None)
    r = consult("①tried 3 fixes ②all treated symptoms ③stack points one layer earlier "
                "④byte-level probe before parsing this time")
    assert r["verdict"] == "one-more-round" and r["source"] == "no_key"
    assert pass_valid(), "fail-open pass must be issued"
    # short-explanation guard
    assert "error" in consult("too short")
    PASS_FILE.unlink(missing_ok=True)
    print("consult selftest: ALL PASS (verdict-parse / fail-open pass / guard)")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        selftest()
    elif len(sys.argv) >= 2:
        print(json.dumps(consult(" ".join(sys.argv[2:]) if sys.argv[1] == "--" else " ".join(sys.argv[1:])),
                         ensure_ascii=False, indent=1))
    else:
        print(__doc__)
