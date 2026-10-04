"""Strict agent-facing argument and result contracts (exportable as JSON Schema)."""

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


PathArgument = Annotated[str, Field(min_length=1, max_length=4096)]


class ListFilesArguments(StrictModel):
    path: PathArgument = Field(description="Relative directory; use . for the repository root.")


class ReadTextArguments(StrictModel):
    path: PathArgument


class GitStatusArguments(StrictModel):
    pass


class GitDiffArguments(StrictModel):
    path: PathArgument | None = Field(description="Relative file, or null for all allowed tracked files.")
    staged: bool


class PytestArguments(StrictModel):
    paths: Annotated[list[PathArgument], Field(min_length=1, max_length=32)]


class CommandArguments(StrictModel):
    command: Annotated[list[PathArgument], Field(min_length=1, max_length=34)] = Field(
        description="Argument vector for pwd, ls, cat, git status/diff, or pytest. No shell syntax.")


class FileListResult(StrictModel):
    files: list[str]
    truncated: bool


class TextResult(StrictModel):
    text: str
    truncated: bool


class ProcessResult(StrictModel):
    sandbox: dict = Field(default_factory=dict)
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool


ErrorCode = Literal[
    "unknown_tool", "invalid_arguments", "denied", "tool_failed",
    "command_failed", "timed_out", "output_limit", "limit_reached", "invalid_call",
]


class ToolFailure(StrictModel):
    code: ErrorCode
    message: str
    gate: str | None = None
    reason: str | None = None


ResultData = TypeVar("ResultData", bound=StrictModel)


class ToolResult(StrictModel, Generic[ResultData]):
    ok: bool
    data: ResultData | None
    error: ToolFailure | None
