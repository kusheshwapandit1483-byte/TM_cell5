#!/usr/bin/env python3
"""Preserve the known player rollback and update the installed raspberrypibase checkout."""
import os
from pathlib import Path
import subprocess
import sys

BRANCH = "fix/raspberrypibase-native-player-recovery"
BASELINE = "16e680d536c0f74928971069258a15bed7b522a6"


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def known_rollback(root, changed):
    if not set(changed).issubset({"app.js", "index.html"}):
        return False
    for name in changed:
        expected = subprocess.check_output(["git", "show", f"{BASELINE}:{name}"], text=True)
        if (root / name).read_text(encoding="utf-8").replace("\r\n", "\n") != expected.replace("\r\n", "\n"):
            return False
    return True


def main():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("Run as aegixcore without sudo; service application requests sudo itself")
    root = Path(git("rev-parse", "--show-toplevel")).resolve()
    installed = subprocess.check_output(["systemctl", "show", "tm-camera-monitor.service", "-p", "WorkingDirectory", "--value"], text=True).strip()
    if not installed or Path(installed).resolve() != root:
        raise RuntimeError("Run from the installed raspberrypibase folder; no source or settings changed")
    subprocess.run(["git", "fetch", "origin", BRANCH], check=True)
    target = git("rev-parse", "FETCH_HEAD")
    changed = git("diff", "--name-only", "HEAD").splitlines()
    if changed:
        if not known_rollback(root, changed):
            raise RuntimeError("Other local changes found; they have not been discarded")
        subprocess.run(["git", "stash", "push", "-m", "Preserve confirmed native player rollback", "--", *changed], check=True)
        print("Your confirmed working player rollback is preserved in git stash.", flush=True)
    exists = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{BRANCH}"]).returncode == 0
    subprocess.run(["git", "switch", BRANCH] if exists else ["git", "switch", "--no-track", "-c", BRANCH, target], check=True)
    subprocess.run(["git", "merge", "--ff-only", target], check=True)
    subprocess.run(["sudo", "python3", str(root / "scripts/apply-native-preview.py")], check=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Upgrade stopped: {exc}", file=sys.stderr)
        sys.exit(1)
