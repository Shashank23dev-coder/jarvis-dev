"""Fixed safe-tool dispatch, strict validation and mandatory metadata-only audit."""

from dataclasses import dataclass
import json
from time import monotonic
from types import MappingProxyType
from typing import Callable, TYPE_CHECKING
from uuid import uuid4

from .policy import ToolDenied
from .repository import CommandResult, RepositoryTools
from .schemas import (
    CommandArguments, FileListResult, GitDiffArguments, GitStatusArguments,
    ListFilesArguments, ProcessResult, PytestArguments, ReadTextArguments,
    StrictModel, TextResult, ToolFailure, ToolResult,
)

if TYPE_CHECKING:
    from core.task_store import TaskStore


ERROR_MESSAGES = MappingProxyType({
    "unknown_tool": "The requested tool is not registered.",
    "invalid_arguments": "Arguments do not match the tool schema.",
    "denied": "Repository policy denied the operation.",
    "tool_failed": "The tool could not complete the operation.",
    "command_failed": "The command exited unsuccessfully.",
    "timed_out": "The command exceeded its time limit.",
    "output_limit": "The command exceeded its output limit.",
    "limit_reached": "The agent execution limit was reached.",
    "invalid_call": "The model returned an invalid tool call.",
})


@dataclass(frozen=True)
class ToolDefinition:
    description: str
    arguments: type[StrictModel]
    result: type[StrictModel]
    handler: Callable


