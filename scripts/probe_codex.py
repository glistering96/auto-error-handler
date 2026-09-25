"""Manual SDK smoke test using the isolated fixture repository."""

import asyncio
import json
import uuid
from pathlib import Path

from aeh.cli import init_fixture
from aeh.config import Settings
from aeh.gateway import SdkCodexGateway
from aeh.gitops import assert_clean, git, resolve_commit, worktree


async def main() -> None:
    settings = Settings(use_fake_codex=False)
    repo = init_fixture(settings)
    policy = settings.snapshot(settings.load_services()["fixture-api"], str(repo))
    event = json.loads((Path(__file__).resolve().parents[1] / "fixtures/event.json").read_text())
    sha = resolve_commit(repo, None, "main")
    with worktree(repo, settings.workspace_root, "probe", str(uuid.uuid4()), sha) as path:
        result, thread_id = await SdkCodexGateway(settings).analyze(path, event, policy)
        print(
            "worktreeStatus:", git(path, "status", "--porcelain", "--untracked-files=all").decode()
        )
        assert_clean(path)
        print(
            json.dumps(
                {
                    "threadId": thread_id,
                    "summary": result.summary,
                    "confidence": result.confidence,
                    "evidence": [item.path for item in result.evidence],
                    "proposedChanges": [item.path for item in result.proposedChanges],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
