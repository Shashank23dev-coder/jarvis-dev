from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Query, APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated, Literal

from core.orchestrator import Orchestrator
from core.task_store import TaskStore
from core.deployment import Settings, DeploymentMiddleware
from core.self_edit import SelfEditService
from tools.policy import ToolDenied

@asynccontextmanager
async def lifespan(application):
    store = application.state.store if hasattr(application.state, "store") else task_store
    try:
        application.state.settings.check_storage()
        store.database.initialize()
        with store.database.transaction() as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database integrity check failed')
        logging.getLogger('jarvis.lifecycle').info('startup_ready')
        yield
    finally:
        store.close()
        logging.getLogger('jarvis.lifecycle').info('shutdown_complete')


app = FastAPI(title="Jarvis", lifespan=lifespan)
router = APIRouter()

orchestrator = Orchestrator()
settings = Settings.from_env()
app.state.settings = settings
app.add_middleware(DeploymentMiddleware, settings=settings)
task_store = TaskStore(settings.database_path)


def get_store():
    return task_store


class TaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=32000, pattern=r"[^\s]")
    mode: Literal["plan", "agent"] = "agent"
    conversation_id: str | None = None


@router.api_route("/", methods=["GET", "HEAD"])
def home():
    return FileResponse(Path(__file__).resolve().parent / "static/index.html")


@router.get("/health")
def health(store: TaskStore = Depends(get_store)):
    try:
        with store.database.transaction() as db:
            db.execute('SELECT 1').fetchone()
    except Exception:
        raise HTTPException(status_code=503, detail='Not ready') from None
    return {"status": "ok"}


@router.get("/runtime")
def runtime(request: Request):
    return {"mode": request.app.state.settings.mode}


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


def recall_service(store, limit, max_bytes):
    from core.recall import RecallService, RecallLimits
    return RecallService(store.database, limits=RecallLimits(results=limit, context_bytes=max_bytes))


@router.get("/conversations/search")
def search_conversations(q: str = Query(min_length=1, max_length=500),
                         conversation_id: str | None = None, exclude_conversation_id: str | None = None,
                         limit: int = Query(default=6, ge=1, le=12),
                         max_bytes: int = Query(default=10000, ge=512, le=16000),
                         store: TaskStore = Depends(get_store)):
    try:
        return recall_service(store, limit, max_bytes).search_messages(q,
            conversation_id=conversation_id, exclude_conversation_id=exclude_conversation_id)
    except ValueError:
        raise HTTPException(status_code=422, detail='Invalid search request') from None


@router.get("/conversations/recent")
def recent_conversations(conversation_id: str | None = None, exclude_conversation_id: str | None = None,
                         limit: int = Query(default=6, ge=1, le=12),
                         max_bytes: int = Query(default=10000, ge=512, le=16000),
                         store: TaskStore = Depends(get_store)):
    try:
        return recall_service(store, limit, max_bytes).recent_history(
            conversation_id=conversation_id, exclude_conversation_id=exclude_conversation_id)
    except ValueError:
        raise HTTPException(status_code=422, detail='Invalid recent-history request') from None


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


@router.get("/memories/search")
def search_durable_memories(q: str = Query(min_length=1, max_length=500),
                           scope: Literal["user", "project"] | None = None, scope_id: str | None = None,
                           limit: int = Query(default=6, ge=1, le=12),
                           max_bytes: int = Query(default=10000, ge=512, le=16000),
                           store: TaskStore = Depends(get_store)):
    try:
        return recall_service(store, limit, max_bytes).search_memories(q, scope=scope, scope_id=scope_id)
    except ValueError:
        raise HTTPException(status_code=422, detail='Invalid memory search request') from None


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


def create_app(database_path=None, *, settings=None):
    """Independent app/store lifecycle for trusted deployments and restart tests."""
    application = FastAPI(title="Jarvis", lifespan=lifespan)
    settings = settings or Settings.from_env(database_path=database_path)
    application.state.settings = settings
    application.add_middleware(DeploymentMiddleware, settings=settings)
    application.state.store = TaskStore(settings.database_path)
    application.include_router(router)
    application.dependency_overrides[get_store] = lambda: application.state.store
    return application


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: str = Field(min_length=1, max_length=32000, pattern=r"[^\s]")
    inspect_paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(min_length=1, max_length=8)
    targeted_tests: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(min_length=1, max_length=8)


def get_self_edit(request: Request, store: TaskStore = Depends(get_store)):
    if request.app.state.settings.mode == "production":
        raise HTTPException(status_code=403, detail="Use chat candidate tools; local candidate execution is disabled")
    from core.engineering_authority import current_owner, require_owner
    from core.candidate_broker import Broker
    try:
        require_owner(current_owner.get())
        service = SelfEditService(store.database)
        Broker(service).check_source()
        return service
    except ToolDenied:
        raise HTTPException(status_code=403, detail="Authenticated owner development capability required") from None


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


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmation: str = Field(min_length=1, max_length=200)


