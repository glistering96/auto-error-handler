from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from aeh.config import Settings
from aeh.db import make_session_factory
from aeh.service import sync_services


def init_fixture(settings: Settings) -> Path:
    source = Path(__file__).resolve().parents[1] / "fixtures/fixture-api"
    target = settings.repository_root.resolve() / "fixture-api"
    if target.exists():
        if not (target / ".git").exists():
            raise ValueError(f"Fixture target exists but is not a Git repository: {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    for argv in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.name", "AEH Fixture"],
        ["git", "config", "user.email", "fixture@example.invalid"],
        ["git", "add", "--", "."],
        ["git", "commit", "-m", "Broken fixture baseline"],
    ):
        subprocess.run(argv, cwd=target, check=True, capture_output=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-fixture", "sync-services"])
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init-fixture":
        print(init_fixture(settings))
    else:
        sync_services(settings, make_session_factory(settings))
        print("Service configuration synchronized")


if __name__ == "__main__":
    main()
