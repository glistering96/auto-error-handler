from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path

from aeh.config import Limits
from aeh.errors import AehError, scrub


def _command(workspace: Path, argv: list[str]) -> list[str]:
    if not shutil.which("bwrap"):
        raise AehError(503, "AEH-CODEX-502-001", "Network-isolated validation is unavailable")
    command = [
        "bwrap",
        "--unshare-net",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/bin",
        "/bin",
        "--ro-bind",
        "/lib",
        "/lib",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/workspace",
        "--bind",
        str(workspace),
        "/workspace",
        "--chdir",
        "/workspace",
        "--setenv",
        "HOME",
        "/tmp",
    ]
    for path in ("/lib64", "/usr/local"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    return command + ["--", *argv]


def run_validation(workspace: Path, policy: dict) -> list[dict]:
    results: list[dict] = []
    for item in policy["validationCommands"]:
        argv = item["argv"]
        if not argv or argv[0] in {"sh", "bash", "zsh", "curl", "wget", "git"}:
            raise AehError(422, "AEH-POLICY-422-001", "Unregistered validation command")
        timeout = min(item["timeoutSeconds"], Limits.command_seconds)
        started = time.monotonic()
        captured = bytearray()
        timed_out = False
        with subprocess.Popen(
            _command(workspace, argv),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
            start_new_session=True,
        ) as process:
            assert process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = started + timeout
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        os.killpg(process.pid, signal.SIGKILL)
                        break
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif len(captured) < Limits.stream_bytes:
                            captured.extend(chunk[: Limits.stream_bytes - len(captured)])
            process.wait()
            exit_code = None if timed_out else process.returncode
        status = "TIMED_OUT" if timed_out else "PASSED" if exit_code == 0 else "FAILED"
        output = scrub(captured.decode(errors="replace"), Limits.stream_bytes)
        results.append(
            {
                "id": item["id"],
                "status": status,
                "exitCode": exit_code,
                "durationMs": int((time.monotonic() - started) * 1000),
                "output": output,
            }
        )
        if status != "PASSED":
            raise ValidationFailure(results)
    return results


class ValidationFailure(AehError):
    def __init__(self, results: list[dict]):
        super().__init__(422, "AEH-VALIDATION-422-001", "Registered validation command failed")
        self.results = results
