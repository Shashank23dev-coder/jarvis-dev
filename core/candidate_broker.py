"""Operator-run host broker. Production never receives this process's authority."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import sys

from tools.policy import ToolDenied
from tools.schemas import StrictModel
from pydantic import Field
from .candidate_bridge import Request, project, MAX_BYTES
from .database import Database
from .self_edit import SelfEditService

SOURCE = Path('/home/ubuntu/jarvis-dev')
STATE = Path('/home/ubuntu/.local/state/jarvis-chat-candidates')
CONTAINER = 'jarvis-jarvis-1'


class ReleasePreparation(StrictModel):
    commit: str = Field(pattern=r'^[0-9a-f]{40}$')
    prepared: bool
    evidence: dict
    required_confirmation: str


class Broker:
    def __init__(self, service=None, *, source=SOURCE, database=None):
        self.service = service
        self.database = service.store.database if service is not None else database
        # Trusted host configuration only; no request may select a repository.
        self.source = Path(source)

    def check_source(self):
        try:
            canonical = self.source.resolve(strict=True)
            actual = self.service.source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolDenied('Broker development checkout unavailable', reason='development_checkout_required') from exc
        if (os.environ.get('JARVIS_MODE') != 'development'
                or not self.source.is_absolute()
                or canonical != self.source
                or self.service.source != self.source
                or actual != self.source
                or not (self.source / '.git').is_dir()
                or (self.source / '.git').is_symlink()):
            raise ToolDenied('Broker requires the configured development checkout', reason='development_checkout_required')

    def execute(self, raw, *, authority=None):
        from .engineering_authority import current_owner, require_owner
        authority = require_owner(authority if authority is not None else current_owner.get())
        request = Request.model_validate(raw).checked()
        if request.action == 'release_prepare':
            from .deployment_release import configured_service, ReleaseService, ReleaseConfig
            # Validation needs source + a private journal, not production access.
            service = (configured_service() if os.environ.get('JARVIS_DEPLOY_CONFIG') else
                ReleaseService(ReleaseConfig(SOURCE, STATE / 'release-preparation', STATE / 'production.env')))
            return ReleasePreparation.model_validate(service.prepare()).model_dump()
        # Registered reads are authorized by owner + host Portfolio + native
        # read operation. Candidate workspace eligibility is unrelated.
        if request.action == 'project_repository':
            from .engineering_repository import inspect_repository
            return inspect_repository(self.database, **request.arguments)
        if request.action == 'project_inspect':
            from .engineering_portfolio import inspect_project_record
            return inspect_project_record(self.database, **request.arguments)
        if request.action == 'decision':
            from .engineering_decision import preflight
            return preflight(self.database, request.goal, task_id=request.task_id, **request.arguments)
        if request.action == 'runtime':
            from .engineering_jobs import EngineeringJobs
            return EngineeringJobs(self.database).runtime()
        if self.service is None:
            self.service = SelfEditService(self.database, source_root=self.source, candidate_root=STATE / 'candidates')
        self.check_source()
        if request.action.startswith('job_'):
            from .engineering_jobs import EngineeringJobs, project_job
            jobs = EngineeringJobs(self.service.store.database)
            if request.action == 'job_create':
                return project_job(jobs.create(request.goal, request.engineering_action.model_dump() if request.engineering_action else None,
                    request.arguments, request.model_dump()))
            if request.action == 'job_list':
                return [project_job(j) for j in jobs.recent(before=request.arguments.get('before'))]
            job = jobs.get(request.arguments['job_id'])
            operation = request.action.removeprefix('job_')
            if operation != 'inspect':
                job = jobs.control(job['id'], operation, authority=authority)
            return project_job(job)
        context = dict(source='production_chat', task_id=request.task_id,
                       conversation_id=request.conversation_id, request_id=request.request_id)
        if request.engineering_action:
            context['engineering_action'] = request.engineering_action.model_dump()
        # The caller authenticates the channel separately from request data.
        # Text and conversation identity are audit metadata, never authority.
        if request.action == 'workspace':
            from .engineering_repository import development_service
            from .candidate_tools import PrepareEngineeringArguments
            args = PrepareEngineeringArguments(**request.arguments)
            service = development_service(self.service, args.project_id)
            result = service.engineering_workspace(request.engineering_action.scope if request.engineering_action else args.scope)
            result['provider'] = request.engineering_action.provider if request.engineering_action else args.provider
            return result
        if request.action in {'create', 'engineer'}:
            from .engineering_decision import associate
            from .engineering_portfolio import Portfolio
            from .engineering_repository import development_service
            project_id = associate(request.goal, Portfolio(self.database).projects(), request.arguments.get('project_id'))
            self.service = development_service(self.service, project_id)
            # Durable idempotency by source task, including crash/retry requests.
            with self.service.store.database.transaction() as db:
                rows = db.execute("SELECT record FROM candidates WHERE json_extract(record, '$.source_context.task_id') = ? LIMIT 1", (request.task_id,)).fetchall()
            for row in rows:
                existing = json.loads(row['record'])
                prior = existing.get('source_context', {})
                if prior.get('source') == 'production_chat' and prior.get('task_id') == request.task_id:
                    if existing.get('source_root') != str(self.service.source):
                        raise ToolDenied('Candidate source mismatch')
                    if prior.get('conversation_id') != request.conversation_id:
                        raise ToolDenied('Source task mismatch')
                    if prior.get('engineering_action') != context.get('engineering_action') or existing['goal'] != request.goal:
                        raise ToolDenied('Source action mismatch')
                    return project(self.service.store.get(existing['id']))
            from .engineering_decision import submission_decision
            decision = submission_decision(self.service.store.database, request.goal,
                task_id=request.task_id, project_id=project_id)
            context.update(project_id=project_id, decision_id=decision['id'])
            handler = self.service.engineer if request.action == 'engineer' else self.service.start
            options = ({'engineering_action': request.engineering_action.model_dump()}
                       if request.engineering_action else {})
            arguments = {key: value for key, value in request.arguments.items() if key != 'project_id'}
            record = handler(request.goal, **arguments, source_context=context, **options)
        else:
            identifier = request.arguments['candidate_id']
            from .engineering_repository import development_service
            self.service = development_service(self.service, candidate_id=identifier)
            existing = self.service.store.get(identifier, internal=True)
            if not existing:
                raise ToolDenied('Candidate unavailable')
            if existing.get('source_root') != str(self.service.source):
                raise ToolDenied('Candidate source mismatch')
            if request.action in {'preview', 'audit', 'apply'}:
                from .candidate_apply import CandidateApply
                apply = CandidateApply(self.service)
                result = (apply.preview(identifier) if request.action == 'preview' else
                    apply.inspect(identifier, read_only=True) if request.action == 'audit' else
                    apply.apply(identifier, request.goal, authority=authority))
                return project_apply(result)
            if request.action == 'inspect':
                return project(self.service.inspect(identifier))
            operations = existing.setdefault('chat_operations', [])
            if any(item['request_id'] == request.request_id for item in operations):
                return project(existing)
            if len(operations) >= 64:
                raise ToolDenied('Candidate operation audit limit reached')
            operations.append(context | {'action': request.action})
            self.service.store.save(existing, 'production_chat_' + request.action)
            handler = {'inspect': self.service.inspect, 'tests': self.service.tests,
                       'review': self.service.review, 'discard': self.service.discard}[request.action]
            record = handler(identifier)
        return project(record)


def project_apply(result):
    """Bounded apply evidence for the private mailbox; no patch or host paths."""
    from .database import valid_id
    from .engineering_authority import AuthorityDecision
    if not valid_id(result.get('candidate_id')):
        raise ToolDenied('Invalid apply evidence')
    value = dict(candidate_id=result['candidate_id'])
    if 'eligible' in result:
        value.update(eligible=result['eligible'] is True,
            required_confirmation=result.get('required_confirmation'))
        if result.get('authority'):
            value['authority'] = AuthorityDecision.model_validate(result['authority']).model_dump()
        if not value['eligible']:
            value['reason'] = 'Tests, review, or the clean baseline need refreshing before apply.'
    else:
        audit = result.get('apply')
        if audit is None:
            value['apply'] = None
        else:
            if audit.get('status') not in {'denied', 'applying', 'verifying', 'committing', 'applied', 'rolling_back', 'rolled_back', 'rollback_failed'}:
                raise ToolDenied('Invalid apply status')
            value['apply'] = {key: audit.get(key) for key in ('status', 'commit', 'rollback', 'post_apply_verified')}
    return value


def transport(operation, payload=None):
    data = json.dumps(dict(operation=operation, payload=payload)).encode()
    with tempfile.TemporaryDirectory(prefix='jarvis-broker-home-') as home:
        result = subprocess.run(['/usr/bin/docker', '--host=unix:///var/run/docker.sock',
                                 'exec', '-i', CONTAINER, 'python', '-B', '-m', 'core.candidate_bridge'],
                                input=data, capture_output=True, timeout=10,
                                env={'PATH': '/usr/bin:/bin', 'HOME': home})
    if result.returncode or len(result.stdout) > MAX_BYTES:
        raise ToolDenied('Candidate mailbox unavailable', reason='unavailable')
    return json.loads(result.stdout)


def main():
    import fcntl
    if os.environ.get('JARVIS_MODE') != 'development' or Path(__file__).resolve().parents[1] != SOURCE:
        raise ToolDenied('Broker requires the authorized development runtime')
    if SOURCE.resolve() != SOURCE or not (SOURCE / '.git').is_dir():
        raise ToolDenied('Invalid development checkout')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.resolve() != STATE or STATE.stat().st_mode & 0o077:
        raise ToolDenied('Unsafe broker state directory')
    fd = os.open(STATE / 'broker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    while True:
        try:
            raw = transport('receive')
            if raw:
                response = execute_current(raw)
                transport('respond', response)
        except Exception:
            # Fixed diagnostic only. Neither Docker output nor requests are logs.
            pass
        time.sleep(1)


def execute_current(raw):
    """The existing operator loop transports; each dispatch loads current code.

    A long-running mailbox process must never cache request authorization from
    an older release. The fixed local child uses this same broker and protocol.
    No request selects its executable, checkout, environment or service.
    """
    result = subprocess.run([sys.executable, '-B', '-m', 'core.candidate_broker', '--execute'],
        input=json.dumps(raw).encode(), capture_output=True, timeout=1320, cwd=SOURCE)
    if result.returncode or len(result.stdout) > MAX_BYTES:
        return dict(request_id=raw['request_id'], ok=False,
                    error=ToolDenied(reason='unavailable').public_error())
    return json.loads(result.stdout)


def execute_private(raw):
    """Called only by the fixed operator entrypoint on its private channel."""
    from pydantic import ValidationError
    from .engineering_authority import OwnerDevAuthority
    if (os.environ.get('JARVIS_MODE') != 'development'
            or Path(__file__).resolve().parents[1] != SOURCE):
        raise ToolDenied('Broker requires authorized host runtime', reason='development_checkout_required')
    database = Database(STATE / 'candidates.sqlite3')
    authority = OwnerDevAuthority()
    try:
        record = Broker(database=database).execute(dict(raw, authority=authority.model_dump()), authority=authority)
        return dict(request_id=raw['request_id'], ok=True, record=record)
    except ToolDenied as exc:
        error = exc.public_error()
    except ValidationError:
        error = ToolDenied(reason='invalid_request').public_error()
    except Exception:
        error = dict(code='tool_failed', gate='broker_execution', reason='execution_failed',
                     message='broker_execution: execution_failed')
    return dict(request_id=raw['request_id'], ok=False, error=error)


if __name__ == '__main__':
    if sys.argv[1:] == ['--execute']:
        raw = json.loads(sys.stdin.buffer.read(MAX_BYTES + 1))
        print(json.dumps(execute_private(raw)))
    elif not sys.argv[1:]:
        main()
    else:
        raise ToolDenied('Unsupported broker entrypoint')