class ToolRegistry:
    """Only trusted host code constructs this registry and its repository tools.

    There is no model-controlled registration, getattr dispatch, root, environment,
    policy, timeout, or executable override. Each execute/reject requires a task.
    """

    MAX_ARGUMENT_BYTES = 16_384
    MAX_RESULT_CHARS = 16_000

    def __init__(self, repository: RepositoryTools | None = None):
        repository = repository or RepositoryTools()
        self._definitions = MappingProxyType({
            "list_files": ToolDefinition(
                "List allowed repository files recursively. Protected paths and links are omitted.",
                ListFilesArguments, FileListResult, repository.list_files),
            "read_text": ToolDefinition(
                "Read a bounded, allowed UTF-8 repository file. Protected paths and links are denied.",
                ReadTextArguments, TextResult, repository.read_text),
            "git_status": ToolDefinition(
                "Inspect filtered repository Git status without changing the index.",
                GitStatusArguments, ProcessResult, repository.git_status),
            "git_diff": ToolDefinition(
                "Inspect allowed tracked file changes; no external helpers or Git mutations.",
                GitDiffArguments, ProcessResult, repository.git_diff),
            "run_pytest": ToolDefinition(
                "Run tests in a read-only Bubblewrap snapshot with no network or host credentials. "
                "Fails closed if isolation is unavailable. Only test paths, never pytest options.",
                PytestArguments, ProcessResult, repository.run_pytest),
            "run_command": ToolDefinition(
                "Run only allowlisted pwd, ls, cat, git status/diff, or sandboxed pytest. "
                "No arbitrary commands, shell syntax, file writes, or Git changes.",
                CommandArguments, ProcessResult, repository.run_command),
        })

    def schemas(self) -> dict[str, dict]:
        """Return independent JSON Schemas for each tool's arguments and envelope."""
        return {name: {"arguments": definition.arguments.model_json_schema(),
                       "result": ToolResult[definition.result].model_json_schema()}
                for name, definition in self._definitions.items()}

    def model_tools(self) -> list[dict]:
        return [{"type": "function", "name": name, "description": definition.description,
                 "parameters": definition.arguments.model_json_schema(), "strict": True}
                for name, definition in self._definitions.items()]

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def _arguments(self, definition: ToolDefinition, raw: str) -> dict:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > self.MAX_ARGUMENT_BYTES:
            raise ValueError("Invalid argument encoding or size")
        values = json.loads(raw, object_pairs_hook=self._unique_object)
        return definition.arguments.model_validate(values).model_dump()

    def _data(self, definition: ToolDefinition, raw) -> StrictModel:
        limit = self.MAX_RESULT_CHARS
        if definition.result is FileListResult:
            # Validate before capping; malformed results must never reach the model.
            data = FileListResult(files=raw, truncated=False)
            files, size = [], 0
            for name in data.files:
                size += len(name)
                if size > limit or len(files) >= 200:
                    break
                files.append(name)
            return FileListResult(files=files, truncated=len(files) < len(data.files))
        if definition.result is TextResult:
            data = TextResult(text=raw, truncated=False)
            return TextResult(text=data.text[:limit], truncated=len(data.text) > limit)
        if isinstance(raw, StrictModel) and type(raw) is definition.result:
            return raw
        if not isinstance(raw, CommandResult):
            raise ValueError("Unexpected process result")
        data = ProcessResult(returncode=raw.returncode, stdout=raw.stdout, stderr=raw.stderr,
                             timed_out=raw.timed_out, truncated=raw.truncated)
        stdout = data.stdout[:limit]
        stderr = data.stderr[:limit - len(stdout)]
        return ProcessResult(returncode=data.returncode, stdout=stdout, stderr=stderr,
                             timed_out=data.timed_out,
                             truncated=data.truncated or len(stdout) + len(stderr) < len(data.stdout) + len(data.stderr))

    @staticmethod
    def _summary(result: ToolResult) -> str:
        # Never interpolate tool output, arguments, model names, call IDs or exceptions.
        if isinstance(result.data, FileListResult):
            return f"Returned {len(result.data.files)} file names; truncated={result.data.truncated}."
        if isinstance(result.data, TextResult):
            return f"Returned {len(result.data.text)} text characters; truncated={result.data.truncated}."
        if isinstance(result.data, ProcessResult):
            data = result.data
            return (f"Exit code {data.returncode}; output characters {len(data.stdout) + len(data.stderr)}; "
                    f"timed_out={data.timed_out}; truncated={data.truncated}.")
        if result.data is not None:
            return "Returned candidate status and bounded evidence."
        return result.error.message

    def execute(self, name: str, arguments: str, *, task_id: str,
                task_store: "TaskStore") -> ToolResult:
        return self._invoke(name, arguments, task_id=task_id, task_store=task_store)

    def reject(self, name: str, code: str, *, task_id: str,
               task_store: "TaskStore") -> ToolResult:
        """Audit a request blocked by the loop without invoking any handler."""
        if code not in {"limit_reached", "invalid_call"}:
            raise ValueError("Invalid rejection code")
        return self._invoke(name, "", task_id=task_id, task_store=task_store, rejection=code)

    def _invoke(self, name, arguments, *, task_id, task_store, rejection=None):
        if task_store.get_task(task_id) is None:
            raise ValueError("Tool execution requires an existing task")
        definition = self._definitions.get(name) if isinstance(name, str) else None
        safe_name = name if definition else "unregistered"
        invocation_id = str(uuid4())
        started_at = task_store.timestamp()
        started = monotonic()
        task_store.add_tool_activity(
            task_id, invocation_id=invocation_id, tool_name=safe_name, status="started",
            started_at=started_at, duration_ms=0, summary="Tool request received.")
        data = None
        code = rejection
        if not code:
            if definition is None:
                code = "unknown_tool"
            else:
                try:
                    values = self._arguments(definition, arguments)
                except (ValueError, TypeError, RecursionError):
                    code = "invalid_arguments"
                else:
                    try:
                        data = self._data(definition, definition.handler(**values))
                        if isinstance(data, ProcessResult):
                            if data.timed_out:
                                code = "timed_out"
                            elif data.truncated:
                                code = "output_limit"
                            elif data.returncode:
                                code = "command_failed"
                    except ToolDenied:
                        code = "denied"
                    except Exception:
                        # Includes schema validation failures and infrastructure errors.
                        # Do not expose exception strings or Pydantic input/error details.
                        code = "tool_failed"
        result_type = ToolResult[definition.result] if definition else ToolResult[TextResult]
        result = result_type(ok=code is None, data=data,
                             error=ToolFailure(code=code, message=ERROR_MESSAGES[code]) if code else None)
        task_store.add_tool_activity(
            task_id, invocation_id=invocation_id, tool_name=safe_name,
            status="completed" if result.ok else "failed", started_at=started_at,
            duration_ms=round(max(0, monotonic() - started) * 1000, 3),
            summary=self._summary(result), error_code=code)
        return result