@router.get("/candidates/{candidate_id}/apply/preview")
def preview_candidate_apply(candidate_id: str, service: SelfEditService = Depends(get_self_edit)):
    from core.candidate_apply import CandidateApply
    return candidate_action(service, candidate_id, CandidateApply(service).preview)


@router.post("/candidates/{candidate_id}/apply")
def apply_candidate(candidate_id: str, request: ApplyRequest,
                    service: SelfEditService = Depends(get_self_edit)):
    from core.candidate_apply import CandidateApply
    return candidate_action(service, candidate_id,
                            lambda identifier: CandidateApply(service).apply(identifier, request.confirmation))


@router.get("/candidates/{candidate_id}/apply")
def inspect_candidate_apply(candidate_id: str, service: SelfEditService = Depends(get_self_edit)):
    from core.candidate_apply import CandidateApply
    return candidate_action(service, candidate_id, CandidateApply(service).inspect)


@router.get("/experiences")
def list_experiences(limit: int = Query(20, ge=1, le=50), store: TaskStore = Depends(get_store)):
    return store.experiences.list(limit=limit)


@router.get("/experiences/search")
def search_experiences(q: str = Query(min_length=1, max_length=500),
                       limit: int = Query(3, ge=1, le=5),
                       context_bytes: int = Query(6000, ge=1024, le=10000),
                       store: TaskStore = Depends(get_store)):
    from core.experience import ExperienceStore, ExperienceLimits
    try:
        return ExperienceStore(store.database, limits=ExperienceLimits(
            results=limit, context_bytes=context_bytes)).search(q)
    except ValueError:
        raise HTTPException(status_code=422, detail='Invalid experience search request') from None


@router.get("/experiences/{experience_id}")
def inspect_experience(experience_id: str, store: TaskStore = Depends(get_store)):
    experience = store.experiences.get(experience_id)
    if experience is None:
        raise HTTPException(status_code=404, detail="Experience not found")
    return experience


@router.delete("/experiences/{experience_id}")
def delete_experience(experience_id: str, store: TaskStore = Depends(get_store)):
    if not store.experiences.delete(experience_id):
        raise HTTPException(status_code=404, detail="Experience not found")
    return {"deleted": True}


class ExperienceFeedback(BaseModel):
    model_config = {"extra": "forbid"}
    verdict: Literal['helpful', 'misleading']
    request_id: str = Field(min_length=36, max_length=36)


@router.get('/experiences/{experience_id}/evaluations')
def experience_evaluations(experience_id: str, limit: int = Query(20, ge=1, le=50),
                          before: int | None = Query(None, ge=1), store: TaskStore = Depends(get_store)):
    experience = inspect_experience(experience_id, store)
    return dict(quality=experience['quality'],
                **store.experiences.evaluation.history(experience_id, limit=limit, before=before))


@router.post('/experiences/{experience_id}/feedback')
def experience_feedback(experience_id: str, feedback: ExperienceFeedback, store: TaskStore = Depends(get_store)):
    inspect_experience(experience_id, store)
    try:
        return store.experiences.evaluation.feedback(experience_id, feedback.verdict, feedback.request_id)
    except ValueError:
        raise HTTPException(status_code=422, detail='Invalid experience feedback') from None


@router.post('/experiences/{experience_id}/confidence/reset')
def reset_experience_confidence(experience_id: str, store: TaskStore = Depends(get_store)):
    inspect_experience(experience_id, store)
    return store.experiences.evaluation.reset(experience_id)


def get_release_service():
    from core.deployment_release import configured_service
    try:
        return configured_service()
    except Exception:
        raise HTTPException(status_code=409, detail='Deployment controller unavailable') from None


def release_action(action):
    try:
        return action()
    except Exception:
        raise HTTPException(status_code=409, detail='Deployment operation denied or failed') from None


def get_release_inspector():
    from core.deployment_status import configured_inspector
    try:
        return configured_inspector()
    except Exception:
        raise HTTPException(status_code=409, detail='Deployment inspection unavailable') from None


@router.get('/deployments/status')
def deployment_status(service=Depends(get_release_inspector)):
    return release_action(service.status)


@router.get('/deployments')
def deployment_history(service=Depends(get_release_inspector)):
    return release_action(service.history)


@router.get('/deployments/preview/{commit}')
def deployment_preview(commit: str, service=Depends(get_release_inspector)):
    return release_action(lambda: service.preview(commit))


@router.get('/deployments/{deployment_id}')
def deployment_inspect(deployment_id: str, service=Depends(get_release_inspector)):
    return release_action(lambda: service.inspect(deployment_id))


@router.post('/deployments/{commit}/deploy')
def deployment_approve(commit: str, request: ApplyRequest, service=Depends(get_release_service)):
    return release_action(lambda: service.deploy(commit, request.confirmation))


app.include_router(router)
