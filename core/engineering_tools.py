"""Closed job tools. Inspection is read-only; controls only change queue state."""
from typing import Annotated, Literal
from pydantic import Field
from tools.schemas import StrictModel
from tools.policy import ToolDenied


class JobArguments(StrictModel):
    job_id: Annotated[str, Field(pattern=r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$')]


class JobListArguments(StrictModel):
    before: Annotated[str, Field(pattern=r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$')] | None = None


class JobEvidence(StrictModel):
    jobs: list[dict]
    next_before: str | None = None


class RuntimeEvidence(StrictModel):
    worker: Literal['active', 'inactive', 'unknown']
    observed_at: float | None
    jobs: list[dict]
    supported: bool
    provider_registry: dict | None = None
    portfolio: dict | None = None
    decisions: list[dict] = []


def project_runtime(value):
    from .engineering_jobs import project_job
    result = RuntimeEvidence.model_validate(value).model_dump()
    result['jobs'] = [project_job(j) for j in result['jobs'][:10]]
    from .engineering_providers import project_registry
    result['provider_registry'] = project_registry(result['provider_registry'])
    from .engineering_portfolio import PortfolioSnapshot
    from .engineering_decision import Decision
    result['portfolio'] = PortfolioSnapshot.model_validate(result['portfolio']).model_dump() if result['portfolio'] is not None else None
    result['decisions'] = [Decision.model_validate(v).model_dump() for v in result['decisions'][:5]]
    return result


def runtime(tools):
    from .engineering_authority import require_owner
    from .engineering_jobs import EngineeringJobs
    require_owner(tools.authority)
    result = (tools._service().invoke('runtime', {}) if tools.production else
              EngineeringJobs(tools.database).runtime())
    return RuntimeEvidence.model_validate(project_runtime(result))


def invoke(tools, operation, job_id=None, *, before=None):
    from .engineering_jobs import EngineeringJobs, project_job
    from .database import valid_id
    from .engineering_authority import require_owner
    require_owner(tools.authority)
    if (operation not in {'list', 'inspect', 'pause', 'cancel', 'resume'}
            or not tools.source_context
            or not all(valid_id(tools.source_context.get(k)) for k in ('task_id', 'conversation_id'))):
        raise ToolDenied('Job source context required')
    if tools.production:
        # Management actions are authorized separately from creation.
        from .candidate_bridge import CandidateClient
        result = CandidateClient(tools.goal, tools.source_context).invoke(
            'job_' + operation, ({'before': before} if before else {}) if operation == 'list' else {'job_id': job_id})
    else:
        jobs = EngineeringJobs(tools.database)
        if operation == 'list':
            result = [project_job(j) for j in jobs.recent(before=before)]
        else:
            job = jobs.get(job_id)
            result = project_job(job if operation == 'inspect' else jobs.control(job_id, operation, authority=tools.authority))
    from .engineering_jobs import JOB_PAGE_SIZE
    return JobEvidence(jobs=result if isinstance(result, list) else [result],
        next_before=result[-1]['id'] if operation == 'list' and len(result) == JOB_PAGE_SIZE else None)
