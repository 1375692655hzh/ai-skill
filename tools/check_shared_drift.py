#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Detect unintentional divergence among shared per-skill copy files.

Root cause this guards against (2026-10-10 audits): shared helpers
(call_llm.py / llm_runner.py / runtime_utils.py …) are COPY-PER-SKILL by
design ("复制单目录即可部署"), so a fix that lands in one copy silently
misses its siblings — e.g. `_resolve_api_key` reached only close-report
while morning/flash kept failing keys; the paraborsa curl swap dropped the
retry loop. This script makes single-copy drift loud.

How it works:
  - Auto-discovers every *.py filename that exists in 2+ skill scripts/ dirs.
  - Groups copies by sha256; compares against the committed baseline
    (tools/shared_baseline.json).
  - Any copy whose hash changed while same-named siblings did not follow
    → reported, exit 1. Intentional divergence → review, then `--update`.

Usage:
  python tools/check_shared_drift.py               # check vs baseline
  python tools/check_shared_drift.py --update      # re-baseline (after intentional change)
  python tools/check_shared_drift.py --diff call_llm.py   # inspect divergence
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / "shared_baseline.json"


def discover() -> dict[str, dict[str, str]]:
    """{filename: {skill_dir: sha256}} for every script name shared by 2+ skills."""
    by_name: dict[str, dict[str, str]] = {}
    for scripts_dir in sorted(ROOT.glob("*/*-skill*/scripts")):
        skill = scripts_dir.parent.name
        for py in sorted(scripts_dir.glob("*.py")):
            digest = hashlib.sha256(py.read_bytes()).hexdigest()[:16]
            by_name.setdefault(py.name, {})[skill] = digest
    return {name: copies for name, copies in by_name.items() if len(copies) >= 2}


def check(current: dict, baseline: dict) -> list[str]:
    problems: list[str] = []
    for name, copies in sorted(current.items()):
        base = baseline.get(name, {})
        changed = [s for s, h in copies.items() if base.get(s) != h]
        if not changed:
            continue
        untouched = [s for s in copies if s not in changed]
        groups = sorted(set(copies.values()))
        head = f"[{name}] changed in: {', '.join(changed)}"
        if len(groups) > 1:
            head += f" | content groups: {len(groups)} ({', '.join(groups)})"
        if untouched:
            head += f"\n    siblings NOT updated (propagate the fix or --update if intentional): {', '.join(untouched)}"
        problems.append(head)
    for name in sorted(set(baseline) - set(current)):
        problems.append(f"[{name}] disappeared from shared set (copies renamed/removed?)")
    return problems


def show_diff(name: str, current: dict) -> int:
    copies = current.get(name)
    if not copies:
        print(f"{name}: not a shared file")
        return 1
    groups: dict[str, str] = {}
    for skill, digest in copies.items():
        if digest not in groups:
            groups[digest] = skill
    if len(groups) == 1:
        print(f"{name}: all {len(copies)} copies identical")
        return 0
    texts = {
        skill: (ROOT / "*/" / skill / "scripts" / name) for skill in copies
    }
    # locate real paths
    paths = {}
    for scripts_dir in ROOT.glob("*/*-skill*/scripts"):
        p = scripts_dir / name
        if p.is_file():
            paths[scripts_dir.parent.name] = p
    reps = list(groups.items())[:2]
    a_rep, b_rep = reps[0][1], reps[1][1]
    diff = difflib.unified_diff(
        paths[a_rep].read_text(encoding="utf-8", errors="replace").splitlines(),
        paths[b_rep].read_text(encoding="utf-8", errors="replace").splitlines(),
        fromfile=f"{a_rep}/{name}", tofile=f"{b_rep}/{name}", lineterm="",
    )
    print("\n".join(diff))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="re-baseline after intentional divergence")
    parser.add_argument("--diff", metavar="FILE", help="show unified diff between the first two content groups")
    args = parser.parse_args()

    current = discover()
    if args.diff:
        return show_diff(args.diff, current)

    baseline = {}
    if BASELINE.exists():
        try:
            baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("baseline corrupt — rerun with --update", file=sys.stderr)

    if args.update:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        n_files = len(current)
        n_copies = sum(len(v) for v in current.values())
        print(f"baseline updated: {n_files} shared files, {n_copies} copies")
        return 0

    if not baseline:
        print("no baseline yet — run with --update to seed it")
        return 0

    problems = check(current, baseline)
    if problems:
        print("SHARED-COPY DRIFT DETECTED:")
        for p in problems:
            print("  " + p)
        print("\nPropagate the fix to the siblings above, or rerun with --update if the divergence is intentional.")
        return 1
    print(f"OK: {len(current)} shared files, {sum(len(v) for v in current.values())} copies — no drift vs baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
