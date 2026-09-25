from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Limits:
    body_bytes = 256 * 1024
    reproduction_depth = 8
    reproduction_values = 1000
    reproduction_string = 4096
    analysis_bytes = 128 * 1024
    diff_bytes = 512 * 1024
    stream_bytes = 32 * 1024
    error_bytes = 2 * 1024
    max_changed_files = 10
    max_diff_lines = 400
    analysis_seconds = 86400
    patch_seconds = 900
    command_seconds = 300
    job_limit = 100
    lease_seconds = 120
    heartbeat_seconds = 30
    max_attempts = 3


class ValidationCommand(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda s: {"timeout_seconds": "timeoutSeconds"}.get(s, s)
    )
    id: str = Field(min_length=1, max_length=100)
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=300, ge=1, le=Limits.command_seconds)

    @field_validator("argv")
    @classmethod
    def safe_argv(cls, value: list[str]) -> list[str]:
        if any(not arg or "\x00" in arg or "\n" in arg for arg in value):
            raise ValueError("validation argv contains an invalid argument")
        if value[0] in {"sh", "bash", "zsh", "curl", "wget", "git"}:
            raise ValueError("shell, network, and git commands are not validation commands")
        return value


class ServicePolicy(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=lambda s: {
            "repository_path": "repositoryPath",
            "default_branch": "defaultBranch",
            "runbook_paths": "runbookPaths",
            "allowed_paths": "allowedPaths",
            "denied_paths": "deniedPaths",
            "max_changed_files": "maxChangedFiles",
            "max_diff_lines": "maxDiffLines",
            "analysis_timeout_seconds": "analysisTimeoutSeconds",
            "patch_timeout_seconds": "patchTimeoutSeconds",
            "validation_commands": "validationCommands",
        }.get(s, s),
    )
    repository_path: str
    default_branch: str = "main"
    runbook_paths: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(min_length=1)
    denied_paths: list[str] = Field(default_factory=list)
    max_changed_files: int = Field(default=10, ge=1, le=Limits.max_changed_files)
    max_diff_lines: int = Field(default=400, ge=1, le=Limits.max_diff_lines)
    analysis_timeout_seconds: int = Field(default=86400, ge=1, le=Limits.analysis_seconds)
    patch_timeout_seconds: int = Field(default=900, ge=1, le=Limits.patch_seconds)
    validation_commands: list[ValidationCommand] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_paths(self) -> ServicePolicy:
        repository = Path(self.repository_path)
        if repository.is_absolute() or ".." in repository.parts or not self.repository_path:
            raise ValueError("repositoryPath must be relative to REPOSITORY_ROOT")
        for pattern in self.allowed_paths + self.denied_paths + self.runbook_paths:
            if (
                not pattern
                or pattern.startswith("/")
                or ".." in Path(pattern).parts
                or "\x00" in pattern
            ):
                raise ValueError("policy paths must be relative and cannot traverse parents")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite+pysqlite:///./data/auto-error-handler.db"
    service_config_path: Path = Path("services.example.yaml")
    repository_root: Path = Path("/tmp/aeh-repositories")
    workspace_root: Path = Path("/tmp/aeh-workspaces")
    codex_model: str = "gpt-6-sol"
    codex_bin: str | None = None
    codex_auth_mode: Literal["local", "api-key"] = "local"
    use_fake_codex: bool = False
    worker_id: str = Field(
        default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    worker_concurrency: int = Field(default=1, ge=1, le=8)
    worker_poll_seconds: float = Field(default=1.0, ge=0.1)
    cursor_secret: str = "local-dev-cursor-secret"

    def load_services(self) -> dict[str, ServicePolicy]:
        raw = yaml.safe_load(self.service_config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("services"), dict):
            raise TypeError("service config requires a services mapping")
        return {key: ServicePolicy.model_validate(value) for key, value in raw["services"].items()}

    def repository_path(self, policy: ServicePolicy) -> Path:
        return self.resolve_repository_reference(policy.repository_path)

    def resolve_repository_reference(self, reference: str) -> Path:
        root = self.repository_root.resolve(strict=True)
        path = (root / reference).resolve(strict=True)
        if path != root and root not in path.parents:
            raise ValueError("repository is outside REPOSITORY_ROOT")
        if not (path / ".git").exists():
            raise ValueError("repository is not a Git repository")
        return path

    def snapshot(self, policy: ServicePolicy, repository_path: str) -> dict:
        data = policy.model_dump(by_alias=True, exclude={"repository_path", "default_branch"})
        data.update(
            repositoryPath=repository_path,
            model=self.codex_model,
            resultSchemaVersion=1,
            commandNetworkAccess=False,
        )
        return data
