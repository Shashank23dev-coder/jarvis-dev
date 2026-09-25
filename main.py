from contextlib import asynccontextmanager
import json

from fastapi import FastAPI, HTTPException, Depends, Query, APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated, Literal

from core.orchestrator import Orchestrator
from core.task_store import TaskStore
from core.database import default_database_path
from core.self_edit import SelfEditService
from tools.policy import ToolDenied

@asynccontextmanager
async def lifespan(application):
    store = application.state.store if hasattr(application.state, "store") else task_store
    store.database.initialize()
    try:
        yield
    finally:
        store.close()


app = FastAPI(title="Jarvis", lifespan=lifespan)
router = APIRouter()

orchestrator = Orchestrator()
task_store = TaskStore(default_database_path())


def get_store():
    return task_store


class TaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=32000, pattern=r"[^\s]")
    mode: Literal["plan", "agent"] = "agent"
    conversation_id: str | None = None


@router.get("/")
def home():
    return FileResponse("static/index.html")


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/tasks")
def list_tasks(store: TaskStore = Depends(get_store)):
    return {
        "tasks": store.list_tasks()
    }


@router.get("/tasks/{task_id}")
def get_task(task_id: str, store: TaskStore = Depends(get_store)):
    task = store.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=404,
            detail="Task not found"
        )

    return task


@router.get("/activity")
def activity(store: TaskStore = Depends(get_store)):
    events = []

    for task in store.list_tasks():
        for event in task["activity"]:
            events.append({
                **event,
                "task_id": task["id"],
                "goal": task["goal"],
            })

    events.sort(
        key=lambda item: item["timestamp"],
        reverse=True
    )

    return {"activity": events}


@router.post("/task")
def create_task(task: TaskRequest, store: TaskStore = Depends(get_store)):
    if task.conversation_id is not None and store.memory.get_conversation(task.conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if "\x00" in task.goal:
        raise HTTPException(status_code=422, detail="Invalid goal")
    new_task = store.create_task(task.goal, task.conversation_id)

    if task.mode == "agent":
        try:
            result = orchestrator.run_goal(task.goal, task_id=new_task["id"], task_store=store)
        except Exception:
            # Never reflect provider, validation, or setup exceptions into activity/HTTP.
            message = "Jarvis could not complete the agent request."
            store.set_status(new_task["id"], "failed")
            store.add_activity(new_task["id"], message, "agent_failed")
            store.update_task(new_task["id"], response=message)
            raise HTTPException(status_code=500, detail=message,
                                headers={"X-Conversation-ID": new_task["conversation_id"]}) from None
        if result.status == "failed":
            raise HTTPException(status_code=500, detail=result.response,
                                headers={"X-Conversation-ID": new_task["conversation_id"]})
        return new_task

    store.set_status(
        new_task["id"],
        "planning"
    )

    store.add_activity(
        new_task["id"],
        "Jarvis started planning",
        "planning"
    )

    try:
        plan = orchestrator.create_plan(task.goal)

        store.update_task(new_task["id"], steps=plan.get("steps", []))
        store.add_assistant_message(new_task["id"], json.dumps(plan))

        store.set_status(
            new_task["id"],
            "planned"
        )

        store.add_activity(
            new_task["id"],
            "Planning completed",
            "completed"
        )

        return new_task

    except Exception as exc:
        store.set_status(
            new_task["id"],
            "failed"
        )

        store.add_activity(
            new_task["id"],
            str(exc),
            "error"
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


@router.post("/conversations")
def create_conversation(store: TaskStore = Depends(get_store)):
    return store.memory.create_conversation()


@router.get("/conversations")
def list_conversations(store: TaskStore = Depends(get_store)):
    return {"conversations": store.memory.list_conversations()}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, store: TaskStore = Depends(get_store)):
    conversation = store.memory.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.get("/conversations/{conversation_id}/messages")
def conversation_history(conversation_id: str, store: TaskStore = Depends(get_store)):
    get_conversation(conversation_id, store)
    return {"messages": store.memory.conversation_history(conversation_id)}


class MemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope: Literal["user", "project"]
    scope_id: str = Field(min_length=1, max_length=200, pattern=r"[^\s]")
    content: str = Field(min_length=1, max_length=32000, pattern=r"[^\s]")


@router.post("/memories")
def create_memory(memory: MemoryRequest, store: TaskStore = Depends(get_store)):
    try:
        return store.memory.create_memory(**memory.model_dump())
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid memory") from None


@router.get("/memories")
def list_memories(scope: Literal["user", "project"],
                  scope_id: str = Query(min_length=1, max_length=200),
                  store: TaskStore = Depends(get_store)):
    try:
        return {"memories": store.memory.list_memories(scope=scope, scope_id=scope_id)}
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid memory scope") from None


@router.get("/memories/{memory_id}")
def get_memory(memory_id: str, store: TaskStore = Depends(get_store)):
    memory = store.memory.get_memory(memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: str, store: TaskStore = Depends(get_store)):
    if not store.memory.delete_memory(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True}


def create_app(database_path):
    """Independent app/store lifecycle for trusted deployments and restart tests."""
    application = FastAPI(title="Jarvis", lifespan=lifespan)
    application.state.store = TaskStore(database_path)
    application.include_router(router)
    application.dependency_overrides[get_store] = lambda: application.state.store
    return application


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: str = Field(min_length=1, max_length=32000, pattern=r"[^\s]")
    inspect_paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(min_length=1, max_length=8)
    targeted_tests: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(min_length=1, max_length=8)


def get_self_edit(store: TaskStore = Depends(get_store)):
    return SelfEditService(store.database)


@router.post("/candidates")
def start_candidate(request: CandidateRequest, service: SelfEditService = Depends(get_self_edit)):
    try:
        return service.start(request.goal, request.inspect_paths, request.targeted_tests)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid candidate request") from None


@router.get("/candidates/{candidate_id}")
def inspect_candidate(candidate_id: str, service: SelfEditService = Depends(get_self_edit)):
    candidate = service.inspect(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


def candidate_action(service, candidate_id, action):
    if service.store.get(candidate_id) is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    try:
        return action(candidate_id)
    except (ValueError, ToolDenied):
        raise HTTPException(status_code=409, detail="Candidate operation unavailable") from None


@router.post("/candidates/{candidate_id}/tests")
def test_candidate(candidate_id: str, service: SelfEditService = Depends(get_self_edit)):
    return candidate_action(service, candidate_id, service.tests)


@router.post("/candidates/{candidate_id}/review")
def review_candidate(candidate_id: str, service: SelfEditService = Depends(get_self_edit)):
    return candidate_action(service, candidate_id, service.review)


@router.delete("/candidates/{candidate_id}")
def discard_candidate(candidate_id: str, service: SelfEditService = Depends(get_self_edit)):
    return candidate_action(service, candidate_id, service.discard)


app.include_router(router)
