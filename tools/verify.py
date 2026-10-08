"""The build gate: the one command that must be green before a commit.

Runs, in order:

  1. the SRD 5.1 conformance gate (``tools/srd_gate.py``)
  2. the token-budget gate   (``tools/token_budget.py --gate``)
  3. the dice engine suite   (``web/tests/dice``)
  4. every web suite         (``web/tests/test_*.py``)
  5. ``node --check`` on the client  (skipped when node is absent)

Exit code 0 only when every step passed. This is the project's CI: one command,
one exit code.

Run from anywhere:

    venv\\Scripts\\python.exe tools\\verify.py
    venv\\Scripts\\python.exe tools\\verify.py --only srd,budget,dice
    venv\\Scripts\\python.exe tools\\verify.py --json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
CLIENT_JS = "web/static/app.js"
ALL_STEPS = ("srd", "budget", "dice", "web", "node")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def step_srd() -> tuple[bool, str, str]:
    proc = _run([PY, "tools/srd_gate.py"])
    ok = proc.returncode == 0
    detail = _last_line(proc.stdout) or _last_line(proc.stderr) or ("GREEN" if ok else "RED")
    return ok, detail, proc.stdout + proc.stderr


def step_budget() -> tuple[bool, str, str]:
    proc = _run([PY, "tools/token_budget.py", "--gate"])
    ok = proc.returncode == 0
    detail = "GREEN" if ok else (_last_line(proc.stderr) or "RED")
    return ok, detail, proc.stdout + proc.stderr


def step_dice() -> tuple[bool, str, str]:
    proc = _run([PY, "-m", "unittest", "discover", "-s", "web/tests/dice", "-t", "."])
    ok = proc.returncode == 0
    ran = next((ln.strip() for ln in proc.stderr.splitlines() if ln.startswith("Ran ")), "")
    verdict = _last_line(proc.stderr) or ("OK" if ok else "FAIL")
    detail = f"{ran}, {verdict}" if ran else verdict
    return ok, detail, proc.stdout + proc.stderr


def step_web() -> tuple[bool, str, str]:
    files = sorted((REPO / "web" / "tests").glob("test_*.py"))
    failed, outs = [], []
    for f in files:
        proc = _run([PY, str(f.relative_to(REPO))])
        if proc.returncode != 0:
            failed.append(f.name)
            outs.append(f"### {f.name}\n{proc.stdout}{proc.stderr}")
    ok = not failed
    detail = f"{len(files)}/{len(files)} PASS" if ok else f"{len(failed)} FAIL: {', '.join(failed)}"
    return ok, detail, "\n".join(outs)


def step_node() -> tuple[bool, str, str]:
    if not shutil.which("node"):
        return True, "SKIP (node not found)", ""
    proc = _run(["node", "--check", CLIENT_JS])
    ok = proc.returncode == 0
    detail = f"{CLIENT_JS} OK" if ok else (_last_line(proc.stderr) or "syntax error")
    return ok, detail, proc.stdout + proc.stderr


STEPS = {"srd": step_srd, "budget": step_budget, "dice": step_dice, "web": step_web,
         "node": step_node}


def main() -> int:
    ap = argparse.ArgumentParser(description="The build gate")
    ap.add_argument("--only", default=None,
                    help="comma-separated subset of: " + ", ".join(ALL_STEPS))
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    ap.add_argument("--verbose", action="store_true", help="print full output for passing steps too")
    args = ap.parse_args()

    names = ALL_STEPS
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in STEPS]
        if unknown:
            print(f"unknown step(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        names = tuple(wanted)

    results, ok_all = [], True
    for i, name in enumerate(names, 1):
        ok, detail, output = STEPS[name]()
        ok_all = ok_all and ok
        results.append({"step": name, "ok": ok, "detail": detail})
        if not args.json:
            print(f"[{i}/{len(names)}] {name:8s} {'PASS' if ok else 'FAIL'}  {detail}")
            if not ok or args.verbose:
                print("-" * 60)
                print((output or "").rstrip())
                print("-" * 60)

    if args.json:
        print(json.dumps({"ok": ok_all, "steps": results}, indent=2))
    else:
        print("\nGATE: " + ("GREEN" if ok_all else "RED"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
