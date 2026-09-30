"""Closed job tools. Inspection is read-only; controls only change queue state."""
import re
from typing import Annotated
from pydantic import Field
from tools.schemas import StrictModel
from tools.policy import ToolDenied


class JobArguments(StrictModel):
    job_id: Annotated[str, Field(pattern=r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$')]


class JobEvidence(StrictModel):
    jobs: list[dict]


def control_authorized(goal, operation, identifier):
    return bool(re.fullmatch(r'(?:Please )?' + re.escape(operation) +
        r' engineering job ' + re.escape(identifier) + r'[.!]?', goal, re.I))


def invoke(tools, operation, job_id=None):
    from .engineering_jobs import EngineeringJobs, project_job
    from .database import valid_id
    if (operation not in {'list', 'inspect', 'pause', 'cancel', 'resume'}
            or not tools.source_context
            or not all(valid_id(tools.source_context.get(k)) for k in ('task_id', 'conversation_id'))):
        raise ToolDenied('Job source context required')
    if operation not in {'list', 'inspect'} and not control_authorized(tools.goal, operation, job_id):
        raise ToolDenied('Explicit engineering job control required')
    if tools.production:
        # Management actions are authorized separately from creation.
        from .candidate_bridge import CandidateClient
        result = CandidateClient(tools.goal, tools.source_context).invoke(
            'job_' + operation, {} if operation == 'list' else {'job_id': job_id})
    else:
        jobs = EngineeringJobs(tools.database)
        if operation == 'list':
            result = [project_job(j) for j in jobs.recent(tools.source_context['conversation_id'])]
        else:
            job = jobs.get(job_id)
            if job['source']['conversation_id'] != tools.source_context['conversation_id']:
                raise ToolDenied('Job outside conversation')
            result = project_job(job if operation == 'inspect' else jobs.control(job_id, operation))
    return JobEvidence(jobs=result if isinstance(result, list) else [result])
