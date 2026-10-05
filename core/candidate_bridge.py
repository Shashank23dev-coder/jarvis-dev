"""Bounded owner mailbox. Fixed project locations are metadata, never authority."""
import json
import os
from pathlib import Path
import stat
import time
from uuid import uuid4

from pydantic import Field
from tools.schemas import StrictModel
from tools.policy import ToolDenied
from .candidate_tools import CreateArguments, CandidateArguments, EngineeringArguments, PrepareEngineeringArguments
from .database import valid_id
from .candidate_action import EngineeringAction, EngineeringWorkspace
from .engineering_authority import OwnerDevAuthority, require_owner

ROOT = Path('/tmp/jarvis-candidate-mailbox')
MAX_BYTES = 48000


class Request(StrictModel):
    request_id: str
    task_id: str
    conversation_id: str
    goal: str = Field(min_length=1, max_length=32000)
    action: str
    arguments: dict
    created: float
    authority: OwnerDevAuthority
    engineering_action: EngineeringAction | None = None

    def checked(self):
        # Serialized authority is audit metadata. Authentication is checked at
        # the HTTP perimeter and the host's private channel, never from JSON.
        if not all(valid_id(v) for v in (self.request_id, self.task_id, self.conversation_id)):
            raise ToolDenied('Invalid request identity', reason='invalid_request')
        if not 0 <= time.time() - self.created <= 330:
            raise ToolDenied('Expired request', reason='expired_request')
        from .engineering_tools import JobArguments, JobListArguments
        from .engineering_decision import DecisionArguments
        from .engineering_portfolio import ProjectArguments
        from .engineering_repository import RepositoryArguments
        schema = (RepositoryArguments if self.action == 'project_repository' else ProjectArguments if self.action == 'project_inspect' else DecisionArguments if self.action == 'decision' else JobListArguments if self.action == 'job_list' else
                  PrepareEngineeringArguments if self.action == 'workspace' else StrictModel if self.action in {'runtime', 'release_prepare'} else
                  EngineeringArguments if self.action in {'engineer', 'job_create'} else
                  JobArguments if self.action.startswith('job_') else
                  CreateArguments if self.action == 'create' else CandidateArguments)
        if self.action not in {'create', 'workspace', 'engineer', 'inspect', 'tests', 'review', 'discard', 'runtime',
                              'job_create', 'job_list', 'job_inspect', 'job_pause', 'job_cancel', 'job_resume', 'decision', 'project_inspect', 'project_repository', 'preview', 'audit', 'apply', 'release_prepare'}:
            raise ToolDenied('Unsupported candidate operation', reason='unsupported_action')
        schema.model_validate(self.arguments)
        if self.engineering_action is not None:
            self.engineering_action.check(self.goal, self.model_dump())
        return self


def root():
    ROOT.mkdir(mode=0o700, exist_ok=True)
    info = ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or ROOT.resolve() != ROOT or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ToolDenied('Unsafe mailbox', reason='mailbox_unsafe')
    return ROOT


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise ToolDenied('Unsafe mailbox entry', reason='mailbox_unsafe')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ToolDenied('Mailbox size exceeded', reason='mailbox_limit')
    return json.loads(data)


