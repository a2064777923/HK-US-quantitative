#!/usr/bin/env python3
"""Run local verification for the QuantMind/Hermes scripts."""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def cleanup_pycache():
    for base in (Path("scripts"), Path("tests")):
        if not base.exists():
            continue
        for path in base.rglob("__pycache__"):
            shutil.rmtree(path, ignore_errors=True)


def run_step(args):
    print("+ " + " ".join(args), flush=True)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(args, env=env).returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-git", action="store_true", help="skip git diff --check")
    args = parser.parse_args()

    steps = [
        [sys.executable, "-m", "compileall", "-q", "scripts", "tests"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
    ]
    if not args.skip_git:
        steps.append(["git", "diff", "--check"])

    try:
        for step in steps:
            rc = run_step(step)
            if rc != 0:
                return rc
    finally:
        cleanup_pycache()
    print("local verification passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
