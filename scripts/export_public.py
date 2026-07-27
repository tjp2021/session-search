#!/usr/bin/env python3
"""Copy the audited public surface into a new empty directory."""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess

from check_public_privacy import ROOT, public_paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=pathlib.Path)
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    if destination.exists():
        raise SystemExit(f"destination already exists: {destination}")
    subprocess.run(
        ["python3", str(ROOT / "scripts" / "check_public_privacy.py")],
        cwd=ROOT,
        check=True,
    )
    destination.mkdir(parents=True)
    for source in public_paths():
        relative = source.relative_to(ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f"Exported {len(public_paths())} files to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
