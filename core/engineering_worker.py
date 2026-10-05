"""Fixed dev-only engineering executor; no shell, apply or deployment interface."""
import fcntl
import os
from pathlib import Path
import time
from uuid import uuid4

from .candidate_broker import Broker, SOURCE, STATE
from .database import Database
from .engineering_jobs import EngineeringJobs
from .self_edit import SelfEditService
from tools.policy import ToolDenied


class EngineeringWorker:
    def __init__(self, service, *, source=SOURCE):
        self.service = service
        self.boundary = Broker(service, source=source)
        self.jobs = EngineeringJobs(service.store.database)

    def once(self):
        self.boundary.check_source()
        path = Path(self.jobs.database.path + '.engineering.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            # Inherited by the candidate executor: a dead supervisor cannot
            # permit overlapping execution while its child still edits/tests.
            self.service.engineering_lock_fd = fd
            self.jobs.heartbeat()
            from .engineering_providers import EngineeringRegistry
            from .self_edit import model_request
            if self.service.model is model_request:
                EngineeringRegistry.defaults(self.jobs.database, model_request).refresh_claude(force=False)
            owner = str(uuid4())
            job = self.jobs.claim(owner)
            if job is None:
                return None
            candidate = None
            try:
                from .engineering_repository import development_service
                service = development_service(self.service, job.get('project_id'))
                if job['candidate_id']:
                    prior = service.store.get(job['candidate_id'], recover=False)
                    if prior and prior['state'] == 'passed' and not prior['busy']:
                        candidate = service.inspect(prior['id'])
                if candidate is None or candidate['state'] != 'passed':
                    context = dict(job['source'], source='engineering_job', engineering_job_id=job['id'],
                                   lease_owner=owner, attempt=job['attempts'], project_id=job.get('project_id'),
                                   decision_id=(job.get('decision') or {}).get('id'))
                    # Only trusted registration selects the source root.
                    arguments = {k: v for k, v in job['arguments'].items() if k != 'project_id'}
                    candidate = service.engineer(job['goal'], **arguments,
                        source_context=context, engineering_action=job['action'])
            except Exception as exc:
                from .candidate_provider import failure_evidence, execution_error_code
                failure = failure_evidence(job['provider'], execution_error_code(exc),
                                          'planning', job['attempts'])
                return self.jobs.finish(job['id'], owner, candidate, failure=failure)
            return self.jobs.finish(job['id'], owner, candidate)
        finally:
            # Closing (rather than LOCK_UN) retains child-held flock on crashes.
            self.service.__dict__.pop('engineering_lock_fd', None)
            os.close(fd)


def main():
    if os.environ.get('JARVIS_MODE') != 'development' or Path(__file__).resolve().parents[1] != SOURCE:
        raise ToolDenied('Engineering worker requires configured dev runtime')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.resolve() != STATE or STATE.stat().st_mode & 0o077:
        raise ToolDenied('Unsafe engineering state')
    database = Database(STATE / 'candidates.sqlite3')
    worker = EngineeringWorker(SelfEditService(database, source_root=SOURCE, candidate_root=STATE / 'candidates'))
    while True:
        worker.once()
        time.sleep(1)


if __name__ == '__main__':
    main()
