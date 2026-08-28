#!/usr/bin/env python3
"""
Cognitive Core Loop Engine — drives the 6-step CLS cycle.

Steps:
  1. Situational Awareness — load state, check environment
  2. Task Execution — scope, pre-flight, execute, verify
  3. Associative Learning — match new knowledge to existing patterns
  4. Abstract Generalization — distill experience into reusable patterns
  5. Context Persistence — write checkpoint to disk
  6. Trajectory Update — record delta-q (mass) and delta-p (momentum)

The loop closes: Step 6 feeds into Step 1 of the next cycle.

Usage:
  python scripts/core-engine/cognitive_core_loop.py --demo
  python scripts/core-engine/cognitive_core_loop.py --task "analyze sensor data"
  python scripts/core-engine/cognitive_core_loop.py --checkpoint /path/to/checkpoint.json
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── project root ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ─── LoopState dataclass ─────────────────────────────────────────

@dataclass
class LoopState:
    """Full state vector for a cognitive loop cycle."""

    # identity
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    iteration: int = 0

    # position in phase space (conceptual coordinates)
    position: float = 0.0          # current "location" in concept space (scalar projection)
    mass: float = 1.0              # accumulated knowledge weight
    momentum: float = 0.0          # rate of change across iterations

    # current step (1-6)
    step: int = 0
    step_name: str = ""

    # knowledge linkage
    knowledge_hits: list[str] = field(default_factory=list)
    associations: list[dict] = field(default_factory=list)
    abstractions: list[str] = field(default_factory=list)

    # error tracking
    errors: list[dict] = field(default_factory=list)

    # timestamps
    started_at: str = ""
    checkpoint_at: str = ""

    # task tracking
    task_description: str = ""
    output_summary: str = ""

    # symbolic dynamics
    entropy_current: float = 0.0
    stability_status: str = "unknown"

    # generic extension slot
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["_version"] = 1
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LoopState":
        d.pop("_version", None)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ─── CognitiveLoop ───────────────────────────────────────────────

class CognitiveLoop:
    """Six-step cognitive loop engine.

    Drives the full cycle: awareness → execute → associate → abstract → persist → update trajectory.
    Each step is a discrete method that can be called independently or via run().
    """

    def __init__(self, checkpoint_dir: Optional[Path] = None):
        self.state = LoopState()
        self._start_time = time.time()
        self._step_timings: dict[str, float] = {}

        # checkpoint persistence directory
        if checkpoint_dir is None:
            checkpoint_dir = PROJECT_ROOT / "data" / "checkpoints"
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    # ── public API ───────────────────────────────────────────────

    def run(self, task_description: str) -> LoopState:
        """Execute the full 6-step cognitive loop on a task.

        Args:
            task_description: Human-readable description of what to do.

        Returns:
            The final LoopState after all 6 steps complete.
        """
        self._log("INFO", f"Starting cognitive loop for task: {task_description[:80]}")

        # Step 1: Situational Awareness
        self._step_1_awareness(task_description)

        # Step 2: Task Execution
        self._step_2_execute(task_description)

        # Step 3: Associative Learning
        self._step_3_associate()

        # Step 4: Abstract Generalization
        self._step_4_abstract()

        # Step 5: Context Persistence
        self._step_5_persist()

        # Step 6: Trajectory Update
        self._step_6_trajectory()

        elapsed = time.time() - self._start_time
        self._log("INFO", f"Cognitive loop complete in {elapsed:.2f}s. "
                  f"Iteration {self.state.iteration}, "
                  f"stability={self.state.stability_status}")

        return self.state

    def save_checkpoint(self, label: str = "") -> Path:
        """Persist current loop state to a JSON checkpoint file.

        Returns the path to the saved checkpoint.
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        fname = f"checkpoint_{ts}"
        if label:
            fname += f"_{label}"
        fname += ".json"

        path = self.checkpoint_dir / fname
        self.state.checkpoint_at = ts
        self.state.checkpoint_at = datetime.now(timezone.utc).isoformat()

        payload = self.state.to_dict()
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self._log("INFO", f"Checkpoint saved to {path}")
        return path

    def load_checkpoint(self, path: Path | str) -> bool:
        """Load a previously saved checkpoint and restore state.

        Returns True on success, False on failure.
        """
        path = Path(path)
        if not path.exists():
            self._log("ERROR", f"Checkpoint not found: {path}")
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.state = LoopState.from_dict(data)
            self.state.iteration += 1
            self._log("INFO", f"Checkpoint loaded from {path}, iteration bumped to {self.state.iteration}")
            return True
        except Exception as e:
            self._log("ERROR", f"Failed to load checkpoint: {e}")
            return False

    # ── step methods ─────────────────────────────────────────────

    def _step_1_awareness(self, task_description: str) -> None:
        """Step 1: Situational Awareness.

        Loads current environment state:
        - Scans project metadata (exists? fresh? stale?)
        - Checks for prior checkpoint continuity
        - Records baseline entropy metrics
        - Prepares working context for upcoming execution

        In a real deployment this would query live state files, service
        health endpoints, and prior session memory.  Here we simulate the
        scan against the project tree.
        """
        self.state.step = 1
        self.state.step_name = "Situational Awareness"
        self.state.task_description = task_description

        t0 = time.time()

        # 1a. Scan project metadata
        state_dir = PROJECT_ROOT / "data"
        if state_dir.exists():
            self.state.meta["project_data_exists"] = True
            # check recency of any JSON in data/
            jsons = sorted(state_dir.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if jsons:
                newest = jsons[0]
                age_s = time.time() - newest.stat().st_mtime
                self.state.meta["newest_data_file"] = str(newest.relative_to(PROJECT_ROOT))
                self.state.meta["data_freshness_seconds"] = round(age_s, 1)
        else:
            self.state.meta["project_data_exists"] = False

        # 1b. Check for prior checkpoint continuity
        prior_ckpt = self._find_latest_checkpoint()
        if prior_ckpt:
            self.state.meta["prior_checkpoint"] = str(prior_ckpt.relative_to(PROJECT_ROOT))
            self.load_checkpoint(prior_ckpt)

        # 1c. Baseline state initialization
        self.state.position = 0.0
        self.state.mass = 1.0
        self.state.momentum = 0.0
        self.state.errors.clear()
        self.state.knowledge_hits.clear()
        self.state.associations.clear()
        self.state.abstractions.clear()

        self._step_timings["step_1"] = time.time() - t0
        self._log("DEBUG", f"Step 1 complete. freshness={self.state.meta.get('data_freshness_seconds', 'N/A')}s")

    def _step_2_execute(self, task_description: str) -> None:
        """Step 2: Task Execution.

        Core execution phase:
        - Scopes task complexity (simple / medium / complex)
        - Runs pre-flight checks (fuse board, premise validation)
        - Executes the task via the appropriate route
        - Captures outputs and any errors encountered

        This is the only step that produces external effects.
        """
        self.state.step = 2
        self.state.step_name = "Task Execution"

        t0 = time.time()

        # 2a. Complexity estimation (heuristic)
        words = len(task_description.split())
        complexity = "simple"
        if words > 15:
            complexity = "medium"
        if words > 40 or "batch" in task_description.lower():
            complexity = "complex"
        self.state.meta["complexity"] = complexity

        # 2b. Pre-flight: check premise (file existence, state freshness)
        required_files = self._infer_required_files(task_description)
        for f in required_files:
            p = PROJECT_ROOT / f
            if not p.exists():
                self.state.errors.append({
                    "step": 2,
                    "type": "missing_prerequisite",
                    "file": f,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        # 2c. Simulated execution
        if "fuse" in task_description.lower() or "trip" in task_description.lower():
            self.state.errors.append({
                "step": 2,
                "type": "fuse_tripped",
                "fuse": "WRITE_PROTECT",
                "reason": "Simulated fuse trip for demonstration",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        # 2d. Record output summary
        if not self.state.errors:
            self.state.output_summary = f"Executed: {task_description[:120]}"
        else:
            self.state.output_summary = f"Executed with {len(self.state.errors)} errors: {task_description[:100]}"

        # momentum = mass * velocity (approximate as mass delta per iteration)
        self.state.momentum = max(0.01, len(self.state.errors) * -0.1 + 0.5)

        self._step_timings["step_2"] = time.time() - t0
        self._log("DEBUG", f"Step 2 complete. complexity={complexity}, errors={len(self.state.errors)}")

    def _step_3_associate(self) -> None:
        """Step 3: Associative Learning.

        Matches execution results and encountered concepts against
        the existing knowledge graph.  Records pairwise associations
        with strength scores.

        High association strength pushes new knowledge linkages.
        """
        self.state.step = 3
        self.state.step_name = "Associative Learning"

        t0 = time.time()

        # Extract key terms from the task description as seed concepts
        terms = set(
            w.lower().strip(",.?!;:()[]{}\"'")
            for w in self.state.task_description.split()
            if len(w) > 3
        )

        # Simulate matching against a conceptual vocabulary
        concept_bases = {
            "data": "information_science",
            "analysis": "analytical_methods",
            "model": "modeling_framework",
            "compute": "computation_engine",
            "design": "design_methodology",
            "system": "systems_engineering",
            "error": "fault_management",
            "state": "state_machines",
            "check": "verification_protocols",
        }

        for term in terms:
            for base, domain in concept_bases.items():
                if base in term:
                    strength = 0.6 + (0.1 * (len(term) % 5))
                    self.state.associations.append({
                        "term": term,
                        "domain": domain,
                        "strength": round(strength, 3),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

        # Record knowledge_hits (concepts that exceeded threshold)
        threshold = 0.7
        self.state.knowledge_hits = [
            f"{a['term']} → {a['domain']}"
            for a in self.state.associations
            if a["strength"] >= threshold
        ]

        # Mass accumulates as a function of total association strength
        total_strength = sum(a["strength"] for a in self.state.associations)
        self.state.mass += round(total_strength * 0.05, 4)

        self._step_timings["step_3"] = time.time() - t0
        self._log("DEBUG", f"Step 3 complete. {len(self.state.associations)} associations, "
                  f"{len(self.state.knowledge_hits)} knowledge hits")

    def _step_4_abstract(self) -> None:
        """Step 4: Abstract Generalization.

        Distills concrete associations into reusable, domain-agnostic patterns.
        Only runs when the system is stable (no active divergence detected).

        Skips if prior steps produced errors, as abstractions derived from
        error-ridden cycles are likely noise.
        """
        self.state.step = 4
        self.state.step_name = "Abstract Generalization"

        t0 = time.time()

        # Guard: skip if errors indicate an unstable cycle
        if len(self.state.errors) > 2:
            self.state.abstractions.append("SKIPPED: too many errors in cycle")
            self.state.meta["abstraction_skipped"] = "errors_exceed_threshold"
            self._step_timings["step_4"] = time.time() - t0
            return

        # Group associations by domain
        domains: dict[str, int] = {}
        for a in self.state.associations:
            d = a["domain"]
            domains[d] = domains.get(d, 0) + 1

        # Generate one abstraction per dominant domain
        for domain, count in sorted(domains.items(), key=lambda x: -x[1]):
            if count >= 2:
                self.state.abstractions.append(
                    f"Pattern in {domain}: {count} related concepts — "
                    f"consider reusable {domain.replace('_', ' ')} module"
                )
            if len(self.state.abstractions) >= 3:
                break

        if not self.state.abstractions:
            self.state.abstractions.append("No high-confidence abstractions this cycle")

        # Position shifts towards abstraction density
        self.state.position += round(len(self.state.abstractions) * 0.1, 4)

        self._step_timings["step_4"] = time.time() - t0
        self._log("DEBUG", f"Step 4 complete. {len(self.state.abstractions)} abstractions generated")

    def _step_5_persist(self) -> None:
        """Step 5: Context Persistence.

        Writes the current loop state to a checkpoint file for later recovery.
        Also updates aggregated learning metrics.

        In a deployed system this would additionally:
        - Push state to a session memory store
        - Update the GPU daemon trajectory feed
        - Rotate log files if thresholds breached
        """
        self.state.step = 5
        self.state.step_name = "Context Persistence"

        t0 = time.time()

        # Save checkpoint
        ckpt_path = self.save_checkpoint(label=f"iter_{self.state.iteration}")

        # Update meta with persistence info
        self.state.meta["last_checkpoint"] = str(ckpt_path.relative_to(PROJECT_ROOT))
        self.state.meta["checkpoint_size_bytes"] = ckpt_path.stat().st_size

        self._step_timings["step_5"] = time.time() - t0
        self._log("DEBUG", f"Step 5 complete. checkpoint={ckpt_path.name}")

    def _step_6_trajectory(self) -> None:
        """Step 6: Trajectory Update.

        Records the delta in phase-space variables (position, mass, momentum).
        Computes stability metrics and outputs the final state summary.

        This step closes the loop — its outputs feed into Step 1 of
        the next iteration, providing continuity across cycles.
        """
        self.state.step = 6
        self.state.step_name = "Trajectory Update"

        t0 = time.time()

        # Compute deltas
        delta_position = self.state.position - 0.0
        delta_mass = self.state.mass - 1.0
        delta_momentum = self.state.momentum - 0.0

        # Stability check: are the deltas within normal bounds?
        # Large jumps in position with small mass increase = potential divergence
        if abs(delta_position) > 2.0:
            self.state.stability_status = "warn"
        elif abs(delta_momentum) > 1.0:
            self.state.stability_status = "critical"
        elif len(self.state.errors) == 0:
            self.state.stability_status = "ok"
        else:
            self.state.stability_status = "warn"

        # Trajectory point for this iteration
        trajectory_point = {
            "iteration": self.state.iteration,
            "step_sequence": [
                {"step": 1, "duration_s": round(self._step_timings.get("step_1", 0), 4)},
                {"step": 2, "duration_s": round(self._step_timings.get("step_2", 0), 4)},
                {"step": 3, "duration_s": round(self._step_timings.get("step_3", 0), 4)},
                {"step": 4, "duration_s": round(self._step_timings.get("step_4", 0), 4)},
                {"step": 5, "duration_s": round(self._step_timings.get("step_5", 0), 4)},
                {"step": 6, "duration_s": round(self._step_timings.get("step_6", 0), 4)},
            ],
            "delta_position": round(delta_position, 4),
            "delta_mass": round(delta_mass, 4),
            "delta_momentum": round(delta_momentum, 4),
            "stability": self.state.stability_status,
            "error_count": len(self.state.errors),
            "association_count": len(self.state.associations),
            "abstraction_count": len(self.state.abstractions),
            "entropy_estimate": round(self.state.entropy_current, 4),
            "output_summary": self.state.output_summary,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Persist trajectory log
        traj_dir = PROJECT_ROOT / "data" / "trajectories"
        traj_dir.mkdir(parents=True, exist_ok=True)
        traj_file = traj_dir / f"{self.state.session_id}.jsonl"

        existing_entries = []
        if traj_file.exists():
            existing_entries = [
                json.loads(line)
                for line in traj_file.read_text(encoding="utf-8").strip().splitlines()
                if line.strip()
            ]

        existing_entries.append(trajectory_point)
        traj_file.write_text(
            "\n".join(json.dumps(entry, ensure_ascii=False) for entry in existing_entries) + "\n",
            encoding="utf-8",
        )

        self.state.meta["trajectory_file"] = str(traj_file.relative_to(PROJECT_ROOT))

        self._step_timings["step_6"] = time.time() - t0
        self._log("DEBUG", f"Step 6 complete. stability={self.state.stability_status}, "
                  f"delta_p={delta_position:.3f}, delta_m={delta_mass:.3f}")

    # ── helpers ─────────────────────────────────────────────────

    def _infer_required_files(self, task_description: str) -> list[str]:
        """Heuristically infer which files a task might depend on.

        Simple keyword-based mapping.  In production this would query
        a dependency graph or capability router.
        """
        patterns = {
            "fuse": ["data/safety-configs/fuses_config.json"],
            "config": ["data/safety-configs/fuses_config.json",
                        "data/safety-configs/compact_health_config.json"],
            "gate": ["scripts/core-engine/qwen_gate.py"],
            "safety": ["data/safety-configs/fuses_config.json"],
            "cad": ["data/safety-configs/fuses_config.json"],
            "checkpoint": [],
        }
        result: list[str] = []
        for keyword, files in patterns.items():
            if keyword in task_description.lower():
                result.extend(files)
        return list(set(result)) or []

    def _find_latest_checkpoint(self) -> Optional[Path]:
        """Find the most recently modified checkpoint file."""
        if not self.checkpoint_dir.exists():
            return None
        jsons = sorted(
            self.checkpoint_dir.glob("checkpoint_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return jsons[0] if jsons else None

    def _log(self, level: str, message: str) -> None:
        """Emit a timestamped log line to stderr (does not pollute stdout)."""
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{ts}] [{level}] [loop] {message}", file=sys.stderr, flush=True)


# ─── CLI ─────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Cognitive Core Loop Engine — 6-step CLS cycle driver",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a demonstration cycle with a built-in sample task.",
    )
    parser.add_argument(
        "--task",
        type=str,
        default=None,
        help="Run the loop on a custom task description.",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default=None,
        help="Path to a checkpoint file to load before running.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of loop iterations to run (default: 1).",
    )
    parser.add_argument(
        "--save-only",
        action="store_true",
        help="Only save a checkpoint of the current state, then exit.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full state after each iteration.",
    )
    args = parser.parse_args()

    loop = CognitiveLoop()

    # Load checkpoint if specified
    if args.checkpoint:
        ckpt_path = Path(args.checkpoint)
        if not ckpt_path.exists():
            print(f"ERROR: checkpoint not found: {args.checkpoint}", file=sys.stderr)
            sys.exit(1)
        if not loop.load_checkpoint(ckpt_path):
            sys.exit(1)

    # Save-only mode
    if args.save_only:
        loop.state.task_description = "(manual checkpoint)"
        path = loop.save_checkpoint(label="manual")
        print(f"Checkpoint saved: {path}")
        sys.exit(0)

    # Determine task
    if args.task:
        task = args.task
    elif args.demo:
        task = "Demonstrate cognitive loop: verify config integrity, check state freshness, "
        task += "associate with safety patterns, abstract reusable validation rule, "
        task += "persist checkpoint, update trajectory."
    else:
        parser.print_help()
        sys.exit(0)

    # Run the loop
    for i in range(args.iterations):
        loop.state.iteration = i
        result = loop.run(task)

        if args.verbose:
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))

    # Summary
    print(f"\n{'='*60}")
    print(f"  Session: {result.session_id}")
    print(f"  Iterations: {args.iterations}")
    print(f"  Stability: {result.stability_status}")
    print(f"  Errors: {len(result.errors)}")
    print(f"  Associations: {len(result.associations)}")
    print(f"  Abstractions: {len(result.abstractions)}")
    print(f"  Mass: {result.mass:.4f}  Momentum: {result.momentum:.4f}  Position: {result.position:.4f}")
    print(f"  Checkpoint: {result.meta.get('last_checkpoint', 'not saved')}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
