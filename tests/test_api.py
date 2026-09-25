"""Regression checks run without calling the model or external services."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import main
from core.task_store import TaskStore


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "task_store", TaskStore())
    with TestClient(main.app) as client:
        yield client


def test_root_health_and_empty_lists(client):
    root = client.get("/")
    assert root.status_code == 200
    assert 'id="assistantView"' in root.text
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/tasks").json() == {"tasks": []}
    assert client.get("/activity").json() == {"activity": []}
    assert client.get("/tasks/missing").status_code == 404


def test_task_plan_and_activity_contract(client):
    steps = [{"id": 1, "description": "Review the code", "agent": "reviewer"}]
    with patch.object(main.orchestrator, "run_goal") as run_goal, \
            patch.object(main.orchestrator, "create_plan", return_value={"steps": steps}):
        response = client.post("/task", json={"goal": "Review Jarvis", "mode": "plan"})
    run_goal.assert_not_called()
    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "planned"
    assert task["steps"] == steps
    assert client.get("/tasks").json()["tasks"] == [task]
    assert client.get("/tasks/" + task["id"]).json() == task
    events = client.get("/activity").json()["activity"]
    assert [event["type"] for event in events] == ["completed", "planning", "task_created"]
    assert all(event["task_id"] == task["id"] and event["goal"] == task["goal"] for event in events)


def test_failed_task_remains_visible(client):
    with patch.object(main.orchestrator, "create_plan", side_effect=RuntimeError("Planning unavailable")):
        response = client.post("/task", json={"goal": "Test failure", "mode": "plan"})
    assert response.status_code == 500
    assert client.get("/tasks").json()["tasks"][0]["status"] == "failed"
    event = client.get("/activity").json()["activity"][0]
    assert event["type"] == "error"
    assert event["message"] == "Planning unavailable"
    assert client.post("/task", json={}).status_code == 422


def test_agent_mode_runs_tools_and_exposes_safe_activity(client, monkeypatch, tmp_path):
    from core.agent import AgentRunner
    from core.orchestrator import Orchestrator
    from models.openai_client import AgentTurn
    from tools import RepositoryTools, ToolRegistry
    import json

    (tmp_path / "note.txt").write_text("FILE_CONTENT_NOT_FOR_LOGGING")
    responses = iter([
        AgentTurn(output=[{"type": "function_call", "name": "read_text", "call_id": "call_1",
                           "arguments": '{"path":"note.txt"}'}], text=""),
        AgentTurn(output=[], text="I inspected the repository file."),
    ])
    runner = AgentRunner(ToolRegistry(RepositoryTools(tmp_path)), model=lambda **kw: next(responses))
    monkeypatch.setattr(main, "orchestrator", Orchestrator(runner))
    response = client.post("/task", json={"goal": "Inspect files", "mode": "agent"})
    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "completed" and task["steps"] == []
    assert task["response"] == "I inspected the repository file."
    assert task["agent"] == {"iterations": 2, "tool_calls": 1, "stop_reason": "final_response"}
    assert client.get("/tasks/" + task["id"]).json() == task
    assert client.get("/tasks").json()["tasks"] == [task]
    events = client.get("/activity").json()["activity"]
    finished = next(event for event in events if event["type"] == "tool_completed")
    assert finished["task_id"] == task["id"] and finished["tool_name"] == "read_text"
    assert finished["duration_ms"] >= 0 and finished["started_at"] and finished["completed_at"]
    assert "FILE_CONTENT_NOT_FOR_LOGGING" not in json.dumps(events)


def test_agent_provider_failure_is_sanitized_and_retained(client, monkeypatch):
    from core.agent import AgentRunner
    from core.orchestrator import Orchestrator

    def fail(**kwargs):
        raise RuntimeError("SECRET_PROVIDER_DETAIL")
    monkeypatch.setattr(main, "orchestrator", Orchestrator(AgentRunner(model=fail)))
    response = client.post("/task", json={"goal": "Inspect", "mode": "agent"})
    assert response.status_code == 500 and "SECRET" not in response.text
    assert client.get("/tasks").json()["tasks"][0]["status"] == "failed"
    assert "SECRET" not in client.get("/activity").text


def test_agent_limit_and_mode_validation(client, monkeypatch):
    from core.agent import AgentRunner
    from core.orchestrator import Orchestrator
    from models.openai_client import AgentTurn

    runner = AgentRunner(model=lambda **kw: AgentTurn(output=[], text=""), max_iterations=1)
    monkeypatch.setattr(main, "orchestrator", Orchestrator(runner))
    response = client.post("/task", json={"goal": "Inspect", "mode": "agent", "max_iterations": 1000})
    assert response.status_code == 200
    assert response.json()["status"] == "limit_reached"
    assert response.json()["agent"]["iterations"] == 1
    assert client.post("/task", json={"goal": "Inspect", "mode": "arbitrary"}).status_code == 422


def test_agent_setup_failure_is_not_reflected(client):
    with patch.object(main.orchestrator, "run_goal", side_effect=RuntimeError("SECRET_SETUP_DETAIL")):
        response = client.post("/task", json={"goal": "Inspect", "mode": "agent"})
    assert response.status_code == 500 and "SECRET" not in response.text
    assert client.get("/tasks").json()["tasks"][0]["status"] == "failed"
    assert "SECRET" not in client.get("/activity").text


@pytest.mark.parametrize("mode", [None, "agent"])
@pytest.mark.parametrize("returncode,output", [(0, "7 passed"), (1, "1 failed, 6 passed")])
def test_assistant_inspects_repository_runs_pytest_and_returns_evidence(
        client, monkeypatch, tmp_path, mode, returncode, output):
    """Real API/orchestrator/loop/registry/tools; only model and subprocess are faked."""
    import json
    from pathlib import Path
    from core.agent import AgentRunner
    from core.orchestrator import Orchestrator
    from models.openai_client import AgentTurn
    from tools import RepositoryTools, ToolRegistry
    from tools.repository import CommandResult

    evidence = "Components: FastAPI endpoint, orchestrator, agent loop, repository tools, UI."
    (tmp_path / "README.md").write_text(evidence)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text("def test_sample(): assert True\n")
    repository = RepositoryTools(tmp_path)
    # An existing file satisfies the executable availability check; execution is
    # intercepted below, while snapshot creation and sandbox arguments stay real.
    repository._bwrap = "/usr/bin/true"
    commands = []

    def execute(argv, *, timeout):
        commands.append(argv)
        assert argv[-2:] == ["--", "tests"]
        assert "--unshare-all" in argv and "--clearenv" in argv
        assert timeout == repository.pytest_timeout
        snapshot = Path(argv[argv.index("/workspace") - 1])
        assert (snapshot / "README.md").read_text() == evidence
        return CommandResult(tuple(argv), returncode, output)

    monkeypatch.setattr(repository, "_execute", execute)
    seen = []

    def model(**request):
        history = request["input"]
        results = [json.loads(item["output"]) for item in history
                   if item.get("type") == "function_call_output"]
        seen.append(results)
        if len(results) == 0:
            name, arguments = "list_files", {"path": "."}
        elif len(results) == 1:
            assert "README.md" in results[0]["data"]["files"]
            name, arguments = "read_text", {"path": "README.md"}
        elif len(results) == 2:
            assert results[1]["data"]["text"] == evidence
            name, arguments = "run_pytest", {"paths": ["tests"]}
        else:
            process = results[2]
            assert process["ok"] is (returncode == 0)
            return AgentTurn(output=[], text=(results[1]["data"]["text"] +
                f" Pytest exit code {process['data']['returncode']}: {process['data']['stdout']}"))
        return AgentTurn(output=[{"type": "function_call", "name": name,
            "arguments": json.dumps(arguments), "call_id": f"call_{len(results)}"}], text="")

    orchestrator = Orchestrator(AgentRunner(ToolRegistry(repository), model=model))
    monkeypatch.setattr(main, "orchestrator", orchestrator)
    goal = ("Inspect your own repository, tell me what major components exist, "
            "and run the test suite. Summarize the result.")
    payload = {"goal": goal}
    if mode is not None:
        payload["mode"] = mode
    with patch.object(orchestrator, "create_plan", side_effect=AssertionError("Unexpected planner")):
        response = client.post("/task", json=payload)
    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "completed" and task["steps"] == []
    assert task["response"] == evidence + f" Pytest exit code {returncode}: {output}"
    assert task["agent"]["tool_calls"] == 3
    assert len(commands) == 1 and len(seen) == 4
    assert [event["tool_name"] for event in task["activity"]
            if event["type"] == "tool_started"] == ["list_files", "read_text", "run_pytest"]


def test_default_conversation_answers_without_planning_or_tools(client, monkeypatch):
    from core.agent import AgentRunner
    from core.orchestrator import Orchestrator
    from models.openai_client import AgentTurn

    def model(**request):
        assert request["input"] == [{"role": "user", "content": "Hello!"}]
        assert request["allow_tools"] is True  # The model can choose; tools aren't forced.
        return AgentTurn(output=[], text="Hello! How can I help?")

    runner = AgentRunner(model=model)
    orchestrator = Orchestrator(runner)
    monkeypatch.setattr(main, "orchestrator", orchestrator)
    with patch.object(runner.registry, "execute") as execute, \
            patch.object(orchestrator, "create_plan") as plan:
        response = client.post("/task", json={"goal": "Hello!"})
    execute.assert_not_called()
    plan.assert_not_called()
    assert response.status_code == 200
    task = response.json()
    assert task["response"] == "Hello! How can I help?"
    assert task["steps"] == [] and task["agent"]["tool_calls"] == 0
