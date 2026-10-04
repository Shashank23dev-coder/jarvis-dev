"""Operator-run host broker. Production never receives this process's authority."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from tools.policy import ToolDenied
from .candidate_bridge import Request, project, MAX_BYTES
from .database import Database
from .self_edit import SelfEditService

SOURCE = Path('/home/ubuntu/jarvis-dev')
STATE = Path('/home/ubuntu/.local/state/jarvis-chat-candidates')
CONTAINER = 'jarvis-jarvis-1'


class Broker:
    def __init__(self, service, *, source=SOURCE):
        self.service = service
        # Trusted host configuration only; no request may select a repository.
        self.source = Path(source)

    def check_source(self):
        try:
            canonical = self.source.resolve(strict=True)
            actual = self.service.source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolDenied('Broker development checkout unavailable') from exc
        if (os.environ.get('JARVIS_MODE') != 'development'
                or not self.source.is_absolute()
                or canonical != self.source
                or self.service.source != self.source
                or actual != self.source
                or not (self.source / '.git').is_dir()
                or (self.source / '.git').is_symlink()):
            raise ToolDenied('Broker requires the configured development checkout')

    def execute(self, raw, *, authority=None):
        from .engineering_authority import current_owner, require_owner
        authority = require_owner(authority if authority is not None else current_owner.get())
        self.check_source()
        request = Request.model_validate(raw).checked()
        if request.action == 'project_inspect':
            from .engineering_portfolio import inspect_project_record
            return inspect_project_record(self.service.store.database, **request.arguments)
        if request.action == 'decision':
            from .engineering_decision import preflight
            return preflight(self.service.store.database, request.goal, task_id=request.task_id, **request.arguments)
        if request.action == 'runtime':
            from .engineering_jobs import EngineeringJobs
            return EngineeringJobs(self.service.store.database).runtime()
        if request.action.startswith('job_'):
            from .engineering_jobs import EngineeringJobs, project_job
            jobs = EngineeringJobs(self.service.store.database)
            if request.action == 'job_create':
                return project_job(jobs.create(request.goal, request.engineering_action.model_dump(),
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
            return self.service.engineering_workspace(request.engineering_action.scope)
        if request.action in {'create', 'engineer'}:
            # Durable idempotency by source task, including crash/retry requests.
            with self.service.store.database.transaction() as db:
                rows = db.execute("SELECT record FROM candidates WHERE json_extract(record, '$.source_context.task_id') = ? LIMIT 1", (request.task_id,)).fetchall()
            for row in rows:
                existing = json.loads(row['record'])
                prior = existing.get('source_context', {})
                if prior.get('source') == 'production_chat' and prior.get('task_id') == request.task_id:
                    if existing.get('source_root') != str(self.source):
                        raise ToolDenied('Candidate source mismatch')
                    if prior.get('conversation_id') != request.conversation_id:
                        raise ToolDenied('Source task mismatch')
                    if prior.get('engineering_action') != context.get('engineering_action') or existing['goal'] != request.goal:
                        raise ToolDenied('Source action mismatch')
                    return project(self.service.store.get(existing['id']))
            from .engineering_decision import submission_decision
            decision = submission_decision(self.service.store.database, request.goal,
                task_id=request.task_id, project_id=request.arguments.get('project_id'))
            context.update(project_id=decision['project_id'], decision_id=decision['id'])
            handler = self.service.engineer if request.action == 'engineer' else self.service.start
            options = ({'engineering_action': request.engineering_action.model_dump()}
                       if request.engineering_action else {})
            arguments = {key: value for key, value in request.arguments.items() if key != 'project_id'}
            record = handler(request.goal, **arguments, source_context=context, **options)
        else:
            identifier = request.arguments['candidate_id']
            existing = self.service.store.get(identifier, internal=True)
            if not existing:
                raise ToolDenied('Candidate unavailable')
            if existing.get('source_root') != str(self.service.source):
                raise ToolDenied('Candidate source mismatch')
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


def transport(operation, payload=None):
    data = json.dumps(dict(operation=operation, payload=payload)).encode()
    with tempfile.TemporaryDirectory(prefix='jarvis-broker-home-') as home:
        result = subprocess.run(['/usr/bin/docker', '--host=unix:///var/run/docker.sock',
                                 'exec', '-i', CONTAINER, 'python', '-B', '-m', 'core.candidate_bridge'],
                                input=data, capture_output=True, timeout=10,
                                env={'PATH': '/usr/bin:/bin', 'HOME': home})
    if result.returncode or len(result.stdout) > MAX_BYTES:
        raise ToolDenied('Candidate mailbox unavailable')
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
    service = SelfEditService(Database(STATE / 'candidates.sqlite3'), source_root=SOURCE,
                              candidate_root=STATE / 'candidates')
    broker = Broker(service)
    while True:
        try:
            raw = transport('receive')
            if raw:
                try:
                    from .engineering_authority import OwnerDevAuthority
                    # Fixed docker-exec into the configured application, whose
                    # mailbox is private to its UID. Candidate/test containers
                    # have neither this mailbox nor the Docker socket mounted.
                    authority = OwnerDevAuthority()
                    # The currently deployed protocol predates authority audit
                    # fields. Issue them from this authenticated private channel
                    # so a dev broker upgrade needs no production deployment.
                    record = broker.execute(dict(raw, authority=authority.model_dump()), authority=authority)
                    response = dict(request_id=raw['request_id'], ok=True, record=record)
                except Exception:
                    response = dict(request_id=raw['request_id'], ok=False)
                transport('respond', response)
        except Exception:
            # Fixed diagnostic only. Neither Docker output nor requests are logs.
            pass
        time.sleep(1)


if __name__ == '__main__':
    main()
