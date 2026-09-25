"""Manual check that model tool commands cannot reach the network."""

import asyncio
import uuid
from pathlib import Path

from openai_codex import ApprovalMode, AsyncCodex, Sandbox

from aeh.cli import init_fixture
from aeh.config import Settings
from aeh.gateway import SdkCodexGateway
from aeh.gitops import resolve_commit, worktree


async def main() -> None:
    settings = Settings(use_fake_codex=False)
    repo = init_fixture(settings)
    sha = resolve_commit(repo, None, "main")
    with worktree(repo, settings.workspace_root, "probe-network", str(uuid.uuid4()), sha) as path:
        gateway = SdkCodexGateway(settings)
        with gateway._config() as config:
            async with AsyncCodex(config) as codex:
                thread = await codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    cwd=str(path),
                    model=settings.codex_model,
                    sandbox=Sandbox.workspace_write,
                    ephemeral=True,
                    config=gateway._thread_config(Path(config.env["CODEX_HOME"])),
                )
                turn = await thread.turn(
                    "Run exactly this command once to test the sandbox: "
                    "python3 -c 'import socket; s=socket.socket(); "
                    'print(s.connect_ex(("1.1.1.1", 443)))\'. '
                    "Report the numeric result. Do not modify files."
                )
                async for event in turn.stream():
                    if event.method != "item/completed":
                        continue
                    item = event.payload.model_dump(exclude_none=True).get("item", {})
                    if item.get("type") in {"commandExecution", "agentMessage"}:
                        print("item:", str(item)[:1000])


if __name__ == "__main__":
    asyncio.run(main())
