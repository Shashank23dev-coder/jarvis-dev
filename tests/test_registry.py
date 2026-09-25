"""Registry validation and metadata-only audit; no model/network calls."""

from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from core.task_store import TaskStore
from tools import CommandResult, RepositoryTools, ToolDenied, ToolError, ToolRegistry
from tools.schemas import ToolResult, ProcessResult


@pytest.fixture
def context(tmp_path):
    (tmp_path / "note.txt").write_text("Evidence from the repository.\n")
    (tmp_path / ".env").write_text("API_KEY=PROTECTED_SECRET")
    store = TaskStore()
    task = store.create_task("Inspect repository")
    return ToolRegistry(RepositoryTools(tmp_path)), store, task


def invoke(context, name, arguments):
    registry, store, task = context
    return registry.execute(name, json.dumps(arguments), task_id=task["id"], task_store=store)


def test_all_schemas_are_strict_and_independent(context):
    registry, _, _ = context
    expected = {"list_files", "read_text", "git_status", "git_diff", "run_pytest", "run_command"}
    assert set(registry.schemas()) == expected
    tools = registry.model_tools()
    assert {tool["name"] for tool in tools} == expected
    for tool in tools:
        schema = tool["parameters"]
        assert tool["type"] == "function" and tool["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema.get("required", [])) == set(schema["properties"])
        result = registry.schemas()[tool["name"]]["result"]
        assert set(result["properties"]) == {"ok", "data", "error"}
        assert result["additionalProperties"] is False
    tools[0]["parameters"]["properties"].clear()
    assert registry.model_tools()[0]["parameters"]["properties"]


@pytest.mark.parametrize("name,arguments,field,expected", [
    ("list_files", {"path": "."}, "files", ["note.txt"]),
    ("read_text", {"path": "note.txt"}, "text", "Evidence from the repository.\n"),
    ("run_command", {"command": ["cat", "note.txt"]}, "stdout", "Evidence from the repository.\n"),
])
def test_real_safe_tools_and_activity(context, name, arguments, field, expected):
    result = invoke(context, name, arguments)
    assert result.ok and result.error is None
    assert result.data.model_dump()[field] == expected
    _, _, task = context
    start, finish = task["activity"][-2:]
    assert start["type"] == "tool_started" and finish["type"] == "tool_completed"
    assert start["invocation_id"] == finish["invocation_id"]
    assert start["started_at"] == finish["started_at"]
    assert finish["tool_name"] == name and finish["task_id"] == task["id"]
    assert finish["duration_ms"] >= 0 and finish["status"] == "completed"
    assert datetime.fromisoformat(finish["completed_at"]) >= datetime.fromisoformat(start["started_at"])
    assert "Evidence from" not in json.dumps(task["activity"])
    assert "note.txt" not in json.dumps(task["activity"])


@pytest.mark.parametrize("name", ["edit_file", "create_file", "delete_file", "git_commit",
                                  "git_checkout", "shell", "_execute", "__class__", "SECRET_NAME", ""])
def test_unauthorized_tools_are_rejected_and_names_not_logged(context, name):
    result = invoke(context, name, {"command": "SECRET_ARGUMENT"})
    assert not result.ok and result.error.code == "unknown_tool"
    activity = context[2]["activity"][-1]
    assert activity["tool_name"] == "unregistered" and activity["type"] == "tool_failed"
    assert "SECRET" not in json.dumps(context[2]["activity"])


@pytest.mark.parametrize("name,args", [
    ("list_files", {}), ("list_files", {"path": 1}), ("read_text", {"path": None}),
    ("read_text", {"path": "note.txt", "root": "/tmp"}),
    ("git_status", {"command": "anything"}), ("git_diff", {"path": None, "staged": "false"}),
    ("git_diff", {"path": None, "staged": 0}), ("git_diff", {"staged": False}),
    ("run_pytest", {"paths": []}), ("run_pytest", {"paths": "tests"}),
    ("run_pytest", {"paths": ["tests"], "timeout": 999}),
    ("run_command", {"command": "pwd"}), ("run_command", {"command": ["pwd"], "env": {}}),
    ("run_command", {"command": ["pwd"], "cwd": "/tmp"}),
    ("read_text", {"path": "x" * 4097}),
])
def test_invalid_argument_shapes(context, name, args):
    result = invoke(context, name, args)
    assert result.error.code == "invalid_arguments"
    assert context[2]["activity"][-1]["error_code"] == "invalid_arguments"


@pytest.mark.parametrize("raw", ["not JSON SECRET", "[]", "null", '"SECRET"',
                                  '{"path":"note.txt","path":".env"}', "[" * 2000,
                                  "x" * 17000, {"path": "note.txt"}])
def test_malformed_json_is_rejected_without_logging_input(context, raw):
    registry, store, task = context
    result = registry.execute("read_text", raw, task_id=task["id"], task_store=store)
    assert result.error.code == "invalid_arguments"
    assert "SECRET" not in json.dumps(task["activity"])


@pytest.mark.parametrize("name,args", [
    ("read_text", {"path": ".env"}), ("read_text", {"path": ".git/config"}),
    ("read_text", {"path": ".venv/pyvenv.cfg"}), ("list_files", {"path": "policies"}),
    ("read_text", {"path": "security.json"}), ("read_text", {"path": "../outside"}),
    ("git_diff", {"path": ".env", "staged": False}),
    ("run_pytest", {"paths": [".env"]}),
    ("run_command", {"command": ["cat", ".env"]}),
    ("run_command", {"command": ["touch", "created"]}),
    ("run_command", {"command": ["rm", "note.txt"]}),
    ("run_command", {"command": ["git", "commit", "-m", "x"]}),
    ("run_command", {"command": ["git", "checkout", "-b", "new"]}),
    ("run_command", {"command": ["bash", "-c", "pwd"]}),
    ("run_command", {"command": ["pwd", ">", "created"]}),
])
def test_existing_policy_is_enforced_through_registry(context, name, args):
    result = invoke(context, name, args)
    assert result.error.code == "denied"
    assert result.data is None
    assert "PROTECTED_SECRET" not in result.model_dump_json()
    assert "PROTECTED_SECRET" not in json.dumps(context[2]["activity"])


def fake_repository(**overrides):
    def success(**kwargs):
        return CommandResult(("test",), 0, "safe")
    methods = {name: success for name in
               ("list_files", "read_text", "git_status", "git_diff", "run_pytest", "run_command")}
    methods.update(overrides)
    return SimpleNamespace(**methods)


@pytest.mark.parametrize("name,args", [("git_status", {}),
                                      ("git_diff", {"path": None, "staged": True}),
                                      ("run_pytest", {"paths": ["tests"]})])
def test_process_tools_dispatch_validated_arguments(context, name, args):
    seen = []
    def handler(**kwargs):
        seen.append(kwargs)
        return CommandResult(("test",), 0, "passed", "")
    registry = ToolRegistry(fake_repository(**{name: handler}))
    _, store, task = context
    result = registry.execute(name, json.dumps(args), task_id=task["id"], task_store=store)
    assert seen == [args] and result.ok
    assert ToolResult[ProcessResult].model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("exception,code", [(ToolDenied("SECRET_ERROR"), "denied"),
                                           (ToolError("SECRET_ERROR"), "tool_failed"),
                                           (RuntimeError("SECRET_ERROR"), "tool_failed")])
def test_exceptions_are_safe_and_logged(context, exception, code):
    def fail(**kwargs):
        raise exception
    registry = ToolRegistry(fake_repository(read_text=fail))
    _, store, task = context
    result = registry.execute("read_text", '{"path":"note.txt"}', task_id=task["id"], task_store=store)
    assert result.error.code == code
    assert "SECRET_ERROR" not in result.model_dump_json() + json.dumps(task["activity"])
    assert task["activity"][-1]["status"] == "failed"


@pytest.mark.parametrize("returncode,timed_out,truncated,code", [
    (1, False, False, "command_failed"), (-9, True, False, "timed_out"),
    (0, False, True, "output_limit"), (-9, False, True, "output_limit"),
])
def test_process_failures_and_outputs_are_not_logged(context, returncode, timed_out, truncated, code):
    raw = CommandResult(("SECRET_COMMAND",), returncode, "SECRET_OUTPUT", "SECRET_ERROR", timed_out, truncated)
    registry = ToolRegistry(fake_repository(run_pytest=lambda **kw: raw))
    _, store, task = context
    result = registry.execute("run_pytest", '{"paths":["tests"]}', task_id=task["id"], task_store=store)
    assert result.error.code == code and result.data.returncode == returncode
    assert "SECRET" not in json.dumps(task["activity"])


def test_unexpected_result_type_fails_closed(context):
    registry = ToolRegistry(fake_repository(read_text=lambda **kw: {"secret": "SECRET_RESULT"}))
    _, store, task = context
    result = registry.execute("read_text", '{"path":"note.txt"}', task_id=task["id"], task_store=store)
    assert result.error.code == "tool_failed" and result.data is None
    assert "SECRET_RESULT" not in json.dumps(task["activity"])


@pytest.mark.parametrize("name,arguments,raw", [
    ("read_text", {"path": "note.txt"}, "x" * 20000),
    ("list_files", {"path": "."}, [f"file_{n}" for n in range(300)]),
    ("git_status", {}, CommandResult(("git", "status"), 0, "x" * 20000, "y" * 20000)),
])
def test_model_results_are_bounded_and_marked(context, name, arguments, raw):
    registry = ToolRegistry(fake_repository(**{name: lambda **kw: raw}))
    _, store, task = context
    result = registry.execute(name, json.dumps(arguments), task_id=task["id"], task_store=store)
    assert result.data.truncated
    assert len(result.model_dump_json()) < 17000


def test_missing_task_or_audit_failure_prevents_execution(context, monkeypatch):
    registry, store, task = context
    with pytest.raises(ValueError):
        registry.execute("read_text", '{"path":"note.txt"}', task_id="missing", task_store=store)
    called = []
    registry = ToolRegistry(fake_repository(read_text=lambda **kw: called.append(kw)))
    def fail_audit(*args, **kwargs):
        raise RuntimeError("Unavailable")
    monkeypatch.setattr(store, "add_tool_activity", fail_audit)
    with pytest.raises(RuntimeError):
        registry.execute("read_text", '{"path":"note.txt"}', task_id=task["id"], task_store=store)
    assert not called