def write(path, data):
    encoded = json.dumps(data, allow_nan=False).encode()
    if len(encoded) > MAX_BYTES:
        raise ToolDenied('Mailbox size exceeded', reason='mailbox_limit')
    temp = path.parent / (str(uuid4()) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class CandidateClient:
    def __init__(self, goal, context, *, engineering_action=None):
        self.goal, self.context = goal, context
        self.engineering_action = engineering_action

    def invoke(self, action, arguments):
        from .engineering_authority import current_owner
        authority = require_owner(current_owner.get())
        if not self.context:
            raise ToolDenied('Source task metadata required', reason='source_metadata_required')
        context = dict(self.context, authority=authority.model_dump())
        request = Request(request_id=str(uuid4()), goal=self.goal, action=action,
                          arguments=arguments, created=time.time(),
                          engineering_action=(self.engineering_action if action in
                              {'workspace', 'engineer', 'job_create'} else None), **context).checked()
        directory = root()
        # Bound concurrent waiting callers and refuse rather than evicting work.
        if len(list(directory.iterdir())) >= 16:
            raise ToolDenied('Candidate mailbox is full', reason='mailbox_full')
        incoming = directory / (request.request_id + '.request')
        outgoing = directory / (request.request_id + '.response')
        write(incoming, request.model_dump())
        try:
            deadline = time.monotonic() + (1300 if action == 'release_prepare' else 310)
            while time.monotonic() < deadline:
                if outgoing.exists():
                    result = read(outgoing)
                    if result.get('request_id') != request.request_id:
                        raise ToolDenied('Invalid broker response', reason='invalid_response')
                    if not result.get('ok'):
                        # Use only closed host diagnostic codes, never raw prose.
                        error = result.get('error') or {}
                        if not isinstance(error, dict):
                            error = {}
                        reason = error.get('reason')
                        if (not isinstance(reason, str) or reason not in ToolDenied.reasons or
                                error.get('gate') != ToolDenied.reasons[reason]):
                            reason = 'legacy_rejection'
                        raise ToolDenied('Candidate broker denied the request', reason=reason)
                    if action == 'runtime':
                        from .engineering_tools import project_runtime
                        return project_runtime(result['record'])
                    if action == 'release_prepare':
                        from .candidate_broker import ReleasePreparation
                        return ReleasePreparation.model_validate(result['record']).model_dump()
                    if action == 'decision':
                        from .engineering_decision import Decision
                        return Decision.model_validate(result['record']).model_dump()
                    if action == 'project_inspect':
                        from .engineering_portfolio import ProjectEvidence
                        return ProjectEvidence.model_validate(result['record']).model_dump()
                    if action == 'project_repository':
                        from .engineering_repository import project_result
                        return project_result(result['record'])
                    if action.startswith('job_'):
                        from .engineering_jobs import project_job
                        return ([project_job(j) for j in result['record']] if action == 'job_list'
                                else project_job(result['record']))
                    if action in {'preview', 'audit', 'apply'}:
                        from .candidate_broker import project_apply
                        return project_apply(result['record'])
                    return (EngineeringWorkspace.model_validate(result['record']).checked().model_dump()
                            if action == 'workspace' else project(result['record']))
                time.sleep(.2)
            raise ToolDenied('Candidate broker unavailable or timed out; inspect before retrying', reason='unavailable')
        finally:
            incoming.unlink(missing_ok=True)
            outgoing.unlink(missing_ok=True)

    def start(self, goal, inspect_paths, targeted_tests):
        if goal != self.goal:
            raise ToolDenied('Goal mismatch')
        return self.invoke('create', dict(inspect_paths=inspect_paths, targeted_tests=targeted_tests))

    def engineering_workspace(self, scope):
        if not self.engineering_action or scope != self.engineering_action.scope:
            raise ToolDenied('Workspace action mismatch')
        return self.invoke('workspace', {})

    def inspect(self, candidate_id):
        return self.invoke('inspect', dict(candidate_id=candidate_id))

    def engineer(self, goal, inspect_paths, targeted_tests, problem_detected, hypothesis):
        if goal != self.goal:
            raise ToolDenied('Goal mismatch')
        return self.invoke('engineer', dict(inspect_paths=inspect_paths, targeted_tests=targeted_tests,
                                          problem_detected=problem_detected, hypothesis=hypothesis))

    def tests(self, candidate_id):
        return self.invoke('tests', dict(candidate_id=candidate_id))

    def review(self, candidate_id):
        return self.invoke('review', dict(candidate_id=candidate_id))

    def discard(self, candidate_id):
        return self.invoke('discard', dict(candidate_id=candidate_id))


def project(record):
    """Only structured status reaches production: no diff, logs, paths or prose."""
    if not valid_id(record['id']) or record['state'] not in {'created', 'passed', 'needs_revision', 'rejected', 'discarded'}:
        raise ToolDenied('Invalid candidate result')
    tests = []
    for item in record.get('test_results', [])[-3:]:
        if item.get('phase') not in {'targeted', 'full_python', 'full_ui'} or type(item.get('returncode')) is not int:
            raise ToolDenied('Invalid test result')
        tests.append(dict(phase=item['phase'], returncode=item['returncode'],
                          timed_out=bool(item.get('timed_out')), truncated=bool(item.get('truncated'))))
    verdict = (record.get('reviewer_result') or {}).get('verdict')
    if verdict not in {None, 'passed', 'needs_revision', 'rejected'}:
        raise ToolDenied('Invalid review result')
    summary = record.get('diff_summary') or {}
    summary = {k: v for k, v in summary.items() if k in {'files', 'added', 'deleted'} and type(v) is int and 0 <= v <= 400}
    from .candidate_provider import project_failure, project_provider_results
    from .candidate_provider import work_attribution, WorkAttribution
    from .engineering_providers import ProviderBlocker
    projected = dict(id=record['id'], state=record['state'], busy=bool(record.get('busy')),
                failure=project_failure(record.get('failure')),
                failures=[project_failure(item) for item in record.get('failures', [])[-8:]],
                provider_results=project_provider_results(record.get('provider_results', [])),
                attribution=WorkAttribution.model_validate(record.get('attribution') or work_attribution(record)).model_dump(),
                provider_blocker=ProviderBlocker.model_validate(record['provider_blocker']).model_dump() if record.get('provider_blocker') else None,
                diff_summary=summary, files_changed=[], test_results=tests,
                reviewer_result=dict(verdict=verdict, summary='Host review completed; detailed evidence remains in dev.') if verdict else None,
                error='Candidate operation did not complete successfully.' if record.get('error') else None)
    if record.get('improvement'):
        improvement = record['improvement']
        from pydantic import TypeAdapter
        from .candidate_provider import ProviderId
        for field in ('implementation_provider', 'review_provider'):
            TypeAdapter(ProviderId).validate_python(improvement.get(field))
        if improvement.get('implementation_provider') == improvement.get('review_provider'):
            raise ToolDenied('Invalid engineering provider evidence')
        comparison = improvement.get('baseline_comparison', {}).get('result')
        if comparison not in {'not_run', 'not_compared', 'inconclusive', 'regressed', 'improved', 'unchanged_pass', 'unchanged_failure', 'changed_test_suite'}:
            raise ToolDenied('Invalid baseline comparison')
        projected['improvement'] = dict(
            problem_detected='Detailed problem evidence remains in dev.',
            hypothesis='Detailed hypothesis remains in dev.',
            files_changed=summary.get('files', 0), tests_run=tests,
            failures_revisions=[{'attempt': item['attempt'], 'state': item['state']}
                for item in improvement.get('failures_revisions', [])[:2]
                if type(item.get('attempt')) is int and item['attempt'] in (1, 2)
                and item.get('state') in {'passed', 'needs_revision', 'rejected'}],
            baseline_comparison={'result': comparison}, review_result=verdict,
            confidence='supported_by_tests_and_review' if record['state'] == 'passed' and not record.get('busy') else 'insufficient',
            implementation_provider=improvement['implementation_provider'], review_provider=improvement['review_provider'])
        if improvement.get('engineering_action') == 'engineer':
            if improvement.get('scope') not in {'documentation', 'code'}:
                raise ToolDenied('Invalid engineering scope')
            projected['improvement'].update(engineering_action='engineer', scope=improvement['scope'])
    return projected


def transfer(operation, payload=None):
    """Trusted broker transport, run by docker exec; never reads application DB."""
    directory = root()
    if operation == 'receive':
        for path in sorted(directory.glob('*.request'))[:16]:
            try:
                request = Request.model_validate(read(path)).checked()
                if path.name != request.request_id + '.request':
                    continue
                return request.model_dump(exclude_none=True)
            except Exception:
                path.unlink(missing_ok=True)
        return None
    if operation == 'respond':
        identifier = payload['request_id']
        if not valid_id(identifier):
            raise ToolDenied('Invalid response identity')
        incoming = directory / (identifier + '.request')
        if incoming.exists():
            write(directory / (identifier + '.response'), payload)
            incoming.unlink(missing_ok=True)
        return None
    raise ToolDenied('Invalid transport operation')


if __name__ == '__main__':
    import sys
    data = json.loads(sys.stdin.buffer.read(MAX_BYTES + 1))
    print(json.dumps(transfer(**data)))
