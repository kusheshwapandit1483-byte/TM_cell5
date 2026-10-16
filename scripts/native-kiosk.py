#!/usr/bin/env python3
"""Use the installed session-aware kiosk runner, adding only the observer extension."""
import importlib.util
from pathlib import Path


def main():
    extension = Path("/usr/local/lib/tm-camera/native-player-observer")
    if not (extension / "manifest.json").is_file():
        raise RuntimeError("Native frame observer is missing")
    spec = importlib.util.spec_from_file_location("installed_kiosk", "/usr/local/lib/tm-camera/kiosk.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    original_command = runner.chromium_command

    def observed_command(*args, **kwargs):
        command = original_command(*args, **kwargs)
        return command + [f"--load-extension={extension}"]

    runner.chromium_command = observed_command
    runner.run()


if __name__ == "__main__":
    main()
