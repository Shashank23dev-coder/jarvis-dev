"""Fixed dev-only engineering executor; no shell, apply or deployment interface."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from .candidate_broker import Broker, SOURCE, STATE
from .database import Database
from .engineering_jobs import EngineeringJobs
from .self_edit import SelfEditService
from tools.policy import ToolDenied

# Runtime reports the worker inactive 20 s after its last heartbeat unless a job lease is live;
# apply preparation runs under no job lease.
HEARTBEAT_SECONDS = 5


@contextmanager
def heartbeat(jobs):
    """Refresh the worker heartbeat from a thread while a long step runs outside any job lease."""
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                jobs.heartbeat()
            except Exception:
                pass  # a missed beat only makes runtime report stale status; never stop the step
    thread = threading.Thread(target=beat, name='engineering-heartbeat', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        jobs.heartbeat()


class EngineeringWorker:
    def __init__(self, service, *, source=SOURCE, direct_dev=False):
        # Opt-in: only the service entry point routes eligible jobs to DirectDevelopment.
        self.service, self.direct_dev = service, direct_dev
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
            # Owner candidate operations queued while this lock was held run first,
            # under the same fence and outside any job lease.
            if self.jobs.deferred(status='queued') or self.jobs.deferred(status='running'):
                with heartbeat(self.jobs):
                    self.service.run_deferred(fd, self.jobs)
            owner = str(uuid4())
            job = self.jobs.claim(owner)
            if job is None:
                if self.direct_dev:
                    # Idle: make one approved-waiting direct candidate apply-ready, outside any job lease.
                    from .direct_dev import pending_preparation, prepare_for_apply
                    from .engineering_repository import development_service
                    pending = pending_preparation(self.service)
                    if pending:
                        job, _ = pending
                        # Preparation holds this lock for up to its whole budget: it pauses
                        # between shards for any queued job and resumes on a later idle pass.
                        with heartbeat(self.jobs):
                            prepare_for_apply(development_service(self.service, job.get('project_id')), self.jobs,
                                              job, pause=self.jobs.queued)
                        return self.jobs.get(job['id'])
                return None
            candidate = None
            try:
                from .engineering_repository import development_service
                service = development_service(self.service, job.get('project_id'))
                if job.get('operator'):
                    from .engineering_operator import run_job
                    candidate = run_job(service, self.jobs, job, owner)
                    return self.jobs.finish(job['id'], owner, candidate)
                from .direct_dev import direct_route, run_direct
                if self.direct_dev and direct_route(service, job)['path'] == 'direct':
                    candidate, failure = run_direct(service, self.jobs, job, owner)
                    return self.jobs.finish(job['id'], owner, candidate, failure=failure)
                continued_review = False
                if job['candidate_id']:
                    prior = service.store.get(job['candidate_id'], recover=False)
                    if prior and not prior['busy'] and prior['state'] == 'passed':
                        candidate = service.inspect(prior['id'])
                    elif (prior and not prior['busy'] and prior['state'] == 'needs_revision'
                          and prior.get('diff') and prior.get('fingerprint')
                          and prior.get('tested_fingerprint') == prior['fingerprint']
                          and prior.get('reviewer_result') is None):
                        candidate = service.inspect(prior['id'])
                        if (candidate and candidate['state'] == 'needs_revision'
                                and candidate.get('diff') and candidate.get('fingerprint')
                                and candidate.get('tested_fingerprint') == candidate['fingerprint']
                                and candidate.get('reviewer_result') is None):
                            record = service.store.get(prior['id'], internal=True)
                            record['source_context'].update(lease_owner=owner, attempt=job['attempts'])
                            service.store.save(record)
                            # The worker already holds the engineering fence and
                            # job lease; the owner-facing review path would
                            # reject that active lease.
                            candidate = service._operate_locked(prior['id'], 'review')
                            continued_review = True
                if not continued_review and (candidate is None or candidate['state'] != 'passed'):
                    if not (job['arguments'].get('inspect_paths') and job['arguments'].get('targeted_tests')):
                        # Admission keeps unscoped jobs off this path; a worker without the
                        # direct path or a later portfolio change must still fail clearly here.
                        from .engineering_jobs import SCOPE_REQUIRED
                        raise ToolDenied(SCOPE_REQUIRED)
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
    worker = EngineeringWorker(SelfEditService(database, source_root=SOURCE, candidate_root=STATE / 'candidates'),
                               direct_dev=True)
    while True:
        worker.once()
        time.sleep(1)


if __name__ == '__main__':
    main()
